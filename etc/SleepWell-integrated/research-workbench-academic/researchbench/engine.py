"""Leakage-resistant experiment splitting, training, and evaluation.

Configuration contract highlights
---------------------------------
``bin_edges`` are finite *internal cut points*. Infinite outer bounds are
implicit; ``n`` cut points produce ``n + 1`` classes. ``bin_labels`` therefore
also has ``n + 1`` entries. The result records the normalized rule under
``target_rule``.

``shared_targets`` makes a common plan eligible when at least one listed target
is present. Each task still removes rows missing its own target inside every
fold and reports planned/effective counts in ``row_scope``. Stratification is
used only when its target is present for every eligible row.

Persisted plans are untrusted input. :func:`validate_split_plan` rechecks their
content hash, row bounds, holdout/fold isolation, group boundaries, and time
order before training. Preprocessing and target transformation are fitted only
on the training rows for the current fold. This build accepts ``device='cpu'``;
unsupported devices fail explicitly instead of silently falling back.
"""

from __future__ import annotations

import hashlib
import json
import platform
import time
import copy
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import (
    GroupKFold,
    GroupShuffleSplit,
    KFold,
    StratifiedKFold,
    TimeSeriesSplit,
    train_test_split,
)
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, RobustScaler, StandardScaler
from sklearn.utils.class_weight import compute_sample_weight

from .metrics import classification_metrics, regression_metrics, summarize


Progress = Callable[[dict[str, Any]], None]
Cancelled = Callable[[], bool]


class ExperimentCancelled(RuntimeError):
    """Raised at a safe checkpoint after cancellation is requested."""


def _native(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray, pd.Index)):
        return [_json_safe(v) for v in value]
    if value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    return _native(value)


