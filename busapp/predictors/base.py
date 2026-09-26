"""Prediction interface. New models implement `Predictor` and register in predictors/__init__.py."""
from dataclasses import dataclass
from typing import Protocol


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
