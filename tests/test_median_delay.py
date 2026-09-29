from busapp.predictors import PREDICTORS
from busapp.predictors.median_delay import MedianDelayPredictor

from .test_average_delay import add_obs, req


def predict(conn, r):
    return MedianDelayPredictor(conn, min_samples=5).predict_many([r])[0]


def test_first_level_with_enough_samples_decides_outright(conn):
    for i, d in enumerate([60, 60, 60, 60, 3000]):
        add_obs(conn, 1, d, start=i)
    add_obs(conn, 50, 0, stop="B:1")         # coarser levels disagree but are not blended in
    p = predict(conn, req())
    assert (p.level, p.n_samples, p.delay_s) == ("route+dir+stop+hour", 5, 60)
    assert p.predicted_ts == req().scheduled_ts + 60


def test_falls_back_when_stop_is_sparse(conn):
    add_obs(conn, 3, 120)                    # too few at this stop
    add_obs(conn, 4, 60, stop="B:1")
    p = predict(conn, req())
    assert (p.level, p.n_samples, p.delay_s) == ("route+dir+hour", 7, 60)


def test_falls_back_to_all_lines_then_no_data(conn):
    p = predict(conn, req())
    assert (p.level, p.delay_s, p.n_samples) == ("no data", 0.0, 0)
    add_obs(conn, 5, 90, route="RED")
    p = predict(conn, req(route="TEAL"))
    assert (p.level, p.delay_s, p.n_samples) == ("all lines", 90, 5)


def test_registered():
    assert PREDICTORS["median_delay"] is MedianDelayPredictor
