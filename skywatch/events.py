"""Turning raw passes into the handful of things worth interrupting someone for."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config, magnitudes, passes, trains
from .passes import Observer, Pass
from .tle import Sat

log = logging.getLogger(__name__)

# Objects people recognise by name and will go outside for regardless of how the
# magnitude model rates a given pass.
STATION_NORADS = {
    25544: "ISS",
    48274: "Tiangong",
}

KIND_LABELS = {
    "starlink_train": "Starlink train",
    "bright_pass": "Bright pass",
    "station": "Space station",
}


@dataclass
class Event:
    kind: str
    title: str
    t_start: datetime
    t_end: datetime
    t_peak: datetime
    passes: list[Pass]
    score: float
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity so an event is never sent twice."""
        lead = self.passes[0]
        return f"{self.kind}:{lead.norad}:{int(self.t_peak.timestamp()) // 300}"

    @property
    def lead(self) -> Pass:
        return self.passes[0]


def find_events(catalog: dict[int, Sat], satcat: dict,
                observer: Observer, settings: dict,
                t0: datetime, t1: datetime,
                mag_model: magnitudes.MagnitudeModel) -> list[Event]:
    """Every alertable event for one location and one set of preferences."""
    kinds = set(settings.get("kinds") or config.DEFAULTS["kinds"])
    min_elev = float(settings.get("min_elevation_deg", config.DEFAULTS["min_elevation_deg"]))
    max_mag = float(settings.get("max_magnitude", config.DEFAULTS["max_magnitude"]))
    sun_below = float(settings.get("sun_below_deg", config.DEFAULTS["sun_below_deg"]))

    events: list[Event] = []

    if "starlink_train" in kinds:
        events += _train_events(catalog, satcat, observer, t0, t1,
                                min_elev, sun_below, mag_model)

    if "bright_pass" in kinds or "station" in kinds:
        bright = bright_candidates(catalog, mag_model, max_mag)
        found = passes.compute_passes(bright, observer, t0, t1,
                                      min_elevation_deg=min_elev,
                                      sun_below_deg=sun_below,
                                      mag_model=mag_model,
                                      max_magnitude=None)
        for p in found:
            is_station = p.norad in STATION_NORADS
            if is_station and "station" in kinds:
                events.append(_pass_event("station", STATION_NORADS[p.norad], p))
            elif "bright_pass" in kinds and p.magnitude is not None and p.magnitude <= max_mag:
                # Catalog names are conventionally upper case ("SL-16 R/B"); do
                # not "prettify" them, it makes them unsearchable.
                events.append(_pass_event("bright_pass", p.name, p))

    events.sort(key=lambda e: e.t_start)
    return _deduplicate(events)


# A pass is at its brightest overhead, where the range is short and the phase
# angle favourable -- worth about 2 magnitudes over the standard (1000 km, 50%
# lit) figure. So to catch everything that could *appear* at the user's cutoff,
# consider objects whose standard magnitude is this much fainter than it.
BRIGHTNESS_MARGIN = 2.5


def bright_candidates(catalog: dict[int, Sat], mag_model, max_mag: float) -> list[Sat]:
    """Objects worth propagating for this subscriber's brightness cutoff.

    Selecting by measured brightness rather than by Celestrak group: the `visual`
    group is only ~160 objects, and checking against heavens-above at Sydney
    showed that cost us 23 of 65 listed passes, 7 of them brighter than mag 3.5.
    """
    limit = max_mag + BRIGHTNESS_MARGIN
    out = []
    for s in catalog.values():
        if s.is_starlink:
            continue                      # handled by the train detector instead
        if s.norad in STATION_NORADS:
            out.append(s)
            continue
        std = mag_model.std_mags.get(s.norad)
        if std is not None:
            if std <= limit:
                out.append(s)
        elif "visual" in s.groups or "stations" in s.groups:
            # No measured magnitude, but Celestrak lists it among the brightest.
            out.append(s)
    return out

# Deliberately NOT falling back to radar cross section for selection, even though
# SATCAT has an RCS for 32,931 objects against McCants' 3,651 magnitudes.
# Measured 2026-08-08: an RCS rule would take the candidate set from 1,994 to
# 5,127 objects at the default cutoff -- 2.5x the propagation cost per location --
# and RCS is a poor brightness proxy. ARIANE 1 R/B has an RCS of 1.05 m2, which
# this model turns into standard magnitude 6.4; heavens-above observes it at 3.8.
# Being wrong by 2.6 magnitudes in the pessimistic direction merely wastes CPU,
# but the same error in the optimistic direction sends someone outside to look at
# something they cannot see, which is the one failure this bot must not have.
#
# The residual gap is real and unfixable from public data: classified US military
# payloads (USA 60, USA 120, USA 267) publish neither a magnitude nor an RCS.
# Heavens-Above knows them from observer-contributed photometry we do not have.
# Measured at Sydney: 0 missed above the default mag 2.0 cutoff, ~7 of 65 at the
# loosest preset.


