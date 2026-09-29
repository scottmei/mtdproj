"""SQLite connection helpers. WAL mode lets the collector write while the web app reads."""
import sqlite3
from pathlib import Path

from . import config
from .timeutil import shift_date

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = config.DB_PATH if path is None else path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to date without losing collected data."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stop_times)")}
    if "stop_headsign" not in cols:  # added 2026-09-28; re-run load_gtfs.py to fill it
        conn.execute("ALTER TABLE stop_times ADD COLUMN stop_headsign TEXT")
    _fix_overnight_service_dates(conn)


def _fix_overnight_service_dates(conn: sqlite3.Connection) -> None:
    """One-off (2026-09-29): before realtime.to_service_dates existed, snapshots of trips
    timetabled after 24:00 were stored under MTD's start_date, one day after their service
    date. Move them back. (Their observed departures were never stored: the day-sized
    'delay' failed the MAX_ABS_DELAY_S check.)"""
    done = "SELECT 1 FROM meta WHERE key = 'overnight_dates_fixed'"
    if conn.execute(done).fetchone():
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # the web app and collector may both start up at once
    if not conn.execute(done).fetchone():
        overnight = ("SELECT trip_id FROM stop_times GROUP BY trip_id "
                     "HAVING MIN(departure_s) >= 86400")
        rows = conn.execute(f"SELECT * FROM mtd_predictions WHERE trip_id IN ({overnight})").fetchall()
        conn.execute(f"DELETE FROM mtd_predictions WHERE trip_id IN ({overnight})")
        conn.executemany("INSERT INTO mtd_predictions VALUES (?,?,?,?,?,?)",
                         [(r[0], shift_date(r[1], -1), *tuple(r)[2:]) for r in rows])
        conn.execute("INSERT INTO meta VALUES ('overnight_dates_fixed', ?)", (str(len(rows)),))
    conn.commit()
