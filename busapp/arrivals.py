"""Combine schedule + MTD realtime + our prediction into an arrivals board for one stop."""
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass

import httpx

from . import config
from .predictors import Prediction, PredictionRequest, Predictor
from .realtime import FeedSnapshot, fetch_trip_updates
from .schedule import ScheduledVisit, boarding_label, scheduled_visits, stop_group

log = logging.getLogger(__name__)

REALTIME_STALE_S = 5 * 60  # a stop still in the feed but predicted >5 min ago is treated as stale
NON_RT_GRACE_S = 60


@dataclass
class ArrivalRow:
    trip_id: str
    route_id: str
    route_short_name: str | None
    route_long_name: str | None
    route_color: str
    route_text_color: str
    headsign: str | None
    stop_id: str
    platform: str
    boarding: str | None   # where on the street to board, e.g. 'NE Corner'
    scheduled_ts: int
    mtd_ts: int | None
    predicted_ts: int
    predicted_delay_s: float
    model_level: str
    model_n: int
    status: str            # 'live' | 'scheduled' | 'canceled'
    vehicle_id: str | None
    best_ts: int           # MTD live time when tracked, else our prediction
    minutes_away: int


def classify(visit: ScheduledVisit, snap: FeedSnapshot | None) -> tuple[str, int | None, str | None]:
    """(status, mtd_ts, vehicle_id); status is 'live', 'scheduled', 'canceled' or 'departed'."""
    trip = snap.trips.get((visit.trip_id, visit.service_date)) if snap else None
    if trip is None:
        return "scheduled", None, None
    if trip.canceled:
        return "canceled", None, None
    s = trip.stops.get(visit.stop_sequence)
    if s is not None:
        return "live", s.predicted_ts, trip.vehicle_id
    if trip.stops and visit.stop_sequence < trip.first_sequence:
        return "departed", None, trip.vehicle_id  # the bus already passed this stop
    return "scheduled", None, trip.vehicle_id


def build_board(visits: list[ScheduledVisit], snap: FeedSnapshot | None, predictor: Predictor,
                now_ts: int, limit: int = config.MAX_ARRIVALS) -> list[ArrivalRow]:
    classified = [(v, *classify(v, snap)) for v in visits]
    classified = [c for c in classified if c[1] != "departed"]
    preds: list[Prediction] = predictor.predict_many([
        PredictionRequest(v.trip_id, v.route_id, v.direction_id, v.stop_id, v.service_date,
                          v.scheduled_ts, now_ts, mtd_ts)
        for v, _, mtd_ts, _ in classified
    ])
    rows = []
    for (v, status, mtd_ts, vehicle), p in zip(classified, preds):
        if status == "live":
            best = mtd_ts
            if best < now_ts - REALTIME_STALE_S:
                continue
        elif status == "canceled":
            best = v.scheduled_ts
            if best < now_ts:
                continue
        else:
            best = p.predicted_ts
            if best < now_ts - NON_RT_GRACE_S:
                continue
        rows.append(ArrivalRow(
            v.trip_id, v.route_id, v.route_short_name, v.route_long_name, v.route_color,
            v.route_text_color, v.headsign, v.stop_id, v.platform, boarding_label(v.platform),
            v.scheduled_ts, mtd_ts,
            p.predicted_ts, round(p.delay_s, 1), p.level, p.n_samples, status, vehicle, best,
            max(0, (best - now_ts) // 60)))
    rows.sort(key=lambda r: (r.best_ts, r.scheduled_ts))
    return rows[:limit]


class RealtimeCache:
    """Shares one GTFS-RT fetch across requests; serves the last good snapshot on failure."""

    def __init__(self, ttl_s: int = config.RT_CACHE_S):
        self.ttl_s = ttl_s
        self._snap: FeedSnapshot | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=10)  # keep-alive across refreshes

    def get(self) -> tuple[FeedSnapshot | None, bool]:
        """(snapshot, is_fresh)."""
        with self._lock:
            if self._snap is None or time.time() - self._fetched_at > self.ttl_s:
                try:
                    self._snap = fetch_trip_updates(self._client)
                    self._fetched_at = time.time()
                except Exception as e:
                    log.warning("GTFS-RT fetch failed: %s", e)
                    return self._snap, False
            return self._snap, True


def arrivals_for_stop(conn: sqlite3.Connection, base_id: str, predictor: Predictor,
                      rt_cache: RealtimeCache, now_ts: int | None = None) -> dict | None:
    group = stop_group(conn, base_id)
    if group is None:
        return None
    name, stop_ids = group
    now_ts = int(time.time()) if now_ts is None else now_ts
    visits = scheduled_visits(conn, stop_ids, now_ts - config.WINDOW_PAST_S,
                              now_ts + config.WINDOW_FUTURE_S)
    snap, fresh = rt_cache.get()
    rows = build_board(visits, snap, predictor, now_ts)
    return {
        "stop": {"id": base_id, "name": name, "stop_ids": stop_ids},
        "now_ts": now_ts,
        "realtime_ok": fresh and snap is not None,
        "realtime_feed_ts": snap.feed_ts if snap else None,
        "model": predictor.name,
        "arrivals": [asdict(r) for r in rows],
    }
