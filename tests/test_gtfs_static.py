from busapp.gtfs_static import base_id, load_gtfs

FILES = {
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon\nIT:1,1,Illinois Terminal (A),40.1,-88.2\nIT:2,1,Illinois Terminal (B),40.1,-88.2\n",
    "routes.txt": "route_id,route_short_name,route_long_name,route_color,route_text_color\nTEAL,12,Teal,006991,ffffff\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id,block_id\nTEAL,S1,T1,North,0,B1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign\n"
                      "T1,24:59:00,25:00:00,IT:1,1,North to Transit Plaza\nT1,25:10:00,25:10:00,IT:2,2,\n",
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
    assert counts["stops"] == 2 and counts["stop_times"] == 2
    st = conn.execute("SELECT arrival_s, departure_s, stop_headsign FROM stop_times "
                      "ORDER BY stop_sequence").fetchall()
    assert tuple(st[0]) == (24 * 3600 + 59 * 60, 25 * 3600, "North to Transit Plaza")
    assert st[1]["stop_headsign"] is None  # blank means "same as the trip's headsign"
    cal = {(r["date"], r["service_id"]): r["exception_type"]
           for r in conn.execute("SELECT * FROM calendar_dates")}
    # S1 only via calendar_dates; S2 Mondays 9/7 and 9/14 from calendar.txt, 9/7 removed
    assert cal[("20260925", "S1")] == 1
    assert cal[("20260914", "S2")] == 1
    assert cal[("20260907", "S2")] == 2
    assert conn.execute("SELECT base_id FROM stops WHERE stop_id='IT:2'").fetchone()[0] == "IT"
    # reload is idempotent
    assert load_gtfs(conn, tmp_path) == counts


def test_migration_adds_stop_headsign_to_old_database(tmp_path):
    from busapp import db

    path = tmp_path / "old.db"
    old = db.connect(path)
    old.execute("CREATE TABLE stop_times (trip_id TEXT NOT NULL, stop_sequence INTEGER NOT NULL, "
                "stop_id TEXT NOT NULL, arrival_s INTEGER NOT NULL, departure_s INTEGER NOT NULL, "
                "PRIMARY KEY (trip_id, stop_sequence)) WITHOUT ROWID")
    old.execute("INSERT INTO stop_times VALUES ('T1', 1, 'A', 10, 10)")
    old.commit()
    db.init_schema(old)
    cols = [r[1] for r in old.execute("PRAGMA table_info(stop_times)")]
    assert cols[-1] == "stop_headsign"
    assert tuple(old.execute("SELECT * FROM stop_times").fetchone()) == ("T1", 1, "A", 10, 10, None)
    db.init_schema(old)  # idempotent


def test_migration_moves_overnight_snapshots_back_one_day(tmp_path):
    from busapp import db

    path = tmp_path / "old.db"
    old = db.connect(path)
    db.init_schema(old)
    old.execute("DELETE FROM meta WHERE key = 'overnight_dates_fixed'")  # as before the fix
    old.executemany("INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_s, departure_s) "
                    "VALUES (?,?,?,?,?)", [("N1", 1, "A", 87300, 87300), ("D1", 1, "A", 3600, 3600)])
    # N1 on two consecutive nights: moving one row onto the other's old date must not collide
    old.executemany("INSERT INTO mtd_predictions VALUES (?,?,?,?,?,?)",
                    [("N1", "20260928", 1, 5, 100, 200), ("N1", "20260929", 1, 5, 300, 400),
                     ("D1", "20260929", 1, 5, 500, 600)])
    old.commit()
    db.init_schema(old)
    db.init_schema(old)  # runs once only
    rows = sorted(tuple(r) for r in old.execute("SELECT trip_id, service_date, made_at FROM mtd_predictions"))
    assert rows == [("D1", "20260929", 500), ("N1", "20260927", 100), ("N1", "20260928", 300)]
