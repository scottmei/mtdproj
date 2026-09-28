"""Where and when are buses late, and how accurate is each estimate there?

Groups the backtest's scored departures by line, stop, hour, day type or
route pattern. Per group:

- lateness: mean and median delay, share >5 min late / >1 min early, and a
  95% confidence interval on the mean clustered by bus trip. A late bus is
  late at every stop it serves, so stops on the same trip are not independent;
  treating them as independent would make the interval far too narrow.
- accuracy: schedule vs MTD (at one horizon) vs our model, scored on the same
  departures within the group (those with an MTD snapshot and model history).
"""
import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass

from .accuracy import Scored
from .schedule import stop_group_names

DAY_LABELS = {"weekday": "Weekday", "saturday": "Saturday", "sunday": "Sunday"}


def _hour_label(h: int) -> str:
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


# dimension -> (row -> (sort key, label, color))
DIMENSIONS = {
    "line": lambda r, names: (r["line"] or r["route_id"], r["line"] or r["route_id"], r["line_color"]),
    "route": lambda r, names: (r["route_id"], r["route_id"], r["line_color"]),
    "stop": lambda r, names: (r["base_id"], names.get(r["base_id"], r["base_id"]), None),
    "hour": lambda r, names: (f"{r['hour_local']:02d}", _hour_label(r["hour_local"]), None),
    "day_type": lambda r, names: (r["day_type"], DAY_LABELS.get(r["day_type"], r["day_type"]), None),
}
NATURAL_ORDER = {"hour", "day_type"}  # sorted by key rather than by lateness


@dataclass
class GroupStats:
    key: str
    label: str
    color: str | None
    n: int
    mean_delay_s: float
    ci95_s: float | None      # half-width of a 95% CI on the mean, clustered by trip
    median_delay_s: float
    pct_late_5min: float
    pct_early_1min: float
    trips: int
    scored_n: int             # departures with an MTD snapshot at the horizon and model history
    schedule_mae_s: float | None
    mtd_mae_s: float | None
    ours_mae_s: float | None


def clustered_ci95(values: list[float], clusters: list) -> float | None:
    """95% CI half-width for the mean, with cluster-robust (by trip) standard error.

    Var(mean) = G/(G-1) * sum_g (sum_{i in g} (x_i - mean))^2 / n^2
    """
    n = len(values)
    mean = sum(values) / n
    resid: dict = defaultdict(float)
    for v, c in zip(values, clusters):
        resid[c] += v - mean
    g = len(resid)
    if g < 2:
        return None
    var = g / (g - 1) * sum(s * s for s in resid.values()) / (n * n)
    return 1.96 * math.sqrt(var)


def _mae(errors: list[float]) -> float | None:
    return round(sum(abs(e) for e in errors) / len(errors), 1) if errors else None


def summarize(group: list[Scored], key: str, label: str, color: str | None,
              horizon: int) -> GroupStats:
    delays = [r["delay_s"] for r, _ in group]
    trips = [(r["trip_id"], r["service_date"]) for r, _ in group]
    n = len(delays)
    ci = clustered_ci95(delays, trips)
    fair = [(r, p) for r, p in group if p.n_samples > 0 and r[f"mtd_{horizon}"] is not None]
    return GroupStats(
        key=key, label=label, color=color, n=n,
        mean_delay_s=round(sum(delays) / n, 1),
        ci95_s=round(ci, 1) if ci is not None else None,
        median_delay_s=float(statistics.median(delays)),
        pct_late_5min=round(100 * sum(d > 300 for d in delays) / n, 1),
        pct_early_1min=round(100 * sum(d < -60 for d in delays) / n, 1),
        trips=len(set(trips)),
        scored_n=len(fair),
        schedule_mae_s=_mae([r["scheduled_ts"] - r["observed_ts"] for r, _ in fair]),
        mtd_mae_s=_mae([r[f"mtd_{horizon}"] - r["observed_ts"] for r, _ in fair]),
        ours_mae_s=_mae([p.predicted_ts - r["observed_ts"] for r, p in fair]),
    )


def breakdown(conn: sqlite3.Connection, scored: list[Scored], by: str, horizon: int = 10,
              min_n: int = 30) -> dict:
    if by not in DIMENSIONS:
        raise ValueError(f"Unknown dimension {by!r}; choose from {sorted(DIMENSIONS)}")
    names = stop_group_names(conn) if by == "stop" else {}
    groups: dict[str, list[Scored]] = defaultdict(list)
    meta: dict[str, tuple[str, str | None]] = {}
    for r, p in scored:
        key, label, color = DIMENSIONS[by](r, names)
        groups[key].append((r, p))
        meta[key] = (label, color)

    kept = [summarize(g, k, *meta[k], horizon) for k, g in groups.items() if len(g) >= min_n]
    if by in NATURAL_ORDER:
        kept.sort(key=lambda s: s.key)
    else:
        kept.sort(key=lambda s: s.mean_delay_s, reverse=True)
    return {
        "by": by,
        "horizon_min": horizon,
        "min_n": min_n,
        "departures": len(scored),
        "groups_shown": len(kept),
        "groups_hidden": len(groups) - len(kept),  # below min_n: too few to trust
        "groups": [asdict(s) for s in kept],
    }
