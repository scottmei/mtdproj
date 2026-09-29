from google.transit import gtfs_realtime_pb2 as rt

from busapp.realtime import overnight_trips, parse_trip_updates, to_service_dates


def make_feed(trips, feed_ts=1000):
    """trips: list of (trip_id, start_date, route_id, [(seq, stop_id, ts)], canceled)."""
    msg = rt.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = feed_ts
    for i, (tid, sd, rid, stops, canceled) in enumerate(trips):
        e = msg.entity.add(id=str(i))
        tu = e.trip_update
        tu.trip.trip_id, tu.trip.start_date, tu.trip.route_id = tid, sd, rid
        if canceled:
            tu.trip.schedule_relationship = rt.TripDescriptor.CANCELED
        tu.vehicle.id = f"V{i}"
        for seq, sid, ts in stops:
            s = tu.stop_time_update.add(stop_sequence=seq, stop_id=sid)
            s.arrival.time = ts
            s.departure.time = ts
    return msg.SerializeToString()


def test_parse_trip_updates():
    data = make_feed([
        ("T1", "20260925", "TEAL", [(5, "A:1", 1100), (6, "B:1", 1200)], False),
        ("T2", "20260925", "RED", [], True),
    ])
    snap = parse_trip_updates(data)
    assert snap.feed_ts == 1000
    t1 = snap.trips[("T1", "20260925")]
    assert t1.route_id == "TEAL" and t1.vehicle_id == "V0" and t1.first_sequence == 5
    assert t1.stops[6].predicted_ts == 1200 and t1.stops[6].stop_id == "B:1"
    assert snap.trips[("T2", "20260925")].canceled


def test_overnight_trips_are_rekeyed_to_their_service_date(conn):
    # N1's first stop is 24:15 on its service day; D1 is an ordinary daytime trip
    conn.executemany("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                     "VALUES (?,?,?,?,?)",
                     [("N1", 1, "A", 87300, 87300), ("N1", 2, "B", 87400, 87400),
                      ("D1", 1, "A", 80000, 80000), ("D1", 2, "B", 90000, 90000)])
    overnight = overnight_trips(conn)
    assert overnight == {"N1"}  # D1 crosses midnight but starts before it: MTD dates it correctly
    snap = parse_trip_updates(make_feed([
        ("N1", "20260929", "ILLINI", [(2, "B:1", 1100)], False),  # MTD: calendar day it starts
        ("D1", "20260928", "TEAL", [(2, "B:1", 1200)], False),
    ]))
    fixed = to_service_dates(snap, overnight)
    assert set(fixed.trips) == {("N1", "20260928"), ("D1", "20260928")}
    assert fixed.trips[("N1", "20260928")].start_date == "20260928"
    assert fixed.trips[("N1", "20260928")].stops[2].predicted_ts == 1100
