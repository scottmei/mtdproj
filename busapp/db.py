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
    conn.commit()
