"""Predict scheduled time + historical mean delay, with hierarchical fallback.

Tries the most specific bucket first and falls back to coarser ones until a
bucket has at least MIN_SAMPLES observations.

MTD's route_id encodes the service pattern ('GREEN', 'GREEN EVENING',
'GREEN SATURDAY', 'GREEN SUNDAY' are all different ids), so route-level
history never carries over between day types. The coarser levels group by
*line* (the route's long name, e.g. 'Green'), which does.
"""
import sqlite3
from collections import OrderedDict

from .. import config
from ..timeutil import day_type, local_dt
from .base import Prediction, PredictionRequest

Level = tuple[str, tuple[str, ...]]

# (label, grouping columns) from most to least specific
LEVELS: tuple[Level, ...] = (
    ("route+dir+stop+hour+day", ("route_id", "direction_id", "stop_id", "hour_local", "day_type")),
    ("route+dir+hour+day", ("route_id", "direction_id", "hour_local", "day_type")),
    ("line+dir+stop+hour", ("line", "direction_id", "stop_id", "hour_local")),
    ("route+hour", ("route_id", "hour_local")),
    ("line+dir+hour", ("line", "direction_id", "hour_local")),
    ("route", ("route_id",)),
    ("line", ("line",)),
)
_SQL_COLUMN = {
    "route_id": "o.route_id",
    "direction_id": "o.direction_id",
    "stop_id": "o.stop_id",
    "hour_local": "o.hour_local",
    "day_type": "o.day_type",
    "line": "r.long_name",
}
CACHE_BUCKET_S = 600  # aggregates are recomputed at most every 10 minutes of `now_ts`


def key_values(req: PredictionRequest, line_of: dict[str, str]) -> dict:
    """Every grouping column's value for a request (shared by the historical models)."""
    return {
        "route_id": req.route_id,
        "direction_id": req.direction_id,
        "stop_id": req.stop_id,
        "hour_local": local_dt(req.scheduled_ts).hour,
        "day_type": day_type(req.service_date),
        "line": line_of.get(req.route_id),
    }


class AverageDelayPredictor:
    name = "avg_delay"

    def __init__(self, conn: sqlite3.Connection, min_samples: int = config.MIN_SAMPLES,
                 lookback_days: int = config.LOOKBACK_DAYS, levels: tuple[Level, ...] = LEVELS,
                 cache_size: int = 8):
        self.conn = conn
        self.min_samples = min_samples
        self.lookback_s = lookback_days * 86400
        self.levels = levels
        self.cache_size = cache_size  # each entry holds every group's stats (~tens of MB)
        self.line_of = dict(conn.execute("SELECT route_id, long_name FROM routes").fetchall())
        self._cache: OrderedDict[int, list[dict]] = OrderedDict()  # cutoff -> per-level averages

    def _aggregates(self, level: int, cutoff: int) -> dict[tuple, tuple[float, int]]:
        """{bucket key: (mean delay, n)} using observations in [cutoff - lookback, cutoff)."""
        if cutoff not in self._cache:
            self._cache[cutoff] = self._all_levels(cutoff)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        self._cache.move_to_end(cutoff)
        return self._cache[cutoff][level]

    def _all_levels(self, cutoff: int) -> list[dict[tuple, tuple[float, int]]]:
        """Every level's averages from ONE grouped query at the finest grain.

        Sums and counts are additive, so coarser levels are exact roll-ups of the finest
        one: one table scan instead of one per level (2.5 s -> 1.4 s at 100k rows).
        """
        grain = tuple(_SQL_COLUMN)  # route_id, direction_id, stop_id, hour_local, day_type, line
        cols = ", ".join(_SQL_COLUMN[c] for c in grain)
        rows = self.conn.execute(
            f"""SELECT {cols}, SUM(o.delay_s), COUNT(*)
                FROM observed_departures o LEFT JOIN routes r ON r.route_id = o.route_id
                WHERE o.scheduled_ts >= ? AND o.scheduled_ts < ? AND o.poll_gap_s <= ?
                GROUP BY {cols}""",
            (cutoff - self.lookback_s, cutoff, config.MAX_POLL_GAP_S),
        ).fetchall()
        levels = []
        for _, names in self.levels:
            idx = [grain.index(c) for c in names]
            line_pos = [i for i, c in enumerate(names) if c == "line"]
            sums: dict[tuple, list] = {}
            for r in rows:
                key = tuple(r[i] for i in idx)
                if any(key[i] is None for i in line_pos):
                    continue  # route missing from the routes table: no line to pool under
                acc = sums.setdefault(key, [0, 0])
                acc[0] += r[-2]
                acc[1] += r[-1]
            levels.append({k: (s / n, n) for k, (s, n) in sums.items()})
        return levels

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]:
        out = []
        for req in reqs:
            cutoff = req.now_ts - req.now_ts % CACHE_BUCKET_S
            vals = key_values(req, self.line_of)  # once per request, not once per level
            pred = None
            for i, (label, cols) in enumerate(self.levels):
                hit = self._aggregates(i, cutoff).get(tuple(vals[c] for c in cols))
                if hit and hit[1] >= self.min_samples:
                    mean, n = hit
                    pred = Prediction(req.scheduled_ts + round(mean), mean, self.name, label, n)
                    break
            out.append(pred or Prediction(req.scheduled_ts, 0.0, self.name, "no data", 0))
        return out
