# Skywatch findings

Things discovered while building this that are not obvious from any doc, recorded
so they do not have to be rediscovered. Measured 2026-08-08 unless stated.

## Celestrak's TLE feeds silently drop everything above NORAD 99999

This is the one that cost the most time and produced the most convincing wrong
answer.

Symptom: the Starlink train detector reported nothing fresher than 29 days old,
while SATCAT clearly listed launches on 2026-07-14, 07-21, 07-25 and 08-01. The
tempting conclusion was "Celestrak's supplemental file lags". It does not.

Cause: NORAD catalog numbers crossed **100000** in July 2026. The classic TLE
format has exactly five columns for the catalog number, so a six-digit object
cannot be represented. Celestrak's `FORMAT=tle` responses simply omit them.

Proof:

| feed | objects returned |
|---|---|
| `GROUP=last-30-days&FORMAT=tle` | 56 |
| `GROUP=last-30-days&FORMAT=csv` | **275** |

The CSV (OMM) form carries `NORAD_CAT_ID` as a plain integer, so nothing is lost.
Parse it with `sgp4.omm.parse_csv` + `omm.initialize(Satrec(), fields)` and wrap
with `skyfield.api.EarthSatellite.from_satrec`. Note `skyfield.omm` does **not**
exist — the OMM helpers live in the `sgp4` package, not Skyfield.

There is also an Alpha-5 encoding (100001 → `A0001`) that some producers use, but
Celestrak's TLE endpoints omit rather than encode. Do not rely on it.

**Rule: for anything launched recently, always `FORMAT=csv`.**

## Celestrak rate-limits by IP, and says so with a 403

Requesting the same `GROUP` twice inside two hours returns **HTTP 403** with a
plain-text body:

```
GP data has not updated since your last successful
download of GROUP=starlink at 2026-08-08 18:12:13 UTC.
Data is updated once every 2 hours.
```

It is not an auth failure and not an error worth retrying. Cache the file, honour
a >2 h refresh interval, and treat the 403 as "keep using what you have".

Separately, a 200 response under ~500 bytes containing `has not updated` is the
same message delivered without the error code. Check for both.

## Comparing mean anomalies across satellites is meaningless

The first train detector grouped a launch, read `satrec.mo` for each member and
measured the spread. That is wrong: every TLE carries its **own epoch**, so those
mean anomalies are quoted at different instants and are not comparable. It
produced plausible-looking garbage.

Correct method: propagate every member to one common instant, project the
positions into the batch's own orbital plane (build the basis from the summed
angular-momentum vector, not from any two members), take the angles, sort, and
measure `360° − largest gap`.

## How fast a Starlink batch disperses

Measured across live batches, propagated to a common instant:

| age | arc occupied |
|---|---|
| 8 days | 150° |
| 15 days | 172° |
| 19 days | 243° |
| 26 days | 327° |

So the visual "string of pearls" is the first few days after launch, and by two
weeks the batch is smeared across half its orbit. Detector threshold is 60° of
arc with at least 8 members below 480 km. On 2026-08-08 nothing in orbit
qualified — correctly, since the most recent launch was 8 days old.

## "Many Starlink at once" is not an event any more

With 10,763 Starlink in orbit, a sliding-window search found **212 of them above
25° within any six-minute window** on a normal night, with peak azimuths spread
over 115°. That is background, not a phenomenon. Only the fresh-launch formation
is worth alerting on.

## The magnitude model, and what anchors it

```
mag = std_mag − 15.75 + 2.5·log10(range_km² / illuminated_fraction) + extinction
```

`std_mag` is brightness at 1000 km, 50% lit, so the constants cancel at that
reference. Standard magnitudes for ~3650 objects come from Mike McCants'
`quicksat.mag`.

- `qsmag.zip` is **404** at `mmccants.org/programs/`, despite being linked from
  the index. Get `quicksat.zip` instead; `quicksat.mag` is inside it.
