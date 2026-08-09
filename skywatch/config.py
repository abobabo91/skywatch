"""Paths, tunable constants and secret loading for skywatch.

Everything a human might want to change lives here or in the per-subscriber
settings in the database. Nothing in this file is a secret.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SKYWATCH_DATA", PROJECT_ROOT / "data"))
TLE_DIR = DATA_DIR / "tle"
DB_PATH = DATA_DIR / "skywatch.sqlite3"
LOG_PATH = DATA_DIR / "skywatch.log"

# Secrets and per-install settings live outside the repo. Searched in order:
#   1. $SKYWATCH_VAULT          absolute path to a TOML file, so an install can
#                               point at a vault it already keeps elsewhere
#   2. <project>/skywatch.toml  gitignored local config -- the usual choice
#   3. ~/.skywatch.toml
# Any single value can also be supplied as SKYWATCH_<KEY> (e.g. SKYWATCH_BOT_TOKEN),
# which beats every file. Nothing here is committed, and there is no bundled
# fallback config: an install with none of the above simply has no token.
def _find_vault() -> Path | None:
    candidates = []
    env = os.environ.get("SKYWATCH_VAULT")
    if env:
        candidates.append(Path(env))
    candidates += [PROJECT_ROOT / "skywatch.toml", Path.home() / ".skywatch.toml"]
    return next((p for p in candidates if p.exists()), None)


VAULT_PATH = _find_vault()

for _d in (DATA_DIR, TLE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- observing defaults (each is overridable per subscriber) -----------------

DEFAULTS = {
    # Only report a pass that climbs at least this high above the horizon.
    # Below ~10 deg you are looking through trees, buildings and haze.
    "min_elevation_deg": 15.0,
    # Report anything at least this bright. Lower number = brighter.
    # Naked eye in a city tops out near 4. Measured over one real night at 47.6N:
    # a cutoff of 3.5 produces ~28 qualifying passes, which is noise rather than
    # news -- most are tumbling rocket bodies. 2.0 keeps it to the ones that stop
    # you in your tracks.
    "max_magnitude": 2.0,
    # Hard ceiling on ordinary bright passes pushed per night, best-first.
    # Starlink trains and the space stations are exempt: they are rare enough
    # that missing one is worse than an extra buzz.
    "max_alerts_per_night": 3,
    # How many minutes before the pass starts to push the alert.
    "lead_minutes": 15,
    # Sun must be at least this far below the horizon for the sky to read as dark.
    # Heavens-Above's all-satellites table starts at roughly -1.5 deg; -6 (civil
    # twilight) is where a mag-3 satellite genuinely stands out.
    "sun_below_deg": -6.0,
    # Suppress pushes between these local hours (inclusive start, exclusive end).
    "quiet_start_hour": 1,
    "quiet_end_hour": 6,
    # Which event kinds this subscriber wants.
    "kinds": ["starlink_train", "bright_pass", "station"],
    # Daily "what's on tonight" digest, local hour. None disables it.
    "digest_hour": 18,
    # How this subscriber wants to hear from us:
    #   "digest"  - one list a day, no per-event pings
    #   "alerts"  - only a reminder shortly before each event
    #   "both"    - the daily list AND the reminders
    "delivery_mode": "both",
}

# Brightness presets, so nobody has to know what a magnitude is. Values are the
# `max_magnitude` cutoff; lower is stricter.
QUALITY_PRESETS = {
    "spectacular": (0.5, "Only the showstoppers",
                    "A handful a month. ISS, the brightest flares, trains."),
    "good": (2.0, "The good ones",
             "A few a week. Bright enough to point at without hunting."),
    "everything": (3.5, "Anything I can see",
                   "Most clear nights. Includes faint tumbling rocket stages."),
}

# --- event detection tuning -------------------------------------------------

# A freshly launched Starlink batch is a "train" while it is still bunched.
# Measured 2026-08-08 against live Celestrak OMM data: a batch spans ~150 deg of
# its orbit by day 8 and ~330 deg by day 26, so the string-of-pearls window is
# the first few days. See docs/findings.md.
TRAIN = {
    "max_arc_deg": 60.0,       # angular span of the batch along its orbit
    "min_members": 8,          # fewer than this is not a "train"
    "max_age_days": 21,        # ignore batches older than this outright
    "max_altitude_km": 480.0,  # once raised to operational height they spread out
    "tight_gap_deg": 3.0,      # consecutive spacing counted as "in formation"
}

# Celestrak refuses a repeat download of the same GROUP from one IP inside this
# window, answering HTTP 403 with an explanatory body rather than an error page.
TLE_MIN_REFRESH_SECONDS = 2 * 3600 + 300
TLE_MAX_AGE_SECONDS = 12 * 3600

# USER_AGENT is defined at the bottom of this file, because it reads the config.

# Celestrak GP feeds. FORMAT=csv (OMM) is mandatory, not cosmetic: NORAD catalog
# numbers passed 100000 in July 2026 and those objects cannot be expressed in the
# 5-digit classic TLE format, so FORMAT=tle silently omits them -- including every
# brand-new Starlink launch, which is exactly what we care about.
CELESTRAK_GROUPS = {
    "visual": "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=csv",
    "stations": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=csv",
    "last-30-days": "https://celestrak.org/NORAD/elements/gp.php?GROUP=last-30-days&FORMAT=csv",
    "starlink": "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=csv",
    # Active payloads (~2.4 MB). The `visual` group is only ~160 objects while
    # McCants lists ~3650 with measured brightness, so this widens the net.
    "active": "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=csv",
    # Spent upper stages. NOT optional, and not covered by `active`: measured
    # 2026-08-08, GROUP=active is 16,301 payloads and exactly 2 rocket bodies,
    # while 1,079 of the 1,840 bright objects we were missing are R/B. Rocket
    # bodies are most of naked-eye satellite spotting -- the SL-16, CZ-4B and
    # Ariane stages that fill the nightly list. Without this, every one of the
    # four passes heavens-above listed brighter than mag 2.0 over Budakalasz was
    # invisible to us. There is no GROUP for them; the NAME query is the way in.
    "rocket-bodies": "https://celestrak.org/NORAD/elements/gp.php?NAME=R/B&FORMAT=csv",
    # Defunct Soviet/Russian payloads. `active` covers working satellites only,
    # so COSMOS 44 (launched 1971) and friends are absent from it despite being
    # naked-eye objects with published magnitudes. Good ratio: 2,879 objects
    # brings in 328 we want.
    "cosmos": "https://celestrak.org/NORAD/elements/gp.php?NAME=COSMOS&FORMAT=csv",
    # Debris. A poor ratio on paper -- 10,157 objects for ~120 we want -- but the
    # ones it brings are the Ariane SYLDA and SPELDA payload adapters, which are
    # among the brightest things in the sky. One was listed at magnitude 1.1 over
    # Singapore, brighter than any star. Missing those would be indefensible.
    "debris": "https://celestrak.org/NORAD/elements/gp.php?NAME=DEB&FORMAT=csv",
}
SATCAT_URL = "https://celestrak.org/pub/satcat.csv"
QUICKSAT_URL = "https://mmccants.org/programs/quicksat.zip"


def vault_get(dotted_key: str, default: str | None = None) -> str | None:
    """Read a leaf value out of vault.toml.

    vault.toml is hand-maintained and has historically contained duplicate table
    names, which makes it invalid TOML that tomllib refuses outright. Every other
    project here greps for the leaf instead, so do the same.
    """
    env = os.environ.get("SKYWATCH_" + dotted_key.rsplit(".", 1)[-1].upper())
    if env:
        return env
    if VAULT_PATH is None:
        return default
    section, _, leaf = dotted_key.rpartition(".")
    text = VAULT_PATH.read_text(encoding="utf-8", errors="replace")
    in_section = not section
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1].strip() == section
            continue
        if not in_section:
            continue
        m = re.match(rf'^{re.escape(leaf)}\s*=\s*["\']([^"\']*)["\']', stripped)
        if m:
            return m.group(1)
    return default


def telegram_token() -> str | None:
    return vault_get("skywatch.telegram.BOT_TOKEN")


def allowed_chat_ids() -> set[str]:
    """Telegram chat ids permitted to talk to the bot.

    An empty set means open to anyone, which is the subscribable-product mode.
    Set `ALLOWED_CHAT_IDS` in the vault (comma-separated) to run it private:
    every other chat is dropped before any parsing, geocoding or pass
    computation happens for it.

    A Telegram bot username is world-visible and searchable, so this is the only
    thing standing between a stranger and work on this machine. Read once when
    the bot starts, so a change needs a daemon restart.
    """
    raw = vault_get("skywatch.telegram.ALLOWED_CHAT_IDS", "") or ""
    return {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}


def default_ntfy_topic() -> str:
    """This install's own ntfy mirror topic, or empty if it has none.

    There is deliberately no fallback topic. ntfy.sh topics are unauthenticated,
    so a hardcoded default would mean every copy of this code that never set one
    pushes into the same person's topic -- leaking their alerts to strangers and
    handing strangers a way to spam their phone.
    """
    return vault_get("skywatch.ntfy.TOPIC", "") or ""


def contact() -> str:
    """Contact point advertised to Nominatim and Celestrak.

    Both enforce their usage policies against whoever the User-Agent names, so an
    install that ships someone else's contact makes that person absorb its abuse.
    Each install sets its own; unset is honest rather than borrowed.
    """
    return vault_get("skywatch.CONTACT", "") or "unconfigured"


USER_AGENT = f"skywatch/1.0 (satellite pass alerter; {contact()})"
