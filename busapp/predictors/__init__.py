"""Predictor registry. To add a model: implement base.Predictor and add it to PREDICTORS."""
import sqlite3

from .average_delay import AverageDelayPredictor
from .base import Prediction, PredictionRequest, Predictor
from .shrunk_median import ShrunkMedianPredictor

PREDICTORS = {
    AverageDelayPredictor.name: AverageDelayPredictor,
    ShrunkMedianPredictor.name: ShrunkMedianPredictor,
}
DEFAULT_PREDICTOR = AverageDelayPredictor.name


def get_predictor(name: str, conn: sqlite3.Connection, **options) -> Predictor:
    try:
        cls = PREDICTORS[name]
    except KeyError:
        raise ValueError(f"Unknown model {name!r}. Available: {sorted(PREDICTORS)}") from None
    return cls(conn, **options)


__all__ = ["Prediction", "PredictionRequest", "Predictor", "PREDICTORS", "DEFAULT_PREDICTOR",
           "get_predictor"]
