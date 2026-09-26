"""Run the GTFS-RT collector until Ctrl-C.

Usage: python scripts/run_collector.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from busapp import config, db  # noqa: E402
from busapp.collector import Collector  # noqa: E402


def main() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.DB_PATH.parent / "collector.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll is enough
    conn = db.connect()
    db.init_schema(conn)
    Collector(conn).run_forever()


if __name__ == "__main__":
    main()
