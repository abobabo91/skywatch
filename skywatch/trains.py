"""Detecting a Starlink 'train' -- the string-of-pearls from a fresh launch.

This is the part that needed the most care, because the obvious approaches are
both wrong:

  * Comparing mean anomalies straight out of the elements does not work. Every
    object carries its own TLE epoch, so their mean anomalies are quoted at
    different instants and are not comparable. You have to propagate the whole
    batch to one common time first.

  * "Lots of Starlink crossing at once" does not work either. There are 10,763 of
    them up there as of August 2026, so on any clear night a couple of hundred
    cross above 25 deg within any six-minute window. That is background, not an
    event.

What actually makes a train is a batch from ONE launch that has not yet spread
around its orbital plane. Measured against live Celestrak data on 2026-08-08, a
batch spans roughly:

    day 8   -> 150 deg of arc        day 19  -> 243 deg
    day 15  -> 172 deg               day 26  -> 327 deg

so the tight formation worth waking someone up for is the first few days after
launch, and it is unambiguous when you measure it properly.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from . import config
from .tle import Sat, timescale

log = logging.getLogger(__name__)
_TS = timescale()
EARTH_RADIUS_KM = 6378.137


@dataclass
class Batch:
    """One launch's worth of satellites sharing an orbital plane."""

    launch: str                 # e.g. "2026-175"
    members: list[Sat]
    arc_deg: float              # angular span occupied along the orbit
    largest_gap_deg: float
    tight_run: int              # longest chain spaced <= tight_gap_deg apart
    mean_altitude_km: float
    period_minutes: float
    launch_date: datetime | None
    age_days: float | None

    @property
    def is_train(self) -> bool:
        return (self.arc_deg <= config.TRAIN["max_arc_deg"]
                and len(self.members) >= config.TRAIN["min_members"]
                and self.mean_altitude_km <= config.TRAIN["max_altitude_km"])

    @property
    def string_duration_minutes(self) -> float:
        """How long the whole string takes to cross a fixed point in the sky."""
        return self.period_minutes * self.arc_deg / 360.0

    @property
    def leader(self) -> Sat:
        return self.members[0]

    def describe(self) -> str:
        age = f"{self.age_days:.0f} d old" if self.age_days is not None else "age unknown"
        return (f"{self.launch}: {len(self.members)} sats, {age}, "
                f"{self.mean_altitude_km:.0f} km, arc {self.arc_deg:.0f} deg, "
                f"string takes {self.string_duration_minutes:.1f} min")


def find_batches(catalog: dict[int, Sat], satcat: dict[str, dict],
                 at: datetime | None = None) -> list[Batch]:
    """Group Starlink into per-launch, per-plane batches and measure each one."""
    at = at or datetime.now(timezone.utc)
    t = _TS.from_datetime(at)

    starlink = [s for s in catalog.values() if s.is_starlink]
    if not starlink:
        return []

    # Drop obviously old launches before doing any propagation.
    fresh: list[Sat] = []
    for s in starlink:
        info = satcat.get(s.launch_key)
        if info is None:
            continue
        age = (at - info["launch_date"]).total_seconds() / 86400.0
        if age <= config.TRAIN["max_age_days"]:
            fresh.append(s)
    if not fresh:
        return []

    positions, keep = [], []
    for s in fresh:
        try:
            positions.append(s.sat.at(t).position.km)
            keep.append(s)
        except Exception:
            continue
    if not keep:
        return []
    pos = np.array(positions)
    altitude = np.linalg.norm(pos, axis=1) - EARTH_RADIUS_KM

    # A single launch can seed more than one plane; split on RAAN before measuring.
    groups: dict[tuple, list[int]] = {}
    for i, s in enumerate(keep):
        raan = math.degrees(s.sat.model.nodeo)
        groups.setdefault((s.launch_key, round(raan / 2.0)), []).append(i)

    batches: list[Batch] = []
    for (launch, _plane), idxs in groups.items():
        if len(idxs) < 3:
            continue
        arc, gap, run, order = _measure_formation(pos[idxs], config.TRAIN["tight_gap_deg"])
        members = [keep[idxs[k]] for k in order]
        mean_motion = np.mean([keep[i].sat.model.no_kozai for i in idxs])  # rad/min
        period = 2 * math.pi / mean_motion if mean_motion else 95.0
        info = satcat.get(launch)
        launch_date = info["launch_date"] if info else None
        age = (at - launch_date).total_seconds() / 86400.0 if launch_date else None
        batches.append(Batch(
            launch=launch, members=members, arc_deg=arc, largest_gap_deg=gap,
            tight_run=run, mean_altitude_km=float(altitude[idxs].mean()),
            period_minutes=float(period), launch_date=launch_date, age_days=age,
        ))

    batches.sort(key=lambda b: (b.arc_deg, -len(b.members)))
    return batches


def _measure_formation(points: np.ndarray, tight_gap_deg: float):
    """Angular span of a set of co-planar satellites, measured in their own plane.

    Returns (arc_deg, largest_gap_deg, longest_tight_run, ordering).
    """
    # Build an in-plane basis from the angular momentum of the whole set, which
    # is far more stable than using any two individual members.
    normal = np.zeros(3)
    for i in range(len(points) - 1):
        normal += np.cross(points[i], points[i + 1])
    if np.linalg.norm(normal) < 1e-6:
        normal = np.cross(points[0], points[-1])
    normal = normal / (np.linalg.norm(normal) or 1.0)

    e1 = points[0] / np.linalg.norm(points[0])
    e2 = np.cross(normal, e1)
    theta = np.degrees(np.arctan2(points @ e2, points @ e1)) % 360.0

    order = np.argsort(theta)
    sorted_theta = theta[order]
    gaps = np.append(np.diff(sorted_theta), 360.0 - sorted_theta[-1] + sorted_theta[0])
    largest = float(gaps.max())
    arc = 360.0 - largest

    run = best = 1
    for g in gaps[:-1]:
        if g <= tight_gap_deg:
            run += 1
            best = max(best, run)
        else:
            run = 1

    # Re-order so the list starts just after the largest gap: that is the head of
    # the string, i.e. the satellite you will see first.
    start = (int(np.argmax(gaps)) + 1) % len(order)
    rolled = np.roll(order, -start)
    return arc, largest, int(best), list(rolled)


def active_trains(catalog: dict[int, Sat], satcat: dict[str, dict],
                  at: datetime | None = None) -> list[Batch]:
    return [b for b in find_batches(catalog, satcat, at) if b.is_train]
