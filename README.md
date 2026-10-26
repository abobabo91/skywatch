# Skywatch

**A message a few minutes before something worth seeing flies over your house.**

On most clear nights there are satellites crossing overhead bright enough to see
with your bare eyes — no telescope, no app held up to the sky. They have never
been hard to see. The hard part is knowing one is coming.

Skywatch is a Telegram bot that fixes exactly that. You tell it your town, once.
From then on it messages you shortly before something good passes over, and the
message says **which way to face, how high to look, when it starts, and how long
it stays up**. You walk outside, look where it says, and it is there.

It only speaks up for things worth the trip outside:

- 🚆 **Starlink trains** — a batch launched days ago, still flying in one tight
  line, satellite after satellite after satellite. It looks like a string of
  pearls being drawn across the sky and it is genuinely startling the first time.
  Each batch only holds formation for a few days, so if nobody tells you, you
  miss it.
- 🚀 **The space stations** — the ISS and Tiangong, brighter than any star up
  there, with people inside them.
- 🛰️ **Anything else bright** — mostly spent rocket stages from decades of
  launches, tumbling end over end and flaring as they catch the sun.

**It stays quiet on purpose.** On a normal night roughly 28 satellites are
technically visible, which is noise rather than news. So by default it sends at
most three a night, picks the best ones rather than the first ones, stays silent
in the small hours, and won't spend tonight's message on a mediocre 21:00 pass
when a spectacular one is coming at 23:30. Trains and space stations always come
through regardless.

You can also just ask it: what's on tonight, what's next, are any trains up.

**It is not a service — it is something you run yourself.** No account, no
website, no company, nothing to sign up for. You make your own bot inside Telegram
in about two minutes and run this on any computer that's switched on in the
evening. It's then entirely yours: your bot, your location, nobody else in the
loop. Out of the box it answers only you and ignores everyone else.

Built because heavens-above.com has the data but you have to remember to go and
look at it, and it will never tell you the next Starlink train is coming.

---

## For developers

A satellite-pass alerter: a Telegram bot plus optional ntfy mirroring, backed by
a local pass-prediction engine over Celestrak orbital elements.

