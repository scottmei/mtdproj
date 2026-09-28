"""SQLite connection helpers. WAL mode lets the collector write while the web app reads."""
import sqlite3
from pathlib import Path

from . import config

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
