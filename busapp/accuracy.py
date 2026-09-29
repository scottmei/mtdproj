"""Backtest: how far off are the schedule, MTD's live estimates, and our model?

Ground truth is observed_departures. For each horizon H we compare, on the
same set of departures, (a) the schedule, (b) MTD's prediction as it stood ~H
minutes before the bus came, and (c) our model, which may only use data from
before the start of that departure's service day (no look-ahead).
"""
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass

from . import config, db
from .predictors import (DEFAULT_PREDICTOR, PREDICTORS, Prediction, PredictionRequest, Predictor,
                         get_predictor)
from .timeutil import service_day_origin

log = logging.getLogger(__name__)


@dataclass
class ErrorStats:
    n: int
    mae_s: float | None   # mean absolute error
    bias_s: float | None  # mean(predicted - observed); positive = predicts too late
    p90_s: float | None   # 90th percentile of absolute error


def error_stats(errors: list[float]) -> ErrorStats:
    if not errors:
        return ErrorStats(0, None, None, None)
    abs_sorted = sorted(abs(e) for e in errors)
    p90 = abs_sorted[min(len(abs_sorted) - 1, int(0.9 * len(abs_sorted)))]
    return ErrorStats(len(errors), round(sum(abs_sorted) / len(errors), 1),
                      round(sum(errors) / len(errors), 1), float(p90))


def _pivot_sql() -> str:
    cols = ",\n  ".join(
        f"MAX(CASE WHEN p.horizon_min = {h} THEN p.predicted_ts END) AS mtd_{h}"
        for h in config.HORIZONS_MIN)
    return f"""
SELECT o.trip_id, o.service_date, o.stop_sequence, o.stop_id, o.route_id, o.direction_id,
       o.scheduled_ts, o.observed_ts, o.delay_s, o.hour_local, o.day_type,
       r.long_name AS line, r.color AS line_color, s.base_id,
  {cols}
FROM observed_departures o
LEFT JOIN routes r ON r.route_id = o.route_id
LEFT JOIN stops s  ON s.stop_id = o.stop_id
LEFT JOIN mtd_predictions p
       ON p.trip_id = o.trip_id AND p.service_date = o.service_date
      AND p.stop_sequence = o.stop_sequence
WHERE o.scheduled_ts >= ? AND o.poll_gap_s <= ?
GROUP BY o.trip_id, o.service_date, o.stop_sequence
"""


Scored = tuple[sqlite3.Row, Prediction]