def _plan_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _target_series(df: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    y = df[config["target"]].copy()
    edges = config.get("bin_edges")
    if config.get("task") == "classification" and edges:
        labels = config.get("bin_labels")
        y = _binned_target(y, edges, labels)
    return y


def _binned_target(series: pd.Series, edges: Any, labels: Any = None) -> pd.Series:
    """Bin from finite internal cut points with implicit infinite boundaries."""
    if len(edges) < 1 or any(not np.isfinite(float(value)) for value in edges):
        raise ValueError("bin_edges must contain at least one finite cut point")
    if any(float(right) <= float(left) for left, right in zip(edges, edges[1:])):
        raise ValueError("bin_edges must be strictly increasing")
    effective_labels = list(labels) if labels is not None else _default_bin_labels(edges)
    if len(effective_labels) != len(edges) + 1:
        raise ValueError("bin_labels must have one label per resulting interval")
    return pd.cut(
        pd.to_numeric(series, errors="coerce"),
        bins=[-np.inf, *[float(v) for v in edges], np.inf],
        labels=effective_labels,
        include_lowest=True,
    )


def _format_cut(value: Any) -> str:
    number = float(value)
    return f"{number:g}"


def _default_bin_labels(edges: Any) -> list[str]:
    cuts = [_format_cut(value) for value in edges]
    return [f"≤ {cuts[0]}", *[f"> {left}–≤ {right}" for left, right in zip(cuts, cuts[1:])], f"> {cuts[-1]}"]


def _target_rule(config: dict[str, Any]) -> dict[str, Any] | None:
    edges = config.get("bin_edges")
    if config.get("task") != "classification" or not edges:
        return None
    labels = list(config.get("bin_labels") or _default_bin_labels(edges))
    return {
        "cut_points": [float(value) for value in edges],
        "effective_edges": ["-inf", *[float(value) for value in edges], "inf"],
        "labels": _json_safe(labels),
        "rationale": str(config.get("bin_rationale", "")),
    }


def _validate_common(df: pd.DataFrame, config: dict[str, Any]) -> None:
    if config.get("task") not in {"classification", "regression"}:
        raise ValueError("task must be classification or regression")
    target = config.get("target")
    if target not in df.columns:
        raise ValueError("target column does not exist")
    features = config.get("features") or []
    if not features:
        raise ValueError("at least one feature is required")
    missing = [name for name in features if name not in df.columns]
    if missing:
        raise ValueError(f"feature columns do not exist: {missing}")
    if target in features:
        raise ValueError("target cannot be used as an input feature")
    if len(features) > 5000:
        raise ValueError("feature limit is 5000")
    if len(features) != len(set(features)):
        raise ValueError("features cannot contain duplicates")
    if config.get("device", "cpu") != "cpu":
        raise ValueError("this build supports CPU execution only")


def _validate_experiment_config(config: dict[str, Any]) -> None:
    if config.get("model", "linear") not in {"dummy", "linear", "forest", "mlp"}:
        raise ValueError("unsupported model")
    if not isinstance(config.get("params", {}), dict):
        raise ValueError("params must be a dictionary")
    class_weight = config.get("class_weight")
    if class_weight is not None and class_weight != "balanced":
        raise ValueError("class_weight must be null or balanced")
    reserved = {"random_state", "n_jobs", "class_weight", "warm_start"}
    conflicts = reserved.intersection(config.get("params", {}))
    if conflicts:
        raise ValueError(f"params contains engine-controlled settings: {sorted(conflicts)}")
    classification_metrics = {
        "roc_auc", "average_precision", "accuracy", "balanced_accuracy",
        "recall", "specificity", "precision", "f1",
    }
    regression_metrics = {"mae", "mse", "rmse", "r2"}
    selected = classification_metrics if config["task"] == "classification" else regression_metrics
    primary = config.get("primary_metric")
    if primary is not None and primary not in selected:
        raise ValueError("primary_metric does not belong to the selected task")
    if config["task"] == "regression" and config.get("bin_edges"):
        raise ValueError("target binning is available only for classification")
    if config["task"] == "classification" and config.get("target_transform", "none") != "none":
        raise ValueError("target_transform is available only for regression")
    if config.get("bin_edges"):
        _default_bin_labels(config["bin_edges"])
        # Run all structural checks without depending on actual data values.
        _binned_target(pd.Series([config["bin_edges"][0]]), config["bin_edges"], config.get("bin_labels"))
    if config.get("imputer", "median") not in {"median", "mean", "constant"}:
        raise ValueError("unsupported imputer")
    if config.get("encoding", "onehot") not in {"onehot", "ordinal"}:
        raise ValueError("unsupported encoding")
    if config.get("scaling", "standard") not in {"standard", "robust", "none"}:
        raise ValueError("unsupported scaling")
    budget = config.get("max_seconds")
    if budget is not None and float(budget) <= 0:
        raise ValueError("max_seconds must be positive")
    params = config.get("params", {})
    if config.get("model") == "mlp":
        epochs = int(params.get("epochs", 30))
        if not 1 <= epochs <= 500:
            raise ValueError("MLP epochs must be between 1 and 500")


def _development_target_and_classes(
    df: pd.DataFrame, config: dict[str, Any], plan: dict[str, Any]
) -> tuple[list[int], np.ndarray, np.ndarray]:
    rows, values = _prepare_target(df, list(plan["train_rows"]), config)
    if config["task"] != "classification":
        return rows, values, np.asarray([], dtype=object)
    if config.get("bin_edges"):
        rule_labels = list(config.get("bin_labels") or _default_bin_labels(config["bin_edges"]))
        observed = set(pd.unique(values).tolist())
        classes = np.asarray([label for label in rule_labels if label in observed])
    else:
        classes = np.asarray(pd.unique(values).tolist())
        try:
            classes = np.asarray(sorted(classes.tolist()))
        except TypeError:
            classes = np.asarray(classes)
    return rows, values, classes


def resolve_config(df: pd.DataFrame, config: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a config before it is queued or persisted.

    Resolution reads development rows only. In particular, it never reads final
    test target values or classes. Returning a deep copy lets the queue persist
    the exact immutable configuration that the completed result will contain.
    """
    normalized = copy.deepcopy(config)
    _validate_common(df, normalized)
    _validate_experiment_config(normalized)
    validate_split_plan(df, normalized, plan)
    _, _, classes = _development_target_and_classes(df, normalized, plan)
    if normalized["task"] == "classification":
        if len(classes) < 2:
            raise ValueError("classification requires at least two development classes")
        if not 0 <= float(normalized.get("threshold", 0.5)) <= 1:
            raise ValueError("threshold must be between 0 and 1")
        if normalized.get("average", "macro") not in {"macro", "weighted"}:
            raise ValueError("average must be macro or weighted")
        positive = normalized.get("positive_class")
        if len(classes) == 2:
            if positive is None:
                normalized["positive_class"] = _native(classes[-1])
            elif not np.any(classes == positive):
                raise ValueError("positive_class is not present in development data")
    # Constructor invocation catches unsupported keyword names without fitting.
    _model(normalized)
    return normalized


def make_split_plan(df: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic holdout and validation plan using positional row IDs."""
    _validate_common(df, config)
    strategy = config.get("split", "random")
    if strategy not in {"random", "group", "time"}:
        raise ValueError("split must be random, group, or time")
    folds = int(config.get("folds", 3))
    if not 2 <= folds <= 20:
        raise ValueError("folds must be between 2 and 20")
    test_size = float(config.get("test_size", 0.2))
    if not 0.05 <= test_size <= 0.5:
        raise ValueError("test_size must be between 0.05 and 0.5")
    seed = int(config.get("seed", 42))
    rows = np.arange(len(df), dtype=int)
    y = _target_series(df, config)
    shared_targets = list(config.get("shared_targets") or [])
    if shared_targets:
        missing_shared = [name for name in shared_targets if name not in df.columns]
        if missing_shared:
            raise ValueError(f"shared target columns do not exist: {missing_shared}")
        eligible_mask = df[shared_targets].notna().any(axis=1)
        eligible = rows[eligible_mask.to_numpy()]
    else:
        eligible = np.asarray([i for i in rows if not pd.isna(y.iloc[i])], dtype=int)
    if len(eligible) < max(6, folds + 2):
        raise ValueError("not enough rows with a non-missing target")

    if strategy == "random":
        stratify = None
        stratify_column = config.get("stratify_target", config.get("target"))
        if config.get("task") == "classification" and stratify_column is not None:
            if stratify_column == config.get("target"):
                stratify_values = y
            elif stratify_column in df.columns:
                stratify_values = df[stratify_column]
            else:
                raise ValueError("stratify_target column does not exist")
            candidate = stratify_values.iloc[eligible]
            counts = candidate.value_counts(dropna=True)
            if not candidate.isna().any() and len(counts) > 1 and int(counts.min()) >= 2:
                stratify = candidate
        train_rows, test_rows = train_test_split(
            eligible, test_size=test_size, random_state=seed, stratify=stratify
        )
        train_rows = np.sort(train_rows)
        test_rows = np.sort(test_rows)
        train_y = y.iloc[train_rows]
        counts = train_y.value_counts(dropna=True)
        use_stratified = (
            config.get("task") == "classification"
            and not train_y.isna().any()
            and len(counts) > 1
            and int(counts.min()) >= folds
        )
        splitter = (
            StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
            if use_stratified
            else KFold(n_splits=folds, shuffle=True, random_state=seed)
        )
        split_iter = splitter.split(train_rows, train_y if use_stratified else None)
    elif strategy == "group":
        group = config.get("group")
        if group not in df.columns:
            raise ValueError("a valid group column is required")
        groups = df[group]
        if groups.iloc[eligible].isna().any():
            raise ValueError("group identifiers cannot be missing")
        unique_groups = groups.iloc[eligible].nunique()
        if unique_groups < folds + 1:
            raise ValueError("group split needs at least folds + 1 unique groups")
        holdout = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
        train_idx, test_idx = next(holdout.split(eligible, groups=groups.iloc[eligible]))
        train_rows, test_rows = np.sort(eligible[train_idx]), np.sort(eligible[test_idx])
        split_iter = GroupKFold(n_splits=folds).split(
            train_rows, groups=groups.iloc[train_rows]
        )
    else:
        time_column = config.get("time")
        if time_column not in df.columns:
            raise ValueError("a valid time column is required")
        parsed = pd.to_datetime(df[time_column], errors="coerce")
        if parsed.iloc[eligible].isna().any():
            raise ValueError("time values must be parseable and non-missing")
        unique_times = np.sort(parsed.iloc[eligible].unique())
        if len(unique_times) <= folds + 1:
            raise ValueError("time split needs at least folds + 2 unique time values")
        n_test_times = max(1, int(np.ceil(len(unique_times) * test_size)))
        train_times, test_times = unique_times[:-n_test_times], unique_times[-n_test_times:]
        train_mask = parsed.iloc[eligible].isin(train_times).to_numpy()
        test_mask = parsed.iloc[eligible].isin(test_times).to_numpy()
        train_rows = eligible[train_mask]
        test_rows = eligible[test_mask]
        train_rows = train_rows[np.argsort(parsed.iloc[train_rows].to_numpy(), kind="stable")]
        test_rows = test_rows[np.argsort(parsed.iloc[test_rows].to_numpy(), kind="stable")]
        if len(train_times) <= folds:
            raise ValueError("not enough pre-test rows for time cross-validation")
        time_pairs = []
        for time_train_idx, time_valid_idx in TimeSeriesSplit(n_splits=folds).split(train_times):
            fold_train_times = train_times[time_train_idx]
            fold_valid_times = train_times[time_valid_idx]
            fold_train = train_rows[parsed.iloc[train_rows].isin(fold_train_times).to_numpy()]
            fold_valid = train_rows[parsed.iloc[train_rows].isin(fold_valid_times).to_numpy()]
            time_pairs.append((fold_train, fold_valid))
        split_iter = time_pairs

    fold_records = []
    for train_idx, valid_idx in split_iter:
        if strategy == "time":
            fold_train_rows, fold_valid_rows = np.asarray(train_idx), np.asarray(valid_idx)
        else:
            fold_train_rows = train_rows[np.asarray(train_idx)]
            fold_valid_rows = train_rows[np.asarray(valid_idx)]
        fold_records.append({
            "train": [int(v) for v in fold_train_rows],
            "valid": [int(v) for v in fold_valid_rows],
        })
    payload = {
        "train_rows": [int(v) for v in train_rows],
        "test_rows": [int(v) for v in test_rows],
        "folds": fold_records,
        "strategy": strategy,
        "seed": seed,
    }
    payload["id"] = _plan_id(payload)
    return payload


def validate_split_plan(df: pd.DataFrame, config: dict[str, Any], plan: dict[str, Any]) -> None:
    """Reject malformed or mutated plans before any model sees data.

    Plans can be persisted and later reloaded, so execution validates identity,
    positional row bounds, holdout isolation, fold boundaries, and strategy
    constraints again rather than trusting a caller-supplied dictionary.
    """
    required = {"id", "train_rows", "test_rows", "folds", "strategy", "seed"}
    if not required.issubset(plan):
        raise ValueError("split plan is missing required fields")
    if plan["strategy"] != config.get("split", "random"):
        raise ValueError("split plan strategy does not match experiment config")
    if int(plan["seed"]) != int(config.get("seed", 42)):
        raise ValueError("split plan seed does not match experiment config")

    def checked_rows(values: Any, name: str) -> list[int]:
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name} must be a non-empty row list")
        if any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) for value in values):
            raise ValueError(f"{name} contains a non-integer row ID")
        rows = [int(value) for value in values]
        if len(rows) != len(set(rows)):
            raise ValueError(f"{name} contains duplicate rows")
        if min(rows) < 0 or max(rows) >= len(df):
            raise ValueError(f"{name} contains an out-of-range row")
        return rows

    train_rows = checked_rows(plan["train_rows"], "train_rows")
    test_rows = checked_rows(plan["test_rows"], "test_rows")
    if not set(train_rows).isdisjoint(test_rows):
        raise ValueError("development and final-test rows overlap")
    if not isinstance(plan["folds"], list) or not plan["folds"]:
        raise ValueError("split plan requires validation folds")
    if len(plan["folds"]) != int(config.get("folds", 3)):
        raise ValueError("split plan fold count does not match experiment config")
    seen_fold_pairs: set[tuple[tuple[int, ...], tuple[int, ...]]] = set()
    seen_validation_rows: set[int] = set()
    development = set(train_rows)
    for number, fold in enumerate(plan["folds"], start=1):
        if not isinstance(fold, dict) or not {"train", "valid"}.issubset(fold):
            raise ValueError(f"fold {number} is malformed")
        fold_train = checked_rows(fold["train"], f"fold {number} train")
        fold_valid = checked_rows(fold["valid"], f"fold {number} valid")
        if not set(fold_train).isdisjoint(fold_valid):
            raise ValueError(f"fold {number} train and validation rows overlap")
        if not set(fold_train).issubset(development) or not set(fold_valid).issubset(development):
            raise ValueError(f"fold {number} contains final-test or unknown rows")
        if not seen_validation_rows.isdisjoint(fold_valid):
            raise ValueError("validation rows repeat across folds")
        seen_validation_rows.update(fold_valid)
        pair = (tuple(fold_train), tuple(fold_valid))
        if pair in seen_fold_pairs:
            raise ValueError("split plan contains a duplicate fold")
        seen_fold_pairs.add(pair)
    if plan["strategy"] in {"random", "group"} and seen_validation_rows != development:
        raise ValueError("validation folds do not cover every development row exactly once")

    identity_payload = {
        "train_rows": train_rows,
        "test_rows": test_rows,
        "folds": plan["folds"],
        "strategy": plan["strategy"],
        "seed": int(plan["seed"]),
    }
    if str(plan["id"]) != _plan_id(identity_payload):
        raise ValueError("split plan identity does not match its contents")

    if plan["strategy"] == "group":
        group = config.get("group")
        if group not in df.columns:
            raise ValueError("a valid group column is required")
        groups = df[group]
        if not set(groups.iloc[train_rows]).isdisjoint(set(groups.iloc[test_rows])):
            raise ValueError("groups cross the development/final-test boundary")
        for number, fold in enumerate(plan["folds"], start=1):
            if not set(groups.iloc[fold["train"]]).isdisjoint(set(groups.iloc[fold["valid"]])):
                raise ValueError(f"groups cross fold {number} boundaries")
    elif plan["strategy"] == "time":
        time_column = config.get("time")
        if time_column not in df.columns:
            raise ValueError("a valid time column is required")
        parsed = pd.to_datetime(df[time_column], errors="coerce")
        if parsed.iloc[train_rows + test_rows].isna().any():
            raise ValueError("time values must be parseable and non-missing")
        if parsed.iloc[train_rows].max() >= parsed.iloc[test_rows].min():
            raise ValueError("final-test rows precede development rows")
        for number, fold in enumerate(plan["folds"], start=1):
            if parsed.iloc[fold["train"]].max() >= parsed.iloc[fold["valid"]].min():
                raise ValueError(f"validation rows precede training rows in fold {number}")


