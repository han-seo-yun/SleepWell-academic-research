from __future__ import annotations

from numbers import Integral, Real
from typing import Any, Sequence

import numpy as np
import pandas as pd
from sklearn.exceptions import NotFittedError
from sklearn.impute import SimpleImputer
from sklearn.tree import DecisionTreeRegressor, export_text


GATE_GRID = np.array([0.0, 0.25, 0.5, 0.75, 1.0])

_HISTORY_COLUMNS = (
    "history_count_7",
    "history_age_days",
    "recent_count14",
    "bed_mad",
    "wake_mad",
    "gap_days_mean",
    "personal_weight",
)
CONTEXT_COLUMNS = (
    *_HISTORY_COLUMNS,
    "missing_fraction",
    "disagreement_bed",
    "disagreement_wake",
)


def _prediction_matrix(
    values: np.ndarray,
    name: str,
    *,
    expected_rows: int | None = None,
) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a numeric array with shape (n, 2)") from exc

    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"{name} must have shape (n, 2); got {array.shape}")
    if expected_rows is not None and array.shape[0] != expected_rows:
        raise ValueError(
            f"{name} must have {expected_rows} rows; got {array.shape[0]}"
        )
    if not np.isfinite(array).all():
        raise ValueError(f"{name} must contain only finite values")
    return array


def _sample_weights(values: np.ndarray, expected_rows: int) -> np.ndarray:
    try:
        weights = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_weight must be a numeric one-dimensional array") from exc

    if weights.ndim != 1 or weights.shape[0] != expected_rows:
        raise ValueError(
            f"sample_weight must have shape ({expected_rows},); got {weights.shape}"
        )
    if not np.isfinite(weights).all():
        raise ValueError("sample_weight must contain only finite values")
    if np.any(weights <= 0.0):
        raise ValueError("sample_weight values must all be positive")
    return weights


