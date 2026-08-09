"""Message text.

The whole point of the alert is that someone reads it on a phone, walks outside
and finds the thing. So every message answers four questions in this order:
when, which way to face, how high to look, and where it travels.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import sky
from .events import Event
from .magnitudes import MagnitudeModel

KIND_ICON = {
    "starlink_train": "\U0001F686",   # train
    "bright_pass": "\U0001F6F0",      # satellite
    "station": "\U0001F680",          # rocket
}


def _local(dt: datetime, tz_name: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt.astimezone(timezone.utc)


def _countdown(t: datetime, now: datetime) -> str:
    mins = int((t - now).total_seconds() // 60)
    if mins < 0:
        return "now"
    if mins == 0:
        return "starting now"
    if mins < 60:
        return f"in {mins} min"
    hours = mins / 60
    if hours < 10:
        return f"in {hours:.1f} h".replace(".0", "")
    return t.strftime("at %H:%M")


def _brightness(mag: float | None) -> str:
    if mag is None:
        return "brightness unknown"
    word = MagnitudeModel({}).brightness_word(mag)
    return f"{word} (mag {mag:+.1f})".replace("+", "")


def _shadow_note(p) -> str:
    if p.ends_in_shadow:
        return "fades out — enters Earth's shadow"
    return "disappears below the horizon"


def _appear_note(p) -> str:
    if p.starts_in_shadow:
        return "appears out of nowhere — leaves Earth's shadow"
    return "appears"


def what_is_it(name: str) -> str:
    """One-line plain English gloss on a catalog name.

    Most bright passes are spent rocket stages with names nobody recognises, and
    "SL-16 R/B" means nothing to a person standing in a garden.
    """
    n = name.upper()
    if n.startswith("STARLINK"):
        return "a Starlink internet satellite"
    if "ISS" in n.split() or n.startswith("ISS"):
        return "the International Space Station"
    if n.startswith("CSS") or "TIANHE" in n:
        return "the Chinese space station"
    if "R/B" in n or "ROCKET" in n:
        if n.startswith("SL-"):
            return "a spent Russian rocket stage — big, tumbling, often bright"
        if n.startswith("CZ-"):
            return "a spent Chinese Long March rocket stage"
        if "ARIANE" in n or "DELTA" in n or "ATLAS" in n or "THORAD" in n:
            return "a spent rocket upper stage"
        return "a spent rocket stage"
    if n.startswith("COSMOS"):
        return "an old Russian satellite"
    if n.startswith("USA "):
        return "a US military satellite"
    if "DEB" in n.split():
        return "a piece of orbital debris"
    return "a satellite"


def render_pass(event: Event, tz_name: str, now: datetime,
                include_diagram: bool = True) -> str:
    p = event.lead
    icon = KIND_ICON.get(event.kind, "\U0001F6F0")
    start, peak, end = (_local(p.t_start, tz_name), _local(p.t_peak, tz_name),
                        _local(p.t_end, tz_name))
    mins = int(p.duration_s // 60)
    secs = int(p.duration_s % 60)

    lines = [
        f"{icon} <b>{event.title}</b> — {_brightness(p.magnitude)}",
        f"<i>{what_is_it(event.title)}</i>",
        f"<b>{_countdown(p.t_start, now)}</b> · {start:%H:%M}–{end:%H:%M} "
        f"({mins}m {secs:02d}s)",
        "",
        f"Face <b>{sky.compass(p.az_start)}</b> {sky.arrow(p.az_start)} "
        f"— it shows up {sky.elevation_hint(p.alt_start)}",
        "",
    ]
    # A satellite that leaves Earth's shadow already at its highest point gives
    # start == peak. Printing both lines repeats the same timestamp and reads
    # like a bug, so say the one thing that is true.
    if (p.t_peak - p.t_start).total_seconds() < 30:
        how = ("leaves Earth's shadow" if p.starts_in_shadow else "appears")
        lines.append(
            f"<code>{start:%H:%M:%S}</code>  {sky.compass(p.az_start):<3}  "
            f"{p.alt_start:.0f}°  {how} already at its highest — "
            f"{sky.elevation_hint(p.alt_peak)}")
    else:
        lines.append(
            f"<code>{start:%H:%M:%S}</code>  {sky.compass(p.az_start):<3}  "
            f"{p.alt_start:.0f}°  {_appear_note(p)}")
        lines.append(
            f"<code>{peak:%H:%M:%S}</code>  {sky.compass(p.az_peak):<3}  "
            f"{p.alt_peak:.0f}°  highest — {sky.elevation_hint(p.alt_peak)}")
    if (p.t_end - p.t_peak).total_seconds() >= 30:
        lines.append(
            f"<code>{end:%H:%M:%S}</code>  {sky.compass(p.az_end):<3}  "
            f"{p.alt_end:.0f}°  {_shadow_note(p)}")
    lines += [
        "",
        f"Path: <b>{sky.track_line(p.az_start, p.az_peak, p.az_end)}</b>",
    ]
    if include_diagram:
        lines += ["", "<pre>" + sky.sky_diagram(
            p.az_start, p.alt_start, p.az_peak, p.alt_peak, p.az_end, p.alt_end) + "</pre>",
            "<i>Centre = straight up, edge = horizon. "
            "o appears · * highest · x gone.</i>"]
    return "\n".join(lines)


def render_train(event: Event, tz_name: str, now: datetime,
                 include_diagram: bool = True) -> str:
    d = event.detail
    first, last = event.passes[0], event.passes[-1]
    brightest = min(event.passes,
                    key=lambda p: p.magnitude if p.magnitude is not None else 9)
    start = _local(event.t_start, tz_name)
    end = _local(event.t_end, tz_name)
    peak = _local(brightest.t_peak, tz_name)

    age = d.get("age_days")
    age_txt = f"{age:.0f} days after launch" if age is not None else "recently launched"

    lines = [
        f"{KIND_ICON['starlink_train']} <b>STARLINK TRAIN</b> — this is the "
        f"string-of-pearls one",
        f"<b>{_countdown(event.t_start, now)}</b> · {start:%H:%M}–{end:%H:%M}",
        "",
        f"Launch {d.get('launch','?')} · {d.get('visible_members','?')} of "
        f"{d.get('members','?')} satellites cross · {age_txt}",
        f"Still bunched together at {d.get('altitude_km',0):.0f} km — they follow "
        f"each other in a line, a few seconds apart.",
        "",
        f"Face <b>{sky.compass(first.az_start)}</b> {sky.arrow(first.az_start)} "
        f"— the first one appears {sky.elevation_hint(first.alt_start)}",
        f"Brightest around <b>{peak:%H:%M}</b>, "
        f"{_brightness(brightest.magnitude)}, "
        f"{sky.compass(brightest.az_peak)} at {brightest.alt_peak:.0f}° "
        f"({sky.elevation_hint(brightest.alt_peak)})",
        f"The whole string takes about <b>{d.get('string_minutes',0):.0f} min</b> to pass.",
        "",
        f"Path: <b>{sky.track_line(brightest.az_start, brightest.az_peak, brightest.az_end)}</b>",
    ]
    if include_diagram:
        lines += ["", "<pre>" + sky.sky_diagram(
            brightest.az_start, brightest.alt_start,
            brightest.az_peak, brightest.alt_peak,
            brightest.az_end, brightest.alt_end) + "</pre>"]
    return "\n".join(lines)


def render_event(event: Event, tz_name: str, now: datetime | None = None,
                 include_diagram: bool = True) -> str:
    now = now or datetime.now(timezone.utc)
    if event.kind == "starlink_train":
        return render_train(event, tz_name, now, include_diagram)
    return render_pass(event, tz_name, now, include_diagram)


def render_digest(events: list[Event], tz_name: str, now: datetime | None = None) -> str:
    """The 'what's on tonight' summary."""
    now = now or datetime.now(timezone.utc)
    if not events:
        return ("\U0001F311 <b>Tonight</b>\n\nNothing bright enough to be worth going "
                "outside for. Clear skies do not help if there is nothing up there.")
    lines = ["\U0001F30C <b>Tonight's sky</b>", ""]
    # Selected best-first, but read chronologically -- this is a plan for the
    # evening, not a leaderboard.
    for e in sorted(events, key=lambda x: x.t_start):
        t = _local(e.t_start, tz_name)
        p = e.lead
        icon = KIND_ICON.get(e.kind, "\U0001F6F0")
        if e.kind == "starlink_train":
            lines.append(
                f"{icon} <b>{t:%H:%M}</b>  Starlink train — "
                f"{e.detail.get('visible_members','?')} sats, "
                f"{sky.track_line(p.az_start, p.az_peak, p.az_end)}, "
                f"up to {p.alt_peak:.0f}°")
        else:
            mag = f"mag {p.magnitude:.1f}" if p.magnitude is not None else "?"
            lines.append(
                f"{icon} <b>{t:%H:%M}</b>  {e.title} — {mag}, "
                f"{sky.track_line(p.az_start, p.az_peak, p.az_end)}, "
                f"peaks {p.alt_peak:.0f}° {sky.compass(p.az_peak)}")
    lines += ["", "<i>I will ping you before the best few, not all of them — "
              "/cap changes how many. Trains and space stations always get "
              "through.</i>"]
    return "\n".join(lines)


def to_plaintext(html: str) -> str:
    """Strip the Telegram markup for transports that only take plain text."""
    import re
    text = re.sub(r"</?(b|i|code|pre|u|s)>", "", html)
    return (text.replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))
