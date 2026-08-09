"""Orbital element fetching and caching.

Uses Celestrak's OMM/CSV feeds, never FORMAT=tle -- see config.CELESTRAK_GROUPS
for why. Caches aggressively because Celestrak rate-limits repeat downloads of a
GROUP to once per 2 hours per IP and answers HTTP 403 (with an explanatory plain
text body) rather than serving a cached copy.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sgp4 import omm
from sgp4.api import Satrec
from skyfield.api import EarthSatellite, load

from . import config

log = logging.getLogger(__name__)
_TS = load.timescale()


def timescale():
    return _TS


@dataclass
class Sat:
    """One tracked object: a Skyfield satellite plus the catalog metadata we need."""

    sat: EarthSatellite
    name: str
    norad: int
    intldes: str          # e.g. "2026-175A"
    groups: set = field(default_factory=set)

    @property
    def launch_key(self) -> str:
        """Designator of the launch this object came from, e.g. '2026-175'."""
        return self.intldes[:8] if len(self.intldes) >= 8 else self.intldes

    @property
    def is_starlink(self) -> bool:
        return self.name.upper().startswith("STARLINK")


def _download(url: str, dest: Path, min_age: float, max_age: float) -> bytes:
    """Fetch `url` to `dest`, honouring Celestrak's refresh window.

    Returns cached bytes when the cache is younger than `min_age`, or when the
    server refuses the refresh but we still hold a copy younger than `max_age`.
    """
    now = time.time()
    age = now - dest.stat().st_mtime if dest.exists() else float("inf")
    if age < min_age:
        return dest.read_bytes()

    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        data = urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()[:200]
        except Exception:
            pass
        if dest.exists() and age < max_age:
            log.warning("refresh refused for %s (HTTP %s: %s); using cache aged %.1f h",
                        url, exc.code, body.decode("utf-8", "replace").strip()[:120], age / 3600)
            return dest.read_bytes()
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        if dest.exists() and age < max_age:
            log.warning("fetch failed for %s (%s); using cache aged %.1f h", url, exc, age / 3600)
            return dest.read_bytes()
        raise

    # Celestrak signals "you already have this" with a 200 + short text body.
    if len(data) < 500 and b"has not updated" in data:
        if dest.exists():
            dest.touch()
            return dest.read_bytes()
        raise RuntimeError(f"Celestrak withheld {url} and no cache exists")

    dest.write_bytes(data)
    return data


def _parse_omm_csv(data: bytes, group: str, into: dict[int, Sat]) -> int:
    added = 0
    text = data.decode("utf-8", "replace")
    for fields in omm.parse_csv(io.StringIO(text)):
        try:
            satrec = Satrec()
            omm.initialize(satrec, fields)
            norad = int(fields.get("NORAD_CAT_ID") or satrec.satnum)
        except Exception:
            continue
        existing = into.get(norad)
        if existing is not None:
            existing.groups.add(group)
            continue
        try:
            es = EarthSatellite.from_satrec(satrec, _TS)
        except Exception:
            continue
        name = (fields.get("OBJECT_NAME") or f"NORAD {norad}").strip()
        es.name = name
        into[norad] = Sat(sat=es, name=name, norad=norad,
                          intldes=(fields.get("OBJECT_ID") or "").strip(),
                          groups={group})
        added += 1
    return added


def load_catalog(groups: list[str] | None = None, force: bool = False) -> dict[int, Sat]:
    """Fetch (or reuse) the requested Celestrak groups and return {norad: Sat}."""
    wanted = groups or list(config.CELESTRAK_GROUPS)
    min_age = 0 if force else config.TLE_MIN_REFRESH_SECONDS
    cat: dict[int, Sat] = {}
    for group in wanted:
        url = config.CELESTRAK_GROUPS[group]
        dest = config.TLE_DIR / f"{group}.csv"
        try:
            data = _download(url, dest, min_age, config.TLE_MAX_AGE_SECONDS)
        except Exception as exc:
            log.error("could not load group %s: %s", group, exc)
            continue
        n = _parse_omm_csv(data, group, cat)
        log.info("group %-13s %5d objects (%d new)", group, len(cat), n)
    return cat


def catalog_age_hours() -> float:
    """Age of the freshest cached group file, in hours."""
    files = list(config.TLE_DIR.glob("*.csv"))
    if not files:
        return float("inf")
    return (time.time() - max(f.stat().st_mtime for f in files)) / 3600


# --- SATCAT: launch dates, object type, radar cross section ------------------


def load_satcat() -> dict[str, dict]:
    """{international designator prefix -> {'launch_date', 'rcs', 'object_type'}}.

    Keyed by launch (e.g. '2026-175') so a whole Starlink batch resolves at once.
    """
    dest = config.TLE_DIR / "satcat.csv"
    data = _download(config.SATCAT_URL, dest, 24 * 3600, 30 * 24 * 3600)
    out: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", "replace")))
    for row in reader:
        oid = (row.get("OBJECT_ID") or "").strip()
        if len(oid) < 8 or row.get("DECAY_DATE"):
            continue
        key = oid[:8]
        if key in out:
            continue
        ld = row.get("LAUNCH_DATE") or ""
        try:
            launched = datetime.strptime(ld, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            rcs = float(row.get("RCS") or "")
        except ValueError:
            rcs = None
        out[key] = {"launch_date": launched, "rcs": rcs,
                    "object_type": (row.get("OBJECT_TYPE") or "").strip()}
    return out


def load_rcs_by_norad() -> dict[int, float]:
    dest = config.TLE_DIR / "satcat.csv"
    data = _download(config.SATCAT_URL, dest, 24 * 3600, 30 * 24 * 3600)
    out: dict[int, float] = {}
    for row in csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))):
        try:
            out[int(row["NORAD_CAT_ID"])] = float(row["RCS"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


# --- McCants standard magnitudes --------------------------------------------


def load_standard_magnitudes() -> dict[int, float]:
    """{norad: standard magnitude} from Mike McCants' quicksat.mag.

    Standard magnitude is the object's brightness at 1000 km range and 50%
    illuminated. ~3650 objects, which covers essentially every satellite bright
    enough for a human to notice. Not updated for Starlink -- those are modelled
    separately in magnitudes.py.
    """
    dest = config.TLE_DIR / "quicksat.zip"
    try:
        data = _download(config.QUICKSAT_URL, dest, 30 * 24 * 3600, 365 * 24 * 3600)
    except Exception as exc:
        log.error("standard magnitude file unavailable: %s", exc)
        return {}
    try:
        raw = zipfile.ZipFile(io.BytesIO(data)).read("quicksat.mag").decode("latin-1")
    except Exception as exc:
        log.error("could not read quicksat.mag: %s", exc)
        return {}
    mags: dict[int, float] = {}
    for line in raw.splitlines()[1:]:
        if len(line) < 40:
            continue
        try:
            norad = int(line[0:5])
            mags[norad] = float(line[32:37].strip())
        except ValueError:
            continue
    return mags
