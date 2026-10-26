# skywatch

Satellite-pass alerter. Telegram bot + optional ntfy. Tells you which way to face,
how high, when, and the path across the sky. Headline feature is the
fresh-Starlink-train alert.

**The bot is private.** `ALLOWED_CHAT_IDS` in the vault gates `Bot.handle()`; every
other chat is dropped before any work. The code still supports the open,
subscribable mode (empty key) — keep both paths working, and never widen the gate
to make a test easier. It runs on a personal laptop with no rate limiting, and one
`/tonight` is ~30 s of CPU.

**Read `docs/findings.md` before touching the data layer.** It records the traps
that produce confident wrong answers, chiefly the NORAD-100000 TLE truncation.

## Non-negotiables

- **Always `FORMAT=csv` (OMM) from Celestrak, never `FORMAT=tle`.** Catalog
  numbers passed 100000 in July 2026 and the classic TLE format cannot hold six
  digits, so TLE feeds silently omit those objects — including every new Starlink
  launch. Parse with `sgp4.omm`, not `skyfield.omm` (which does not exist).
- **Celestrak 403s a repeat GROUP download inside 2 hours.** That is rate limiting,
  not an error. Cache and fall back; never retry in a loop.
- **Never compare mean anomalies across satellites.** Different TLE epochs make
  them incomparable. Propagate to a common instant first.
- **Alert volume is the product.** A mag-3.5 cutoff produces ~28 passes a night.
  Any change that raises volume needs a matching change to the nightly cap, or it
  turns the bot into spam and people unsubscribe.
- **Nominatim has a usage policy and it is enforced in `geocode.py`.** Identifying
  User-Agent, max one request per second, results cached. Do not remove the rate
  limit or fan geocoding out across threads — it is a free service run on
  donations and abuse gets the IP blocked.
- **ASCII only in `tasks/*.bat`.** cmd.exe parses .bat as the system codepage; one
  em-dash corrupts the parser.
- **`PYTHONIOENCODING=utf-8` on every subprocess and console entry point.** The
  console here is cp1250 and rendered messages contain emoji and degree signs.
- **Liveness is never decided by a subprocess with a timeout, and "unknown" means
  "running".** 63 daemons piled up on 2026-10-26 because a `tasklist` timeout
  read as "not running". The daemon holds a mutex (`singleton.py`); keep it.

## Layout

| Path | Role |
|---|---|
| `skywatch/tle.py` | Celestrak/SATCAT/McCants fetch + cache |
| `skywatch/passes.py` | Pass engine (Skyfield/SGP4, sunlit, shadow) |
| `skywatch/trains.py` | Starlink formation detection |
| `skywatch/magnitudes.py` | Brightness model + extinction |
| `skywatch/events.py` | What is worth alerting; nightly budget |
| `skywatch/render.py` | Message text + sky diagram |
| `skywatch/engine.py` | Shared catalog + per-location event cache |
| `skywatch/geocode.py` | Place-name search + reverse lookup (Nominatim) |
| `skywatch/bot.py` | Telegram long-poll bot |
| `skywatch/daemon.py` | Scheduler loop |
| `skywatch/supervisor.py` | Starts the daemon when none runs; in-process liveness check, never `tasklist` |
| `skywatch/singleton.py` | The daemon's own one-per-machine lock (named mutex / flock) |
| `skywatch/diary_location.py` | Last point of the latest diary day (the diary site's `map.json`) -> the private subscribers' location, once per diary day |
| `skywatch/cli.py` | Everything the bot does, without Telegram |

`data/` holds the sqlite DB, TLE cache and log. Gitignored.

## Testing without Telegram

`python -m skywatch.cli next|tonight|trains|demo-train`. `demo-train` relaxes the
formation threshold so the train message can be eyeballed when no live train
exists — which is most of the time.

Do not test by waiting for a real alert; register a temporary ntfy subscriber with
a wide `lead_minutes` and run `daemon --once`.

## Secrets

`[skywatch.telegram] BOT_TOKEN` and `[skywatch.ntfy] TOPIC` in
`tools/vault/vault.toml`. `vault.toml` has historically contained duplicate table
names and is not valid TOML, so `config.vault_get` greps for the leaf rather than
parsing.
