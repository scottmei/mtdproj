# CU Bus Predictor

MTD's live arrival times in Champaign-Urbana are often wrong. This app looks up a stop and lists
upcoming buses that haven't left yet, soonest first. For each bus it shows three estimates side by side:

| Column | Source |
|---|---|
| **Scheduled** | Static GTFS timetable |
| **MTD live** | MTD's GTFS-Realtime trip updates |
| **Our prediction** | Schedule + historical average delay for this route/stop/hour/day type, from a SQLite database we build ourselves |

A backtest then measures how far off each estimate is against what actually happened.

## Architecture

```
 gtfs-rt.mtd.org ──poll 20s──► collector ──► SQLite (data/bus.db, WAL)
   (protobuf)                  infers observed departures     ▲ reads
                               + snapshots MTD predictions    │
 gtfs/*.txt ──load_gtfs──► static tables ─────────────────────┤
                                                              │
 Browser ◄── FastAPI :8080 ── arrivals ── predictors/ ────────┘
              ▲                  └ live GTFS-RT (15 s cache)
              └ /api/stops/search ── MTD REST v3 (X-ApiKey) ─fallback─► local stops
```

The **collector** and the **web app** are separate processes that share one SQLite file (WAL mode lets
one writer and many readers work at the same time). Collection keeps running while the web app restarts.

| Module | Role |
|---|---|
| `busapp/gtfs_static.py` | Loads the static GTFS files into SQLite |
| `busapp/realtime.py` | Parses the GTFS-RT protobuf into `TripRT`/`StopRT` |
| `busapp/collector.py` | Turns the realtime feed into ground-truth departures |
| `busapp/schedule.py` | Scheduled visits to a stop across service days |
| `busapp/predictors/` | `Predictor` interface, registry, `AverageDelayPredictor` |
| `busapp/arrivals.py` | Merges schedule, realtime and prediction; drops buses that already left |
| `busapp/accuracy.py` | Backtest; `ScoreCache` scores complete service days once a day (at 1 AM), in the background on its own connection |
| `busapp/breakdown.py` | Lateness and accuracy by line, stop, hour, day type or route pattern |
| `busapp/coverage.py` | Collector gaps, weighted by how much service is actually observable |
| `busapp/web.py`, `busapp/static/` | FastAPI app and single-page UI |

## What the data looks like (and why it matters)

These are quirks of MTD's feeds that the design depends on. All were verified against live data.

- **GTFS-RT has absolute times only.** MTD fills `arrival.time`/`departure.time` but never `delay`, so
  delay is always *estimate − schedule*.
- **Stops disappear once the bus passes them.** A trip's `stop_time_updates` only lists stops still to
  come. That gives us two things:
  - *Has the bus left?* If the trip is in the feed but our stop isn't, yes.
  - *When did it actually leave?* The last time MTD predicted for the stop, just before it disappeared,
    capped at the time we noticed. This is our ground truth.
- **Stops sometimes reappear.** A trip's remaining-stop list can move *backwards*, sometimes after the whole
  trip has dropped out of the feed for a while (up to 53 minutes observed). The collector remembers recorded
  stops for an hour after a trip was last listed, and retracts any that come back.
- **MTD sometimes clears a trip in bulk.** Dozens of remaining stops, kilometres apart, get the same
  timestamp and drop out together. Taken at face value, that looks like buses leaving up to an hour early (or
  late). A bus can't be in two places at once, so when 11 or more stops of one trip "depart" in the same
  second, the collector rejects them. Before these two fixes (2026-09-29), such false departures were 2% of
  the data and made Ruby look 4 minutes *early* on average; it actually runs about 2 minutes late.
  `scripts\remove_false_departures.py` finds and removes them from existing data (dry run by default).
- **Trips that haven't started are in the feed too.** Their first stop is sequence 1.
- **Destination signs change mid-trip.** 7% of stop times carry their own `stop_headsign`, different
  from the trip's. At Illinois Terminal, a Red trip signed "U to Illinois Terminal - Urbana Meijer"
  actually shows "U to Urbana Meijer". The board uses the per-stop sign.
- **Boarding-point names are inconsistent.** MTD writes the same kind of spot 45 ways: `(NE)`,
  `(NE Corner)`, `(SE Far)`, `(SS)`, `(East side)`, `(N. Side)`. `schedule.boarding_label()` spells them
  out in one vocabulary ("NE Corner", "SE Far Side", "South Side", "Island Shelter"), and shows nothing
  when a stop has no location. These describe where on the street you board, not the direction of travel.
- **After-midnight trips are dated by the calendar day, not the service day.** A trip timetabled at
  `24:15:00` on service date D arrives in the feed with `start_date` D+1. Every one of the 390 such trip
  runs we checked did this, even when first listed before midnight. Taken at face value, that puts the
  schedule a day late, and each observed delay comes out near −86,400 s. Until 2026-09-29 the sanity
  check silently dropped all of them, which looked like "late-night service is never tracked".
  `realtime.to_service_dates` maps these trips back to their service date for both the collector and the
  board. `coverage.py` learns the capture rate for each hour of the day from what is actually observed.
