"""Collector coverage: find polling gaps and estimate what each one cost.

A gap only matters if we would have observed buses during it. The timetable
alone overstates this: MTD schedules late-night service until ~5 AM that
never appears in the realtime feed (0 of ~650-990 stop times per hour were
observed overnight, vs ~97% during the day). So each gap's scheduled stop
times are weighted by the capture rate we actually achieve at that local
hour, learned from hours when the collector was fully up.
"""
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta

from . import config
from .timeutil import local_dt, service_day_origin

FULL_HOUR_POLLS = int(0.9 * 3600 / config.POLL_INTERVAL_S)  # an hour counts as fully covered
MIN_RATE_SCHEDULED = 50  # scheduled stop times needed before trusting an hour's capture rate
RATES_CACHE_S = 600

_GAPS_SQL = """
SELECT prev_ts, poll_ts FROM (
    SELECT poll_ts, LAG(poll_ts) OVER (ORDER BY poll_ts) AS prev_ts
    FROM polls
    WHERE error IS NULL AND poll_ts >= ?
)
WHERE poll_ts - prev_ts > ?
"""

_SCHEDULED_BY_HOUR_SQL = """
SELECT st.departure_s / 3600 AS bucket, COUNT(*), MIN(st.departure_s), MAX(st.departure_s)
FROM stop_times st
JOIN trips t           ON t.trip_id = st.trip_id
JOIN calendar_dates cd ON cd.service_id = t.service_id AND cd.date = ? AND cd.exception_type = 1
WHERE st.departure_s > ? AND st.departure_s < ?
GROUP BY bucket
"""


@dataclass
class Gap:
    start_ts: int                    # last successful poll before the gap
    end_ts: int                      # first successful poll after it (now, if ongoing)
    duration_s: int
    ongoing: bool
    scheduled_stop_times: int        # timetabled stop times inside the gap (incl. final stops)
    expected_observations: int       # scheduled x capture rate: what we'd normally have recorded
    first_scheduled_ts: int | None
    last_scheduled_ts: int | None

    @property
    def missed_service(self) -> bool:
        return self.expected_observations >= 1


def scheduled_by_hour(conn: sqlite3.Connection, start_ts: int,
                      end_ts: int) -> dict[int, tuple[int, int, int]]:
    """{epoch hour start: (count, first_ts, last_ts)} of stop times strictly inside (start, end).

    Service-day origins fall on whole hours, so buckets line up with clock hours.
    """
    out: dict[int, tuple[int, int, int]] = {}
    day = local_dt(start_ts).date() - timedelta(days=1)  # yesterday's trips can run past midnight
    while day <= local_dt(end_ts).date():
        sd = day.strftime("%Y%m%d")
        origin = service_day_origin(sd)
        for bucket, n, lo, hi in conn.execute(_SCHEDULED_BY_HOUR_SQL,
                                              (sd, start_ts - origin, end_ts - origin)):
            h, lo, hi = origin + bucket * 3600, origin + lo, origin + hi
            prev = out.get(h)
            out[h] = (n, lo, hi) if prev is None else (prev[0] + n, min(prev[1], lo), max(prev[2], hi))
        day += timedelta(days=1)
    return out


def capture_rates(conn: sqlite3.Connection, now_ts: int, days: int = 7,
                  min_scheduled: int = MIN_RATE_SCHEDULED) -> dict[int, float]:
    """{local hour: observed / scheduled} over hours when the collector was fully up."""
    since = now_ts - days * 86400
    covered = [h for h, n in conn.execute(
        "SELECT poll_ts / 3600 * 3600, COUNT(*) FROM polls "
        "WHERE error IS NULL AND poll_ts >= ? GROUP BY 1", (since,))
        if n >= FULL_HOUR_POLLS and h + 3600 <= now_ts]
    if not covered:
        return {}
    observed = dict(conn.execute(
        "SELECT scheduled_ts / 3600 * 3600, COUNT(*) FROM observed_departures "
        "WHERE scheduled_ts >= ? GROUP BY 1", (min(covered),)).fetchall())
    scheduled = scheduled_by_hour(conn, min(covered) - 1, max(covered) + 3600)
    num: dict[int, int] = defaultdict(int)
    den: dict[int, int] = defaultdict(int)
    for h in covered:
        if h in scheduled:
            hour = local_dt(h).hour
            num[hour] += observed.get(h, 0)
            den[hour] += scheduled[h][0]
    return {hour: min(1.0, num[hour] / den[hour]) for hour in den if den[hour] >= min_scheduled}


def find_gaps(conn: sqlite3.Connection, now_ts: int, days: int = 7,
              min_gap_s: int = config.MAX_POLL_GAP_S, rates: dict[int, float] | None = None,
              cache: dict | None = None) -> list[Gap]:
    """Polling gaps longer than `min_gap_s` in the last `days`, newest first.

    Hours with no learned capture rate are assumed fully observable (rate 1), so
    unknown gaps are flagged rather than hidden.
    """
    cache = {} if cache is None else cache
    rates = capture_rates(conn, now_ts, days) if rates is None else rates
    spans = [(a, b, False) for a, b in conn.execute(_GAPS_SQL, (now_ts - days * 86400, min_gap_s))]
    last_ok = conn.execute("SELECT MAX(poll_ts) FROM polls WHERE error IS NULL").fetchone()[0]
    if last_ok is not None and now_ts - last_ok > min_gap_s:
        spans.append((last_ok, now_ts, True))  # collector is down right now

    gaps = []
    for start, end, ongoing in spans:
        key = ("sched", start, end)
        if ongoing or key not in cache:
            by_hour = scheduled_by_hour(conn, start, end)
            if not ongoing:
                cache[key] = by_hour  # the timetable inside a closed gap never changes
        else:
            by_hour = cache[key]
        n = sum(v[0] for v in by_hour.values())
        expected = sum(v[0] * rates.get(local_dt(h).hour, 1.0) for h, v in by_hour.items())
        gaps.append(Gap(start, end, end - start, ongoing, n, round(expected),
                        min((v[1] for v in by_hour.values()), default=None),
                        max((v[2] for v in by_hour.values()), default=None)))
    return sorted(gaps, key=lambda g: g.start_ts, reverse=True)


def coverage_report(conn: sqlite3.Connection, now_ts: int, days: int = 7,
                    cache: dict | None = None, max_listed: int = 20) -> dict:
    cache = {} if cache is None else cache
    rk = ("rates", days, now_ts // RATES_CACHE_S)
    if rk not in cache:
        for k in [k for k in cache if k[0] == "rates"]:
            del cache[k]
        cache[rk] = capture_rates(conn, now_ts, days)
    gaps = find_gaps(conn, now_ts, days, rates=cache[rk], cache=cache)
    missed = [g for g in gaps if g.missed_service]
    return {
        "window_days": days,
        "gaps": len(gaps),
        "gaps_missing_service": len(missed),
        "missed_expected_observations": sum(g.expected_observations for g in missed),
        "capture_rate_by_hour": {h: round(r, 3) for h, r in sorted(cache[rk].items())},
        "gap_list": [{**asdict(g), "missed_service": g.missed_service} for g in gaps[:max_listed]],
    }
