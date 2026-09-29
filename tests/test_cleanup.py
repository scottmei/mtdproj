from busapp import config
from busapp.cleanup import delete_rows, find_false_departures

SD = "20260928"


def obs(conn, trip, seq, observed_ts):
    conn.execute("INSERT INTO observed_departures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (trip, SD, seq, f"S{seq}", "GREEN", 0, "V", 1000 + seq * 60, observed_ts,
                  observed_ts - 1000 - seq * 60, 18, "weekday", 20))


def test_finds_relisted_and_bulk_cleared_rows(conn):
    obs(conn, "OK", 1, 1100)
    obs(conn, "OK", 2, 1100)                       # two neighbouring stops sharing a second: fine
    conn.execute("INSERT INTO mtd_predictions VALUES ('OK', ?, 1, 2, 1110, 1100)", (SD,))  # same poll
    obs(conn, "RELIST", 5, 2000)
    conn.execute("INSERT INTO mtd_predictions VALUES ('RELIST', ?, 5, 10, 2300, 2900)", (SD,))
    for seq in range(1, config.COLLAPSE_MIN_STOPS + 1):
        obs(conn, "BULK", seq, 5000)
    found = find_false_departures(conn)
    assert found[("RELIST", SD, 5)] == "relisted"
    assert sum(r == "bulk clear" for r in found.values()) == config.COLLAPSE_MIN_STOPS
    assert not any(k[0] == "OK" for k in found)
    assert delete_rows(conn, found) == len(found)
    assert conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0] == 2
