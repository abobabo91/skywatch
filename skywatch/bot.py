"""The Telegram bot: how anyone subscribes and configures their own alerts.

Long polling, not webhooks, so this works from a laptop behind NAT with no
public address and no tunnel.
"""
from __future__ import annotations

import html as html_mod
import logging
import threading
import time
from datetime import datetime, timezone

from . import config, geocode, render, store, transports
from .engine import Engine
from .passes import Observer

log = logging.getLogger(__name__)

WELCOME = """\U0001F30C <b>Skywatch</b>

I watch for satellites that are actually worth walking outside for, and message \
you before each one with which way to face and how high to look.

You get:
\U0001F686 <b>Starlink trains</b> — a freshly launched batch still flying in \
formation, the string-of-pearls. Rare: only for a few days after each launch.
\U0001F680 <b>The space stations</b> — ISS and Tiangong.
\U0001F6F0 <b>Anything else bright</b> — big rocket bodies and old satellites \
that flare up.

First I need to know where you are — satellite passes look completely different \
100 km away.

\U0001F449 <b>Just type the name of your town</b> — "Tatabanya", "Budapest", \
"Lisbon".

Or, if you prefer: tap the button below to use your phone's GPS (mobile Telegram \
only), send a location pin, paste a Google Maps link, or type coordinates like \
<code>47.64 18.93</code>."""

ASK_LOCATION = """Where are you?

\U0001F449 <b>Type your town's name</b> — that is usually easiest.

Also works: the GPS button below (mobile Telegram only), a location pin, a pasted \
Google Maps link, or coordinates like <code>47.64 18.93</code>."""

HELP = """<b>Ask me things</b>

<code>/tonight</code> — what's visible tonight
<code>/next</code> — the next thing coming up
<code>/trains</code> — status of every fresh Starlink batch in orbit
<code>/settings</code> — show your current setup

<b>Set it up</b>
<code>/setup</code> — redo the guided setup (delivery, pickiness, how many)
<code>/mode</code> — daily list, per-event reminders, or both
<code>/quality</code> — how picky, as three presets
<code>/where</code> — set location again

<b>Fine tuning</b>
<code>/digest 18</code> — hour for the daily list (<code>/digest off</code>)
<code>/lead 5</code> — minutes of warning before each event
<code>/cap 3</code> — most ordinary passes per night. Starlink trains and the \
space stations ignore this limit.
<code>/mag 2.0</code> — exact brightness cutoff if the presets don't fit. Lower \
is stricter.
<code>/elev 15</code> — ignore passes that stay below this many degrees.
<code>/quiet 1 6</code> — no pings between these local hours.
<code>/kinds</code> — pick which event types you want.
<code>/ntfy mytopic</code> — also mirror to an ntfy.sh topic (<code>/ntfy off</code>).

<code>/pause</code> and <code>/resume</code> — stop and restart alerts.
<code>/forget</code> — delete everything I store about you."""

