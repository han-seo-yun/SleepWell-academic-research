"""Importable research transformers, including cross-process serialization."""
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin


class TrainQuantileClip(BaseEstimator, TransformerMixin):
    def fit(self, x, y=None):
        self.low_ = np.quantile(x, .01, axis=0)
        self.high_ = np.quantile(x, .99, axis=0)
        return self

    def transform(self, x):
        return np.clip(np.asarray(x, dtype=float), self.low_, self.high_)
