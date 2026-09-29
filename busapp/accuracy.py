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
from .timeutil import local_dt, service_day_origin

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
WHERE o.scheduled_ts >= ? AND o.scheduled_ts < ? AND o.poll_gap_s <= ?
GROUP BY o.trip_id, o.service_date, o.stop_sequence
ORDER BY o.service_date  -- one service day at a time: each model builds each day's stats once
"""


Scored = tuple[sqlite3.Row, Prediction]


def score(conn: sqlite3.Connection, predictor: Predictor, days: int = 7,
          until_ts: int | None = None) -> list[Scored]:
    """Every departure observed in [until_ts - days, until_ts) (default: up to now), with MTD's
    horizon snapshots pivoted into columns and our model's walk-forward prediction (only data
    from before its service day)."""
    until_ts = int(time.time()) if until_ts is None else until_ts
    rows = conn.execute(_pivot_sql(), (until_ts - days * 86400, until_ts,
                                       config.MAX_POLL_GAP_S)).fetchall()
    preds = predictor.predict_many([
        PredictionRequest(r["trip_id"], r["route_id"], r["direction_id"], r["stop_id"],
                          r["service_date"], r["scheduled_ts"],
                          now_ts=service_day_origin(r["service_date"]))
        for r in rows
    ])
    return list(zip(rows, preds))


class ScoreCache:
    """Walk-forward scores of complete service days, and the accuracy panel's summary of them,
    shared by the accuracy and breakdown endpoints.

    The models learn only from history before each service day, so a finished day's
    predictions never change; what changes is which days are finished. So scores cover the
    `days` service days before `cutoff()` and are computed once per cutoff, i.e. once a day.
    Scoring takes ~7 s at 115k rows and summarizing ~5 s more, on this cache's own SQLite
    connection (WAL allows concurrent readers), never holding the web app's connection lock.
    """

    DEFAULT = (DEFAULT_PREDICTOR, 7)
    WARM_DAYS = 7  # the window the accuracy panel shows

    def __init__(self, db_path=None):
        self.conn = db.connect(db_path)
        self.predictors = {name: get_predictor(name, self.conn) for name in PREDICTORS}
        self._lock = threading.RLock()  # summary() calls get() while holding it
        self._data: dict[tuple[str, int], tuple[int, list[Scored]]] = {}   # stamped with cutoff
        self._summaries: dict[tuple[str, int], tuple[int, dict]] = {}

    @staticmethod
    def cutoff(now_ts: int | None = None) -> int:
        """Start of the latest service day that began at least MAX_ABS_DELAY_S ago.

        The collector drops delays beyond MAX_ABS_DELAY_S, so by then every departure
        scheduled before this cutoff has been observed or never will be: the days before
        it are complete. Between midnight and 1 AM this is still yesterday's start."""
        now_ts = int(time.time()) if now_ts is None else now_ts
        return service_day_origin(local_dt(now_ts - config.MAX_ABS_DELAY_S).strftime("%Y%m%d"))

    def _check(self, model: str) -> None:
        if model not in self.predictors:
            raise ValueError(f"Unknown model {model!r}; choose from {sorted(self.predictors)}")

    def _scored(self, model: str, days: int, cutoff: int, refresh: bool) -> list[Scored]:
        hit = self._data.get((model, days))
        if not refresh and hit and hit[0] == cutoff:  # no lock: never wait behind a rebuild
            return hit[1]
        with self._lock:  # concurrent callers wait for one computation instead of repeating it
            hit = self._data.get((model, days))
            if not refresh and hit and hit[0] == cutoff:
                return hit[1]
            scored = score(self.conn, self.predictors[model], days, until_ts=cutoff)
            # keep the default window plus the most recently requested other one
            self._data = {k: v for k, v in self._data.items() if k == self.DEFAULT}
            self._data[(model, days)] = (cutoff, scored)
            return scored

    def get(self, model: str, days: int, refresh: bool = False) -> list[Scored]:
        self._check(model)
        return self._scored(model, days, self.cutoff(), refresh)

    def summary(self, model: str, days: int, refresh: bool = False) -> dict:
        """backtest() for this model and window. Small, so kept for every model."""
        self._check(model)
        cutoff = self.cutoff()
        hit = self._summaries.get((model, days))
        if not refresh and hit and hit[0] == cutoff:
            return hit[1]
        with self._lock:
            hit = self._summaries.get((model, days))
            if not refresh and hit and hit[0] == cutoff:
                return hit[1]
            scored = self._scored(model, days, cutoff, refresh)
            result = backtest(self.conn, self.predictors[model], days=days, scored=scored)
            result["until_ts"] = cutoff
            self._summaries[(model, days)] = (cutoff, result)
            return result

    def keep_warm(self, check_s: int = 60) -> None:
        """Run forever in a daemon thread: when a new service day completes, score and
        summarize the accuracy panel's window for every model (default last, so its scores
        stay cached for the breakdown page). In between it only checks the clock."""
        models = sorted(self.predictors, key=lambda m: m == self.DEFAULT[0])
        done = None
        while True:
            cutoff = self.cutoff()
            if cutoff != done:
                t0, ok = time.time(), True
                for model in models:
                    try:
                        self.summary(model, self.WARM_DAYS)
                    except Exception as e:
                        ok = False
                        log.warning("Background scoring of %s failed: %s", model, e)
                log.info("Scored %d models on service days before %s in %.1fs", len(models),
                         local_dt(cutoff).date(), time.time() - t0)
                done = cutoff if ok else None  # retry next check after a failure
            time.sleep(check_s)


def backtest(conn: sqlite3.Connection, predictor: Predictor, days: int = 7,
             now_ts: int | None = None, scored: list[Scored] | None = None) -> dict:
    scored = score(conn, predictor, days, until_ts=now_ts) if scored is None else scored
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
