from busapp.collector import Collector, horizon_snapshots, infer_departures
from busapp.realtime import FeedSnapshot, StopRT, TripRT
from busapp.timeutil import gtfs_to_epoch

SD = "20260925"


def trip(tid, stops, canceled=False, route="TEAL"):
    t = TripRT(tid, SD, route, "V1", canceled)
    for seq, sid, ts in stops:
        t.stops[seq] = StopRT(seq, sid, ts)
    return t


def snap(*trips):
    return FeedSnapshot(0, {t.key: t for t in trips})


def test_stop_dropping_out_is_a_departure():
    prev = snap(trip("T1", [(1, "A", 1000), (2, "B", 1100), (3, "C", 1200)]))
    cur = snap(trip("T1", [(3, "C", 1210)]))
    deps = infer_departures(prev, cur, poll_ts=1105)
    assert [(d.stop_sequence, d.observed_ts) for d in deps] == [(1, 1000), (2, 1100)]


def test_observed_time_capped_at_poll_time():
    # MTD still predicted 1300 but the stop is already gone at 1150 -> bus left early
    prev = snap(trip("T1", [(1, "A", 1300), (2, "B", 1400)]))
    cur = snap(trip("T1", [(2, "B", 1400)]))
    assert infer_departures(prev, cur, poll_ts=1150)[0].observed_ts == 1150


def test_no_change_no_departures():
    s = snap(trip("T1", [(1, "A", 1000)]))
    assert infer_departures(s, s, poll_ts=900) == []


def test_vanished_trip_only_keeps_stops_that_were_due():
    prev = snap(trip("T1", [(9, "Y", 1000), (10, "Z", 5000)]))
    deps = infer_departures(prev, snap(), poll_ts=1020)
    assert [d.stop_sequence for d in deps] == [9]


def test_canceled_trips_ignored():
    prev = snap(trip("T1", [(1, "A", 1000)], canceled=True))
    assert infer_departures(prev, snap(), poll_ts=2000) == []


def test_horizon_snapshots_once_per_horizon_and_only_near_h():
    seen = set()
    # 9.5 min out -> counts for H=10 only
    s1 = snap(trip("T1", [(1, "A", 10_570)]))
    out = horizon_snapshots(s1, 10_000, seen)
    assert [(h.horizon_min, h.predicted_ts) for h in out] == [(10, 10_570)]
    assert horizon_snapshots(s1, 10_010, seen) == []  # not recorded twice
    # a trip first seen 3 min out must not fill the 5/10/15... buckets
    s2 = snap(trip("T2", [(1, "A", 10_180)]))
    assert horizon_snapshots(s2, 10_000, set()) == []


def seed_static(conn):
    conn.execute("INSERT INTO trips VALUES ('T1','TEAL','S1',0,'North','B1')")
    for seq, stop, secs in [(1, "A", 18 * 3600), (2, "B", 18 * 3600 + 300), (3, "C", 18 * 3600 + 600)]:
        conn.execute("INSERT INTO stop_times VALUES ('T1',?,?,?,?)", (seq, stop, secs, secs))
    conn.commit()


def test_collector_writes_observations(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    # first poll only seeds state
    assert c.process(snap(trip("T1", [(1, "A", base + 60), (2, "B", base + 360), (3, "C", base + 660)])), base) == 0
    # stop 1 served 60s late, noticed 20s later
    assert c.process(snap(trip("T1", [(2, "B", base + 360), (3, "C", base + 660)])), base + 80) == 1
    row = conn.execute("SELECT * FROM observed_departures").fetchone()
    assert row["delay_s"] == 60 and row["hour_local"] == 18 and row["day_type"] == "weekday"
    assert row["poll_gap_s"] == 80 and row["route_id"] == "TEAL" and row["direction_id"] == 0
    # every poll is logged for health/gap monitoring
    assert conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 2


def test_collector_drops_absurd_delays(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    c.process(snap(trip("T1", [(1, "A", base + 7200), (2, "B", base + 7300)])), base + 7000)
    assert c.process(snap(trip("T1", [(2, "B", base + 7300)])), base + 7220) == 0