def _pass_event(kind: str, title: str, p: Pass) -> Event:
    return Event(kind=kind, title=title, t_start=p.t_start, t_end=p.t_end,
                 t_peak=p.t_peak, passes=[p], score=_score_pass(kind, p))


def _score_pass(kind: str, p: Pass) -> float:
    """Higher = more worth going outside for."""
    mag = p.magnitude if p.magnitude is not None else 6.0
    score = (5.0 - mag) * 10          # brightness dominates
    score += p.alt_peak / 3.0          # high passes beat horizon-skimmers
    score += min(p.duration_s, 480) / 60.0
    if kind == "station":
        score += 25
    return score


def _train_events(catalog, satcat, observer: Observer,
                  t0: datetime, t1: datetime,
                  min_elev: float, sun_below: float,
                  mag_model) -> list[Event]:
    """A train event is the whole batch crossing, not one satellite."""
    out: list[Event] = []
    for batch in trains.active_trains(catalog, satcat, t0):
        member_passes = passes.compute_passes(
            batch.members, observer, t0, t1,
            min_elevation_deg=min_elev, sun_below_deg=sun_below,
            mag_model=mag_model, max_magnitude=None)
        if not member_passes:
            continue
        # Cluster the batch's passes: members of one train cross within roughly
        # the time the string takes to go by.
        window = timedelta(minutes=max(batch.string_duration_minutes * 1.5, 10))
        member_passes.sort(key=lambda p: p.t_peak)
        cluster: list[Pass] = [member_passes[0]]
        for p in member_passes[1:] + [None]:
            if p is not None and p.t_peak - cluster[-1].t_peak <= window:
                cluster.append(p)
                continue
            if len(cluster) >= max(3, config.TRAIN["min_members"] // 2):
                best = min(cluster, key=lambda x: x.magnitude if x.magnitude is not None else 9)
                out.append(Event(
                    kind="starlink_train",
                    title=f"Starlink train ({batch.launch})",
                    t_start=min(x.t_start for x in cluster),
                    t_end=max(x.t_end for x in cluster),
                    t_peak=best.t_peak,
                    passes=sorted(cluster, key=lambda x: x.t_peak),
                    score=200 + len(cluster) * 2 + max(x.alt_peak for x in cluster) / 3,
                    detail={"launch": batch.launch, "members": len(batch.members),
                            "visible_members": len(cluster),
                            "age_days": batch.age_days,
                            "altitude_km": batch.mean_altitude_km,
                            "arc_deg": batch.arc_deg,
                            "string_minutes": batch.string_duration_minutes},
                ))
            if p is not None:
                cluster = [p]
    return out


def _deduplicate(events: list[Event]) -> list[Event]:
    """Drop a bright-pass event when a train already covers the same object."""
    train_windows = [(e.t_start, e.t_end, {p.norad for p in e.passes})
                     for e in events if e.kind == "starlink_train"]
    out = []
    for e in events:
        if e.kind != "starlink_train":
            covered = any(e.lead.norad in norads and s <= e.t_peak <= t
                          for s, t, norads in train_windows)
            if covered:
                continue
        out.append(e)
    return out


def best_of_night(events: list[Event], limit: int = 6) -> list[Event]:
    return sorted(events, key=lambda e: -e.score)[:limit]


# Rare enough that they always get through, however many ordinary passes there are.
ALWAYS_PUSH = {"starlink_train", "station"}


def night_of(when: datetime, tz_name: str):
    """Which observing night an event belongs to.

    Shifted by twelve hours so a 00:40 pass counts as part of the evening that
    preceded it rather than as the start of a new night.
    """
    from zoneinfo import ZoneInfo
    try:
        local = when.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = when
    return (local - timedelta(hours=12)).date()


def select_for_push(candidates: list[Event], settings: dict, tz_name: str) -> set[str]:
    """Event keys allowed to become a push notification.

    Chosen best-first per night rather than first-come-first-served, so a
    mediocre 21:00 pass never uses up the budget that a spectacular 23:30 one
    deserved.
    """
    cap = int(settings.get("max_alerts_per_night",
                           config.DEFAULTS["max_alerts_per_night"]))
    nights: dict = {}
    for e in candidates:
        nights.setdefault(night_of(e.t_start, tz_name), []).append(e)

    allowed: set[str] = set()
    for group in nights.values():
        ordinary = []
        for e in group:
            if e.kind in ALWAYS_PUSH:
                allowed.add(e.key)
            else:
                ordinary.append(e)
        ordinary.sort(key=lambda e: -e.score)
        allowed.update(e.key for e in ordinary[:max(0, cap)])
    return allowed
