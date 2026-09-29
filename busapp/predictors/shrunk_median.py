"""Shrunk median: robust to skewed delays, and small groups borrow strength from coarser ones.

Walks the same fallback levels as AverageDelayPredictor, but instead of "first level with at
least 5 samples decides everything", every level contributes in proportion to its data
(partial pooling):

    estimate = median of all history
    for each level, coarsest -> most specific, if the request's group has n observations:
        estimate = (n * group median + k * estimate) / (n + k)

k is how many observations the coarser estimate is worth: a group needs k observations of its
own before its median counts for half (n=5 -> 20% own, n=100 -> 83%). Medians because delays
are right-skewed, with occasional implausible outliers.

Walk-forward evaluation vs the mean model (Sun + Mon 2026-09-27/28, 54,761 departures):
MAE 191 s -> 178 s (-7%; 95% CI over bus trips -16 to -11 s), RMSE 293 -> 276 s,
within 2 min 46% -> 50%. k=20 was not tuned.

Stats use history up to the start of the request's service day, computed once per day
(base.DailyStats), like every model here.
"""
import collections
import sqlite3
import statistics

from .. import config
from .average_delay import _SQL_COLUMN, LEVELS, Level, key_values
from .base import DailyStats, Prediction, PredictionRequest

SHRINK_K = 20

Stats = tuple[list[dict[tuple, tuple[int, float]]], tuple[int, float] | None]


class ShrunkMedianPredictor(DailyStats):
    name = "shrunk_median"

    def __init__(self, conn: sqlite3.Connection, k: float = SHRINK_K,
                 lookback_days: int = config.LOOKBACK_DAYS, levels: tuple[Level, ...] = LEVELS,
                 cache_size: int = 8):
        super().__init__(cache_size)
        self.conn = conn
        self.k = k
        self.lookback_s = lookback_days * 86400
        self.levels = levels
        self.line_of = dict(conn.execute("SELECT route_id, long_name FROM routes").fetchall())

    def _compute(self, cutoff: int) -> Stats:
        """Per level {group key: (n, median delay)}, plus (n, median) over all history.

        Medians aren't additive, so unlike the mean model this groups the raw delays in
        Python (~3 s for 110k rows; SQLite window-function medians were 2.5x slower)."""
        grain = tuple(_SQL_COLUMN)
        cols = ", ".join(_SQL_COLUMN[c] for c in grain)
        rows = self.conn.execute(
            f"""SELECT {cols}, o.delay_s
                FROM observed_departures o LEFT JOIN routes r ON r.route_id = o.route_id
                WHERE o.scheduled_ts >= ? AND o.scheduled_ts < ? AND o.poll_gap_s <= ?""",
            (cutoff - self.lookback_s, cutoff, config.MAX_POLL_GAP_S),
        ).fetchall()
        if not rows:
            return [{} for _ in self.levels], None
        levels = []
        for _, names in self.levels:
            idx = [grain.index(c) for c in names]
            line_pos = [i for i, c in enumerate(names) if c == "line"]
            groups: dict[tuple, list[int]] = collections.defaultdict(list)
            for r in rows:
                key = tuple(r[i] for i in idx)
                if any(key[i] is None for i in line_pos):
                    continue  # route missing from the routes table: no line to pool under
                groups[key].append(r[-1])
            levels.append({k: (len(v), statistics.median(v)) for k, v in groups.items()})
        return levels, (len(rows), statistics.median(r[-1] for r in rows))

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]:
        out = []
        for req in reqs:
            levels, overall = self.stats(self.cutoff_for(req.now_ts))
            if overall is None:
                out.append(Prediction(req.scheduled_ts, 0.0, self.name, "no data", 0))
                continue
            vals = key_values(req, self.line_of)
            estimate, (label, n_label) = overall[1], ("all lines", overall[0])
            for (lvl_label, cols), groups in reversed(list(zip(self.levels, levels))):
                hit = groups.get(tuple(vals[c] for c in cols))
                if hit:
                    n, med = hit
                    estimate = (n * med + self.k * estimate) / (n + self.k)
                    label, n_label = lvl_label, n  # report the most specific level that contributed
            # e.g. "line+dir+hour (pooled)": the finest level with data, whose n observations
            # carry n/(n+k) of the weight; coarser levels supply the rest
            out.append(Prediction(req.scheduled_ts + round(estimate), estimate, self.name,
                                  f"{label} (pooled)", n_label))
        return out
