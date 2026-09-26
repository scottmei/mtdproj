"""Scheduled visits to a stop, from static GTFS."""
import re
import sqlite3
from dataclasses import dataclass

from .timeutil import service_dates_around, service_day_origin


@dataclass(frozen=True)
class ScheduledVisit:
    trip_id: str
    service_date: str
    stop_sequence: int
    stop_id: str
    platform: str
    route_id: str
    direction_id: int | None
    headsign: str | None
    route_short_name: str | None
    route_long_name: str | None
    route_color: str
    route_text_color: str
    scheduled_ts: int


def stop_group(conn: sqlite3.Connection, base_id: str) -> tuple[str, list[str]] | None:
    """(display name, boarding-point stop_ids) for a stop the rider picked, e.g. 'IT'."""
    rows = conn.execute(
        "SELECT stop_id, stop_name FROM stops WHERE base_id = ? ORDER BY stop_id", (base_id,)
    ).fetchall()
    if not rows:
        return None
    parent = next((r["stop_name"] for r in rows if r["stop_id"] == base_id), None)
    name = parent or re.sub(r"\s*\([^)]*\)\s*$", "", rows[0]["stop_name"])
    return name, [r["stop_id"] for r in rows if r["stop_id"] != base_id or len(rows) == 1]


_VISITS_SQL = """
SELECT st.trip_id, st.stop_sequence, st.stop_id, st.departure_s,
       s.stop_name, t.route_id, t.direction_id, t.headsign,
       r.short_name, r.long_name, r.color, r.text_color
FROM stop_times st
JOIN trips t           ON t.trip_id = st.trip_id
JOIN calendar_dates cd ON cd.service_id = t.service_id AND cd.date = ? AND cd.exception_type = 1
JOIN routes r          ON r.route_id = t.route_id
JOIN stops s           ON s.stop_id = st.stop_id
WHERE st.stop_id IN ({placeholders})
  AND st.departure_s BETWEEN ? AND ?
  -- skip drop-off-only visits: the trip ends here, so nobody can board it
  AND st.stop_sequence < (SELECT MAX(stop_sequence) FROM stop_times x WHERE x.trip_id = st.trip_id)
"""


def scheduled_visits(conn: sqlite3.Connection, stop_ids: list[str],
                     start_ts: int, end_ts: int) -> list[ScheduledVisit]:
    """Visits to any of `stop_ids` scheduled within [start_ts, end_ts], across service days."""
    sql = _VISITS_SQL.format(placeholders=",".join("?" * len(stop_ids)))
    out: list[ScheduledVisit] = []
    for sd in sorted(set(service_dates_around(start_ts) + service_dates_around(end_ts))):
        origin = service_day_origin(sd)
        for r in conn.execute(sql, (sd, *stop_ids, start_ts - origin, end_ts - origin)):
            out.append(ScheduledVisit(
                r["trip_id"], sd, r["stop_sequence"], r["stop_id"], r["stop_name"], r["route_id"],
                r["direction_id"], r["headsign"], r["short_name"], r["long_name"],
                r["color"] or "888888", r["text_color"] or "000000", origin + r["departure_s"]))
    return out