- **`calendar.txt` is all zeros.** Which services run each day comes only from `calendar_dates.txt`.
- **Times go past 24:00** (up to `29:09:00`). A GTFS time is measured from "noon minus 12 h" on the
  service date, so the conversion is DST-safe (see `timeutil.py` and its DST test).
- **Stops are boarding points** (`IT:1`, `IT:2`, `IT:5`). The rider-facing stop is the part before the
  colon, and the board shows the platform on each row.
- **REST API v3** (`https://api.mtd.dev`, header `X-ApiKey`) has an hourly per-developer rate limit and
  returns 429 with `retryAfter` when you hit it. We only use it for fuzzy stop search, with a 24-hour
  cache, back-off on 429, and fallback to local search. The keyless GTFS-RT feed covers every trip in
  one call, so it drives everything else.

## Setup (Windows, PowerShell)

1. Install **Python 3.12+** from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
2. Create a virtual environment and install dependencies from this folder:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   pip install -r requirements.txt
   ```
3. **Optional:** add your MTD API key for REST stop search. Get one at https://developer.mtd.org.
   ```powershell
   copy .env.example .env           # then set MTD_API_KEY=...
   ```
4. **Optional:** install [DB Browser for SQLite](https://sqlitebrowser.org/dl/) to browse `data/bus.db`.

## Run

```powershell
python scripts\load_gtfs.py         # creates data\bus.db and loads gtfs\ (about 4 s)
python scripts\run_collector.py     # leave running in its own window; logs to data\collector.log
python scripts\run_web.py           # http://localhost:8080  (API docs at /docs)
python -m pytest                    # 95 tests
```

The model needs history, so start the collector as early as possible. While it runs, the collector asks
Windows not to idle-sleep (closing the lid still sleeps the machine). If more than 90 s pass between
successful polls, it starts from a fresh baseline instead of guessing departure times across the gap.
`/api/health` lists collection gaps and estimates what each one cost: scheduled stop times × the
capture rate for that hour, so a gap during hours we rarely observe costs little and a gap during daytime
service is flagged. When MTD publishes a new GTFS feed,
replace `gtfs\` and re-run `load_gtfs.py`. Historical tables are kept.

## Database

| Table | Rows / day | Contents |
|---|---|---|
| `stops`, `routes`, `trips`, `stop_times`, `calendar_dates` | static | Static GTFS (196k stop times) |
| `observed_departures` | tens of thousands | One row per bus per stop served: scheduled vs observed, `delay_s`, `hour_local`, `day_type`, `poll_gap_s` |
| `mtd_predictions` | ~6× observations | MTD's estimate as it stood about 2/5/10/15/20/30 min before arrival |
| `polls` | 4,320 | Collector health; errors are logged, not fatal |

The model's hierarchical average comes from one grouped query at the finest grain. Sums and counts
add up, so every coarser level is rolled up from its result in Python:

```sql
SELECT o.route_id, o.direction_id, o.stop_id, o.hour_local, r.long_name AS line,
       SUM(o.delay_s), COUNT(*)
FROM observed_departures o LEFT JOIN routes r ON r.route_id = o.route_id
WHERE o.scheduled_ts >= :cutoff - 28*86400 AND o.scheduled_ts < :cutoff  -- no look-ahead
  AND o.poll_gap_s <= 90                                                 -- drop low-quality rows
GROUP BY 1, 2, 3, 4, 5;
```

`:cutoff` is the start of the current service day. Every model computes its statistics once per
service day, in a background thread (`predictors/base.py: DailyStats`), so a stop lookup never waits
on this query. The backtest uses the same cutoff, so the live models are exactly the ones evaluated.

The levels, from most to least specific, are:

1. route + direction + stop + hour
2. line + direction + stop + hour
3. route + direction + hour
4. line + direction + hour
5. route + hour
6. line + hour
7. route
8. line
9. no data (predict the schedule)

The first level with at least 5 samples wins, and the UI shows which level and `n` were used.
The route levels need no day-type column: each `route_id` runs on only one day type (see below).

A **line** is the route's long name ("Green"). MTD's `route_id` encodes the service pattern: `GREEN`,
`GREEN EVENING`, `GREEN SATURDAY` and `GREEN SUNDAY` are all different IDs. So route-level history
never carries over between day types; after three days of collection, no scored departure had any.
The line levels fixed that: 0 → 30,612 of 44,657 departures scorable. With one day of history, the
first data we could score (weekend of 2026-09-26/27) gave:

| Horizon | Schedule | MTD live | Ours |
|---|---|---|---|
| 2 min | 243 s | 96 s | 214 s |
| 10 min | 246 s | 142 s | 217 s |
| 30 min | 247 s | 189 s | 217 s |

Average error in seconds, on the same departures in every column. The most specific level that fired,
line + direction + stop + hour, averaged 134 s. The coarse line levels averaged about 224 s, so
specificity pays off as history grows. The level order is a parameter (`AverageDelayPredictor(levels=...)`).
Four candidate orders scored identically on this data, because route-level history didn't exist yet;
re-run the comparison once weekday data repeats.

The backtest pivots MTD's horizon snapshots onto the observations:

```sql
SELECT o.*, MAX(CASE WHEN p.horizon_min = 10 THEN p.predicted_ts END) AS mtd_10, ...
FROM observed_departures o
LEFT JOIN mtd_predictions p USING (trip_id, service_date, stop_sequence)
GROUP BY o.trip_id, o.service_date, o.stop_sequence;
```

The app scores **complete service days only**, once a day. The models learn only from days before the
one they predict, so a finished day's scores never change; only the set of finished days does. The
rebuild waits until 1 AM, because the collector drops delays over an hour: by then every departure
scheduled before midnight has either been observed or never will be. The accuracy panel and the
Insights page both say which days they cover.

## Insights page

`http://localhost:8080/insights` (API: `/api/breakdown?by=line|stop|hour|day_type|day_of_week|route[&line=Green]`) answers
"which line or stop is the most late, and when?" For each group it shows:

