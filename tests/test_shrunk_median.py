import pytest

from busapp.predictors import PREDICTORS, get_predictor
from busapp.predictors.shrunk_median import ShrunkMedianPredictor
from busapp.timeutil import gtfs_to_epoch

from .test_average_delay import FRI, H18, add_obs, req, seed_routes

ROUTE_ONLY = (("route", ("route_id",)),)


def test_small_group_is_pulled_toward_overall_median(conn):
    add_obs(conn, 5, 100, route="TEAL")       # 5 obs at +100 s
    add_obs(conn, 15, 0, route="RED")         # overall median of all 20 obs is 0
    p = ShrunkMedianPredictor(conn, k=20, levels=ROUTE_ONLY).predict_many([req(route="TEAL")])[0]
    assert p.delay_s == pytest.approx((5 * 100 + 20 * 0) / (5 + 20))   # 20 s, not 100 s
    assert (p.level, p.n_samples) == ("route (pooled)", 5)


def test_big_group_mostly_keeps_its_own_median(conn):
    add_obs(conn, 180, 100, route="TEAL")
    add_obs(conn, 200, 0, route="RED")        # overall median 0
    p = ShrunkMedianPredictor(conn, k=20, levels=ROUTE_ONLY).predict_many([req(route="TEAL")])[0]
    assert p.delay_s == pytest.approx(180 * 100 / 200)                  # 90 s


def test_median_ignores_an_outlier(conn):
    for i, d in enumerate([60, 60, 60, 60, 3000]):
        add_obs(conn, 1, d, route="TEAL", start=i)
    p = ShrunkMedianPredictor(conn, k=0, levels=ROUTE_ONLY).predict_many([req(route="TEAL")])[0]
    assert p.delay_s == 60   # a mean would say 636 s


def test_chain_blends_every_level_and_reports_most_specific(conn):
    seed_routes(conn)
    add_obs(conn, 10, 200, route="TEAL", stop="A:1")                    # this stop
    add_obs(conn, 30, 100, route="TEAL SATURDAY", stop="B:1")           # same line elsewhere
    p = ShrunkMedianPredictor(conn, k=20).predict_many([req(route="TEAL", stop="A:1")])[0]
    assert p.level == "route+dir+stop+hour+day (pooled)" and p.n_samples == 10
    assert 100 < p.delay_s < 200   # own data pulled toward the line's typical delay


def test_uses_only_history_before_the_service_day(conn):
    add_obs(conn, 10, 999, route="TEAL", date=FRI)                      # same day: must be ignored
    live = req(route="TEAL", date=FRI, now=gtfs_to_epoch(FRI, H18 + 3600))
    p = ShrunkMedianPredictor(conn).predict_many([live])[0]
    assert (p.level, p.delay_s) == ("no data", 0.0)
    add_obs(conn, 10, 120, route="TEAL", date="20260924", start=100)    # the day before: used
    p = ShrunkMedianPredictor(conn).predict_many([live])[0]
    assert p.level != "no data" and p.delay_s > 0


def test_registry_and_warm(conn):
    assert "shrunk_median" in PREDICTORS
    p = get_predictor("shrunk_median", conn, cache_size=2)
    assert p.cache_size == 2
    p.warm(now_ts=gtfs_to_epoch(FRI, H18))
    assert len(p._cache) == 1