def _feature_frame(df: pd.DataFrame, rows: list[int], features: list[str]) -> pd.DataFrame:
    frame = df.iloc[rows][features].copy()
    for column in features:
        if pd.api.types.is_numeric_dtype(frame[column]) and not pd.api.types.is_bool_dtype(frame[column]):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = frame[column].astype(object)
            frame[column] = frame[column].where(pd.notna(frame[column]), np.nan)
    return frame


def _preprocessor(frame: pd.DataFrame, config: dict[str, Any]) -> ColumnTransformer:
    numeric = [
        c for c in frame.columns
        if pd.api.types.is_numeric_dtype(frame[c]) and not pd.api.types.is_bool_dtype(frame[c])
    ]
    categorical = [c for c in frame.columns if c not in numeric]
    strategy = config.get("imputer", "median")
    if strategy not in {"median", "mean", "constant"}:
        raise ValueError("unsupported imputer")
    numeric_strategy = strategy
    numeric_kwargs: dict[str, Any] = {}
    if strategy == "constant":
        numeric_kwargs["fill_value"] = 0.0
    scaling = config.get("scaling", "standard")
    if scaling not in {"standard", "robust", "none"}:
        raise ValueError("unsupported scaling")
    num_steps: list[tuple[str, Any]] = [
        ("imputer", SimpleImputer(strategy=numeric_strategy, keep_empty_features=True, **numeric_kwargs))
    ]
    if scaling == "standard":
        num_steps.append(("scaler", StandardScaler()))
    elif scaling == "robust":
        num_steps.append(("scaler", RobustScaler()))

    encoding = config.get("encoding", "onehot")
    if encoding == "onehot":
        encoder: Any = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    elif encoding == "ordinal":
        encoder = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
    else:
        raise ValueError("unsupported encoding")
    cat_steps = [
        ("imputer", SimpleImputer(strategy="constant", fill_value="(missing)", keep_empty_features=True)),
        ("encoder", encoder),
    ]
    transformers = []
    if numeric:
        transformers.append(("numeric", Pipeline(num_steps), numeric))
    if categorical:
        transformers.append(("categorical", Pipeline(cat_steps), categorical))
    return ColumnTransformer(transformers=transformers, remainder="drop")


