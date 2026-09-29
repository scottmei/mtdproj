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
    assert (p.level, p.n_samples, p.delay_s) == ("route+dir+stop+hour", 5, 120)
    assert p.predicted_ts == req().scheduled_ts + 120


def test_falls_back_to_route_dir_hour_when_stop_is_sparse(conn):
    add_obs(conn, 3, 120)                    # too few at this stop
    add_obs(conn, 3, 60, stop="B:1")         # other stops on the route/dir/hour
    p = predict(conn, req())
    assert p.level == "route+dir+hour" and p.n_samples == 6 and p.delay_s == 90


def test_falls_back_to_route_hour_across_directions(conn):
    add_obs(conn, 5, 300, direction=1)       # other direction only
    p = predict(conn, req())
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


def seed_routes(conn):
    conn.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
        ("TEAL", "12", "Teal", "006991", "ffffff"),
        ("TEAL SATURDAY", "120", "Teal", "006991", "ffffff"),   # same line, different pattern
        ("TEAL ALT", "12", "Teal Alternate", "006991", "ffffff"),  # different line
    ])
    conn.commit()


def test_line_level_carries_history_across_service_patterns(conn):
    seed_routes(conn)
    add_obs(conn, 5, 240, route="TEAL", secs=H18)  # weekday TEAL history only
    p = predict(conn, req(route="TEAL SATURDAY", date=SAT))
    assert (p.level, p.n_samples, p.delay_s) == ("line+dir+stop+hour", 5, 240)


def test_line_levels_fall_back_to_line_dir_hour_then_line(conn):
    seed_routes(conn)
    add_obs(conn, 5, 100, route="TEAL", stop="B:1")        # other stop, same hour
    assert predict(conn, req(route="TEAL SATURDAY", date=SAT)).level == "line+dir+hour"
    conn.execute("DELETE FROM observed_departures")
    add_obs(conn, 5, 100, route="TEAL", secs=8 * 3600)     # other hour
    assert predict(conn, req(route="TEAL SATURDAY", date=SAT)).level == "line"


def test_different_line_does_not_share_history(conn):
    seed_routes(conn)
    add_obs(conn, 5, 240, route="TEAL")
    assert predict(conn, req(route="TEAL ALT")).level == "no data"


def test_route_level_still_preferred_when_available(conn):
    seed_routes(conn)
    add_obs(conn, 5, 999, route="TEAL", date="20260919", day="saturday")    # line data
    add_obs(conn, 5, 60, route="TEAL SATURDAY", date="20260919", day="saturday")
    p = predict(conn, req(route="TEAL SATURDAY", date=SAT))
    assert p.level == "route+dir+stop+hour" and p.delay_s == 60


def test_one_pass_rollup_matches_direct_group_by(conn):
    import math
    import random

    from busapp import config
    from busapp.predictors.average_delay import _SQL_COLUMN

    seed_routes(conn)
    rng = random.Random(7)
    for i in range(400):
        add_obs(conn, 1, rng.randint(-120, 900), route=rng.choice(["TEAL", "TEAL SATURDAY", "TEAL ALT"]),
                direction=rng.choice([0, 1]), stop=rng.choice(["A:1", "B:1", "C:2"]),
                secs=rng.choice([8, 12, 18]) * 3600, day=rng.choice(["weekday", "saturday"]), start=i)
    p = AverageDelayPredictor(conn)
    cutoff = gtfs_to_epoch(FRI, 0)
    for (label, names), agg in zip(p.levels, p._compute(cutoff)):
        cols = ", ".join(_SQL_COLUMN[c] for c in names)
        join = "JOIN routes r ON r.route_id = o.route_id" if "line" in names else ""
        direct = {tuple(r[:len(names)]): (r[-2], r[-1]) for r in conn.execute(
            f"SELECT {cols}, AVG(o.delay_s), COUNT(*) FROM observed_departures o {join} "
            f"WHERE o.scheduled_ts >= ? AND o.scheduled_ts < ? AND o.poll_gap_s <= ? GROUP BY {cols}",
            (cutoff - p.lookback_s, cutoff, config.MAX_POLL_GAP_S))}
        assert direct.keys() == agg.keys(), label
        for k, (mean, n) in direct.items():
            assert agg[k][1] == n and math.isclose(agg[k][0], mean), (label, k)


def test_uses_only_history_before_the_service_day_and_warms(conn):
    add_obs(conn, 5, 300, date=FRI, secs=8 * 3600)   # this morning: not used until tomorrow
    evening = req(date=FRI, now=gtfs_to_epoch(FRI, H18))
    p = AverageDelayPredictor(conn, cache_size=2)
    assert p.predict_many([evening])[0].level == "no data"
    tomorrow = req(date="20260926", now=gtfs_to_epoch("20260926", H18))
    assert p.predict_many([tomorrow])[0].delay_s == 300
    p.warm(now_ts=gtfs_to_epoch("20260927", H18))    # background thread computes ahead of requests
    assert len(p._cache) == 2
