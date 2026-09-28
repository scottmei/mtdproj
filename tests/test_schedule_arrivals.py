from busapp.arrivals import build_board, classify
from busapp.predictors import Prediction
from busapp.realtime import FeedSnapshot, StopRT, TripRT
from busapp.schedule import ScheduledVisit, scheduled_visits, stop_group
from busapp.timeutil import gtfs_to_epoch

SD = "20260925"


def seed(conn):
    conn.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?)", [
        ("IT", "IT", "1", "Illinois Terminal", 0, 0),
        ("IT:1", "IT", "1", "Illinois Terminal (Platform A)", 0, 0),
        ("IT:2", "IT", "1", "Illinois Terminal (Platform B)", 0, 0),
        ("GWN:1", "GWN", "2", "Goodwin & Green (NE Corner)", 0, 0),
        ("Z:1", "Z", "3", "End", 0, 0),
    ])
    conn.execute("INSERT INTO routes VALUES ('TEAL','12','Teal','006991','ffffff')")
    conn.executemany("INSERT INTO trips VALUES (?,?,?,?,?,?)", [
        ("RUNS", "TEAL", "S_FRI", 0, "North", "B"),
        ("LATE", "TEAL", "S_THU", 0, "North", "B"),   # Thursday service running past midnight
        ("OFF", "TEAL", "S_SAT", 0, "North", "B"),    # not running Friday
        ("ENDS", "TEAL", "S_FRI", 0, "To IT", "B"),   # terminates at IT
    ])
    conn.executemany("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                     "VALUES (?,?,?,?,?)", [
        ("RUNS", 1, "IT:1", 18 * 3600, 18 * 3600), ("RUNS", 2, "Z:1", 18 * 3600 + 600, 18 * 3600 + 600),
        ("LATE", 1, "IT:2", 24 * 3600 + 600, 24 * 3600 + 600), ("LATE", 2, "Z:1", 25 * 3600, 25 * 3600),
        ("OFF", 1, "IT:1", 18 * 3600, 18 * 3600), ("OFF", 2, "Z:1", 19 * 3600, 19 * 3600),
        ("ENDS", 1, "Z:1", 17 * 3600, 17 * 3600), ("ENDS", 2, "IT:1", 18 * 3600, 18 * 3600),
    ])
    conn.executemany("INSERT INTO calendar_dates VALUES (?,?,1)", [
        ("S_FRI", "20260925"), ("S_THU", "20260924"), ("S_SAT", "20260926")])
    conn.commit()


def test_stop_group(conn):
    seed(conn)
    assert stop_group(conn, "IT") == ("Illinois Terminal", ["IT:1", "IT:2"])
    assert stop_group(conn, "GWN") == ("Goodwin & Green", ["GWN:1"])
    assert stop_group(conn, "NOPE") is None


def test_scheduled_visits_respects_calendar_and_skips_terminating_trips(conn):
    seed(conn)
    t = gtfs_to_epoch(SD, 18 * 3600)
    visits = scheduled_visits(conn, ["IT:1", "IT:2"], t - 60, t + 60)
    assert [(v.trip_id, v.scheduled_ts) for v in visits] == [("RUNS", t)]


def test_scheduled_visits_past_midnight_uses_previous_service_day(conn):
    seed(conn)
    t = gtfs_to_epoch("20260924", 24 * 3600 + 600)  # 00:10 on Friday
    visits = scheduled_visits(conn, ["IT:2"], t - 60, t + 60)
    assert [(v.trip_id, v.service_date) for v in visits] == [("LATE", "20260924")]


def visit(trip_id, seq, sched):
    return ScheduledVisit(trip_id, SD, seq, "IT:1", "Platform A", "TEAL", 0, "North", "12", "Teal",
                          "006991", "ffffff", sched)


def feed(*trips):
    return FeedSnapshot(0, {(t.trip_id, t.start_date): t for t in trips})


