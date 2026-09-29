"""Remove false departures (re-listed stops and bulk clears) from observed_departures.

Usage: python scripts/remove_false_departures.py            # dry run: report only
       python scripts/remove_false_departures.py --apply    # back up data/bus.db, then delete
"""
import argparse
import collections
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from busapp import config, db  # noqa: E402
from busapp.cleanup import delete_rows, find_false_departures  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete the rows (after backing up the database)")
    args = ap.parse_args()

    conn = db.connect()
    found = find_false_departures(conn)
    total = conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0]
    by_reason = collections.Counter(found.values())
    print(f"{len(found):,} false departures of {total:,} rows ({100 * len(found) / max(total, 1):.2f}%): "
          + ", ".join(f"{n:,} {reason}" for reason, n in by_reason.most_common()))
    if not args.apply or not found:
        print("Dry run; pass --apply to delete." if found else "Nothing to delete.")
        return

    backup = config.DB_PATH.with_name(f"{config.DB_PATH.stem}.pre-cleanup-{time.strftime('%Y%m%d-%H%M%S')}.db")
    with sqlite3.connect(backup) as dst:
        conn.backup(dst)  # consistent copy even while the collector is writing
    print(f"Backed up to {backup}")
    print(f"Deleted {delete_rows(conn, found):,} rows.")


if __name__ == "__main__":
    main()
