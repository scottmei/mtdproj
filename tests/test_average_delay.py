from busapp.predictors import PredictionRequest, get_predictor
from busapp.predictors.average_delay import AverageDelayPredictor
from busapp.timeutil import gtfs_to_epoch

FRI, SAT = "20260925", "20260926"
H18 = 18 * 3600


def add_obs(conn, n, delay, route="TEAL", direction=0, stop="A:1", date="20260918", secs=H18,
            day="weekday", gap=20, start=0):
    sched = gtfs_to_epoch(date, secs)
    for i in range(n):
        conn.execute("INSERT INTO observed_departures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"{route}{direction}{stop}{delay}{date}{secs}-{start + i}", date, 1, stop,
                      route, direction, "V", sched, sched + delay, delay,
                      18 if secs == H18 else secs // 3600 % 24, day, gap))
    conn.commit()


def req(stop="A:1", route="TEAL", direction=0, date=FRI, secs=H18 + 600, now=None):
    sched = gtfs_to_epoch(date, secs)
    return PredictionRequest("T", route, direction, stop, date, sched, now or sched - 600)


def predict(conn, r):
    return AverageDelayPredictor(conn, min_samples=5).predict_many([r])[0]


def test_most_specific_level(conn):
    add_obs(conn, 5, 120)
    p = predict(conn, req())
    assert (p.level, p.n_samples, p.delay_s) == ("route+dir+stop+hour+day", 5, 120)
    assert p.predicted_ts == req().scheduled_ts + 120


def test_falls_back_to_route_dir_hour_day_when_stop_is_sparse(conn):
    add_obs(conn, 3, 120)                    # too few at this stop
    add_obs(conn, 3, 60, stop="B:1")         # other stops on the route/dir/hour/day
    p = predict(conn, req())
    assert p.level == "route+dir+hour+day" and p.n_samples == 6 and p.delay_s == 90


def test_falls_back_to_route_hour_across_day_types(conn):
    add_obs(conn, 5, 300, date="20260919", day="saturday")  # Saturday data only
    p = predict(conn, req(date=FRI))
    assert p.level == "route+hour" and p.delay_s == 300


def test_falls_back_to_route_then_no_data(conn):
    add_obs(conn, 5, 30, secs=8 * 3600)
    assert predict(conn, req()).level == "route"
    p = predict(conn, req(route="RED"))
    assert (p.level, p.delay_s, p.n_samples) == ("no data", 0.0, 0)
    assert p.predicted_ts == req().scheduled_ts


def test_no_lookahead_leakage(conn):
    # observations from the same evening, after "now", must not be used
    add_obs(conn, 5, 500, date=FRI)
    r = req(date=FRI, now=gtfs_to_epoch(FRI, H18 - 3600))
    assert predict(conn, r).level == "no data"


def test_ignores_observations_after_collector_gap(conn):
    add_obs(conn, 5, 999, gap=600)
    assert predict(conn, req()).level == "no data"


def test_registry(conn):
    assert get_predictor("avg_delay", conn).name == "avg_delay"
    try:
        get_predictor("nope", conn)
        raise AssertionError
    except ValueError:
        pass
