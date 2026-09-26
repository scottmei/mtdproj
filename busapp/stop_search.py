"""Stop search: MTD REST API first, local GTFS stops as the fallback."""
import logging
import re
import sqlite3

from .gtfs_static import base_id
from .mtd_rest import MtdRestClient

log = logging.getLogger(__name__)


def _group_name(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", name)


def local_search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """Every word must appear in the stop name, or the query matches a stop code / id exactly."""
    q = query.strip()
    words = [w for w in re.split(r"[\s&/,]+", q) if w]
    if not words:
        return []
    like = " AND ".join("stop_name LIKE ?" for _ in words)
    rows = conn.execute(
        f"""SELECT base_id, MIN(stop_name) AS name, MIN(stop_code) AS code
            FROM stops
            WHERE ({like}) OR stop_code = ? OR base_id = UPPER(?)
            GROUP BY base_id
            ORDER BY (MIN(stop_name) LIKE ?) DESC, LENGTH(MIN(stop_name))
            LIMIT ?""",
        (*[f"%{w}%" for w in words], q, q, f"{q}%", limit),
    ).fetchall()
    return [{"id": r["base_id"], "name": _group_name(r["name"]), "detail": f"Stop code {r['code']}",
             "source": "local"} for r in rows]


def search_stops(conn: sqlite3.Connection, rest: MtdRestClient | None, query: str,
                 limit: int = 10) -> list[dict]:
    if rest is not None and rest.enabled:
        try:
            known = {r[0] for r in conn.execute("SELECT DISTINCT base_id FROM stops")}
            out, seen = [], set()
            for r in rest.search_stops(query):
                bid = base_id(str(r.get("stopId", "")))
                if bid in known and bid not in seen:  # only stops our schedule knows about
                    seen.add(bid)
                    out.append({"id": bid, "name": r.get("name") or bid,
                                "detail": r.get("city") or "", "source": "mtd"})
            if out:
                return out[:limit]
        except Exception as e:  # never let search break the page
            log.warning("REST stop search failed, using local search: %s", e)
    return local_search(conn, query, limit)
