from google.transit import gtfs_realtime_pb2 as rt

from busapp.realtime import parse_trip_updates


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
