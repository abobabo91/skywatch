"""Shared state: the catalog, the magnitude model, and a per-location event cache.

Computing passes is the expensive part, so it is done once per distinct location
rather than once per subscriber. Locations are bucketed to two decimal places
(~1 km), which is far finer than the difference it makes to where you point.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, events, magnitudes, tle
from .passes import Observer

log = logging.getLogger(__name__)

CATALOG_REFRESH_SECONDS = 3 * 3600
EVENT_HORIZON_HOURS = 30
EVENT_RECOMPUTE_SECONDS = 30 * 60


class Engine:
    def __init__(self):
        self._lock = threading.RLock()
        self.catalog: dict[int, tle.Sat] = {}
        self.satcat: dict = {}
        self.mag_model: magnitudes.MagnitudeModel | None = None
        self.catalog_loaded_at: float = 0.0
        self._event_cache: dict[str, tuple[float, list[events.Event]]] = {}

    # --- catalog ------------------------------------------------------------

    def ensure_catalog(self, force: bool = False) -> None:
        with self._lock:
            fresh = time.time() - self.catalog_loaded_at < CATALOG_REFRESH_SECONDS
            if self.catalog and fresh and not force:
                return
            log.info("refreshing catalog (forced=%s)", force)
            catalog = tle.load_catalog(force=force)
            if not catalog:
                log.error("catalog refresh produced nothing; keeping previous data")
                return
            satcat = tle.load_satcat()
            std = tle.load_standard_magnitudes()
            rcs = tle.load_rcs_by_norad()
            self.catalog = catalog
            self.satcat = satcat
            self.mag_model = magnitudes.MagnitudeModel(std, rcs)
            self.catalog_loaded_at = time.time()
            self._event_cache.clear()
            log.info("catalog: %d objects, %d launches, %d standard magnitudes",
                     len(catalog), len(satcat), len(std))

    # --- events -------------------------------------------------------------

    def _cache_key(self, observer: Observer, settings: dict) -> str:
        return "|".join([
            observer.key,
            f"{settings.get('min_elevation_deg')}",
            f"{settings.get('max_magnitude')}",
            f"{settings.get('sun_below_deg')}",
            ",".join(sorted(settings.get("kinds") or [])),
        ])

    def events_for(self, observer: Observer, settings: dict,
                   hours: int = EVENT_HORIZON_HOURS,
                   use_cache: bool = True) -> list[events.Event]:
        self.ensure_catalog()
        if not self.catalog or self.mag_model is None:
            return []
        key = self._cache_key(observer, settings)
        now = time.time()
        with self._lock:
            hit = self._event_cache.get(key)
            if use_cache and hit and now - hit[0] < EVENT_RECOMPUTE_SECONDS:
                return hit[1]

        t0 = datetime.now(timezone.utc)
        t1 = t0 + timedelta(hours=hours)
        started = time.time()
        found = events.find_events(self.catalog, self.satcat, observer, settings,
                                   t0, t1, self.mag_model)
        log.info("computed %d events for %s in %.1fs", len(found), observer.key,
                 time.time() - started)
        with self._lock:
            self._event_cache[key] = (now, found)
        return found

    def upcoming(self, observer: Observer, settings: dict,
                 within_hours: int = EVENT_HORIZON_HOURS) -> list[events.Event]:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=within_hours)
        return [e for e in self.events_for(observer, settings)
                if now <= e.t_start <= horizon]

    def tonight(self, observer: Observer, settings: dict) -> list[events.Event]:
        """Events between now and the end of the coming night."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=18)
        found = [e for e in self.events_for(observer, settings) if now <= e.t_start <= cutoff]
        return events.best_of_night(found, limit=8)

    def train_status(self) -> list:
        """Every fresh Starlink batch and how bunched it still is."""
        from . import trains
        self.ensure_catalog()
        return trains.find_batches(self.catalog, self.satcat)
