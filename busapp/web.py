"""FastAPI app: stop search, arrivals board, health. Serves the single-page UI from /static."""
import hashlib
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .accuracy import ScoreCache, backtest
from .arrivals import RealtimeCache, arrivals_for_stop
from .breakdown import DIMENSIONS, breakdown
from .coverage import coverage_report
from .mtd_rest import MtdRestClient
from .predictors import DEFAULT_PREDICTOR, PREDICTORS, get_predictor
from .stop_search import search_stops

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
_ASSET_REF = re.compile(r'(/static/[\w.-]+\.(?:js|css))"')


def asset_version(name: str) -> str:
    """Short content hash of a static file; changes whenever the file does."""
    return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]


def render_page(name: str) -> str:
    """HTML with every script/stylesheet URL versioned (`app.js?v=<hash>`).

    Browsers that cached an older app.js before we sent Cache-Control would otherwise
    pair it with new HTML (e.g. old app.js + new common.js both declaring `$` -> the
    page's script dies). A new URL per content version makes that impossible.
    """
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    return _ASSET_REF.sub(lambda m: f'{m[1]}?v={asset_version(m[1].rsplit("/", 1)[1])}"', html)


def warm_predictors(predictors: dict, interval_s: int = 600) -> None:
    """Run forever: let models that precompute daily stats (shrunk_median) do so before the
    first request of the day instead of during it."""
    while True:
        for name, p in predictors.items():
            if hasattr(p, "warm"):
                try:
                    p.warm()
                except Exception as e:
                    log.warning("Warming %s failed: %s", name, e)
        time.sleep(interval_s)


def create_app(db_path=None, rest: MtdRestClient | None = None,
               rt_cache: RealtimeCache | None = None, background: bool = True) -> FastAPI:
    """`background=False` skips the keep-warm threads (tests)."""
    app = FastAPI(title="CU Bus Arrival Predictor",
                  description="Scheduled vs MTD live vs historical-delay predictions for CUMTD stops.")
    conn = db.connect(db_path)
    db.init_schema(conn)
    lock = threading.Lock()  # one shared SQLite connection; endpoints run in a threadpool
    # live models: own connections (a warm-up thread may compute while requests read) and a
    # small cache, since the live page only ever needs the latest cutoff
    predictors = {name: get_predictor(name, db.connect(db_path), cache_size=2) for name in PREDICTORS}
    rest = rest if rest is not None else MtdRestClient()
    rt_cache = rt_cache or RealtimeCache()
    scores = ScoreCache(db_path)  # own connection: scoring never blocks the arrivals board
    if background:
        if rest.enabled:
            threading.Thread(target=rest.keep_warm, name="mtd-rest-warm", daemon=True).start()
        threading.Thread(target=scores.keep_warm, name="score-warm", daemon=True).start()
        threading.Thread(target=warm_predictors, args=(predictors,), name="model-warm",
                         daemon=True).start()

    @app.middleware("http")
    async def revalidate_assets(request, call_next):
        """Without Cache-Control, browsers may reuse a stale style.css/app.js for hours after
        an update (heuristic freshness). `no-cache` = always revalidate; unchanged files cost
        only a 304 thanks to the ETag StaticFiles already sends."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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

    def scored_for(model: str, days: int):
        try:
            return scores.get(model, days)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/accuracy")
    def api_accuracy(model: str = DEFAULT_PREDICTOR, days: int = Query(7, ge=1, le=60)):
        """Backtest of schedule vs MTD live vs our model."""
        scored = scored_for(model, days)  # validates the model name
        return backtest(conn, predictors[model], days=days, scored=scored)

    @app.get("/api/breakdown")
    def api_breakdown(by: str = "line", model: str = DEFAULT_PREDICTOR,
                      days: int = Query(7, ge=1, le=60), horizon: int = 10,
                      min_n: int = Query(30, ge=1)):
        """Lateness and accuracy grouped by line, stop, hour, day_type or route."""
        if by not in DIMENSIONS:
            raise HTTPException(400, f"Unknown dimension {by!r}; choose from {sorted(DIMENSIONS)}")
        if horizon not in config.HORIZONS_MIN:
            raise HTTPException(400, f"horizon must be one of {list(config.HORIZONS_MIN)}")
        scored = scored_for(model, days)
        with lock:
            res = breakdown(conn, scored, by, horizon=horizon, min_n=min_n)
        return {"model": model, "days": days, **res}

    @app.get("/api/models")
    def api_models():
        return {"default": DEFAULT_PREDICTOR, "models": sorted(predictors)}

    gap_cache: dict = {}

    @app.get("/api/health")
    def api_health():
        now = int(time.time())
        with lock:
            last = conn.execute("SELECT MAX(poll_ts) FROM polls WHERE error IS NULL").fetchone()[0]
            n_obs = conn.execute("SELECT COUNT(*) FROM observed_departures").fetchone()[0]
            n_pred = conn.execute("SELECT COUNT(*) FROM mtd_predictions").fetchone()[0]
            first = conn.execute("SELECT MIN(observed_ts) FROM observed_departures").fetchone()[0]
            feed = conn.execute("SELECT value FROM meta WHERE key='feed_version'").fetchone()
            coverage = coverage_report(conn, now, cache=gap_cache)
        return {
            "collector_last_poll_ts": last,
            "collector_ok": last is not None and now - last < 3 * config.POLL_INTERVAL_S,
            "observations": n_obs,
            "mtd_prediction_snapshots": n_pred,
            "collecting_since_ts": first,
            "rest_search_enabled": rest.enabled,
            "gtfs_feed_version": feed[0] if feed else None,
            "coverage": coverage,
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return HTMLResponse(render_page("index.html"))

        @app.get("/insights", include_in_schema=False)
        def insights():
            return HTMLResponse(render_page("insights.html"))

    return app