- The file has no Starlink entries at all, so Starlink is modelled by mission
  phase: 4.0 while raising orbit (below 480 km, not yet in operational attitude —
  this is the bright train phase), 5.9 once on station.

Anchors that make the model believable: ISS → −4.0 overhead (observed max ≈ −3.9);
fresh Starlink at 350 km → 1.7 (trains reported at 1–3); operational Starlink at
550 km → 4.6 (reported "barely visible").

**Atmospheric extinction is not optional.** At 15° elevation it costs about a full
magnitude. Most predictors skip it, which is why their low passes look
over-promised.

## Heavens-Above's all-satellites page uses a very loose darkness limit

Its first listed pass began when the sun was at **−1.56°**, i.e. barely below the
horizon, not civil twilight. Skywatch defaults to −6° instead: at −1.5° a mag-3
satellite is not realistically findable.

## Validation against heavens-above

For 47.6407N 18.9324E, matching rows by name: peak time within **4 seconds**,
peak elevation median delta **0.30°**, max **0.49°**. Only 36 of 127 rows could be
matched because Heavens-Above covers the full catalogue while Skywatch tracks the
`visual` + `stations` + `last-30-days` + `starlink` groups; the unmatched rows are
faint objects outside those groups, not disagreements.

## Worldwide validation, and the coverage gap it exposed

Checked against heavens-above at three more sites, using `tz=UCT` on both sides so
no DST mapping can confuse the comparison:

| site | matched rows | peak elevation delta | peak compass point |
|---|---|---|---|
| Sydney (−33.9) | 23 | median 0.20°, max 0.55° | 22/23 exact |
| Singapore (+1.4) | 16 | median 0.29°, max 0.55° | 16/16 exact |
| Ushuaia (−54.8) | 25 | median 0.22°, max 0.50° | 25/25 exact |

Geometry is as good in the southern hemisphere and on the equator as it is at
home. Sixteen locations from Longyearbyen to McMurdo produced no sanity-check
failures, and the event counts are physically sensible: 0 at Tromsø and
Longyearbyen (August has no astronomical night above the Arctic circle), 412 at
McMurdo (polar night, 21.7 dark hours in a 30 h window), and low counts on the
equator (16–26) despite long darkness, because high-inclination orbits spend
proportionally less time over the tropics.

What the comparison *did* expose: only 23 of Sydney's 65 listed passes were
objects we tracked at all, because the candidate list was the `visual` group
(~160 objects) rather than everything with a known brightness (~3650 in McCants).
Fixed by selecting on standard magnitude instead of group membership, with a
2.5 magnitude margin because an overhead pass runs ~2 magnitudes brighter than
the 1000 km reference.

That closes part of it. The rest does not close: of the 12 brightest missed
objects, 3 gain a McCants magnitude, and the others are classified US payloads
(USA 60, USA 120, USA 267, OBJECT A) that publish **neither a magnitude nor a
radar cross section**. Heavens-Above knows them from observer-contributed
photometry that is not in any public dataset.

Rejected fix: selecting on RCS instead. SATCAT has an RCS for 32,931 objects, but
it is a bad brightness proxy — ARIANE 1 R/B has RCS 1.05 m² which the Lambertian
model turns into standard magnitude 6.4, against 3.8 observed. It would also take
the candidate set from 1,994 to 5,127 at the default cutoff. Wrong-and-pessimistic
merely wastes CPU; wrong-and-optimistic sends someone outside to look at something
invisible, which is the failure mode this bot cannot have.

**Net honest position:** at the default cutoff (mag 2.0) the measured gap at
Sydney is zero. At the loosest preset it is about 7 passes in 65, all faint.

### Correction, same day: two of the above claims were wrong

