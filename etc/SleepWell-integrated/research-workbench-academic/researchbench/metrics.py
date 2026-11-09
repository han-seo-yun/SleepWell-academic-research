"""Metric calculation with explicit unavailable-value reasons."""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    roc_auc_score,
)
from sklearn.preprocessing import label_binarize


CLASSIFICATION_METRICS = (
    "roc_auc", "average_precision", "accuracy", "balanced_accuracy",
    "recall", "specificity", "precision", "f1",
)
REGRESSION_METRICS = ("mae", "mse", "rmse", "r2")


def _native(value: Any) -> Any:
    return value.item() if isinstance(value, np.generic) else value


def _safe(callable_, reason: str = "metric_undefined") -> tuple[float | None, str | None]:
    try:
        value = float(callable_())
        if not np.isfinite(value):
            return None, reason
        return value, None
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return None, reason


def classification_metrics(
    y_true: Any,
    probabilities: Any,
    classes: Any,
    *,
    positive_class: Any = None,
    threshold: float = 0.5,
    average: str = "macro",
) -> tuple[dict[str, float | None], dict[str, str], np.ndarray]:
    """Calculate classification metrics and threshold-derived predictions.

    ROC-AUC/AP always use continuous probabilities. Metrics that cannot be
    computed are represented as ``None`` plus a stable reason code.
    """
    y = np.asarray(y_true)
    probs = None if probabilities is None else np.asarray(probabilities, dtype=float)
    # Preserve homogeneous numeric/string dtypes: sklearn deliberately rejects
    # an object array containing otherwise valid integer class labels.
    cls = np.asarray(list(classes))
    metrics: dict[str, float | None] = {name: None for name in CLASSIFICATION_METRICS}
    reasons: dict[str, str] = {}
    if len(cls) < 2:
        predicted = np.repeat(cls[0] if len(cls) else None, len(y))
        for name in CLASSIFICATION_METRICS:
            reasons[name] = "training_has_one_class"
        return metrics, reasons, predicted

    binary = len(cls) == 2
    if binary:
        positive = cls[-1] if positive_class is None else positive_class
        matches = np.flatnonzero(cls == positive)
        if not len(matches):
            raise ValueError("positive_class is not present in training classes")
        pos_idx = int(matches[0])
        negative = cls[1 - pos_idx]
        if probs is None:
            score = None
            predicted = np.repeat(negative, len(y))
        else:
            score = probs[:, pos_idx]
            predicted = np.where(score >= threshold, positive, negative)
        y_binary = np.asarray([1 if value == positive else 0 for value in y])
        if score is None:
            reasons["roc_auc"] = reasons["average_precision"] = "probability_unavailable"
        elif len(np.unique(y_binary)) < 2:
            reasons["roc_auc"] = "validation_has_one_class"
            if int(y_binary.sum()) == 0:
                reasons["average_precision"] = "validation_has_no_positive"
            else:
                metrics["average_precision"] = 1.0
        else:
            metrics["roc_auc"], why = _safe(lambda: roc_auc_score(y_binary, score))
            if why:
                reasons["roc_auc"] = why
            metrics["average_precision"], why = _safe(lambda: average_precision_score(y_binary, score))
            if why:
                reasons["average_precision"] = why

        tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[negative, positive]).ravel()
        if tn + fp:
            metrics["specificity"] = float(tn / (tn + fp))
        else:
            reasons["specificity"] = "validation_has_no_negative"
        if tp + fn:
            metrics["recall"] = float(tp / (tp + fn))
        else:
            reasons["recall"] = "validation_has_no_positive"
        if tp + fp:
            metrics["precision"] = float(tp / (tp + fp))
        else:
            reasons["precision"] = "no_predicted_positive"
        if 2 * tp + fp + fn:
            metrics["f1"] = float(2 * tp / (2 * tp + fp + fn))
        else:
            reasons["f1"] = "no_positive_in_truth_or_prediction"
    else:
        if probs is None:
            predicted = np.repeat(cls[0], len(y))
            reasons["roc_auc"] = reasons["average_precision"] = "probability_unavailable"
        else:
            predicted = cls[np.argmax(probs, axis=1)]
            if len(np.unique(y)) < len(cls):
                reason = "validation_has_one_class" if len(np.unique(y)) < 2 else "validation_missing_classes"
                reasons["roc_auc"] = reasons["average_precision"] = reason
            else:
                metrics["roc_auc"], why = _safe(
                    lambda: roc_auc_score(y, probs, labels=cls.tolist(), multi_class="ovr", average=average)
                )
                if why:
                    reasons["roc_auc"] = why
                y_bin = label_binarize(y, classes=cls)
                metrics["average_precision"], why = _safe(
                    lambda: average_precision_score(y_bin, probs, average=average)
                )
                if why:
                    reasons["average_precision"] = why
        per_class: list[dict[str, float | int | None]] = []
        for label in cls:
            truth_positive = y == label
            predicted_positive = predicted == label
            support = int(truth_positive.sum())
            predicted_count = int(predicted_positive.sum())
            tp = int((truth_positive & predicted_positive).sum())
            negative_count = len(y) - support
            true_negatives = int(((~truth_positive) & (~predicted_positive)).sum())
            per_class.append({
                "support": support,
                "recall": float(tp / support) if support else None,
                "precision": float(tp / predicted_count) if predicted_count else None,
                "f1": float(2 * tp / (support + predicted_count)) if support + predicted_count else None,
                "specificity": float(true_negatives / negative_count) if negative_count else None,
            })

        reason_codes = {
            "recall": "validation_missing_classes",
            "precision": "one_or_more_classes_never_predicted",
            "f1": "class_absent_in_truth_and_prediction",
            "specificity": "class_has_no_negative_examples",
        }
        for name in ("recall", "precision", "f1", "specificity"):
            if average == "weighted":
                included = [item for item in per_class if int(item["support"]) > 0]
                values = [item[name] for item in included]
                weights = np.asarray([item["support"] for item in included], dtype=float)
            else:
                included = per_class
                values = [item[name] for item in included]
                weights = None
            if not included or any(value is None for value in values):
                reasons[name] = reason_codes[name]
            elif average == "weighted":
                metrics[name] = float(np.average(np.asarray(values, dtype=float), weights=weights))
            else:
                metrics[name] = float(np.mean(np.asarray(values, dtype=float)))

    metrics["accuracy"], why = _safe(lambda: accuracy_score(y, predicted))
    if why:
        reasons["accuracy"] = why
    if len(np.unique(y)) < 2:
        reasons["balanced_accuracy"] = "validation_has_one_class"
    elif not binary and len(np.unique(y)) < len(cls):
        reasons["balanced_accuracy"] = "validation_missing_classes"
    else:
        metrics["balanced_accuracy"], why = _safe(lambda: balanced_accuracy_score(y, predicted))
        if why:
            reasons["balanced_accuracy"] = why
    return metrics, reasons, np.asarray([_native(v) for v in predicted], dtype=object)