def _context_matrix(
    context: pd.DataFrame,
    *,
    expected_rows: int | None = None,
    allow_empty: bool = False,
) -> np.ndarray:
    if not isinstance(context, pd.DataFrame):
        raise TypeError("context must be a pandas DataFrame")
    if tuple(context.columns) != CONTEXT_COLUMNS:
        raise ValueError(
            "context columns must exactly match CONTEXT_COLUMNS in the declared order"
        )
    if expected_rows is not None and len(context) != expected_rows:
        raise ValueError(f"context must have {expected_rows} rows; got {len(context)}")
    if not allow_empty and len(context) == 0:
        raise ValueError("context must contain at least one row")

    try:
        matrix = context.to_numpy(dtype=float, na_value=np.nan, copy=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("context values must be numeric or missing") from exc
    if np.isinf(matrix).any():
        raise ValueError("context may contain missing values but not infinite values")
    return matrix


def _validate_training_inputs(
    data_pred: np.ndarray,
    physical_pred: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray,
    *,
    expected_rows: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    data = _prediction_matrix(data_pred, "data_pred", expected_rows=expected_rows)
    physical = _prediction_matrix(
        physical_pred, "physical_pred", expected_rows=data.shape[0]
    )
    labels = _prediction_matrix(y, "y", expected_rows=data.shape[0])
    weights = _sample_weights(sample_weight, data.shape[0])
    if data.shape[0] == 0:
        raise ValueError("training inputs must contain at least one row")
    return data, physical, labels, weights


def make_context(
    features: pd.DataFrame,
    data_pred: np.ndarray,
    physical_pred: np.ndarray,
    data_feature_names: list[str],
) -> pd.DataFrame:
    """Build inference-safe gate features in the fixed gate contract order."""

    if not isinstance(features, pd.DataFrame):
        raise TypeError("features must be a pandas DataFrame")
    if not features.columns.is_unique:
        raise ValueError("features must have unique column names")
    if isinstance(data_feature_names, (str, bytes)) or not isinstance(
        data_feature_names, Sequence
    ):
        raise TypeError("data_feature_names must be a sequence of column names")

    requested = list(data_feature_names)
    if not requested:
        raise ValueError("data_feature_names must contain at least one column name")
    if not all(isinstance(name, str) for name in requested):
        raise TypeError("data_feature_names must contain only strings")
    if len(set(requested)) != len(requested):
        raise ValueError("data_feature_names must not contain duplicates")

    required = list(_HISTORY_COLUMNS) + requested
    missing = [name for name in required if name not in features.columns]
    if missing:
        raise ValueError(f"features is missing required columns: {missing}")

    data = _prediction_matrix(data_pred, "data_pred", expected_rows=len(features))
    physical = _prediction_matrix(
        physical_pred, "physical_pred", expected_rows=len(features)
    )

    try:
        history = features.loc[:, list(_HISTORY_COLUMNS)].to_numpy(
            dtype=float, na_value=np.nan, copy=True
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("history context columns must be numeric or missing") from exc
    if np.isinf(history).any():
        raise ValueError(
            "history context columns may contain missing values but not infinite values"
        )

    context = pd.DataFrame(history, index=features.index, columns=_HISTORY_COLUMNS)
    context["missing_fraction"] = (
        features.loc[:, requested].isna().mean(axis=1).to_numpy(dtype=float)
    )
    with np.errstate(over="ignore", invalid="ignore"):
        disagreement = np.abs(physical - data)
    if not np.isfinite(disagreement).all():
        raise ValueError("prediction disagreements must be finite")
    context["disagreement_bed"] = disagreement[:, 0]
    context["disagreement_wake"] = disagreement[:, 1]
    return context.loc[:, list(CONTEXT_COLUMNS)]


def best_fixed_weight(
    data_pred: np.ndarray,
    physical_pred: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray,
) -> float:
    """Return the lowest grid weight attaining minimum weighted joint MAE."""

    data, physical, labels, weights = _validate_training_inputs(
        data_pred, physical_pred, y, sample_weight
    )
    delta = physical - data
    losses = np.empty(GATE_GRID.shape[0], dtype=float)
    with np.errstate(over="ignore", invalid="ignore"):
        for index, gate_weight in enumerate(GATE_GRID):
            prediction = data + gate_weight * delta
            joint_error = np.abs(prediction - labels).mean(axis=1)
            losses[index] = np.average(joint_error, weights=weights)
    if not np.isfinite(losses).all():
        raise ValueError("weighted joint MAE must be finite")
    return float(GATE_GRID[int(np.argmin(losses))])


def _row_optimal_weights(
    data: np.ndarray, physical: np.ndarray, labels: np.ndarray
) -> np.ndarray:
    with np.errstate(over="ignore", invalid="ignore"):
        candidates = data[:, None, :] + GATE_GRID[None, :, None] * (
            physical - data
        )[:, None, :]
        row_losses = np.abs(candidates - labels[:, None, :]).mean(axis=2)
    if not np.isfinite(row_losses).all():
        raise ValueError("per-row joint MAE must be finite")
    return GATE_GRID[np.argmin(row_losses, axis=1)]


class ConditionalGate:
    """A shallow context gate calibrated only from caller-supplied OOF rows."""

    def __init__(
        self,
        depth: int,
        min_samples_leaf: int | float = 0.1,
        seed: int = 20261026,
    ) -> None:
        if isinstance(depth, bool) or not isinstance(depth, Integral) or depth < 1:
            raise ValueError("depth must be a positive integer")
        if isinstance(min_samples_leaf, bool) or not isinstance(
            min_samples_leaf, Real
        ):
            raise ValueError("min_samples_leaf must be an integer or float")
        if isinstance(min_samples_leaf, Integral):
            if min_samples_leaf < 1:
                raise ValueError("integer min_samples_leaf must be at least 1")
            normalized_min_samples_leaf: int | float = int(min_samples_leaf)
        else:
            if not 0.0 < float(min_samples_leaf) <= 1.0:
                raise ValueError("float min_samples_leaf must be in (0, 1]")
            normalized_min_samples_leaf = float(min_samples_leaf)
        if isinstance(seed, bool) or not isinstance(seed, Integral):
            raise ValueError("seed must be an integer")

        self.depth = int(depth)
        self.min_samples_leaf = normalized_min_samples_leaf
        self.seed = int(seed)

    def fit(
        self,
        context: pd.DataFrame,
        data_pred: np.ndarray,
        physical_pred: np.ndarray,
        y: np.ndarray,
        sample_weight: np.ndarray,
    ) -> ConditionalGate:
        """Fit on TRAIN-OOF predictions supplied by the caller."""

        context_matrix = _context_matrix(context)
        data, physical, labels, weights = _validate_training_inputs(
            data_pred,
            physical_pred,
            y,
            sample_weight,
            expected_rows=context_matrix.shape[0],
        )
        row_targets = _row_optimal_weights(data, physical, labels)

        imputer = SimpleImputer(strategy="median", keep_empty_features=True)
        imputed_context = imputer.fit_transform(context_matrix)
        if imputed_context.shape != context_matrix.shape:
            raise RuntimeError("imputer did not preserve the gate context columns")
        if not np.isfinite(imputed_context).all():
            raise ValueError("imputed context must contain only finite values")

        tree = DecisionTreeRegressor(
            max_depth=self.depth,
            min_samples_leaf=self.min_samples_leaf,
            random_state=self.seed,
        )
        tree.fit(imputed_context, row_targets, sample_weight=weights)

        leaves = tree.apply(imputed_context)
        leaf_weights: dict[int, float] = {}
        for leaf_id in np.unique(leaves):
            in_leaf = leaves == leaf_id
            leaf_weights[int(leaf_id)] = best_fixed_weight(
                data[in_leaf],
                physical[in_leaf],
                labels[in_leaf],
                weights[in_leaf],
            )

        self.imputer_ = imputer
        self.tree_ = tree
        self.leaf_weights_ = leaf_weights
        self.feature_names_in_ = tuple(CONTEXT_COLUMNS)
        return self

    def _require_fitted(self) -> None:
        required = ("imputer_", "tree_", "leaf_weights_", "feature_names_in_")
        if any(not hasattr(self, attribute) for attribute in required):
            raise NotFittedError("ConditionalGate must be fitted before prediction")

    def predict_weights(self, context: pd.DataFrame) -> np.ndarray:
        """Return one shared onset/wake physics coefficient per row."""

        self._require_fitted()
        context_matrix = _context_matrix(context)
        imputed_context = self.imputer_.transform(context_matrix)
        if imputed_context.shape != context_matrix.shape:
            raise RuntimeError("imputer did not preserve the gate context columns")
        if not np.isfinite(imputed_context).all():
            raise ValueError("imputed context must contain only finite values")

        leaves = self.tree_.apply(imputed_context)
        try:
            weights = np.fromiter(
                (self.leaf_weights_[int(leaf_id)] for leaf_id in leaves),
                dtype=float,
                count=len(leaves),
            )
        except KeyError as exc:
            raise RuntimeError("tree produced an uncalibrated leaf") from exc
        if not np.isfinite(weights).all() or np.any((weights < 0.0) | (weights > 1.0)):
            raise RuntimeError("calibrated gate weights must be finite and in [0, 1]")
        return weights

    def predict(
        self,
        context: pd.DataFrame,
        data_pred: np.ndarray,
        physical_pred: np.ndarray,
    ) -> np.ndarray:
        weights = self.predict_weights(context)
        data = _prediction_matrix(data_pred, "data_pred", expected_rows=len(weights))
        physical = _prediction_matrix(
            physical_pred, "physical_pred", expected_rows=len(weights)
        )
        with np.errstate(over="ignore", invalid="ignore"):
            prediction = data + weights[:, None] * (physical - data)
        if not np.isfinite(prediction).all():
            raise ValueError("blended predictions must be finite")
        return prediction

    def description(self) -> dict[str, Any]:
        """Return a JSON-serializable description of the fitted gate."""

        self._require_fitted()
        return {
            "feature_names": list(CONTEXT_COLUMNS),
            "grid": [float(value) for value in GATE_GRID],
            "leaf_weights": {
                str(leaf_id): float(weight)
                for leaf_id, weight in sorted(self.leaf_weights_.items())
            },
            "tree_rules": export_text(
                self.tree_, feature_names=list(CONTEXT_COLUMNS)
            ),
            "depth": self.depth,
        }