**1. Measuring coverage by object name overstates the gap.** Heavens-Above writes
`OKEAN O`, `ALOS 2`, `BLUEWALKER 3`; Celestrak writes `OKEAN-O`, `ALOS (DAICHI)`,
`BLUEWALKER-3`. Six of the fourteen "missing" objects in the first pass were
actually tracked all along. Heavens-Above links each row to
`Satellite.aspx?satid=NNNNN`, so match on that NORAD id and the comparison is
exact. Never compare satellite catalogues by name.

**2. `GROUP=active` was close to useless for this.** It is 16,301 payloads and
exactly **2** rocket bodies. But 1,079 of the 1,840 bright objects we were
missing are rocket bodies — the spent SL-16, CZ-4B and Ariane stages that make up
most of naked-eye satellite spotting. Adding it moved candidates 172 → 207.

There is no Celestrak GROUP for rocket bodies, but the `NAME=` query works as a
substring search, which is the way in:

| feed | objects | bright ones it adds | verdict |
|---|---|---|---|
| `NAME=R/B` | 2,188 | ~720 | essential |
| `NAME=COSMOS` | 2,879 | 328 | good ratio, defunct payloads `active` omits |
| `NAME=DEB` | 10,157 | ~120 | poor ratio, taken anyway — the Ariane SYLDA/SPELDA adapters are in it and one was listed at **magnitude 1.1**, brighter than any star |

Coverage of what Heavens-Above lists, matched by NORAD id, after all feeds:

| site | mag ≤ 2.0 | mag ≤ 3.5 | mag ≤ 5.0 |
|---|---|---|---|
| Budakalász | 3/4 | 41/47 (87%) | 160/189 (85%) |
| Sydney | 1/1 | 18/22 (82%) | 54/65 (83%) |
| Singapore | 3/4 | 28/32 (88%) | 85/98 (87%) |

Cost: 29,750 objects, 108 MB resident, 6 s to load, ~27 s to compute one
location's events (cached 30 min, shared by everyone within ~1 km).

The residual is dominated by classified payloads — USA 186, USA 305, USA 215,
HELIOS 1A, the NOSS naval surveillance triplets — which publish neither a
magnitude nor a radar cross section. Heavens-Above has them from observer
photometry that exists in no public dataset. Complete coverage would need a
space-track.org account and the full catalogue; ~85% is where free public data
runs out.

## Alert volume is the real design problem

A magnitude cutoff of 3.5 yields **28 qualifying passes in one night** at 47°N.
Delivered as-is (verified by actually pushing them to ntfy) it is unusable spam.
Two fixes, both necessary:

1. Default cutoff 2.0, not 3.5.
2. A per-night cap chosen **best-first**, not first-come-first-served.

And filter quiet hours *before* applying the cap — otherwise three 04:00 passes
consume the night's budget and are then silenced, so nothing is sent at all.

## The supervisor multiplied the daemon 63 times

Measured 2026-10-26 on the watcher laptop (7.4 GB RAM, swapping). The old
`supervisor._pid_running` shelled out to `tasklist /FI "PID eq N"` with a 30 s
timeout and returned False on timeout. Under load the timeout fired on every
5-minute run, so each run started another daemon and never stopped the previous
ones. 63 `python -m skywatch.daemon` processes were running by 22:00:

| State | Daemons | One event computation for Budakalász |
|---|---|---|
| 22:42 | 63 | 3,366 s |
| 22:45 | ~1 (others just killed) | 133 s |
| 22:51 | 1 | 63 s |

Every copy also long-polled Telegram with the same token, so the log filled with
`getUpdates failed: HTTP 409 Conflict: terminated by other getUpdates request`.
The machine itself sat at 170 MB free and 96 % CPU; freeing the duplicates gave
back ~1.5 GB.

Two consequences in the code: the daemon owns a named mutex and a second copy
exits before loading anything (`singleton.py`), and the supervisor's liveness
check is an in-process Win32 call that treats "cannot decide" as "running"
(`supervisor.py`). A missed restart costs five minutes; a false start used to
cost a duplicate forever.
