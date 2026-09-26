"""Predictor registry. To add a model: implement base.Predictor and add it to PREDICTORS."""
import sqlite3

from .average_delay import AverageDelayPredictor
from .base import Prediction, PredictionRequest, Predictor

PREDICTORS = {
    AverageDelayPredictor.name: AverageDelayPredictor,
}
DEFAULT_PREDICTOR = AverageDelayPredictor.name


def get_predictor(name: str, conn: sqlite3.Connection) -> Predictor:
    try:
        return PREDICTORS[name](conn)
    except KeyError:
        raise ValueError(f"Unknown model {name!r}. Available: {sorted(PREDICTORS)}") from None


__all__ = ["Prediction", "PredictionRequest", "Predictor", "PREDICTORS", "DEFAULT_PREDICTOR",
           "get_predictor"]