- **Lateness:** average and median delay, the share of buses more than 5 min late or more than 1 min
  early, and a **95% confidence interval clustered by bus trip**. A late bus is late at every stop it
  serves, so treating its stops as independent would make the interval far too narrow.
- **Accuracy:** schedule vs MTD (from a chosen horizon) vs our model, on the same departures within the
  group. Our model uses only earlier-day history.

Groups below a minimum sample size are hidden, so a stop with six buses can't top the list by chance.
Clicking a stop opens its arrivals board.

## Models

Pick a model from the dropdown on the arrivals page, or with `?model=` on any API endpoint.

- **`avg_delay`** (default): at the first fallback level with at least 5 samples, the mean delay.
- **`shrunk_median`**: medians, because delays are right-skewed with occasional implausible outliers.
  Instead of an all-or-nothing sample cutoff, every level contributes in proportion to its data
  (partial pooling):

  ```
  estimate = median of all history
  for each level, coarsest -> most specific, with n observations in the request's group:
      estimate = (n * group median + k * estimate) / (n + k)          # k = 20
  ```

  `k` is how many observations the coarser estimate is worth. A group needs k observations of its own
  before its median counts for half (n=5 gives 20%, n=100 gives 83%). Stats are computed once per
  service day from history up to its start, which is exactly what the backtest scores.

  Walk-forward comparison on the same 81,738 departures (Sat-Mon, 2026-09-26 to 09-28): MAE
  203 s -> 192 s; Sunday 202 -> 172 s, Monday 190 -> 179 s; within 2 minutes 43.5% -> 46.6%. A
  comparison of mean, median, trimmed, winsorized and shrunk variants, with bootstrap CIs over bus
  trips, found shrinkage gave most of the gain. The median alone was not significantly better.
  `k` was not tuned; tune it on later days.
- **`median_delay`**: `avg_delay`'s rule with medians. The first fallback level with at least 5
  samples decides outright, with no pooling; if none qualifies, the median of all history. Uses the
  same once-per-service-day stats as `shrunk_median`.

## Adding a model

1. Implement `predict_many(reqs: list[PredictionRequest]) -> list[Prediction]` in a new file under
   `busapp/predictors/`. Each request carries the scheduled time, the route/stop/direction,
   `now_ts` (the information cut-off) and MTD's live estimate.
2. Add it to `PREDICTORS` in `busapp/predictors/__init__.py`.

It then shows up in the UI's model dropdown, in `?model=` on the API, and in the backtest.

Candidate models:

- **Median or trimmed mean.** Delay distributions are right-skewed, so these are more robust than the mean.
- **Exponential time decay.** Weights recent weeks more heavily.
- **MTD bias correction.** `MTD estimate + mean(MTD error | route, horizon)`, which combines live
  position with learned bias.
- **Upstream-delay propagation.** Uses how late the bus is now at earlier stops on the same trip or block.
- **Gradient-boosted trees** on route, stop, hour, day type, horizon, current delay, weather and the
  academic calendar.

## Limitations

- Ground truth comes from MTD's own final (near-zero-horizon) estimate, so the 2-minute horizon slightly
  favours MTD. The fix would be to cross-check against `/vehicle-positions` `STOPPED_AT` events.
- Observations are only as precise as the poll interval (20 s; the feed itself refreshes about every
  30–40 s).
- A few days of data leaves many specific buckets thin, so the fallback levels matter a lot early on.

## 30-minute interview outline

| Time | Topic |
|---|---|
| 5 min | **Problem:** unreliable live times. Show the board next to MTD's own. |
| 8 min | **Architecture and data quirks:** stops dropping out as ground truth, the calendar and >24h time traps, the re-listing bug found in live data. |
| 7 min | **Live demo:** search a stop, compare the three columns, point out model level and `n`, show `/docs`. |
| 5 min | **SQL, model and backtest:** hierarchical average, no-look-ahead evaluation, error by horizon. |
| 5 min | **Next steps:** bias-corrected MTD model, vehicle-position ground truth, Postgres/TimescaleDB for scale. |
