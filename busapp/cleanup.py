"""Find false departures recorded before the collector could catch them (see collector.py).

Two kinds, both undone live by the collector since 2026-09-29:
- 'relisted': MTD listed the stop again after we recorded it as departed. The proof is a
  horizon snapshot of MTD's prediction for that stop taken after our observed time.
- 'bulk clear': COLLAPSE_MIN_STOPS or more stops of one trip run share the same second.
"""
import sqlite3

from . import config

Key = tuple[str, str, int]  # trip_id, service_date, stop_sequence

RELISTED_SQL = """
SELECT DISTINCT o.trip_id, o.service_date, o.stop_sequence
FROM observed_departures o
JOIN mtd_predictions p ON p.trip_id = o.trip_id AND p.service_date = o.service_date
                      AND p.stop_sequence = o.stop_sequence
WHERE p.made_at > o.observed_ts + ?
"""

BULK_CLEAR_SQL = """
SELECT o.trip_id, o.service_date, o.stop_sequence
FROM observed_departures o
JOIN (SELECT trip_id, service_date, observed_ts FROM observed_departures
      GROUP BY trip_id, service_date, observed_ts HAVING COUNT(*) >= ?) g
  ON g.trip_id = o.trip_id AND g.service_date = o.service_date AND g.observed_ts = o.observed_ts
"""

RELIST_SLACK_S = 20  # a snapshot this soon after the observed time can be the same poll


def find_false_departures(conn: sqlite3.Connection) -> dict[Key, str]:
    """{row key: reason}; a row matching both rules is reported as 'relisted'."""
    found = {tuple(r): "bulk clear" for r in conn.execute(BULK_CLEAR_SQL, (config.COLLAPSE_MIN_STOPS,))}
    found.update({tuple(r): "relisted" for r in conn.execute(RELISTED_SQL, (RELIST_SLACK_S,))})
    return found


def delete_rows(conn: sqlite3.Connection, keys) -> int:
    with conn:
        before = conn.total_changes
        conn.executemany("DELETE FROM observed_departures "
                         "WHERE trip_id=? AND service_date=? AND stop_sequence=?", list(keys))
        return conn.total_changes - before
