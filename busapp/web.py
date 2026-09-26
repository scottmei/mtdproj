"""FastAPI app: stop search, arrivals board, health. Serves the single-page UI from /static."""
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .accuracy import backtest
from .arrivals import RealtimeCache, arrivals_for_stop
from .mtd_rest import MtdRestClient
from .predictors import DEFAULT_PREDICTOR, PREDICTORS, get_predictor
from .stop_search import search_stops

STATIC_DIR = Path(__file__).with_name("static")


def create_app(db_path=None, rest: MtdRestClient | None = None,
               rt_cache: RealtimeCache | None = None) -> FastAPI:
    app = FastAPI(title="CU Bus Arrival Predictor",
                  description="Scheduled vs MTD live vs historical-delay predictions for CUMTD stops.")
    conn = db.connect(db_path)
    db.init_schema(conn)
    lock = threading.Lock()  # one shared SQLite connection; endpoints run in a threadpool
    predictors = {name: get_predictor(name, conn) for name in PREDICTORS}
    rest = rest if rest is not None else MtdRestClient()
    rt_cache = rt_cache or RealtimeCache()

    @app.get("/api/stops/search")
    def api_search(q: str = Query(..., min_length=1, max_length=50)):
        with lock:
            return {"query": q, "results": search_stops(conn, rest, q)}

    @app.get("/api/stops/{stop_id}/arrivals")
    def api_arrivals(stop_id: str, model: str = DEFAULT_PREDICTOR):
        if model not in predictors:
            raise HTTPException(400, f"Unknown model {model!r}; choose from {sorted(predictors)}")
        rt_cache.get()  # network refresh outside the DB lock
        with lock:
            res = arrivals_for_stop(conn, stop_id.split(":")[0], predictors[model], rt_cache)
        if res is None:
            raise HTTPException(404, f"Unknown stop {stop_id!r}")
        return res

    accuracy_cache: dict = {}

    @app.get("/api/accuracy")
    def api_accuracy(model: str = DEFAULT_PREDICTOR, days: int = Query(7, ge=1, le=60)):
        """Backtest of schedule vs MTD live vs our model (cached for 10 minutes)."""
        if model not in predictors:
            raise HTTPException(400, f"Unknown model {model!r}")
        key = (model, days)
        hit = accuracy_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        with lock:
            res = backtest(conn, predictors[model], days=days)
        accuracy_cache[key] = (time.time(), res)
        return res

    @app.get("/api/models")
    def api_models():
        return {"default": DEFAULT_PREDICTOR, "models": sorted(predictors)}

    @app.get("/api/health")
    def api_health():
        with lock:
            last = conn.execute("SELECT MAX(poll_ts) FROM polls WHERE error IS NULL").fetchone()[0]
            n_obs = conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0]
            n_pred = conn.execute("SELECT COUNT(*) FROM mtd_predictions").fetchone()[0]
            first = conn.execute("SELECT MIN(observed_ts) FROM observed_departures").fetchone()[0]
            feed = conn.execute("SELECT value FROM meta WHERE key='feed_version'").fetchone()
        now = int(time.time())
        return {
            "collector_last_poll_ts": last,
            "collector_ok": last is not None and now - last < 3 * config.POLL_INTERVAL_S,
            "observations": n_obs,
            "mtd_prediction_snapshots": n_pred,
            "collecting_since_ts": first,
            "rest_search_enabled": rest.enabled,
            "gtfs_feed_version": feed[0] if feed else None,
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app
