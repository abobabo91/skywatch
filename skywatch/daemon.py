"""The always-on loop: recompute, then fire alerts at the right moment.

Runs two threads. The bot thread long-polls Telegram so subscriptions and
setting changes take effect immediately. The scheduler thread wakes once a
minute, asks the engine what is coming up for each subscriber, and pushes
anything whose lead time has arrived.

Catch-up behaviour matters here because this runs on a desktop that sleeps. On
waking, the scheduler will see a pile of events whose alert time has passed. It
sends nothing for an event that has already started -- a notification telling you
to look at a satellite that crossed forty minutes ago is worse than silence.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, diary_location, events, render, singleton, store, transports
from .bot import Bot
from .engine import Engine
from .passes import Observer

log = logging.getLogger("skywatch")

TICK_SECONDS = 60
# Never alert about something that already began; and do not resurrect an alert
# whose moment passed while the machine was asleep.
MAX_LATENESS_SECONDS = 120

# A missed daily digest is still worth sending late -- the list covers the whole
# night ahead, so it stays useful for hours. Stop before midnight so a digest for
# a night that is already over is never sent.
DIGEST_CATCHUP_UNTIL_HOUR = 23


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.handlers.RotatingFileHandler(
        config.LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def in_quiet_hours(settings: dict, tz_name: str, when: datetime) -> bool:
    try:
        local = when.astimezone(ZoneInfo(tz_name))
    except Exception:
        return False
    start = int(settings.get("quiet_start_hour", 1))
    end = int(settings.get("quiet_end_hour", 6))
    h = local.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end       # window wraps past midnight


class Scheduler:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("scheduler started")
        last_prune = 0.0
        last_follow = 0.0
        while not self._stop.is_set():
            started = time.time()
            # Where is the observer? Where the latest daily diary ends. Checked
            # hourly (a 304 when nothing changed), applied once per diary day.
            if time.time() - last_follow > diary_location.CHECK_SECONDS:
                try:
                    diary_location.follow()
                except Exception:
                    log.exception("diary follow failed")
                last_follow = time.time()
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            if time.time() - last_prune > 6 * 3600:
                removed = store.prune_sent(7)
                log.info("pruned %d old ledger rows", removed)
                last_prune = time.time()
            self._stop.wait(max(5.0, TICK_SECONDS - (time.time() - started)))

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        subs = store.active_subscribers()
        if not subs:
            return
        for sub in subs:
            try:
                self._tick_subscriber(sub, now)
            except Exception:
                log.exception("subscriber %s failed", sub["id"])

    def _tick_subscriber(self, sub: dict, now: datetime) -> None:
        settings = sub["settings"]
        observer = Observer(sub["lat"], sub["lon"], sub.get("elevation_m") or 0.0)
        lead = timedelta(minutes=int(settings.get("lead_minutes", 15)))
        mode = settings.get("delivery_mode", "both")

        if mode in ("digest", "both"):
            self._maybe_send_digest(sub, observer, settings, now)
        if mode == "digest":
            # Daily list only; no per-event pings for this subscriber.
            return

        # Drop anything that would be silenced anyway BEFORE budgeting the night,
        # otherwise three 04:00 passes eat the quota and the good 22:00 one that
        # would actually have been sent gets crowded out.
        # Quiet hours never suppress a Starlink train -- that is the whole reason
        # most people subscribe, and it is rare enough to be worth the buzz.
        candidates = [e for e in self.engine.events_for(observer, settings)
                      if e.kind == "starlink_train"
                      or not in_quiet_hours(settings, sub["tz"], e.t_start)]
        # Budget best-first, so the brightest pass wins rather than the earliest.
        pushable = events.select_for_push(candidates, settings, sub["tz"])

        for event in candidates:
            if event.key not in pushable:
                continue
            alert_at = event.t_start - lead
            if now < alert_at:
                continue
            lateness = (now - alert_at).total_seconds()
            if event.t_start <= now or lateness > lead.total_seconds() + MAX_LATENESS_SECONDS:
                continue
            if store.was_sent(sub["id"], event.key):
                continue

            html = render.render_event(event, sub["tz"], now)
            priority = "high" if event.kind == "starlink_train" else "default"
            title = "Starlink train" if event.kind == "starlink_train" else event.title
            if transports.deliver(sub, html, title=title, priority=priority):
                store.mark_sent(sub["id"], event.key)
                log.info("sent %s (%s) to subscriber %s", event.key, event.title, sub["id"])
            else:
                log.warning("delivery failed for %s to subscriber %s", event.key, sub["id"])

    def _maybe_send_digest(self, sub: dict, observer: Observer,
                           settings: dict, now: datetime) -> None:
        hour = settings.get("digest_hour")
        # None or a negative hour both mean "no daily list".
        if hour is None or int(hour) < 0:
            return
        try:
            local = now.astimezone(ZoneInfo(sub["tz"]))
        except Exception:
            return
        # Catch up rather than requiring the machine to be awake during that
        # exact hour. This runs on a desktop that sleeps; an exact-hour match
        # means one nap over 18:00 silently costs the whole day's digest, which
        # for a digest-only subscriber is the entire product.
        if local.hour < int(hour) or local.hour >= DIGEST_CATCHUP_UNTIL_HOUR:
            return
        key = f"digest:{local:%Y-%m-%d}"
        if store.was_sent(sub["id"], key):
            return
        found = self.engine.tonight(observer, settings)
        html = render.render_digest(found, sub["tz"], now)
        if transports.deliver(sub, html, title="Tonight's sky"):
            store.mark_sent(sub["id"], key)
            log.info("sent digest to subscriber %s (%d events)", sub["id"], len(found))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Skywatch satellite alert daemon")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-bot", action="store_true",
                    help="run the scheduler only (no Telegram polling)")
    ap.add_argument("--once", action="store_true",
                    help="run a single scheduler tick and exit")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    # One daemon per machine, decided here and not by the supervisor's guess:
    # 63 copies ran side by side on 2026-10-26 before this existed.
    if not args.once and not singleton.acquire():
        log.info("another skywatch daemon holds the lock; exiting (pid %d)", os.getpid())
        return 0
    # First thing in the log, before the slow work. Importing skyfield/numpy can
    # take the best part of a minute on a loaded machine, and without this line
    # a healthy daemon looks hung.
    log.info("daemon starting (pid %d)", os.getpid())
    store.init()

    engine = Engine()
    engine.ensure_catalog()

    scheduler = Scheduler(engine)
    if args.once:
        scheduler.tick()
        return 0

    threads = []
    token = config.telegram_token()
    bot = None
    if token and not args.no_bot:
        bot = Bot(engine, token)
        t = threading.Thread(target=bot.run, name="telegram", daemon=True)
        t.start()
        threads.append(t)
    elif not token:
        log.warning("no Telegram token in the vault; running scheduler only. "
                    "Add [skywatch.telegram] BOT_TOKEN to vault.toml.")

    def shutdown(signum, frame):
        log.info("signal %s received, shutting down", signum)
        scheduler.stop()
        if bot:
            bot.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    scheduler.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
