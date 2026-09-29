from busapp import config
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
        conn.execute("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                     "VALUES ('T1',?,?,?,?)", (seq, stop, secs, secs))
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


def test_relisted_stops_are_retracted_then_rerecorded(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    full = [(1, "A", base + 60), (2, "B", base + 360), (3, "C", base + 660)]
    c.process(snap(trip("T1", full)), base)
    c.process(snap(trip("T1", full[2:])), base + 40)          # feed glitch: stops 1-2 "gone"
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 2
    c.process(snap(trip("T1", full[1:])), base + 80)          # stop 2 comes back
    assert c.last_retracted == 1
    assert [r[0] for r in conn.execute("SELECT stop_sequence FROM observed_departures")] == [1]
    c.process(snap(trip("T1", full[2:])), base + 100)         # stop 2 really served now
    row = conn.execute("SELECT observed_ts FROM observed_departures WHERE stop_sequence=2").fetchone()
    assert row[0] == base + 100  # min(last prediction, poll time)


def test_long_gap_reseeds_instead_of_guessing(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    full = [(1, "A", base + 60), (2, "B", base + 360), (3, "C", base + 660)]
    c.process(snap(trip("T1", full)), base)
    # laptop slept 10 minutes: stops 1-2 vanished at unknown times -> nothing recorded
    assert c.process(snap(trip("T1", full[2:])), base + 600) == 0
    assert c.last_reseed_gap == 600
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 0
    # normal polling resumes from the new baseline: stop 3 served 20 s later is recorded
    assert c.process(snap(trip("T1", [(4, "D", base + 900)])), base + 620) == 1
    assert c.last_reseed_gap is None


def test_gap_at_threshold_still_infers(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    full = [(1, "A", base + 60), (2, "B", base + 360)]
    c.process(snap(trip("T1", full)), base)
    assert c.process(snap(trip("T1", full[1:])), base + 90) == 1


def test_collector_records_trips_timetabled_after_midnight(conn):
    # N1 runs at 24:15 on service date SD; MTD's feed dates it SD + 1
    conn.execute("INSERT INTO trips VALUES ('N1','ILLINI','S1',0,'North','B1')")
    for seq, secs in [(1, 87300), (2, 87400)]:
        conn.execute("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                     "VALUES ('N1',?,?,?,?)", (seq, f"S{seq}", secs, secs))
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 87300)

    def mtd_trip(stops):
        t = TripRT("N1", "20260926", "ILLINI", "V1")
        t.stops.update({seq: StopRT(seq, f"S{seq}", ts) for seq, ts in stops})
        return snap(t)

    c.process(mtd_trip([(1, base + 90), (2, base + 190)]), base + 60)
    assert c.process(mtd_trip([(2, base + 190)]), base + 100) == 1
    row = conn.execute("SELECT service_date, delay_s FROM observed_departures").fetchone()
    assert tuple(row) == (SD, 90)
    assert {r[0] for r in conn.execute("SELECT service_date FROM mtd_predictions")} == {SD}


def seed_long_trip(conn, n=15):
    conn.execute("INSERT INTO trips VALUES ('L','GREEN','S1',0,'East','B1')")
    for seq in range(1, n + 1):
        secs = 18 * 3600 + seq * 120
        conn.execute("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                     "VALUES ('L',?,?,?,?)", (seq, f"S{seq}", secs, secs))
    return gtfs_to_epoch(SD, 18 * 3600)


def long_trip(stops):
    return snap(trip("L", [(seq, f"S{seq}", ts) for seq, ts in stops], route="GREEN"))


def honest(base, seqs):
    return [(s, base + s * 120 + 30) for s in seqs]  # every stop 30 s late


def bulk_clear(conn, n_cleared):
    """MTD sets the first n stops to one instant, then drops them: 'departures' in the same second."""
    base = seed_long_trip(conn)
    c = Collector(conn)
    c.process(long_trip(honest(base, range(1, 16))), base)
    now = base + 20
    c.process(long_trip([(s, now) for s in range(1, n_cleared + 1)] + honest(base, range(n_cleared + 1, 16))), now)
    c.process(long_trip(honest(base, range(n_cleared + 1, 16))), now + 20)
    return c


def test_bulk_clear_of_eleven_stops_is_rejected(conn):
    c = bulk_clear(conn, 11)
    assert c.last_collapsed == 11
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 0


def test_ten_stops_sharing_a_second_are_kept(conn):
    bulk_clear(conn, 10)
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 10


def test_bulk_clear_straddling_two_polls_is_rejected(conn):
    base = seed_long_trip(conn)
    c = Collector(conn)
    c.process(long_trip(honest(base, range(1, 16))), base)
    now = base + 20
    c.process(long_trip([(s, now) for s in range(1, 13)] + honest(base, range(13, 16))), now)
    c.process(long_trip([(s, now) for s in range(7, 13)] + honest(base, range(13, 16))), now + 20)
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 6  # not yet 11
    c.process(long_trip(honest(base, range(13, 16))), now + 40)  # 6 more at the same second: 12
    assert c.last_collapsed == 12
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 0


def test_stops_relisted_after_trip_vanished_are_retracted(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    full = [(1, "A", base + 60), (2, "B", base + 360), (3, "C", base + 660)]
    c.process(snap(trip("T1", full)), base)
    c.process(snap(), base + 20)  # trip vanishes; stop 1 was due, so it is recorded
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 1
    c.process(snap(), base + 40)
    c.process(snap(trip("T1", full)), base + 60)  # ...and comes back with stop 1 still to come
    assert c.last_retracted == 1
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 0


def test_retraction_memory_expires(conn):
    seed_static(conn)
    c = Collector(conn)
    base = gtfs_to_epoch(SD, 18 * 3600)
    c.process(snap(trip("T1", [(1, "A", base + 60), (2, "B", base + 360)])), base)
    c.process(snap(), base + 80)
    assert ("T1", SD) in c.observed
    c.process(snap(), base + 80 + config.RETRACT_MEMORY_S + 20)
    assert c.observed == {}