It runs in one of two modes, set by `ALLOWED_CHAT_IDS`: **private** to a list of
Telegram chat ids (the default, and what you want on a personal machine), or
**open**, where anyone can subscribe with their own location and thresholds. See
[Private vs open](#private-vs-open).

## What it alerts on

| Kind | What it is | How often |
|---|---|---|
| `starlink_train` | A freshly launched Starlink batch still flying in tight formation — the string-of-pearls | Rare. Only for a few days after each launch |
| `station` | ISS and Tiangong | A few passes most weeks |
| `bright_pass` | Anything else over the brightness threshold — mostly spent rocket stages, which tumble and flare | Capped at 3/night, best-first |

The nightly cap matters. At a magnitude cutoff of 3.5 there are about **28**
qualifying passes on a normal night at 47°N — that is noise. The default cutoff is
2.0 and the default cap is 3 ordinary passes per night, chosen best-first so a
mediocre 21:00 pass never uses the budget a spectacular 23:30 one deserved. Trains
and space stations are exempt from both the cap and quiet hours.

## Quick start

```bash
# what's up tonight from a given spot
python -m skywatch.cli tonight --lat 47.6407 --lon 18.9324

# the next thing worth going outside for, fully rendered
python -m skywatch.cli next --lat 47.6407 --lon 18.9324

# every fresh Starlink batch and whether it is still a train
python -m skywatch.cli trains

# exercise the train message when no live train exists
python -m skywatch.cli demo-train --lat 47.6407 --lon 18.9324

# force a catalog refresh
python -m skywatch.cli refresh --force
```

## Setting up the Telegram bot

Every install is standalone: **your own bot, your own token, your own machine.**
Nothing here talks to anyone else's instance, and there is no shared server —
the bot long-polls Telegram outbound, so it needs no public address, no tunnel
and no open port.

1. `cp skywatch.toml.example skywatch.toml` and fill it in. (`~/.skywatch.toml`
   works too, or point `SKYWATCH_VAULT` at any TOML file with those sections.)
2. Message [@BotFather](https://t.me/BotFather) → `/newbot`, pick a name and a
   username, paste the token into `BOT_TOKEN`.
3. Put your own Telegram id in `ALLOWED_CHAT_IDS` — see below. Get it from
   [@userinfobot](https://t.me/userinfobot).
4. Set `CONTACT` to your own URL or email.
5. Restart the daemon. It logs `telegram bot @yourbot online (...)`, with the
   access mode in the brackets.
6. Message your bot `/start` and share your location.

Without a token the daemon still runs and serves ntfy subscribers; it logs a
warning and skips Telegram.

### Private vs open

A Telegram bot username is world-visible and searchable, and this daemon runs on
your own machine. Every message a stranger sends executes code there: a `/tonight`
costs a full pass computation (**31.9 s** for 20 events, measured 2026-08-09), any
plain text costs a Nominatim lookup from your IP, and `/start` writes a subscriber
row. There is no rate limiting, and the bot thread handles updates one at a time,
so one slow command stalls the rest.

So keep the bot locked to an allowlist:

```toml
[skywatch.telegram]
ALLOWED_CHAT_IDS = '123456789'   # comma-separated; needs a daemon restart
```

Anything from another chat is dropped in `Bot.handle()` before parsing, geocoding
or any pass computation, and logged as `ignored update from chat <id>` — which is
also the only record of incoming traffic, so `grep -c` on the log tells you who is
knocking. The drop is silent by design: replying would confirm the bot is live and
spend an API call per spam message.

Leave the key empty to run it **open**, the subscribable-product mode, where
anyone can subscribe with their own location and thresholds. Do that only if you
accept strangers running unmetered pass computations on your machine.

The allowlist gates the inbound bot, not the outbound scheduler: existing
subscriber rows keep receiving alerts. Remove one with `/forget` from their chat,
or delete the row from `data/skywatch.sqlite3`.

### Nothing personal is committed

`data/` (the sqlite DB with every subscriber's coordinates, the logs) and
`skywatch.toml` are gitignored. There is no bundled fallback token, ntfy topic or
contact string — an unconfigured install is inert rather than borrowing someone
else's identity or pushing into their notification channel.

### Subscribing

`/start` asks where you are. Five ways to answer, because asking a normal person
for coordinates is not reasonable:

| Input | Notes |
|---|---|
| **Type a town name** — `Tatabanya` | The path most people take. Accents optional. Ambiguous names (`Springfield` → 5 hits) become a pick-one button list labelled with the region |
| Tap **Share my location** | One tap, GPS accurate. Mobile Telegram only — the desktop client cannot share location |
| Send a **location pin** | Drop a pin anywhere, useful for "where I'll be camping" |
| Paste a **Google Maps / OSM link** | Shortened `maps.app.goo.gl` links are followed to their target |
| Type **coordinates** — `47.64 18.93` | The escape hatch |

Whatever you send is reverse-geocoded and named back to you (`📍 Csobánka,
Hungary`) so a wrong pin or the wrong Springfield is obvious immediately, rather
than after a week of alerts pointing at the wrong bit of sky.

Geocoding is OpenStreetMap Nominatim: free, no API key, rate-limited to 1 req/s
and cached in-process, per their usage policy.

Then a short guided setup — one tap per question, inline buttons, no numbers to
understand:

1. **How do you want to hear from me?** — a list once a day / just before each
   one / both
2. **How picky should I be?** — only the showstoppers (mag 0.5) / the good ones
   (2.0) / anything I can see (3.5)
3. **At most how many alerts a night?** — 1 / 3 / 5 / no limit
4. **How much warning?** — 5 / 15 / 30 min
5. **When should the daily list arrive?** — 08:00 / 18:00 / 21:00

Questions that cannot apply are skipped: pick "a list once a day" and you get
three questions, not five, because there is no per-event ping to schedule.
`/setup` re-runs the whole thing.

### Bot commands

| Command | Does |
|---|---|
| `/start` | Subscribe, set location, run setup |
| `/setup` | Redo the guided setup |
| `/tonight` | Digest of tonight's sky |
| `/next` | Next event, fully rendered |
| `/trains` | Status of every fresh Starlink batch |
| `/settings` | Show current configuration |
| `/mode` | Daily list / per-event reminders / both |
| `/quality` | Pickiness, as three presets |
| `/digest 18` | Hour for the daily list (`/digest off`) |
| `/lead 5` | Minutes of warning |
| `/cap 3` | Max ordinary passes pushed per night |
| `/mag 2.0` | Exact brightness cutoff, if the presets don't fit |
| `/elev 15` | Ignore passes below this elevation |
| `/quiet 1 6` | Silence window, local hours |
| `/kinds` | Toggle event types (inline buttons) |
| `/ntfy topic` | Mirror alerts to an ntfy.sh topic |
| `/where` | Change location |
| `/pause`, `/resume`, `/forget` | Self-explanatory |

## Running it

The daemon is long-running. Task Scheduler runs `tasks/run.bat` every 5 minutes
via `run-hidden.vbs`; `skywatch.supervisor` exits immediately if the daemon is
already alive and starts it if not, which covers reboots, crashes and waking from
sleep.

**One daemon, enforced twice.** The daemon takes a named mutex on startup
(`skywatch/singleton.py`, `Local\skywatch-daemon`; an flock on
`data/skywatch.lock` off Windows) and a second copy exits before loading the
catalog with `another skywatch daemon holds the lock`. The supervisor checks
that same mutex and then the pid file through in-process Win32 calls — no
`tasklist`, no timeout — and treats anything it cannot decide as *running*.
Both exist because of 2026-10-26: the old `tasklist` probe timed out on a
swapping laptop, was read as "not running", and 63 daemons accumulated, each
recomputing the full catalog (3,366 s per pass instead of 63 s) and fighting
over the Telegram long-poll (HTTP 409). Verify with a second instance:
`python -m skywatch.daemon --no-bot` must log the lock line and exit.

```powershell
$base     = "$PWD\tasks"        # run from the project root
$action   = New-ScheduledTaskAction -Execute "wscript.exe" `
  -Argument "`"$base\run-hidden.vbs`" `"$base\run.bat`""
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Skywatch" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Satellite pass alerter (Telegram + ntfy)"
```

**The `-Settings` line is required on a laptop, not cosmetic.** Omit it and the
task inherits the Task Scheduler default `DisallowStartIfOnBatteries = true`:
the 5-minute supervisor silently stops firing the moment the machine is
unplugged, and does not resume until AC is back. Nothing is logged — the task
shows `State: Ready`, `LastTaskResult: 0`, and a `NextRunTime` that keeps
rolling forward without ever running. The already-running daemon survives, so
the only thing lost is the restart-on-crash guarantee, which is exactly what
the task exists for. Verify with:

```powershell
(Get-ScheduledTask -TaskName Skywatch).Settings.DisallowStartIfOnBatteries  # must be False
```

If `Register-ScheduledTask` returns `Access is denied` unelevated, use
`schtasks /Create` instead — it succeeds where the cmdlet does not. It has the
same battery default, so patch the two flags afterwards:

```powershell
schtasks /Create /TN "Skywatch" /TR "wscript.exe `"$base\run-hidden.vbs`" `"$base\run.bat`"" `
  /SC MINUTE /MO 5 /RL LIMITED /F
$t = Get-ScheduledTask -TaskName "Skywatch"
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries     = $false
$t.Settings.StartWhenAvailable          = $true
Set-ScheduledTask -TaskName "Skywatch" -Settings $t.Settings
```

**Caveat:** alerts only fire while the machine is awake. Most passes are between
21:00 and 01:00 so this is usually fine, but an overnight sleep drops that night's
early-morning ones. The daemon deliberately does *not* catch up on wake — a push
telling you to look at something that crossed forty minutes ago is worse than
silence.

Run it in the foreground to watch it work:

```bash
python -m skywatch.daemon --verbose
python -m skywatch.daemon --once      # single scheduler tick, then exit
```

## Following the diary

The private bot's allowed chats follow the daily diary, so `/tonight` and the
alerts are for wherever you actually are. Once a day the diary site publishes
`/data/map.json` (one entry per diary day with that day's GPS route);
`skywatch/diary_location.py` takes the last point of the most recent day and
moves each follower there when it is more than 2 km from the stored spot —
timezone from `timezonefinder`, place name from Nominatim, same as a shared
Telegram pin. The engine caches passes per location, so the next tick simply
computes for the new place.

Cadence is "once a day" in the diary's sense: the file is polled hourly with
`If-None-Match` (a 304 when nothing changed), and a move is applied at most
once per diary day (`bot_state.diary_day`), so a new diary moves you the hour
it is published, and nothing happens in between. A latest day older than 3
days (diary not running) moves nobody, and open mode (no `ALLOWED_CHAT_IDS`)
never follows — strangers' alerts must not track the owner's diary. `/where`
still works immediately; the next new diary day overrides it only if its last
point is 2+ km away.

Credentials: `[diary.web]` in the same `vault.toml` as the bot token — `url`
(the diary site) and `password` (the site's HTTP Basic password, any username).
Without the section nothing is fetched and nobody moves. The password is not
recoverable from Vercel (`vercel env pull` yields `[SENSITIVE]`), so it is
entered by hand.

```bash
python -m skywatch.cli diary           # where the latest diary ends, who would move, how far
python -m skywatch.cli diary --apply   # move them now, ignoring the once-per-day memory
```

## How it works

```
Celestrak OMM/CSV ──┐
SATCAT (launch dates, RCS) ──┼─→ engine ─→ events ─→ render ─→ Telegram / ntfy
McCants quicksat.mag ──┘         ↑
                            per-location cache
```

- **`tle.py`** fetches and caches orbital elements.
- **`passes.py`** is the pass engine — Skyfield/SGP4, sunlit test, shadow entry.
- **`trains.py`** detects Starlink formations.
- **`magnitudes.py`** models how bright each object looks.
- **`events.py`** decides what is worth an alert and budgets the night.
- **`render.py`** writes the message.
- **`bot.py`** / **`daemon.py`** are the Telegram loop and the scheduler.

Passes are computed once per distinct location (bucketed to ~1 km), not once per
subscriber, and cached for 30 minutes.

## Accuracy

Validated against heavens-above.com on 2026-08-08 for 47.6407N 18.9324E: peak
time agreed to within **4 seconds** and peak elevation to a median of **0.30°**
(max 0.49°) across every row that could be matched by name.

Magnitude is a model, not a measurement — expect ±0.5. Sanity anchors: ISS
computes to −4.0 overhead (observed max ≈ −3.9), a fresh Starlink at 350 km to
1.7 (trains are reported at 1–3), an operational one at 550 km to 4.6 (reported
"barely visible"). Atmospheric extinction is included, which most predictors skip
and which costs a full magnitude near the horizon.

## Gotchas worth knowing

Written up in full in `docs/findings.md`. The two that will bite anyone rebuilding
this:

- **Never use Celestrak's `FORMAT=tle`.** NORAD catalog numbers passed 100000 in
  July 2026 and cannot be expressed in the 5-digit classic TLE format, so the TLE
  feeds silently omit those objects — including every brand-new Starlink launch,
  which is exactly what a train detector needs. `FORMAT=csv` (OMM) carries the
  full number. Measured: `last-30-days` returned 56 objects as TLE and 275 as CSV.
- **Celestrak rate-limits by IP**, refusing a repeat download of the same GROUP
  within 2 hours with HTTP 403 and an explanatory plain-text body. Cache, and
  treat the 403 as "use what you have".

## Dependencies

```
pip install skyfield sgp4 numpy timezonefinder
```

Python 3.11+. Skyfield downloads `de421.bsp` (~17 MB) on first run.
