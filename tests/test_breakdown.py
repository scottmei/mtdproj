import math

import pytest

from busapp.breakdown import breakdown, clustered_ci95
from busapp.predictors import Prediction

from .test_schedule_arrivals import seed


def test_clustered_ci_is_wider_than_naive_when_trips_are_correlated():
    # two buses, each consistently late/on time at both of its stops
    values, trips = [100, 100, 0, 0], ["A", "A", "B", "B"]
    ci = clustered_ci95(values, trips)
    # mean 50; per-trip residual sums +100, -100; var = 2/1 * 20000 / 16 = 2500 -> se 50
    assert ci == pytest.approx(1.96 * 50)
    naive = 1.96 * math.sqrt(sum((v - 50) ** 2 for v in values) / 3 / 4)
    assert ci > naive
    assert clustered_ci95([5, 7], ["A", "A"]) is None  # one trip: no spread to estimate


def row(trip, delay, line="Teal", route="TEAL", base="IT", hour=18, day="weekday",
        mtd10_err=None, sched_ts=10_000, date="20260925"):
    obs = sched_ts + delay
    return {"trip_id": trip, "service_date": date, "delay_s": delay, "line": line,
            "line_color": "006991", "route_id": route, "base_id": base, "hour_local": hour,
            "day_type": day, "scheduled_ts": sched_ts, "observed_ts": obs,
            "mtd_10": None if mtd10_err is None else obs + mtd10_err}


def pred(r, ours_err=None):
    if ours_err is None:
        return Prediction(r["scheduled_ts"], 0.0, "avg_delay", "no data", 0)
    return Prediction(r["observed_ts"] + ours_err, 0.0, "avg_delay", "line", 9)


def test_groups_sorted_by_lateness_with_min_n(conn):
    scored = [(r, pred(r)) for r in
              [row("T1", 300), row("T2", 100), row("T3", 200),              # Teal: mean 200
               row("G1", 600, line="Green"), row("G2", 0, line="Green"),   # Green: mean 300
               row("R1", 999, line="Red")]]                                # Red: only 1 -> hidden
    res = breakdown(conn, scored, "line", min_n=2)
    assert [g["key"] for g in res["groups"]] == ["Green", "Teal"]
    assert res["groups_hidden"] == 1
    teal = res["groups"][1]
    assert (teal["n"], teal["mean_delay_s"], teal["median_delay_s"], teal["trips"]) == (3, 200, 200, 3)
    assert teal["pct_late_5min"] == 0 and res["groups"][0]["pct_late_5min"] == 50.0


def test_accuracy_uses_same_departures_within_group(conn):
    r1, r2, r3 = row("T1", 120, mtd10_err=-60), row("T2", 60, mtd10_err=30), row("T3", 0)
    scored = [(r1, pred(r1, ours_err=40)), (r2, pred(r2, ours_err=-20)),
              (r3, pred(r3, ours_err=5))]      # r3 has no MTD snapshot -> not scored
    (g,) = breakdown(conn, scored, "line", min_n=1)["groups"]
    assert g["scored_n"] == 2
    assert (g["schedule_mae_s"], g["mtd_mae_s"], g["ours_mae_s"]) == (90.0, 45.0, 30.0)


def test_stop_hour_and_day_labels(conn):
    seed(conn)  # stop groups: IT -> "Illinois Terminal", GWN -> "Goodwin & Green"
    scored = [(r, pred(r)) for r in [row("A", 60, base="IT"), row("B", 30, base="GWN", hour=7),
                                     row("C", 10, base="GWN", hour=0, day="sunday")]]
    stops = breakdown(conn, scored, "stop", min_n=1)["groups"]
    assert {g["key"]: g["label"] for g in stops} == {"IT": "Illinois Terminal", "GWN": "Goodwin & Green"}
    hours = breakdown(conn, scored, "hour", min_n=1)["groups"]
    assert [g["label"] for g in hours] == ["12 AM", "7 AM", "6 PM"]  # natural order
    days = breakdown(conn, scored, "day_type", min_n=1)["groups"]
    assert [g["label"] for g in days] == ["Sunday", "Weekday"]
    with pytest.raises(ValueError):
        breakdown(conn, scored, "planet")


def test_day_of_week_for_one_line(conn):
    scored = [(r, pred(r)) for r in [
        row("F1", 60, date="20260925"), row("F2", 120, date="20260925"),   # Teal, Friday
        row("M1", 300, date="20260928"),                                    # Teal, Monday
        row("M2", 999, line="Green", date="20260928"),                      # other line
        row("W1", 30, date="20260923"), row("W1", 90, date="20260916")]]    # Teal, two Wednesdays
    res = breakdown(conn, scored, "day_of_week", min_n=1, line="Teal")
    assert res["lines"] == ["Green", "Teal"] and res["line"] == "Teal"
    assert [(g["label"], g["n"], g["mean_delay_s"], g["service_days"]) for g in res["groups"]] == [
        ("Monday", 1, 300, 1), ("Wednesday", 2, 60, 2), ("Friday", 2, 90, 1)]
    assert res["departures"] == 5
