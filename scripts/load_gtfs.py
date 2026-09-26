"""Create the database schema and load a static GTFS feed.

Usage: python scripts/load_gtfs.py [gtfs_dir]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from busapp import config, db  # noqa: E402
from busapp.gtfs_static import load_gtfs  # noqa: E402


def main() -> None:
    gtfs_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else config.GTFS_DIR
    conn = db.connect()
    db.init_schema(conn)
    t0 = time.perf_counter()
    counts = load_gtfs(conn, gtfs_dir)
    print(f"Loaded {gtfs_dir} into {config.DB_PATH} in {time.perf_counter() - t0:.1f}s")
    for table, n in counts.items():
        print(f"  {table:<15} {n:>8,}")


if __name__ == "__main__":
    main()