LOCATION_KEYBOARD = {
    "keyboard": [[{"text": "\U0001F4CD Share my location", "request_location": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

# --- guided setup -----------------------------------------------------------
# Four short questions, each one tap. Steps that cannot apply to the chosen
# delivery mode are skipped rather than asked and ignored.

SETUP_QUESTIONS = {
    "mode": (
        "How do you want to hear from me?",
        [("\U0001F4C5 A list once a day", "digest"),
         ("\U0001F514 Just before each one", "alerts"),
         ("\U0001F4C5+\U0001F514 Both", "both")],
    ),
    "quality": (
        "How picky should I be?",
        [("\U00002B50 Only the showstoppers", "spectacular"),
         ("\U0001F44D The good ones", "good"),
         ("\U0001F441 Anything I can see", "everything")],
    ),
    "count": (
        "At most how many alerts a night?\n"
        "<i>Starlink trains and space stations always come through regardless.</i>",
        [("1", "1"), ("3", "3"), ("5", "5"), ("No limit", "20")],
    ),
    "lead": (
        "How much warning before each one?",
        [("5 min", "5"), ("15 min", "15"), ("30 min", "30")],
    ),
    "digest": (
        "When should the daily list arrive?",
        [("Morning (08:00)", "8"), ("Early evening (18:00)", "18"),
         ("Late evening (21:00)", "21")],
    ),
}

SETUP_ORDER = ["mode", "quality", "count", "lead", "digest"]


class Bot:
    def __init__(self, engine: Engine, token: str):
        self.engine = engine
        self.token = token
        self.allowed = config.allowed_chat_ids()
        self._stop = threading.Event()
        # Place-search results awaiting a tap, per chat. Callback data is capped
        # at 64 bytes, so the candidates are held here and referenced by index.
        self._pending_places: dict[int, list] = {}

    # --- plumbing -----------------------------------------------------------

    def run(self) -> None:
        me = transports.telegram_call(self.token, "getMe", {})
        if not me or not me.get("ok"):
            log.error("Telegram token rejected; bot disabled")
            return
        username = me["result"].get("username")
        access = (f"private, {len(self.allowed)} allowed chat(s)" if self.allowed
                  else "OPEN — anyone who finds the username can use it")
        log.info("telegram bot @%s online (%s)", username, access)
        store.set_state("bot_username", username or "")

        offset = int(store.get_state("update_offset", "0") or 0)
        while not self._stop.is_set():
            result = transports.telegram_call(
                self.token, "getUpdates",
                {"offset": offset, "timeout": 25,
                 "allowed_updates": ["message", "callback_query"]},
                timeout=40)
            if not result or not result.get("ok"):
                time.sleep(5)
                continue
            for update in result["result"]:
                offset = update["update_id"] + 1
                store.set_state("update_offset", offset)
                try:
                    self.handle(update)
                except Exception:
                    log.exception("failed handling update %s", update.get("update_id"))

    def stop(self) -> None:
        self._stop.set()

    def reply(self, chat_id, text: str, keyboard=None) -> None:
        transports.send_telegram(self.token, chat_id, text, reply_markup=keyboard)

    # --- dispatch -----------------------------------------------------------

    @staticmethod
    def _chat_id_of(update: dict):
        if "callback_query" in update:
            msg = (update.get("callback_query") or {}).get("message") or {}
        else:
            msg = update.get("message") or {}
        return (msg.get("chat") or {}).get("id")

    def handle(self, update: dict) -> None:
        # Allowlist first, before parsing, geocoding or any pass computation --
        # a stranger's message must cost one log line and nothing else. Dropped
        # silently on purpose: replying confirms the bot is live and spends an
        # API call per spam message. These log lines are also the only record of
        # incoming traffic, so they double as the request counter.
        if self.allowed:
            chat_id = self._chat_id_of(update)
            if str(chat_id) not in self.allowed:
                log.info("ignored update from chat %s (not in allowlist)", chat_id)
                return

        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]

        if "location" in msg:
            loc = msg["location"]
            return self._set_location(chat_id, loc["latitude"], loc["longitude"])

        text = (msg.get("text") or "").strip()
        if not text:
            return
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        args = args.strip()

        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_help,
            "/tonight": self._cmd_tonight,
            "/next": self._cmd_next,
            "/trains": self._cmd_trains,
            "/settings": self._cmd_settings,
            "/setup": self._cmd_setup,
            "/mode": self._cmd_mode,
            "/quality": self._cmd_quality,
            "/digest": self._cmd_digest,
            "/mag": self._cmd_mag,
            "/cap": self._cmd_cap,
            "/elev": self._cmd_elev,
            "/lead": self._cmd_lead,
            "/quiet": self._cmd_quiet,
            "/kinds": self._cmd_kinds,
            "/ntfy": self._cmd_ntfy,
            "/where": self._cmd_where,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/forget": self._cmd_forget,
        }
        handler = handlers.get(cmd)
        if handler:
            return handler(chat_id, args)

        if text.startswith("/"):
            self.reply(chat_id, "I do not know that command. Try /help.")
            return
        # Anything else is treated as an attempt to tell me where they are:
        # coordinates, a pasted map link, or -- most likely -- a place name.
        return self._resolve_location_text(chat_id, text)

    def _resolve_location_text(self, chat_id, text: str) -> None:
        coords = _parse_coords(text)
        if coords:
            return self._set_location(chat_id, *coords)

        coords = geocode.coords_from_link(text)
        if coords:
            return self._set_location(chat_id, *coords)

        if "http" in text:
            self.reply(chat_id, "I could not find coordinates in that link. "
                                "Try typing the place name instead.")
            return

        matches = geocode.search(text)
        if not matches:
            self.reply(chat_id,
                       f"I could not find <b>{html_mod.escape(text[:60])}</b>. "
                       "Try a bigger nearby town, or send coordinates like "
                       "<code>47.64 18.93</code>.")
            return
        if len(matches) == 1:
            p = matches[0]
            return self._set_location(chat_id, p.lat, p.lon, p.short)

        self._pending_places[chat_id] = matches
        rows = [[{"text": p.label[:60], "callback_data": f"geo:{i}"}]
                for i, p in enumerate(matches)]
        self.reply(chat_id, f"I found {len(matches)}. Which one?",
                   {"inline_keyboard": rows})

    # --- subscriber helpers -------------------------------------------------

    def _sub(self, chat_id) -> dict | None:
        return store.get_subscriber("telegram", str(chat_id))

    def _require_location(self, chat_id) -> dict | None:
        sub = self._sub(chat_id)
        if sub is None or sub.get("lat") is None:
            self.reply(chat_id, ASK_LOCATION, LOCATION_KEYBOARD)
            return None
        return sub

    def _observer(self, sub: dict) -> Observer:
        return Observer(sub["lat"], sub["lon"], sub.get("elevation_m") or 0.0)

    # --- commands -----------------------------------------------------------

    def _cmd_start(self, chat_id, args: str) -> None:
        store.upsert_subscriber("telegram", str(chat_id))
        self.reply(chat_id, WELCOME, LOCATION_KEYBOARD)

    def _cmd_help(self, chat_id, args: str) -> None:
        self.reply(chat_id, HELP)

    def _cmd_where(self, chat_id, args: str) -> None:
        if args.strip():
            return self._resolve_location_text(chat_id, args.strip())
        self.reply(chat_id, ASK_LOCATION, LOCATION_KEYBOARD)

    def _set_location(self, chat_id, lat: float, lon: float,
                      place: str | None = None) -> None:
        tz_name = _timezone_for(lat, lon)
        # Name the spot back to them so a wrong pin or a same-named town in the
        # wrong country is obvious immediately, rather than after a week of
        # alerts pointing at the wrong bit of sky.
        if place is None:
            place = geocode.reverse(lat, lon)
        store.upsert_subscriber("telegram", str(chat_id), lat=lat, lon=lon,
                                tz=tz_name, place=place, active=1)
        where = f"<b>{html_mod.escape(place)}</b>" if place else f"<b>{lat:.3f}, {lon:.3f}</b>"
        existing = self._sub(chat_id)
        already_set_up = bool(existing and existing["overrides"].get("delivery_mode"))
        self.reply(chat_id,
                   f"\U0001F4CD {where}\n"
                   f"<code>{lat:.4f}, {lon:.4f}</code> · {tz_name}\n\n"
                   + ("Location updated." if already_set_up
                      else "A few quick questions and you are done."),
                   {"remove_keyboard": True})
        if already_set_up:
            found = self.engine.tonight(self._observer(self._sub(chat_id)),
                                        existing["settings"])
            self.reply(chat_id, render.render_digest(found, tz_name))
            return
        self._ask_setup(chat_id, "mode")

    # --- guided setup -------------------------------------------------------

    def _applicable_steps(self, settings: dict) -> list[str]:
        """Which setup questions make sense for the chosen delivery mode."""
        mode = settings.get("delivery_mode", "both")
        skip = set()
        if mode == "digest":
            skip |= {"count", "lead"}      # no per-event pings to tune
        if mode == "alerts":
            skip |= {"digest"}             # no daily list to schedule
        return [s for s in SETUP_ORDER if s not in skip]

    def _ask_setup(self, chat_id, step: str, message_id=None) -> None:
        prompt, options = SETUP_QUESTIONS[step]
        sub = self._sub(chat_id)
        # Number the questions against the steps that actually apply, so a
        # digest-only subscriber does not see "question 4 of 5" with two skipped.
        steps = self._applicable_steps(sub["settings"] if sub else {})
        if step in steps:
            prompt = f"<b>{steps.index(step) + 1}/{len(steps)}. {prompt}</b>"
        else:
            prompt = f"<b>{prompt}</b>"
        rows = [[{"text": label, "callback_data": f"setup:{step}:{value}"}]
                for label, value in options]
        markup = {"inline_keyboard": rows}
        if message_id:
            transports.telegram_call(self.token, "editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": prompt, "parse_mode": "HTML", "reply_markup": markup})
        else:
            self.reply(chat_id, prompt, markup)

    def _apply_setup(self, chat_id, step: str, value: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return
        if step == "mode":
            store.update_settings(sub["id"], delivery_mode=value)
        elif step == "quality":
            cutoff = config.QUALITY_PRESETS[value][0]
            store.update_settings(sub["id"], max_magnitude=cutoff, quality=value)
        elif step == "count":
            store.update_settings(sub["id"], max_alerts_per_night=int(value))
        elif step == "lead":
            store.update_settings(sub["id"], lead_minutes=int(value))
        elif step == "digest":
            store.update_settings(sub["id"], digest_hour=int(value))

    def _next_setup_step(self, chat_id, step: str) -> str | None:
        sub = self._sub(chat_id)
        steps = self._applicable_steps(sub["settings"] if sub else {})
        if step not in steps:
            return steps[0] if steps else None
        idx = steps.index(step)
        return steps[idx + 1] if idx + 1 < len(steps) else None

    def _finish_setup(self, chat_id, message_id=None) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return
        s = sub["settings"]
        mode = s.get("delivery_mode", "both")
        quality = s.get("quality", "good")
        label = config.QUALITY_PRESETS.get(quality, ("", quality, ""))[1]

        bits = [f"\U00002705 <b>All set.</b>", ""]
        if mode in ("digest", "both"):
            bits.append(f"\U0001F4C5 Daily list at <b>{int(s['digest_hour']):02d}:00</b>")
        if mode in ("alerts", "both"):
            bits.append(f"\U0001F514 Reminder <b>{int(s['lead_minutes'])} min</b> before, "
                        f"up to <b>{int(s['max_alerts_per_night'])}</b> a night")
        bits += [f"\U0001F441 Picking: <b>{label.lower()}</b>",
                 "",
                 "Change anything any time: /setup to redo this, /settings to see "
                 "it, /help for the individual commands.",
                 "",
                 "Here is tonight:"]
        text = "\n".join(bits)
        if message_id:
            transports.telegram_call(self.token, "editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": text, "parse_mode": "HTML"})
        else:
            self.reply(chat_id, text)
        found = self.engine.tonight(self._observer(sub), sub["settings"])
        self.reply(chat_id, render.render_digest(found, sub["tz"]))

    def _cmd_setup(self, chat_id, args: str) -> None:
        sub = self._require_location(chat_id)
        if not sub:
            return
        self._ask_setup(chat_id, "mode")

    def _cmd_mode(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        self._ask_setup(chat_id, "mode")

    def _cmd_quality(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        self._ask_setup(chat_id, "quality")

    def _cmd_digest(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        arg = args.strip().lower()
        if arg in ("off", "none", "stop"):
            store.update_settings(sub["id"], digest_hour=-1)
            self.reply(chat_id, "Daily list off. You still get the per-event "
                                "reminders unless you /mode away from them.")
            return
        if arg.isdigit() and 0 <= int(arg) <= 23:
            store.update_settings(sub["id"], digest_hour=int(arg))
            self.reply(chat_id, f"Daily list will arrive at <b>{int(arg):02d}:00</b>.")
            return
        self._ask_setup(chat_id, "digest")

    def _cmd_tonight(self, chat_id, args: str) -> None:
        sub = self._require_location(chat_id)
        if not sub:
            return
        found = self.engine.tonight(self._observer(sub), sub["settings"])
        self.reply(chat_id, render.render_digest(found, sub["tz"]))

    def _cmd_next(self, chat_id, args: str) -> None:
        sub = self._require_location(chat_id)
        if not sub:
            return
        upcoming = self.engine.upcoming(self._observer(sub), sub["settings"])
        if not upcoming:
            self.reply(chat_id, "Nothing coming up in the next day and a bit that "
                                "clears your thresholds. Try a looser /mag.")
            return
        self.reply(chat_id, render.render_event(upcoming[0], sub["tz"]))

    def _cmd_trains(self, chat_id, args: str) -> None:
        batches = self.engine.train_status()
        if not batches:
            self.reply(chat_id, "No Starlink launches in the last three weeks are "
                                "still being tracked as a group.")
            return
        lines = ["\U0001F686 <b>Fresh Starlink batches</b>", ""]
        for b in batches[:10]:
            verdict = ("<b>still a train</b>" if b.is_train
                       else f"spread over {b.arc_deg:.0f}° — no longer a string")
            age = f"{b.age_days:.0f} d" if b.age_days is not None else "?"
            lines.append(f"• <b>{b.launch}</b> — {len(b.members)} sats, {age} old, "
                         f"{b.mean_altitude_km:.0f} km: {verdict}")
        lines += ["", "<i>A batch is a train for roughly the first few days after "
                  "launch, then it spreads around its orbit.</i>"]
        self.reply(chat_id, "\n".join(lines))

    def _cmd_settings(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        s = sub["settings"]
        if sub.get("lat") is None:
            loc = "not set"
        elif sub.get("place"):
            loc = f"{html_mod.escape(sub['place'])} ({sub['lat']:.3f}, {sub['lon']:.3f})"
        else:
            loc = f"{sub['lat']:.3f}, {sub['lon']:.3f}"
        kinds = ", ".join(s.get("kinds") or []) or "none"
        mirror = s.get("ntfy_topic") or "off"
        mode = s.get("delivery_mode", "both")
        mode_label = {"digest": "daily list only",
                      "alerts": "per-event reminders only",
                      "both": "daily list + reminders"}.get(mode, mode)
        hour = int(s.get("digest_hour", 18))
        digest_label = "off" if hour < 0 else f"{hour:02d}:00"
        quality = s.get("quality")
        quality_label = (config.QUALITY_PRESETS[quality][1].lower()
                         if quality in config.QUALITY_PRESETS else "custom")

        lines = ["<b>Your settings</b>", "",
                 f"Location: <b>{loc}</b> ({sub['tz']})",
                 f"Delivery: <b>{mode_label}</b>"]
        if mode in ("digest", "both"):
            lines.append(f"Daily list at: <b>{digest_label}</b>")
        if mode in ("alerts", "both"):
            lines.append(f"Warning time: <b>{int(s['lead_minutes'])} min</b>")
            lines.append(f"Nightly limit: <b>{int(s['max_alerts_per_night'])}"
                         f" ordinary passes</b>")
        lines += [
            f"Pickiness: <b>{quality_label}</b> (mag {s['max_magnitude']})",
            f"Minimum elevation: <b>{s['min_elevation_deg']}°</b>",
            f"Quiet hours: <b>{s['quiet_start_hour']}:00–{s['quiet_end_hour']}:00</b>",
            f"Event types: <b>{kinds}</b>",
            f"ntfy mirror: <b>{mirror}</b>",
            f"Status: <b>{'on' if sub['active'] else 'paused'}</b>",
            "",
            "/setup to redo it all, /help for the individual commands.",
        ]
        self.reply(chat_id, "\n".join(lines))

    def _numeric_setting(self, chat_id, args, key, label, lo, hi, cast=float, cmd=""):
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        try:
            value = cast(args.replace(",", "."))
        except (TypeError, ValueError):
            self.reply(chat_id, f"Give me a number, e.g. <code>{cmd} "
                                f"{config.DEFAULTS[key]}</code>.")
            return
        if not lo <= value <= hi:
            self.reply(chat_id, f"{label} has to be between {lo} and {hi}.")
            return
        store.update_settings(sub["id"], **{key: value})
        self.reply(chat_id, f"{label} set to <b>{value}</b>.")

    def _cmd_mag(self, chat_id, args): self._numeric_setting(
        chat_id, args, "max_magnitude", "Brightness cutoff", -5, 7, cmd="/mag")

    def _cmd_cap(self, chat_id, args): self._numeric_setting(
        chat_id, args, "max_alerts_per_night", "Nightly alert limit", 0, 20, int, cmd="/cap")

    def _cmd_elev(self, chat_id, args): self._numeric_setting(
        chat_id, args, "min_elevation_deg", "Minimum elevation", 0, 80, cmd="/elev")

    def _cmd_lead(self, chat_id, args): self._numeric_setting(
        chat_id, args, "lead_minutes", "Warning time", 0, 180, int, cmd="/lead")

    def _cmd_quiet(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        parts = args.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            self.reply(chat_id, "Usage: <code>/quiet 1 6</code> — no pushes between "
                                "01:00 and 06:00 local.")
            return
        a, b = int(parts[0]) % 24, int(parts[1]) % 24
        store.update_settings(sub["id"], quiet_start_hour=a, quiet_end_hour=b)
        self.reply(chat_id, f"Quiet hours: <b>{a}:00–{b}:00</b>.")

    def _cmd_kinds(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        self.reply(chat_id, "Which alerts do you want? Tap to toggle.",
                   self._kinds_keyboard(sub["settings"].get("kinds") or []))

    @staticmethod
    def _kinds_keyboard(active: list[str]) -> dict:
        from .events import KIND_LABELS
        rows = []
        for kind, label in KIND_LABELS.items():
            mark = "✅" if kind in active else "⬜"
            rows.append([{"text": f"{mark} {label}", "callback_data": f"kind:{kind}"}])
        rows.append([{"text": "Done", "callback_data": "kind:done"}])
        return {"inline_keyboard": rows}

    def _cmd_ntfy(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub is None:
            return self._cmd_start(chat_id, "")
        topic = args.strip()
        if topic.lower() in ("off", "none", "stop", ""):
            # update_settings ignores None, so clear with an empty string.
            store.update_settings(sub["id"], ntfy_topic="")
            self.reply(chat_id, "ntfy mirroring off.")
            return
        store.update_settings(sub["id"], ntfy_topic=topic)
        transports.send_ntfy(topic, "Skywatch is now mirroring your alerts here.",
                             title="Skywatch connected")
        self.reply(chat_id, f"Mirroring alerts to <code>ntfy.sh/{html_mod.escape(topic)}</code>. "
                            "I just sent a test message there.")

    def _cmd_pause(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub:
            store.set_active(sub["id"], False)
        self.reply(chat_id, "Paused. /resume when you want them back.")

    def _cmd_resume(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub:
            store.set_active(sub["id"], True)
        self.reply(chat_id, "Alerts back on.")

    def _cmd_forget(self, chat_id, args: str) -> None:
        sub = self._sub(chat_id)
        if sub:
            store.delete_subscriber(sub["id"])
        self.reply(chat_id, "Deleted everything I had about you. /start to begin again.",
                   {"remove_keyboard": True})

    # --- callbacks ----------------------------------------------------------

    def _handle_callback(self, cq: dict) -> None:
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq.get("data") or ""
        transports.telegram_call(self.token, "answerCallbackQuery",
                                 {"callback_query_id": cq["id"]})

        if data.startswith("geo:"):
            idx = int(data.split(":", 1)[1])
            options = self._pending_places.pop(chat_id, [])
            if idx < len(options):
                p = options[idx]
                transports.telegram_call(self.token, "editMessageText", {
                    "chat_id": chat_id, "message_id": message_id,
                    "text": f"Picked: <b>{html_mod.escape(p.label)}</b>",
                    "parse_mode": "HTML"})
                self._set_location(chat_id, p.lat, p.lon, p.label)
            else:
                self.reply(chat_id, "That search expired — send the place name again.")
            return

        if data.startswith("setup:"):
            _, step, value = data.split(":", 2)
            self._apply_setup(chat_id, step, value)
            nxt = self._next_setup_step(chat_id, step)
            if nxt:
                self._ask_setup(chat_id, nxt, message_id)
            else:
                self._finish_setup(chat_id, message_id)
            return

        if not data.startswith("kind:"):
            return
        kind = data.split(":", 1)[1]
        sub = self._sub(chat_id)
        if sub is None:
            return
        if kind == "done":
            transports.telegram_call(self.token, "editMessageText", {
                "chat_id": chat_id, "message_id": cq["message"]["message_id"],
                "text": "Saved.", "parse_mode": "HTML"})
            return
        active = list(sub["settings"].get("kinds") or [])
        if kind in active:
            active.remove(kind)
        else:
            active.append(kind)
        store.update_settings(sub["id"], kinds=active)
        transports.telegram_call(self.token, "editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": cq["message"]["message_id"],
            "reply_markup": self._kinds_keyboard(active)})


# --- helpers ----------------------------------------------------------------


def _parse_coords(text: str):
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _timezone_for(lat: float, lon: float) -> str:
    try:
        from timezonefinder import TimezoneFinder
        tz = TimezoneFinder().timezone_at(lat=lat, lng=lon)
        if tz:
            return tz
    except Exception:
        log.warning("timezone lookup failed for %.3f,%.3f", lat, lon)
    return "UTC"
