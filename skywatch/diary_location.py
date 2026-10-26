"""The observer is wherever the latest daily diary ends.

Once a day the diary site publishes `/data/map.json`: one entry per diary day
with the GPS route of that day (`days[-1].route[-1]` is the last point of the
most recent day) plus `generated`, the build time. The daemon fetches that file
behind the site's HTTP Basic password, and when a diary day appears that it has
not applied yet, it moves the private subscribers to that day's last point if
it is more than MIN_MOVE_KM from the stored spot -- timezone from
timezonefinder, place name from Nominatim, exactly as for a shared Telegram pin.
The engine caches passes per location, so the next tick computes for the new
place without further ado.

Cadence: the file is polled every CHECK_SECONDS with `If-None-Match`, so a day
with no new diary costs one 304. The move itself happens at most once per
diary day (`bot_state["diary_day"]`), which is what "once a day" means here.
A latest day older than MAX_AGE_DAYS (diary not running) moves nobody.

Credentials: `[diary.web]` in the same vault.toml as the bot token -- `url`
(the site) and `password` (any username; the Vercel project's DIARY_PASSWORD).
Without the section nothing is fetched and nobody moves.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import config, geocode, store

log = logging.getLogger("skywatch.diary")

CHECK_SECONDS = 60 * 60
MIN_MOVE_KM = 2.0
MAX_AGE_DAYS = 3
STATE_KEY = "diary_day"

VAULT_SECTION = "diary.web"
MAP_PATH = "/data/map.json"

_etag: str | None = None


@dataclass(frozen=True)
class Spot:
    lat: float
    lon: float
    day: str          # the diary day the point belongs to, YYYY-MM-DD
    generated: str    # when the diary built the file

    @property
    def age_days(self) -> int:
        return (date.today() - date.fromisoformat(self.day)).days


def _creds() -> dict[str, str] | None:
    if config.VAULT_PATH is None:
        return None
    text = config.VAULT_PATH.read_text(encoding="utf-8", errors="replace")
    start = re.search(rf"^\[{re.escape(VAULT_SECTION)}\]\s*$", text, re.MULTILINE)
    if not start:
        return None
    rest = text[start.end():]
    nxt = re.search(r"^\[", rest, re.MULTILINE)
    block = rest[: nxt.start()] if nxt else rest
    out = dict(re.findall(r"^(\w+)\s*=\s*[\"']([^\"']*)[\"']", block, re.MULTILINE))
    if not out.get("url") or not out.get("password"):
        return None
    return out


def configured() -> bool:
    return _creds() is not None


def fetch(use_etag: bool = True) -> dict | None:
    """The diary's map.json, or None when unconfigured or unchanged since last time.

    `use_etag=False` always downloads: the CLI's --apply calls this twice in one
    process, and the second call must not be answered by the first one's 304.
    """
    global _etag
    creds = _creds()
    if creds is None:
        return None
    auth = base64.b64encode(f"skywatch:{creds['password']}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "User-Agent": config.USER_AGENT}
    if _etag and use_etag:
        headers["If-None-Match"] = _etag
    req = urllib.request.Request(creds["url"].rstrip("/") + MAP_PATH, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            _etag = r.headers.get("ETag") or _etag
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise


def latest_spot(data: dict) -> Spot | None:
    days = [d for d in data.get("days") or [] if d.get("route")]
    if not days:
        return None
    last = max(days, key=lambda d: d["day"])
    lat, lon = last["route"][-1][:2]
    return Spot(float(lat), float(lon), last["day"], data.get("generated") or "")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def followers() -> list[dict]:
    """The subscribers that follow the diary: the private bot's allowed chats.

    In open mode (no ALLOWED_CHAT_IDS) nobody follows -- strangers' alerts must
    not move with the owner's diary.
    """
    allowed = config.allowed_chat_ids()
    if not allowed:
        return []
    return [s for s in store.active_subscribers()
            if s["transport"] == "telegram" and str(s["target"]) in allowed]


def _timezone_for(lat: float, lon: float) -> str:
    from .bot import _timezone_for as tzf
    return tzf(lat, lon)


def follow(apply: bool = True, force: bool = False) -> list[tuple[dict, Spot, float]]:
    """Apply the latest diary day once. Returns the moves (also when apply=False)."""
    subs = followers()
    if not subs:
        return []
    try:
        data = fetch(use_etag=not force)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        log.warning("diary map unavailable: %s", exc)
        return []
    if data is None:
        return []                       # unconfigured, or 304: nothing new
    spot = latest_spot(data)
    if spot is None:
        log.warning("diary map has no day with a route")
        return []
    if not force and store.get_state(STATE_KEY) == spot.day:
        return []                       # this diary day was already applied
    if spot.age_days > MAX_AGE_DAYS:
        log.info("latest diary day %s is %d days old; not moving anyone", spot.day, spot.age_days)
        return []
    moves = []
    for sub in subs:
        km = distance_km(sub["lat"], sub["lon"], spot.lat, spot.lon)
        if km < MIN_MOVE_KM:
            continue
        moves.append((sub, spot, km))
        if not apply:
            continue
        tz_name = _timezone_for(spot.lat, spot.lon)
        place = geocode.reverse(spot.lat, spot.lon)
        store.upsert_subscriber(sub["transport"], sub["target"], lat=spot.lat, lon=spot.lon,
                                tz=tz_name, place=place)
        log.info("subscriber %s follows the diary: %s -> %s (%.4f, %.4f, %s), %.0f km, diary day %s",
                 sub["id"], sub.get("place"), place, spot.lat, spot.lon, tz_name, km, spot.day)
    if apply:
        store.set_state(STATE_KEY, spot.day)
    return moves
