from busapp.gtfs_static import base_id, load_gtfs

FILES = {
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\nIT:1,1,Illinois Terminal (A),40.1,-88.2\nIT:2,1,Illinois Terminal (B),40.1,-88.2\n",
    "routes.txt": "route_id,route_short_name,route_long_name,route_color,route_text_color\nTEAL,12,Teal,006991,ffffff\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id,block_id\nTEAL,S1,T1,North,0,B1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,24:59:00,25:00:00,IT:1,1\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "S1,0,0,0,0,0,0,0,20260901,20260930\nS2,1,0,0,0,0,0,0,20260901,20260914\n",
    "calendar_dates.txt": "service_id,date,exception_type\nS1,20260925,1\nS2,20260907,2\n",
    "feed_info.txt": "feed_version\nv1\n",
}


def test_base_id():
    assert base_id("IT:1") == "IT"
    assert base_id("GWNGRG") == "GWNGRG"


def test_load_gtfs(conn, tmp_path):
    for name, text in FILES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    counts = load_gtfs(conn, tmp_path)
    assert counts["stops"] == 2 and counts["stop_times"] == 1
    st = conn.execute("SELECT arrival_s, departure_s FROM stop_times").fetchone()
    assert tuple(st) == (24 * 3600 + 59 * 60, 25 * 3600)
    cal = {(r["date"], r["service_id"]): r["exception_type"]
           for r in conn.execute("SELECT * FROM calendar_dates")}
    # S1 only via calendar_dates; S2 Mondays 9/7 and 9/14 from calendar.txt, 9/7 removed
    assert cal[("20260925", "S1")] == 1
    assert cal[("20260914", "S2")] == 1
    assert cal[("20260907", "S2")] == 2
    assert conn.execute("SELECT base_id FROM stops WHERE stop_id='IT:2'").fetchone()[0] == "IT"
    # reload is idempotent
    assert load_gtfs(conn, tmp_path) == counts
