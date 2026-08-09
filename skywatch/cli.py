"""Command line access to everything the bot does, for testing without Telegram.

    python -m skywatch.cli tonight  --lat 47.6407 --lon 18.9324
    python -m skywatch.cli next     --lat 47.6407 --lon 18.9324
    python -m skywatch.cli trains
    python -m skywatch.cli refresh  --force
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import config, render, store
from .engine import Engine
from .passes import Observer


def _settings(args) -> dict:
    s = dict(config.DEFAULTS)
    if args.mag is not None:
        s["max_magnitude"] = args.mag
    if args.elev is not None:
        s["min_elevation_deg"] = args.elev
    if args.kinds:
        s["kinds"] = args.kinds.split(",")
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skywatch.cli")
    ap.add_argument("command",
                    choices=["tonight", "next", "all", "trains", "refresh",
                             "subscribers", "demo-train", "coverage"])
    ap.add_argument("--lat", type=float, default=47.6407)
    ap.add_argument("--lon", type=float, default=18.9324)
    ap.add_argument("--tz", default="Europe/Budapest")
    ap.add_argument("--mag", type=float)
    ap.add_argument("--elev", type=float)
    ap.add_argument("--kinds")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Windows consoles default to the system codepage (cp1250 here), which cannot
    # encode the emoji or the degree signs in a rendered message.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    store.init()
    engine = Engine()

    if args.command == "refresh":
        engine.ensure_catalog(force=args.force)
        print(f"catalog: {len(engine.catalog)} objects, "
              f"{len(engine.satcat)} launches in SATCAT")
        return 0

    if args.command == "coverage":
        # How much of the sky we can actually see. Exits non-zero when a group
        # is missing, so this doubles as a health check.
        from . import events
        engine.ensure_catalog(force=args.force)
        missing = [g for g in config.CELESTRAK_GROUPS
                   if not (config.TLE_DIR / f"{g}.csv").exists()]
        print(f"catalog        : {len(engine.catalog)} objects")
        print(f"groups cached  : "
              f"{sorted(p.stem for p in config.TLE_DIR.glob('*.csv'))}")
        print(f"missing groups : {missing or 'none'}")
        print(f"standard mags  : {len(engine.mag_model.std_mags) if engine.mag_model else 0}")
        for cut in (0.5, 2.0, 3.5):
            n = len(events.bright_candidates(engine.catalog, engine.mag_model, cut))
            print(f"  candidates at mag {cut:<4}: {n}")
        return 1 if missing else 0

    if args.command == "subscribers":
        subs = store.active_subscribers()
        if not subs:
            print("no active subscribers")
        for s in subs:
            print(f"#{s['id']} {s['transport']}:{s['target']} "
                  f"{s['lat']:.3f},{s['lon']:.3f} {s['tz']} "
                  f"mag<={s['settings']['max_magnitude']} "
                  f"kinds={','.join(s['settings']['kinds'])}")
        return 0

    engine.ensure_catalog(force=args.force)

    if args.command == "trains":
        batches = engine.train_status()
        if not batches:
            print("No Starlink launch in the last three weeks is still grouped.")
            return 0
        for b in batches:
            print(("TRAIN  " if b.is_train else "       ") + b.describe())
        return 0

    observer = Observer(args.lat, args.lon)
    settings = _settings(args)
    now = datetime.now(timezone.utc)

    if args.command == "demo-train":
        # There is usually no tight formation in orbit -- a batch only qualifies
        # for a few days after each launch. Relax the arc threshold so the
        # freshest batch qualifies, purely to exercise and eyeball the train
        # message. Everything else (geometry, timing, brightness) stays real.
        batches = engine.train_status()
        if not batches:
            print("No recent Starlink batch is being tracked at all.")
            return 1
        freshest = min(batches, key=lambda b: b.age_days if b.age_days is not None else 999)
        original = config.TRAIN["max_arc_deg"]
        config.TRAIN["max_arc_deg"] = max(original, freshest.arc_deg + 1)
        try:
            settings["kinds"] = ["starlink_train"]
            found = engine.events_for(observer, settings, use_cache=False)
            print(f"[simulation] treating {freshest.launch} "
                  f"(arc {freshest.arc_deg:.0f}°, age {freshest.age_days:.0f} d) as a train\n")
            if not found:
                print("Batch is tracked but does not cross this location while dark.")
                return 0
            for event in found[:args.limit]:
                print(render.to_plaintext(render.render_event(event, args.tz, now)))
                print("\n" + "-" * 60 + "\n")
        finally:
            config.TRAIN["max_arc_deg"] = original
        return 0

    if args.command == "tonight":
        found = engine.tonight(observer, settings)
        print(render.to_plaintext(render.render_digest(found, args.tz, now)))
        return 0

    upcoming = engine.upcoming(observer, settings)
    if not upcoming:
        print("Nothing upcoming that clears the thresholds.")
        return 0

    for event in upcoming[: 1 if args.command == "next" else args.limit]:
        print(render.to_plaintext(render.render_event(event, args.tz, now)))
        print("\n" + "-" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
