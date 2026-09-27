from busapp.coverage import capture_rates, coverage_report, find_gaps
from busapp.timeutil import gtfs_to_epoch

from .test_schedule_arrivals import seed

FRI, THU = "20260925", "20260924"


def t(date, h, m=0):
    return gtfs_to_epoch(date, h * 3600 + m * 60)


def add_polls(conn, start, end, error=None):
    conn.executemany("INSERT INTO polls VALUES (?,?,?,?,?)",
                     [(ts, ts, 10, 0, error) for ts in range(start, end + 1, 20)])
    conn.commit()


def test_gap_during_service_counts_missed_departures(conn):
    seed(conn)  # Friday stop times: ENDS Z:1 17:00, IT:1 18:00 (its last stop); RUNS IT:1 18:00, Z:1 18:10
    add_polls(conn, t(FRI, 16), t(FRI, 16, 30))
    add_polls(conn, t(FRI, 18, 30), t(FRI, 18, 40))
    (g,) = find_gaps(conn, now_ts=t(FRI, 18, 40))
    assert (g.start_ts, g.end_ts, g.ongoing) == (t(FRI, 16, 30), t(FRI, 18, 30), False)
    assert g.missed_service and g.scheduled_stop_times == 4  # final stops are observed too
    assert (g.first_scheduled_ts, g.last_scheduled_ts) == (t(FRI, 17), t(FRI, 18, 10))


def test_overnight_gap_without_service_is_harmless(conn):
    seed(conn)
    add_polls(conn, t(FRI, 2), t(FRI, 2, 10))
    add_polls(conn, t(FRI, 5), t(FRI, 5, 10))
    (g,) = find_gaps(conn, now_ts=t(FRI, 5, 10))
    assert not g.missed_service and g.scheduled_stop_times == 0


def test_gap_after_midnight_includes_previous_service_day(conn):
    seed(conn)  # Thursday's LATE trip departs IT:2 at 24:10 (Fri 00:10)
    add_polls(conn, t(THU, 23, 50), t(THU, 23, 55))
    add_polls(conn, t(FRI, 0, 30), t(FRI, 0, 35))
    (g,) = find_gaps(conn, now_ts=t(FRI, 0, 35))
    assert g.scheduled_stop_times == 1 and g.first_scheduled_ts == t(THU, 24, 10)


def test_failed_polls_count_as_gap_and_ongoing_gap_reported(conn):
    seed(conn)
    add_polls(conn, t(FRI, 16), t(FRI, 16, 30))
    add_polls(conn, t(FRI, 16, 31), t(FRI, 18, 30), error="HTTP 500")  # feed down
    gaps = find_gaps(conn, now_ts=t(FRI, 18, 30))
    assert len(gaps) == 1 and gaps[0].ongoing and gaps[0].scheduled_stop_times == 4


def test_report_and_cache(conn):
    seed(conn)
    add_polls(conn, t(FRI, 2), t(FRI, 2, 10))
    add_polls(conn, t(FRI, 5), t(FRI, 16, 30))
    add_polls(conn, t(FRI, 18, 30), t(FRI, 18, 40))
    cache = {}
    rep = coverage_report(conn, now_ts=t(FRI, 18, 40), cache=cache)
    assert (rep["gaps"], rep["gaps_missing_service"], rep["missed_expected_observations"]) == (2, 1, 4)
    assert rep["gap_list"][0]["missed_service"] is True  # newest first
    assert sorted(k[0] for k in cache) == ["rates", "sched", "sched"]  # rates + 2 closed gaps
    assert coverage_report(conn, now_ts=t(FRI, 18, 40), cache=cache) == rep


def test_untracked_hours_are_not_flagged(conn):
    seed(conn)
    add_polls(conn, t(FRI, 16), t(FRI, 16, 30))
    add_polls(conn, t(FRI, 18, 30), t(FRI, 18, 40))
    (g,) = find_gaps(conn, now_ts=t(FRI, 18, 40), rates={17: 0.0, 18: 0.0})
    assert g.scheduled_stop_times == 4 and g.expected_observations == 0 and not g.missed_service
    (g,) = find_gaps(conn, now_ts=t(FRI, 18, 40), rates={17: 1.0, 18: 0.25})
    assert g.expected_observations == 2 and g.missed_service  # 1*1.0 + 3*0.25 = 1.75


def test_capture_rate_learned_from_fully_polled_hours(conn):
    seed(conn)  # hour 18 on Friday has 3 scheduled stop times
    add_polls(conn, t(FRI, 18), t(FRI, 19))
    sched = t(FRI, 18)
    conn.execute("INSERT INTO observed_departures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("RUNS", FRI, 1, "IT:1", "TEAL", 0, "V", sched, sched + 60, 60, 18, "weekday", 20))
    conn.commit()
    rates = capture_rates(conn, now_ts=t(FRI, 19, 30), min_scheduled=1)
    assert rates == {18: 1 / 3}
    assert capture_rates(conn, now_ts=t(FRI, 19, 30)) == {}  # too few samples to trust