@dataclass
class _TargetTransform:
    method: str
    mean: float = 0.0
    scale: float = 1.0

    def fit(self, y: np.ndarray) -> "_TargetTransform":
        if self.method == "standard":
            self.mean = float(np.mean(y))
            std = float(np.std(y))
            self.scale = std if std > 0 else 1.0
        elif self.method == "log1p" and np.any(y <= -1):
            raise ValueError("log1p target transformation requires values greater than -1")
        elif self.method not in {"none", "standard", "log1p"}:
            raise ValueError("unsupported target transformation")
        return self

    def transform(self, y: np.ndarray) -> np.ndarray:
        if self.method == "standard":
            return (y - self.mean) / self.scale
        if self.method == "log1p":
            return np.log1p(y)
        return y

    def inverse(self, y: np.ndarray) -> np.ndarray:
        if self.method == "standard":
            return y * self.scale + self.mean
        if self.method == "log1p":
            return np.expm1(y)
        return y


def _model(config: dict[str, Any]) -> Any:
    task = config["task"]
    name = config.get("model", "linear")
    params = dict(config.get("params") or {})
    params.pop("epochs", None)
    seed = int(config.get("seed", 42))
    class_weight = config.get("class_weight")
    if task == "classification":
        if name == "dummy":
            return DummyClassifier(strategy=params.pop("strategy", "prior"), **params)
        if name == "linear":
            return LogisticRegression(
                max_iter=params.pop("max_iter", 1000), random_state=seed,
                class_weight=class_weight, **params
            )
        if name == "forest":
            n_estimators = int(params.pop("n_estimators", 200))
            if not 1 <= n_estimators <= 2000:
                raise ValueError("n_estimators must be between 1 and 2000")
            return RandomForestClassifier(
                n_estimators=n_estimators, random_state=seed, n_jobs=1,
                class_weight=class_weight, **params
            )
        if name == "mlp":
            return MLPClassifier(
                hidden_layer_sizes=tuple(params.pop("hidden_layer_sizes", (64, 32))),
                random_state=seed, max_iter=1, warm_start=True, **params
            )
    else:
        if name == "dummy":
            return DummyRegressor(strategy=params.pop("strategy", "mean"), **params)
        if name == "linear":
            return Ridge(**params)
        if name == "forest":
            n_estimators = int(params.pop("n_estimators", 200))
            if not 1 <= n_estimators <= 2000:
                raise ValueError("n_estimators must be between 1 and 2000")
            return RandomForestRegressor(
                n_estimators=n_estimators, random_state=seed, n_jobs=1, **params
            )
        if name == "mlp":
            return MLPRegressor(
                hidden_layer_sizes=tuple(params.pop("hidden_layer_sizes", (64, 32))),
                random_state=seed, max_iter=1, warm_start=True, **params
            )
    raise ValueError(f"unsupported model: {name}")