def score(conn: sqlite3.Connection, predictor: Predictor, days: int = 7,
          now_ts: int | None = None) -> list[Scored]:
    """Every observed departure in the window, with MTD's horizon snapshots pivoted into
    columns and our model's walk-forward prediction (only data from before its service day)."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    rows = conn.execute(_pivot_sql(), (now_ts - days * 86400, config.MAX_POLL_GAP_S)).fetchall()
    preds = predictor.predict_many([
        PredictionRequest(r["trip_id"], r["route_id"], r["direction_id"], r["stop_id"],
                          r["service_date"], r["scheduled_ts"],
                          now_ts=service_day_origin(r["service_date"]))
        for r in rows
    ])
    return list(zip(rows, preds))


class ScoreCache:
    """Walk-forward scores, and the accuracy panel's summary of them, shared by the accuracy
    and breakdown endpoints.

    Scoring every departure takes ~7 s at 115k rows and summarizing it ~5 s more, so both run
    on this cache's own SQLite connection (WAL allows concurrent readers) and never hold the
    web app's connection lock. `keep_warm()` recomputes the default window for every model
    every `ttl_s`; requests accept results up to twice that old, so a visitor is served the
    previous result instead of waiting for a refresh the background thread is about to do.
    """

    DEFAULT = (DEFAULT_PREDICTOR, 7)
    WARM_DAYS = 7  # the window the accuracy panel shows

    def __init__(self, db_path=None, ttl_s: int = 600):
        self.conn = db.connect(db_path)
        self.predictors = {name: get_predictor(name, self.conn) for name in PREDICTORS}
        self.ttl_s = ttl_s
        self._lock = threading.RLock()  # summary() calls get() while holding it
        self._data: dict[tuple[str, int], tuple[float, list[Scored]]] = {}
        self._summaries: dict[tuple[str, int], tuple[float, dict]] = {}  # stamped with scoring time

    def _check(self, model: str) -> None:
        if model not in self.predictors:
            raise ValueError(f"Unknown model {model!r}; choose from {sorted(self.predictors)}")

    def _fresh(self, hit: tuple[float, object] | None) -> bool:
        return hit is not None and time.time() - hit[0] < 2 * self.ttl_s

    def get(self, model: str, days: int, refresh: bool = False) -> list[Scored]:
        self._check(model)
        hit = self._data.get((model, days))
        if not refresh and self._fresh(hit):  # no lock: never wait behind a background refresh
            return hit[1]
        with self._lock:  # concurrent callers wait for one computation instead of repeating it
            hit = self._data.get((model, days))
            if not refresh and self._fresh(hit):
                return hit[1]
            scored = score(self.conn, self.predictors[model], days)
            # keep the default window plus the most recently requested other one
            self._data = {k: v for k, v in self._data.items() if k == self.DEFAULT}
            self._data[(model, days)] = (time.time(), scored)
            return scored

    def summary(self, model: str, days: int, refresh: bool = False) -> dict:
        """backtest() for this model and window. Small, so kept for every model."""
        self._check(model)
        hit = self._summaries.get((model, days))
        if not refresh and self._fresh(hit):
            return hit[1]
        with self._lock:
            hit = self._summaries.get((model, days))
            if not refresh and self._fresh(hit):
                return hit[1]
            scored = self.get(model, days, refresh=refresh)
            stamp = self._data[(model, days)][0]  # expires with the scores it summarizes
            result = backtest(self.conn, self.predictors[model], days=days, scored=scored)
            self._summaries[(model, days)] = (stamp, result)
            return result

    def keep_warm(self) -> None:
        """Run forever in a daemon thread: rescore and summarize the accuracy panel's window
        for every model, default last so its scores stay cached for the breakdown page."""
        models = sorted(self.predictors, key=lambda m: m == self.DEFAULT[0])
        while True:
            t0 = time.time()
            for model in models:
                try:
                    self.summary(model, self.WARM_DAYS, refresh=True)
                except Exception as e:
                    log.warning("Background scoring of %s failed: %s", model, e)
            log.info("Scored and summarized %d models in %.1fs", len(models), time.time() - t0)
            time.sleep(max(60, self.ttl_s - (time.time() - t0)))


def backtest(conn: sqlite3.Connection, predictor: Predictor, days: int = 7,
             now_ts: int | None = None, scored: list[Scored] | None = None) -> dict:
    scored = score(conn, predictor, days, now_ts) if scored is None else scored
    rows = [r for r, _ in scored]
    ours = [(r, p) for r, p in scored if p.n_samples > 0]

    by_horizon = []
    for h in config.HORIZONS_MIN:
        subset = [(r, p) for r, p in ours if r[f"mtd_{h}"] is not None]
        with_mtd = [r for r in rows if r[f"mtd_{h}"] is not None]
        by_horizon.append({
            "horizon_min": h,
            # same departures for all three -> a fair comparison
            "schedule": asdict(error_stats([r["scheduled_ts"] - r["observed_ts"] for r, _ in subset])),
            "mtd": asdict(error_stats([r[f"mtd_{h}"] - r["observed_ts"] for r, _ in subset])),
            "ours": asdict(error_stats([p.predicted_ts - r["observed_ts"] for r, p in subset])),
            # schedule vs MTD on every departure with an MTD snapshot (available from day one)
            "all": {
                "schedule": asdict(error_stats([r["scheduled_ts"] - r["observed_ts"] for r in with_mtd])),
                "mtd": asdict(error_stats([r[f"mtd_{h}"] - r["observed_ts"] for r in with_mtd])),
            },
        })

    return {
        "model": predictor.name,
        "days": days,
        "observations": len(rows),
        "observations_with_history": len(ours),
        "overall": {
            "schedule": asdict(error_stats([r["scheduled_ts"] - r["observed_ts"] for r, _ in ours])),
            "ours": asdict(error_stats([p.predicted_ts - r["observed_ts"] for r, p in ours])),
        },
        "by_horizon": by_horizon,
    }