def regression_metrics(y_true: Any, y_pred: Any) -> tuple[dict[str, float | None], dict[str, str]]:
    y = np.asarray(y_true, dtype=float)
    pred = np.asarray(y_pred, dtype=float)
    metrics: dict[str, float | None] = {name: None for name in REGRESSION_METRICS}
    reasons: dict[str, str] = {}
    for name, func in (
        ("mae", lambda: mean_absolute_error(y, pred)),
        ("mse", lambda: mean_squared_error(y, pred)),
        ("rmse", lambda: np.sqrt(mean_squared_error(y, pred))),
    ):
        metrics[name], why = _safe(func)
        if why:
            reasons[name] = why
    if len(y) < 2:
        reasons["r2"] = "requires_two_samples"
    elif len(np.unique(y)) < 2:
        reasons["r2"] = "constant_target"
    else:
        metrics["r2"], why = _safe(lambda: r2_score(y, pred), "r2_undefined")
        if why:
            reasons["r2"] = why
    return metrics, reasons


def summarize(folds: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    names = CLASSIFICATION_METRICS if folds and "roc_auc" in folds[0]["metrics"] else REGRESSION_METRICS
    summary: dict[str, dict[str, float | int | None]] = {}
    for name in names:
        values = [fold["metrics"].get(name) for fold in folds]
        finite = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
        summary[name] = {
            "mean": float(np.mean(finite)) if len(finite) else None,
            "std": float(np.std(finite, ddof=1)) if len(finite) > 1 else (0.0 if len(finite) == 1 else None),
            "n": int(len(finite)),
        }
    return summary
