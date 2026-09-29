"""Strict median with hierarchical fallback: AverageDelayPredictor's rule, medians instead of means.

Walks the same levels, most specific first, and the first group with at least MIN_SAMPLES
observations decides the estimate outright (no pooling with coarser levels, unlike
shrunk_median). If no level qualifies, falls back to the median of all history.

Stats come from ShrunkMedianPredictor's per-service-day computation (history up to the start of
the request's service day), so the live model and the backtested model are the same.
"""
import sqlite3

from .. import config
from .average_delay import LEVELS, Level, key_values
from .base import Prediction, PredictionRequest
from .shrunk_median import ShrunkMedianPredictor


class MedianDelayPredictor(ShrunkMedianPredictor):
    name = "median_delay"

    def __init__(self, conn: sqlite3.Connection, min_samples: int = config.MIN_SAMPLES,
                 lookback_days: int = config.LOOKBACK_DAYS, levels: tuple[Level, ...] = LEVELS,
                 cache_size: int = 8):
        super().__init__(conn, lookback_days=lookback_days, levels=levels, cache_size=cache_size)
        self.min_samples = min_samples

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]:
        out = []
        for req in reqs:
            levels, overall = self.stats(self.cutoff_for(req.now_ts))
            if overall is None:
                out.append(Prediction(req.scheduled_ts, 0.0, self.name, "no data", 0))
                continue
            vals = key_values(req, self.line_of)
            (n, med), label = overall, "all lines"
            for (lvl_label, cols), groups in zip(self.levels, levels):
                hit = groups.get(tuple(vals[c] for c in cols))
                if hit and hit[0] >= self.min_samples:
                    (n, med), label = hit, lvl_label
                    break
            out.append(Prediction(req.scheduled_ts + round(med), float(med), self.name, label, n))
        return out
