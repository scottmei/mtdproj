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


_COMPASS = {"N": "North", "S": "South", "E": "East", "W": "West"}
_CORNERS = ("NE", "NW", "SE", "SW")


def boarding_label(stop_name: str) -> str | None:
    """Where on the street a boarding point is, written out consistently.

    MTD names the same kind of spot 45 different ways: '(NE)', '(NE Corner)', '(SE Far)',
    '(SS)', '(East side)', '(South)'. Riders need one vocabulary: 'NE Corner', 'SE Far Side',
    'South Side', 'Island Shelter', 'Platform A'. None when the name has no location.
    """
    m = re.search(r"\(([^)]*)\)\s*$", stop_name)
    if not m or not m[1].strip():
        return None
    raw = " ".join(m[1].split())
    if raw.isdigit():                      # e.g. '(1101)': a street number, not a location
        return None
    side = re.fullmatch(r"([NSEW])\.? ?Side", raw)
    if side:                               # 'N. Side'
        return f"{_COMPASS[side[1]]} Side"
    if raw in _CORNERS:
        return f"{raw} Corner"
    if raw in _COMPASS or raw in _COMPASS.values():
        return f"{_COMPASS.get(raw, raw)} Side"
    if len(raw) == 2 and raw[1] == "S" and raw[0] in _COMPASS:   # 'SS' = South Side
        return f"{_COMPASS[raw[0]]} Side"
    words = raw.split()
    if len(words) == 2 and words[0] in _CORNERS and words[1] == "Far":
        return f"{words[0]} Far Side"
    # otherwise keep MTD's wording, capitalized consistently ('East side' -> 'East Side')
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


def _group_name(base_id: str, rows: list) -> str:
    """Parent stop's name if there is one, else the first platform's name minus '(NE Corner)'."""
    parent = next((r["stop_name"] for r in rows if r["stop_id"] == base_id), None)
    return parent or re.sub(r"\s*\([^)]*\)\s*$", "", rows[0]["stop_name"])


def stop_group(conn: sqlite3.Connection, base_id: str) -> tuple[str, list[str]] | None:
    """(display name, boarding-point stop_ids) for a stop the rider picked, e.g. 'IT'."""
    rows = conn.execute(
        "SELECT stop_id, stop_name FROM stops WHERE base_id = ? ORDER BY stop_id", (base_id,)
    ).fetchall()
    if not rows:
        return None
    return _group_name(base_id, rows), [r["stop_id"] for r in rows
                                        if r["stop_id"] != base_id or len(rows) == 1]


def stop_group_names(conn: sqlite3.Connection) -> dict[str, str]:
    """{base_id: display name} for every stop group, named the same way as stop_group()."""
    groups: dict[str, list] = {}
    for r in conn.execute("SELECT base_id, stop_id, stop_name FROM stops ORDER BY stop_id"):
        groups.setdefault(r["base_id"], []).append(r)
    return {bid: _group_name(bid, rows) for bid, rows in groups.items()}


_VISITS_SQL = """
SELECT st.trip_id, st.stop_sequence, st.stop_id, st.departure_s,
       s.stop_name, t.route_id, t.direction_id,
       COALESCE(st.stop_headsign, t.headsign) AS headsign,  -- MTD changes signs mid-trip
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
