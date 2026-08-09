"""Turning what a person types into a latitude and longitude.

Uses OpenStreetMap's Nominatim: free, no API key, no account. Its usage policy
asks for an identifying User-Agent and at most one request per second, both of
which are enforced here. Volume is a handful of lookups per new subscriber, so
this sits comfortably inside what the service asks for.

Results are cached forever in memory -- place coordinates do not move.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org"
# Nominatim requires a real contact point in the User-Agent, and enforces its
# policy against whoever that names. One string for the whole project, built from
# this install's own `skywatch.CONTACT` -- see config.contact().
USER_AGENT = config.USER_AGENT
MIN_INTERVAL = 1.1        # seconds between requests, per their usage policy

_lock = threading.Lock()
_last_call = 0.0
_cache: dict[str, object] = {}


@dataclass
class Place:
    lat: float
    lon: float
    name: str

    @property
    def parts(self) -> list[str]:
        return [p.strip() for p in self.name.split(",") if p.strip()]

    @property
    def short(self) -> str:
        """Place and country. For confirming a choice already made."""
        parts = self.parts
        if len(parts) <= 2:
            return ", ".join(parts)
        return f"{parts[0]}, {parts[-1]}"

    @property
    def label(self) -> str:
        """Place, region and country, for telling candidates apart.

        `short` is not enough on a disambiguation list: a search for
        "Springfield" returns five of them, all of which render as
        "Springfield, United States" without the state.
        """
        parts = self.parts
        if len(parts) <= 2:
            return ", ".join(parts)
        return f"{parts[0]}, {parts[-2]}, {parts[-1]}"


def _get(path: str, params: dict) -> object | None:
    global _last_call
    url = f"{NOMINATIM}/{path}?" + urllib.parse.urlencode(params)
    if url in _cache:
        return _cache[url]
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            log.warning("nominatim %s failed: %s", path, exc)
            return None
    _cache[url] = data
    return data


def search(query: str, limit: int = 5) -> list[Place]:
    """Place name -> candidate coordinates, best match first."""
    query = query.strip()
    if not query:
        return []
    data = _get("search", {"q": query, "format": "json", "limit": limit})
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        try:
            out.append(Place(float(row["lat"]), float(row["lon"]),
                             row.get("display_name", query)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def reverse(lat: float, lon: float) -> str | None:
    """Coordinates -> a human-readable place name, for confirming a GPS share."""
    data = _get("reverse", {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                            "format": "json", "zoom": "12"})
    if not isinstance(data, dict):
        return None
    name = data.get("display_name")
    if not name:
        return None
    parts = [p.strip() for p in name.split(",")]
    # Trim postcodes and the deep administrative middle; keep place + country.
    keep = [p for p in parts if not re.fullmatch(r"[\d\s\-]+", p)]
    if len(keep) <= 2:
        return ", ".join(keep)
    return f"{keep[0]}, {keep[-1]}"


MAP_LINK_PATTERNS = [
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),                  # google maps /@lat,lon,z
    re.compile(r"[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)"),          # ?q=lat,lon
    re.compile(r"[?&]ll=(-?\d+\.\d+),\s*(-?\d+\.\d+)"),         # ?ll=lat,lon
    re.compile(r"[?&#]map=\d+/(-?\d+\.\d+)/(-?\d+\.\d+)"),      # openstreetmap #map=z/lat/lon
    re.compile(r"/(-?\d+\.\d+),(-?\d+\.\d+)"),                  # bare /lat,lon in a path
]


SHORTENERS = ("goo.gl", "maps.app", "bit.ly", "tinyurl.com", "osm.org")


def _match_coords(text: str) -> tuple[float, float] | None:
    for pattern in MAP_LINK_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                lat, lon = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


def _expand(url: str) -> str | None:
    """Follow a shortened link to whatever it really points at.

    The Share button in the Google Maps app produces a maps.app.goo.gl link with
    no coordinates in it, which is precisely the link people paste.
    """
    m = re.search(r"https?://\S+", url)
    if not m:
        return None
    try:
        req = urllib.request.Request(m.group(0), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            final = resp.geturl()
            body = resp.read(200_000).decode("utf-8", "replace")
        return f"{final}\n{body}"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("could not expand %s: %s", m.group(0)[:80], exc)
        return None


def coords_from_link(text: str) -> tuple[float, float] | None:
    """Pull coordinates out of a pasted map URL.

    People share a Google Maps link far more readily than they type numbers.
    """
    if "http" not in text:
        return None
    direct = _match_coords(text)
    if direct:
        return direct
    if any(s in text for s in SHORTENERS):
        expanded = _expand(text)
        if expanded:
            return _match_coords(expanded)
    return None
