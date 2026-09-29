from busapp.accuracy import backtest, error_stats
from busapp.predictors.average_delay import AverageDelayPredictor
from busapp.timeutil import gtfs_to_epoch

D1, D2 = "20260924", "20260925"   # Thu, Fri (both weekdays)


def test_error_stats():
    s = error_stats([60, -60, 120, 0])
    assert (s.n, s.mae_s, s.bias_s, s.p90_s) == (4, 60.0, 30.0, 120.0)
    assert error_stats([]).n == 0


def add(conn, date, i, delay, mtd10=None):
    sched = gtfs_to_epoch(date, 18 * 3600)
    conn.execute("INSERT INTO observed_departures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"T{i}", date, 1, "A:1", "TEAL", 0, "V", sched, sched + delay, delay, 18,
                  "weekday", 20))
    if mtd10 is not None:
        conn.execute("INSERT INTO mtd_predictions VALUES (?,?,?,?,?,?)",
                     (f"T{i}", date, 1, 10, sched - 600, sched + mtd10))


def test_backtest_uses_only_prior_days_and_compares_same_subset(conn):
    for i in range(5):
        add(conn, D1, i, 120)                      # history: TEAL runs 2 min late
    add(conn, D2, 100, 180, mtd10=0)               # MTD said on time 10 min out; bus was 3 min late
    add(conn, D2, 101, 60, mtd10=60)               # MTD right
    conn.commit()
    now = gtfs_to_epoch(D2, 23 * 3600)
    res = backtest(conn, AverageDelayPredictor(conn, min_samples=5), days=7, now_ts=now)
    assert res["observations"] == 7
    # day-1 rows had no earlier history -> excluded; day-2 rows predicted from day 1 only
    assert res["observations_with_history"] == 2
    h10 = next(h for h in res["by_horizon"] if h["horizon_min"] == 10)
    assert h10["ours"]["n"] == h10["mtd"]["n"] == h10["schedule"]["n"] == 2
    assert h10["schedule"]["mae_s"] == 120.0     # |−180|, |−60|
    assert h10["mtd"]["mae_s"] == 90.0           # |−180|, |0|
    assert h10["ours"]["mae_s"] == 60.0          # sched+120 vs 180 and 60 -> |−60|, |60|
    assert next(h for h in res["by_horizon"] if h["horizon_min"] == 5)["mtd"]["n"] == 0
    assert h10["all"]["mtd"]["n"] == 2 and h10["all"]["schedule"]["mae_s"] == 120.0



def score_cache(tmp_path, monkeypatch, clock):
    """A ScoreCache over a small database whose clock is `clock[0]` (fixed dates, not today's)."""
    from busapp import db
    from busapp.accuracy import ScoreCache

    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)
    for i in range(5):
        add(c, D1, i, 120)
    add(c, D2, 100, 180, mtd10=0)
    c.commit()
    monkeypatch.setattr(ScoreCache, "cutoff", staticmethod(
        lambda now_ts=None, real=ScoreCache.cutoff: real(clock[0])))
    return ScoreCache(path), c


def test_score_cache_covers_complete_service_days_and_rebuilds_once_a_day(tmp_path, monkeypatch):
    import pytest

    clock = [gtfs_to_epoch(D2, 20 * 3600)]           # Friday evening
    cache, c = score_cache(tmp_path, monkeypatch, clock)
    thu = cache.get("avg_delay", 7)
    assert {r["service_date"] for r, _ in thu} == {D1}   # Friday isn't finished: not scored yet
    add(c, D1, 50, 120)                               # a late write into the scored window...
    c.commit()
    assert cache.get("avg_delay", 7) is thu            # ...waits for the next daily rebuild
    clock[0] = gtfs_to_epoch("20260926", 30 * 60)      # Sat 00:30: Friday's buses may still run
    assert cache.get("avg_delay", 7) is thu
    clock[0] = gtfs_to_epoch("20260926", 90 * 60)      # Sat 01:30: Friday is complete
    fri = cache.get("avg_delay", 7)
    assert fri is not thu and {r["service_date"] for r, _ in fri} == {D1, D2}
    assert cache.get("avg_delay", 7, refresh=True) is not fri
    with pytest.raises(ValueError):
        cache.get("nope", 7)


def test_score_cache_summaries_are_cached_per_model_and_day(tmp_path, monkeypatch):
    import pytest

    clock = [gtfs_to_epoch("20260926", 12 * 3600)]
    cache, _ = score_cache(tmp_path, monkeypatch, clock)
    first = cache.summary("avg_delay", 7)
    assert first["model"] == "avg_delay" and first["observations"] == 6
    assert first["until_ts"] == gtfs_to_epoch("20260926", 0)
    assert cache.summary("avg_delay", 7) is first                     # served from cache
    other = cache.summary("shrunk_median", 7)
    assert other["model"] == "shrunk_median"
    assert cache.summary("shrunk_median", 7) is other                 # every model keeps its own
    assert cache.summary("avg_delay", 7) is first
    clock[0] += 86400                                                  # next day: rebuilt
    assert cache.summary("avg_delay", 7) is not first
    with pytest.raises(ValueError):
        cache.summary("nope", 7)