"""Prediction interface. New models implement `Predictor` and register in predictors/__init__.py."""
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from ..timeutil import local_dt, service_day_origin


@dataclass(frozen=True)
class PredictionRequest:
    trip_id: str
    route_id: str
    direction_id: int | None
    stop_id: str
    service_date: str
    scheduled_ts: int
    now_ts: int                     # information cut-off: models may only use data from before this
    mtd_estimate_ts: int | None = None  # MTD's live estimate, for models that correct it


@dataclass(frozen=True)
class Prediction:
    predicted_ts: int
    delay_s: float
    method: str
    level: str       # which data the estimate came from, e.g. "route+hour"
    n_samples: int


class Predictor(Protocol):
    name: str

    def predict_many(self, reqs: list[PredictionRequest]) -> list[Prediction]: ...


class DailyStats:
    """Base for models built from history before the request's service day.

    Subclasses implement `_compute(cutoff)`. Its result is computed once per service day and
    cached: the same cutoff the backtest scores with, so the live model is the evaluated one.
    `warm()` lets a background thread compute today's before the first request, so a stop
    lookup never waits for it.
    """

    def __init__(self, cache_size: int = 8):
        self.cache_size = cache_size  # each entry holds every group's stats (~tens of MB)
        self._cache: OrderedDict[int, object] = OrderedDict()
        self._lock = threading.Lock()  # one computation per cutoff, even with a warm-up thread
        self._cutoffs: dict[int, int] = {}

    def _compute(self, cutoff: int):
        raise NotImplementedError

    def cutoff_for(self, now_ts: int) -> int:
        """Start of the service day `now_ts` falls in: history is used up to there."""
        hour = now_ts - now_ts % 3600  # local midnight is on an hour boundary, so memo by hour
        if hour not in self._cutoffs:
            if len(self._cutoffs) > 1000:
                self._cutoffs.clear()
            self._cutoffs[hour] = service_day_origin(local_dt(now_ts).strftime("%Y%m%d"))
        return self._cutoffs[hour]

    def stats(self, cutoff: int):
        with self._lock:
            if cutoff not in self._cache:
                self._cache[cutoff] = self._compute(cutoff)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(cutoff)
            return self._cache[cutoff]

    def warm(self, now_ts: int | None = None) -> None:
        """Compute today's stats ahead of the first request (called from a background thread)."""
        self.stats(self.cutoff_for(int(time.time()) if now_ts is None else now_ts))
