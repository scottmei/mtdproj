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


class AverageDelayPredictor:
    name = "avg_delay"

    def __init__(self, conn: sqlite3.Connection, min_samples: int = config.MIN_SAMPLES,
                 lookback_days: int = config.LOOKBACK_DAYS, levels: tuple[Level, ...] = LEVELS):
        self.conn = conn
        self.min_samples = min_samples
        self.lookback_s = lookback_days * 86400
        self.levels = levels
        self.line_of = dict(conn.execute("SELECT route_id, long_name FROM routes").fetchall())
        self._cache: OrderedDict[tuple[int, int], dict] = OrderedDict()

    def _aggregates(self, level: int, cutoff: int) -> dict[tuple, tuple[float, int]]:
        """{bucket key: (mean delay, n)} using observations in [cutoff - lookback, cutoff)."""
        ck = (level, cutoff)
        if ck in self._cache:
            self._cache.move_to_end(ck)
            return self._cache[ck]
        names = self.levels[level][1]
        cols = ", ".join(_SQL_COLUMN[c] for c in names)
        join = "JOIN routes r ON r.route_id = o.route_id" if "line" in names else ""
        rows = self.conn.execute(
            f"""SELECT {cols}, AVG(o.delay_s) AS mean_delay, COUNT(*) AS n
                FROM observed_departures o {join}
                WHERE o.scheduled_ts >= ? AND o.scheduled_ts < ? AND o.poll_gap_s <= ?
                GROUP BY {cols}""",
            (cutoff - self.lookback_s, cutoff, config.MAX_POLL_GAP_S),
        ).fetchall()
        agg = {tuple(r[:len(names)]): (r["mean_delay"], r["n"]) for r in rows}
        self._cache[ck] = agg
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return agg

    def _key(self, req: PredictionRequest, cols: tuple[str, ...]) -> tuple:
        vals = {
            "route_id": req.route_id,
            "direction_id": req.direction_id,
            "stop_id": req.stop_id,
            "hour_local": local_dt(req.scheduled_ts).hour,
            "day_type": day_type(req.service_date),
            "line": self.line_of.get(req.route_id),
        }
        return tuple(vals[c] for c in cols)

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]:
        out = []
        for req in reqs:
            cutoff = req.now_ts - req.now_ts % CACHE_BUCKET_S
            pred = None
            for i, (label, cols) in enumerate(self.levels):
                hit = self._aggregates(i, cutoff).get(self._key(req, cols))
                if hit and hit[1] >= self.min_samples:
                    mean, n = hit
                    pred = Prediction(req.scheduled_ts + round(mean), mean, self.name, label, n)
                    break
            out.append(pred or Prediction(req.scheduled_ts, 0.0, self.name, "no data", 0))
        return out
