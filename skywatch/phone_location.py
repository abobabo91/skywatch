"""Follow the phone: the newest GPS ping is where the observer is.

The phone appends one line per ping to a text file on Dropbox (an iOS Shortcut
feeds the daily diary the same way). The last line of that file is the current
location, so the daemon reads it every FOLLOW_SECONDS and moves the private
subscribers there when the phone is more than MIN_MOVE_KM from the stored spot.
Nothing else is needed: the engine caches passes per location, so a move simply
means the next tick computes for the new place.

Line shape (phone-local time, no zone):

    10/26/26, 6:01 PM- 32.08199703278895, -81.0921401184344, Mail

Freshness comes from Dropbox's own `server_modified`, not from the line: the
phone's clock zone is unknown and changes with travel. A file older than
MAX_AGE_SECONDS (phone off, Dropbox not syncing) moves nobody.

Credentials: the `[dropbox.diary_reader]` section of the same vault.toml the
bot token comes from (app_key, app_secret, refresh_token). Section-scoped on
purpose: `refresh_token` is a leaf name other sections use too.
"""
from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, geocode, store

log = logging.getLogger("skywatch.phone")

FOLLOW_SECONDS = 15 * 60
MIN_MOVE_KM = 2.0
MAX_AGE_SECONDS = 3 * 24 * 3600

VAULT_SECTION = "dropbox.diary_reader"
REMOTE_PATH = "/locations.txt"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"

LINE = re.compile(
    r"(\d+/\d+/\d+,\s*\d+:\d+\s*[   ]?[AP]M)"
    r"\s*-\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Ping:
    lat: float
    lon: float
    stamp: str            # the phone's own text, for logs only
    modified: datetime    # Dropbox server_modified, UTC

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.modified).total_seconds()


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
    if {"app_key", "app_secret", "refresh_token"} - out.keys():
        return None
    return out


def configured() -> bool:
    return _creds() is not None


def _access_token(creds: dict[str, str]) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["app_key"],
        "client_secret": creds["app_secret"],
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def latest() -> Ping | None:
    """The newest ping in the Dropbox file, or None when unconfigured/unreadable."""
    creds = _creds()
    if creds is None:
        return None
    token = _access_token(creds)
    req = urllib.request.Request(DOWNLOAD_URL, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": json.dumps({"path": REMOTE_PATH}),
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        meta = json.loads(r.headers.get("Dropbox-API-Result") or "{}")
        body = r.read().decode("utf-8", errors="replace")
    last = None
    for m in LINE.finditer(body):
        last = m
    if last is None:
        return None
    modified = datetime.fromisoformat(meta["server_modified"].replace("Z", "+00:00"))
    return Ping(float(last.group(2)), float(last.group(3)), last.group(1), modified)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def followers() -> list[dict]:
    """The subscribers that follow the phone: the private bot's allowed chats.

    In open mode (no ALLOWED_CHAT_IDS) nobody follows -- strangers' alerts must
    not move with the owner's phone.
    """
    allowed = config.allowed_chat_ids()
    if not allowed:
        return []
    return [s for s in store.active_subscribers()
            if s["transport"] == "telegram" and str(s["target"]) in allowed]


def _timezone_for(lat: float, lon: float) -> str:
    from .bot import _timezone_for as tzf
    return tzf(lat, lon)


def follow(apply: bool = True) -> list[tuple[dict, Ping, float]]:
    """Move each follower to the phone if it is far enough away. Returns the moves."""
    subs = followers()
    if not subs:
        return []
    try:
        ping = latest()
    except (OSError, urllib.error.URLError, KeyError, ValueError) as exc:
        log.warning("phone location unavailable: %s", exc)
        return []
    if ping is None:
        log.warning("phone location: no ping found (vault section or file missing)")
        return []
    if ping.age_seconds > MAX_AGE_SECONDS:
        log.info("phone location is %.1f h old (%s); not moving anyone",
                 ping.age_seconds / 3600, ping.stamp)
        return []
    moves = []
    for sub in subs:
        km = distance_km(sub["lat"], sub["lon"], ping.lat, ping.lon)
        if km < MIN_MOVE_KM:
            continue
        moves.append((sub, ping, km))
        if not apply:
            continue
        tz_name = _timezone_for(ping.lat, ping.lon)
        place = geocode.reverse(ping.lat, ping.lon)
        store.upsert_subscriber(sub["transport"], sub["target"], lat=ping.lat, lon=ping.lon,
                                tz=tz_name, place=place)
        log.info("subscriber %s follows the phone: %s -> %s (%.4f, %.4f, %s), %.0f km, ping %s",
                 sub["id"], sub.get("place"), place, ping.lat, ping.lon, tz_name, km, ping.stamp)
    return moves