def _prepare_target(df: pd.DataFrame, rows: list[int], config: dict[str, Any]) -> tuple[list[int], np.ndarray]:
    series = df.iloc[rows][config["target"]].copy()
    if config["task"] == "classification" and config.get("bin_edges"):
        series = _binned_target(series, config["bin_edges"], config.get("bin_labels"))
    valid = ~series.isna()
    kept = [int(row) for row, include in zip(rows, valid.to_numpy()) if include]
    values = series[valid].to_numpy()
    if config["task"] == "regression":
        values = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(values)
        kept = [row for row, include in zip(kept, finite) if include]
        values = values[finite]
    return kept, values


def _check_cancel(start: float, config: dict[str, Any], cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise ExperimentCancelled("experiment cancelled")
    budget = config.get("max_seconds")
    if budget is not None and float(budget) <= 0:
        raise ValueError("max_seconds must be positive")
    if budget is not None and time.monotonic() - start > float(budget):
        raise TimeoutError("experiment time budget exceeded")


def _emit(progress: Progress | None, event: dict[str, Any]) -> None:
    if progress:
        progress(_json_safe(event))


def _aligned_probabilities(estimator: Any, transformed: np.ndarray, global_classes: np.ndarray) -> np.ndarray | None:
    if not hasattr(estimator, "predict_proba"):
        return None
    local = np.asarray(estimator.predict_proba(transformed), dtype=float)
    local_classes = np.asarray(estimator.classes_, dtype=object)
    aligned = np.zeros((len(transformed), len(global_classes)), dtype=float)
    for source, label in enumerate(local_classes):
        match = np.flatnonzero(global_classes == label)
        if len(match):
            aligned[:, int(match[0])] = local[:, source]
    return aligned


def _fit_fold(
    df: pd.DataFrame,
    config: dict[str, Any],
    train_rows: list[int],
    valid_rows: list[int],
    fold_number: int,
    total_folds: int,
    global_classes: np.ndarray,
    progress: Progress | None,
    cancelled: Cancelled | None,
    start: float,
    monitor_validation: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    train_rows, y_train = _prepare_target(df, train_rows, config)
    valid_rows, y_valid = _prepare_target(df, valid_rows, config)
    if not train_rows or not valid_rows:
        raise ValueError("a fold has no eligible target rows")
    _check_cancel(start, config, cancelled)
    features = list(config["features"])
    x_train = _feature_frame(df, train_rows, features)
    x_valid = _feature_frame(df, valid_rows, features)
    preprocess = _preprocessor(x_train, config)
    transformed_train = preprocess.fit_transform(x_train)
    # For a final-test run, do not even transform the held-out features until
    # the locked model has finished training. Validation runs may transform now
    # because validation loss is a legitimate epoch monitor.
    transformed_valid = preprocess.transform(x_valid) if monitor_validation else None
    estimator = _model(config)
    history: list[dict[str, Any]] = []
    target_transform = _TargetTransform(config.get("target_transform", "none"))
    fit_y: np.ndarray
    if config["task"] == "regression":
        target_transform.fit(np.asarray(y_train, dtype=float))
        fit_y = target_transform.transform(np.asarray(y_train, dtype=float))
    else:
        fit_y = y_train

    _emit(progress, {"stage": "training", "fold": fold_number, "total_folds": total_folds})
    if config.get("model") == "mlp":
        epochs = int((config.get("params") or {}).get("epochs", 30))
        if not 1 <= epochs <= 500:
            raise ValueError("MLP epochs must be between 1 and 500")
        # ``epochs`` controls our loop, not the sklearn constructor.
        estimator_params = config.get("params") or {}
        if "epochs" in estimator_params:
            copied = dict(config)
            copied["params"] = {k: v for k, v in estimator_params.items() if k != "epochs"}
            estimator = _model(copied)
        weights = None
        if config["task"] == "classification" and config.get("class_weight") == "balanced":
            weights = compute_sample_weight("balanced", fit_y)
        for epoch in range(1, epochs + 1):
            _check_cancel(start, config, cancelled)
            if config["task"] == "classification":
                kwargs = {"classes": global_classes} if epoch == 1 else {}
                if weights is not None:
                    kwargs["sample_weight"] = weights
                estimator.partial_fit(transformed_train, fit_y, **kwargs)
                eps = np.finfo(float).eps
                train_probs = _aligned_probabilities(estimator, transformed_train, global_classes)
                train_indices = np.asarray([int(np.flatnonzero(global_classes == v)[0]) for v in y_train])
                train_loss = float(-np.mean(np.log(np.clip(
                    train_probs[np.arange(len(y_train)), train_indices], eps, 1
                ))))
                if monitor_validation:
                    valid_probs = _aligned_probabilities(estimator, transformed_valid, global_classes)
                    valid_indices = np.asarray([int(np.flatnonzero(global_classes == v)[0]) for v in y_valid])
                    valid_loss = float(-np.mean(np.log(np.clip(
                        valid_probs[np.arange(len(y_valid)), valid_indices], eps, 1
                    ))))
                else:
                    valid_loss = None
                loss_metric = "cross_entropy"
            else:
                estimator.partial_fit(transformed_train, fit_y)
                train_current = target_transform.inverse(estimator.predict(transformed_train))
                train_loss = float(np.mean((np.asarray(y_train, dtype=float) - train_current) ** 2))
                if monitor_validation:
                    valid_current = target_transform.inverse(estimator.predict(transformed_valid))
                    valid_loss = float(np.mean((np.asarray(y_valid, dtype=float) - valid_current) ** 2))
                else:
                    valid_loss = None
                loss_metric = "mse_original_unit"
            record = {
                "fold": fold_number, "epoch": epoch,
                "train_loss": train_loss, "valid_loss": valid_loss,
                "optimizer_loss": float(estimator.loss_), "loss_metric": loss_metric,
            }
            history.append(record)
            _emit(progress, {
                "stage": "training", "fold": fold_number, "total_folds": total_folds,
                "epoch": epoch, "total_epochs": epochs,
                "train_loss": record["train_loss"], "valid_loss": valid_loss,
                "optimizer_loss": record["optimizer_loss"], "loss_metric": loss_metric,
            })
    else:
        estimator.fit(transformed_train, fit_y)

    _check_cancel(start, config, cancelled)
    if transformed_valid is None:
        # This is the single first access to transformed final-test features for
        # an MLP final run, after all epochs have completed.
        transformed_valid = preprocess.transform(x_valid)
    predictions: list[dict[str, Any]] = []
    if config["task"] == "classification":
        probs = _aligned_probabilities(estimator, transformed_valid, global_classes)
        metric_values, reasons, predicted = classification_metrics(
            y_valid, probs, global_classes,
            positive_class=config.get("positive_class"),
            threshold=float(config.get("threshold", 0.5)),
            average=config.get("average", "macro"),
        )
        for i, row_id in enumerate(valid_rows):
            predictions.append({
                "row_id": int(row_id), "fold": fold_number,
                "y_true": _native(y_valid[i]), "y_pred": _native(predicted[i]),
                "probabilities": None if probs is None else [float(v) for v in probs[i]],
            })
    else:
        predicted = target_transform.inverse(np.asarray(estimator.predict(transformed_valid), dtype=float))
        metric_values, reasons = regression_metrics(y_valid, predicted)
        for i, row_id in enumerate(valid_rows):
            predictions.append({
                "row_id": int(row_id), "fold": fold_number,
                "y_true": float(y_valid[i]), "y_pred": float(predicted[i]),
                "probabilities": None,
            })
    fold = {
        "fold": fold_number, "metrics": metric_values, "reasons": reasons,
        "n_train": len(train_rows), "n_valid": len(valid_rows),
        "train_rows": [int(v) for v in train_rows],
        "valid_rows": [int(v) for v in valid_rows],
    }
    _emit(progress, {
        "stage": "fold_completed", "fold": fold_number, "total_folds": total_folds,
        "metrics": metric_values,
    })
    return fold, predictions, history


def _run(
    df: pd.DataFrame,
    config: dict[str, Any],
    plan: dict[str, Any],
    phase: str,
    progress: Progress | None,
    cancelled: Cancelled | None,
) -> dict[str, Any]:
    config = resolve_config(df, config, plan)
    start = time.monotonic()
    all_train, all_y, global_classes = _development_target_and_classes(df, config, plan)
    effective_test, final_y = _prepare_target(df, list(plan["test_rows"]), config)
    if config["task"] == "classification":
        if phase == "final_test":
            unseen = [value for value in pd.unique(final_y).tolist() if not np.any(global_classes == value)]
            if unseen:
                raise ValueError(f"final test contains classes absent from development data: {_json_safe(unseen)}")

    if phase == "validation":
        specs = [(fold["train"], fold["valid"]) for fold in plan["folds"]]
    else:
        specs = [(list(plan["train_rows"]), list(plan["test_rows"]))]
    fold_results: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for index, (train_rows, valid_rows) in enumerate(specs, start=1):
        fold, fold_predictions, fold_history = _fit_fold(
            df, config, list(train_rows), list(valid_rows), index, len(specs), global_classes,
            progress, cancelled, start, monitor_validation=phase == "validation",
        )
        fold_results.append(fold)
        predictions.extend(fold_predictions)
        history.extend(fold_history)
    duration = time.monotonic() - start
    result = {
        "id": str(config.get("id", _plan_id({"config": config, "time": time.time_ns()}))),
        "name": str(config.get("name", "Experiment")),
        "task": config["task"], "config": _json_safe(config),
        "split_id": str(plan["id"]), "data_hash": config.get("data_hash"),
        "phase": phase, "status": "completed", "folds": fold_results,
        "summary": summarize(fold_results), "predictions": predictions,
        "classes": [_native(v) for v in global_classes], "history": history,
        "target_rule": _target_rule(config),
        "row_scope": {
            "planned_development": len(plan["train_rows"]),
            "effective_development": len(all_train),
            "excluded_development_target_missing": len(plan["train_rows"]) - len(all_train),
            "planned_final_test": len(plan["test_rows"]),
            "effective_final_test": len(effective_test),
            "excluded_final_test_target_missing": len(plan["test_rows"]) - len(effective_test),
        },
        "duration": float(duration),
        "versions": {
            "python": platform.python_version(), "numpy": np.__version__,
            "pandas": pd.__version__, "scikit_learn": sklearn.__version__,
        },
        "device": "cpu",
    }
    _emit(progress, {"stage": "completed", "total_folds": len(specs), "duration": duration})
    return _json_safe(result)


def run_experiment(
    df: pd.DataFrame,
    config: dict[str, Any],
    plan: dict[str, Any],
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, Any]:
    """Run validation folds only. The final holdout remains untouched."""
    return _run(df, config, plan, "validation", progress, cancelled)


def evaluate_final(
    df: pd.DataFrame,
    config: dict[str, Any],
    plan: dict[str, Any],
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, Any]:
    """Fit the locked configuration on all development rows and test once."""
    return _run(df, config, plan, "final_test", progress, cancelled)
