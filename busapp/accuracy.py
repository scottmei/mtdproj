"""Backtest: how far off are the schedule, MTD's live estimates, and our model?

Ground truth is observed_departures. For each horizon H we compare, on the
same set of departures, (a) the schedule, (b) MTD's prediction as it stood ~H
minutes before the bus came, and (c) our model, which may only use data from
before the start of that departure's service day (no look-ahead).
"""
import sqlite3
import time
from dataclasses import asdict, dataclass

from . import config
from .predictors import PredictionRequest, Predictor
from .timeutil import service_day_origin


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
       o.scheduled_ts, o.observed_ts,
  {cols}
FROM observed_departures o
LEFT JOIN mtd_predictions p
       ON p.trip_id = o.trip_id AND p.service_date = o.service_date
      AND p.stop_sequence = o.stop_sequence
WHERE o.scheduled_ts >= ? AND o.poll_gap_s <= ?
GROUP BY o.trip_id, o.service_date, o.stop_sequence
"""


def backtest(conn: sqlite3.Connection, predictor: Predictor, days: int = 7,
             now_ts: int | None = None) -> dict:
    now_ts = int(time.time()) if now_ts is None else now_ts
    rows = conn.execute(_pivot_sql(), (now_ts - days * 86400, config.MAX_POLL_GAP_S)).fetchall()

    preds = predictor.predict_many([
        PredictionRequest(r["trip_id"], r["route_id"], r["direction_id"], r["stop_id"],
                          r["service_date"], r["scheduled_ts"],
                          now_ts=service_day_origin(r["service_date"]))  # only prior days' data
        for r in rows
    ])
    ours = [(r, p) for r, p in zip(rows, preds) if p.n_samples > 0]

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