def rt(trip_id, stops, canceled=False):
    t = TripRT(trip_id, SD, "TEAL", "V9", canceled)
    for seq, ts in stops:
        t.stops[seq] = StopRT(seq, "IT:1", ts)
    return t


def test_classify():
    v = visit("T", 5, 1000)
    assert classify(v, None) == ("scheduled", None, None)
    assert classify(v, feed(rt("T", [(5, 1100), (6, 1200)]))) == ("live", 1100, "V9")
    assert classify(v, feed(rt("T", [(7, 1300)])))[0] == "departed"
    assert classify(v, feed(rt("T", [], canceled=True)))[0] == "canceled"


class FixedDelay:
    name = "fixed"

    def __init__(self, delay):
        self.delay = delay

    def predict_many(self, reqs):
        return [Prediction(r.scheduled_ts + self.delay, self.delay, "fixed", "test", 1) for r in reqs]


def test_board_filters_departed_and_sorts_by_best_estimate():
    now = 10_000
    visits = [
        visit("GONE", 5, now + 60),        # RT says bus already passed -> dropped
        visit("LIVE", 5, now + 120),       # MTD says now+900
        visit("SCHED", 5, now + 300),      # untracked: ours = sched + 60 = now+360
        visit("PAST", 5, now - 600),       # untracked, predicted time long gone -> dropped
        visit("LATE", 5, now - 600),       # tracked, stop still ahead, MTD says now+30
    ]
    snap = feed(rt("GONE", [(8, now + 500)]), rt("LIVE", [(5, now + 900)]), rt("LATE", [(5, now + 30)]))
    rows = build_board(visits, snap, FixedDelay(60), now)
    assert [r.trip_id for r in rows] == ["LATE", "SCHED", "LIVE"]
    live = rows[2]
    assert (live.status, live.mtd_ts, live.predicted_ts, live.minutes_away) == ("live", now + 900, now + 180, 15)
    assert rows[1].status == "scheduled" and rows[1].mtd_ts is None and rows[1].best_ts == now + 360


def test_visit_uses_sign_shown_at_that_stop(conn):
    seed(conn)
    conn.execute("UPDATE stop_times SET stop_headsign='North to Transit Plaza' "
                 "WHERE trip_id='RUNS' AND stop_sequence=1")
    conn.commit()
    t = gtfs_to_epoch(SD, 18 * 3600)
    (v,) = scheduled_visits(conn, ["IT:1"], t - 60, t + 60)
    assert v.headsign == "North to Transit Plaza"          # mid-trip sign wins
    conn.execute("UPDATE stop_times SET stop_headsign=NULL")
    (v,) = scheduled_visits(conn, ["IT:1"], t - 60, t + 60)
    assert v.headsign == "North"                            # falls back to the trip's sign


import pytest  # noqa: E402

from busapp.schedule import boarding_label  # noqa: E402


@pytest.mark.parametrize("name,label", [
    ("Goodwin & Gregory (NE Corner)", "NE Corner"),
    ("Green & Sixth (NE)", "NE Corner"),
    ("Neil & Kirby (SE Far)", "SE Far Side"),
    ("Florida & Lincoln (South)", "South Side"),
    ("Main & Race (SS)", "South Side"),
    ("First & Gregory (E)", "East Side"),
    ("Kirby & Neil (N. Side)", "North Side"),
    ("Springfield & Mattis (East side)", "East Side"),
    ("Illini Union (Island Shelter)", "Island Shelter"),
    ("Illinois Terminal (Platform A)", "Platform A"),
    ("Some Road (1101)", None),
    ("First at iHotel", None),
])
def test_boarding_label_is_spelled_out(name, label):
    assert boarding_label(name) == label


def test_board_rows_carry_boarding_label():
    v = ScheduledVisit("T", SD, 5, "GWNGRG:3", "Goodwin & Gregory (SS)", "TEAL", 0, "East", "12",
                       "Teal", "006991", "ffffff", 10_000)
    (row,) = build_board([v], None, FixedDelay(0), 9_000)
    assert row.boarding == "South Side"
