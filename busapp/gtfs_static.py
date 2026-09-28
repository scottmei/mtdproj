"""Load a static GTFS feed (directory of .txt files) into SQLite."""
import csv
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from .timeutil import parse_date, parse_gtfs_time

STATIC_TABLES = ("stops", "routes", "trips", "stop_times", "calendar_dates")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _rows(gtfs_dir: Path, name: str) -> Iterator[dict]:
    path = gtfs_dir / name
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def base_id(stop_id: str) -> str:
    """'IT:1' -> 'IT'. Boarding points share the part before the colon."""
    return stop_id.split(":", 1)[0]


def _calendar_rows(gtfs_dir: Path) -> dict[tuple[str, str], int]:
    """(date, service_id) -> exception_type, expanding calendar.txt then applying calendar_dates.txt."""
    out: dict[tuple[str, str], int] = {}
    for r in _rows(gtfs_dir, "calendar.txt"):
        flags = [r[d] == "1" for d in WEEKDAYS]
        if not any(flags):
            continue  # MTD's calendar.txt is all zeros; service comes from calendar_dates
        d, end = parse_date(r["start_date"]), parse_date(r["end_date"])
        while d <= end:
            if flags[d.weekday()]:
                out[(d.strftime("%Y%m%d"), r["service_id"])] = 1
            d += timedelta(days=1)
    for r in _rows(gtfs_dir, "calendar_dates.txt"):
        out[(r["date"], r["service_id"])] = int(r["exception_type"])
    return out


def load_gtfs(conn: sqlite3.Connection, gtfs_dir: Path) -> dict[str, int]:
    """Replace all static tables with the contents of `gtfs_dir`. Returns row counts."""
    gtfs_dir = Path(gtfs_dir)
    with conn:  # single transaction
        for t in STATIC_TABLES:
            conn.execute(f"DELETE FROM {t}")

        conn.executemany(
            "INSERT INTO stops VALUES (?,?,?,?,?,?)",
            (
                (r["stop_id"], base_id(r["stop_id"]), r.get("stop_code"), r["stop_name"],
                 float(r["stop_lat"] or 0), float(r["stop_lon"] or 0))
                for r in _rows(gtfs_dir, "stops.txt")
            ),
        )
        conn.executemany(
            "INSERT INTO routes VALUES (?,?,?,?,?)",
            (
                (r["route_id"], r.get("route_short_name"), r.get("route_long_name"),
                 r.get("route_color") or "888888", r.get("route_text_color") or "000000")
                for r in _rows(gtfs_dir, "routes.txt")
            ),
        )
        conn.executemany(
            "INSERT INTO trips VALUES (?,?,?,?,?,?)",
            (
                (r["trip_id"], r["route_id"], r["service_id"],
                 int(r["direction_id"]) if r.get("direction_id") else None,
                 r.get("trip_headsign"), r.get("block_id"))
                for r in _rows(gtfs_dir, "trips.txt")
            ),
        )
        conn.executemany(
            "INSERT INTO stop_times VALUES (?,?,?,?,?,?)",
            (
                (r["trip_id"], int(r["stop_sequence"]), r["stop_id"],
                 parse_gtfs_time(r["arrival_time"]), parse_gtfs_time(r["departure_time"]),
                 (r.get("stop_headsign") or "").strip() or None)
                for r in _rows(gtfs_dir, "stop_times.txt")
                if r["arrival_time"] and r["departure_time"]
            ),
        )
        conn.executemany(
            "INSERT INTO calendar_dates VALUES (?,?,?)",
            ((sid, d, et) for (d, sid), et in _calendar_rows(gtfs_dir).items()),
        )
        feed = next(_rows(gtfs_dir, "feed_info.txt"), {})
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('feed_version', ?)", (feed.get("feed_version", ""),)
        )

    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in STATIC_TABLES}
