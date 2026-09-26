"""Predict scheduled time + historical mean delay, with hierarchical fallback.

Tries the most specific bucket first and falls back to coarser ones until a
bucket has at least MIN_SAMPLES observations.
"""
import sqlite3
from collections import OrderedDict

from .. import config
from ..timeutil import day_type, local_dt
from .base import Prediction, PredictionRequest

# (label, grouping columns) from most to least specific
LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("route+dir+stop+hour+day", ("route_id", "direction_id", "stop_id", "hour_local", "day_type")),
    ("route+dir+hour+day", ("route_id", "direction_id", "hour_local", "day_type")),
    ("route+hour", ("route_id", "hour_local")),
    ("route", ("route_id",)),
)
CACHE_BUCKET_S = 600  # aggregates are recomputed at most every 10 minutes of `now_ts`


class AverageDelayPredictor:
    name = "avg_delay"

    def __init__(self, conn: sqlite3.Connection, min_samples: int = config.MIN_SAMPLES,
                 lookback_days: int = config.LOOKBACK_DAYS):
        self.conn = conn
        self.min_samples = min_samples
        self.lookback_s = lookback_days * 86400
        self._cache: OrderedDict[tuple[int, int], dict] = OrderedDict()

    def _aggregates(self, level: int, cutoff: int) -> dict[tuple, tuple[float, int]]:
        """{bucket key: (mean delay, n)} using observations in [cutoff - lookback, cutoff)."""
        ck = (level, cutoff)
        if ck in self._cache:
            self._cache.move_to_end(ck)
            return self._cache[ck]
        cols = ", ".join(LEVELS[level][1])
        rows = self.conn.execute(
            f"""SELECT {cols}, AVG(delay_s) AS mean_delay, COUNT(*) AS n
                FROM observed_departures
                WHERE scheduled_ts >= ? AND scheduled_ts < ? AND poll_gap_s <= ?
                GROUP BY {cols}""",
            (cutoff - self.lookback_s, cutoff, config.MAX_POLL_GAP_S),
        ).fetchall()
        k = len(LEVELS[level][1])
        agg = {tuple(r[:k]): (r["mean_delay"], r["n"]) for r in rows}
        self._cache[ck] = agg
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return agg

    @staticmethod
    def _key(req: PredictionRequest, cols: tuple[str, ...]) -> tuple:
        vals = {
            "route_id": req.route_id,
            "direction_id": req.direction_id,
            "stop_id": req.stop_id,
            "hour_local": local_dt(req.scheduled_ts).hour,
            "day_type": day_type(req.service_date),
        }
        return tuple(vals[c] for c in cols)

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]:
        out = []
        for req in reqs:
            cutoff = req.now_ts - req.now_ts % CACHE_BUCKET_S
            pred = None
            for i, (label, cols) in enumerate(LEVELS):
                hit = self._aggregates(i, cutoff).get(self._key(req, cols))
                if hit and hit[1] >= self.min_samples:
                    mean, n = hit
                    pred = Prediction(req.scheduled_ts + round(mean), mean, self.name, label, n)
                    break
            out.append(pred or Prediction(req.scheduled_ts, 0.0, self.name, "no data", 0))
        return out
