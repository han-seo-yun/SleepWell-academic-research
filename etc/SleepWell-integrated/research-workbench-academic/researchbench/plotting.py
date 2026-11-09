"""Publication-oriented Matplotlib figures and result table exports."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import matplotlib as mpl
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from sklearn.metrics import confusion_matrix, precision_recall_curve, roc_curve
from sklearn.preprocessing import label_binarize


mpl.rcParams["font.family"] = "sans-serif"
mpl.rcParams["font.sans-serif"] = ["Malgun Gothic", "DejaVu Sans"]
mpl.rcParams["axes.unicode_minus"] = False

RATIO_METRICS = {
    "roc_auc", "average_precision", "accuracy", "balanced_accuracy", "recall",
    "specificity", "precision", "f1",
}
LOWER_IS_BETTER = {"mae", "mse", "rmse"}
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]


def _labels(lang: str) -> dict[str, str]:
    code = "ko" if lang == "ko" else "en"
    path = Path(__file__).with_name("locales") / f"plots_{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _tr(labels: dict[str, str], key: str, **values: Any) -> str:
    return labels.get(key, key).format(**values)


def _fig(options: dict[str, Any]) -> tuple[Figure, Any]:
    figure = Figure(
        figsize=(float(options.get("width", 8.0)), float(options.get("height", 5.4))),
        dpi=int(options.get("dpi", 100)), constrained_layout=True, facecolor="white",
    )
    return figure, figure.subplots()


def _finish(ax: Any, options: dict[str, Any], default_title: str, default_x: str = "", default_y: str = "") -> None:
    title = default_title if options.get("title") is None else options.get("title", "")
    xlabel = default_x if options.get("xlabel") is None else options.get("xlabel", "")
    ylabel = default_y if options.get("ylabel") is None else options.get("ylabel", "")
    size = float(options.get("font_size", 11))
    ax.set_title(title, fontsize=size + 2, fontweight="bold")
    ax.set_xlabel(xlabel, fontsize=size)
    ax.set_ylabel(ylabel, fontsize=size)
    ax.tick_params(labelsize=max(7, size - 1))
    annotation = options.get("annotation")
    if annotation:
        ax.text(0.01, -0.17, str(annotation), transform=ax.transAxes, fontsize=max(7, size - 2), va="top")


def _no_data(ax: Any, labels: dict[str, str]) -> None:
    ax.text(0.5, 0.5, labels["no_data"], ha="center", va="center", transform=ax.transAxes)
    ax.set_axis_off()


def _legend(ax: Any, options: dict[str, Any], **kwargs: Any) -> None:
    location = options.get("legend", "best")
    if location in {False, None, "none"}:
        return
    ax.legend(loc=str(location), **kwargs)


def _result_values(result: dict[str, Any], metric: str) -> list[float]:
    values = []
    for fold in result.get("folds", []):
        value = fold.get("metrics", {}).get(metric)
        if value is not None and np.isfinite(value):
            values.append(float(value))
    return values


def _prediction_frame(result: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(result.get("predictions", []))


def _comparison_compatibility(result: dict[str, Any], baseline: dict[str, Any], metric: str | None = None) -> tuple[bool, str]:
    config = result.get("config", {})
    baseline_config = baseline.get("config", {})
    checks = [
        (result.get("task") == baseline.get("task"), "different_task"),
        (result.get("split_id") == baseline.get("split_id"), "different_split"),
        (result.get("data_hash") == baseline.get("data_hash"), "different_data"),
        (result.get("phase") == baseline.get("phase"), "different_phase"),
        (config.get("target") == baseline_config.get("target"), "different_target"),
        (config.get("bin_edges") == baseline_config.get("bin_edges"), "different_target_binning"),
        (config.get("bin_labels") == baseline_config.get("bin_labels"), "different_target_binning"),
        (config.get("positive_class") == baseline_config.get("positive_class"), "different_positive_class"),
        (config.get("average", "macro") == baseline_config.get("average", "macro"), "different_metric_average"),
    ]
    for passed, reason in checks:
        if not passed:
            return False, reason
    current_rows = sorted(prediction.get("row_id") for prediction in result.get("predictions", []))
    baseline_rows = sorted(prediction.get("row_id") for prediction in baseline.get("predictions", []))
    if current_rows and baseline_rows and current_rows != baseline_rows:
        return False, "different_evaluation_rows"
    return True, "comparable"


def comparison_table(results: Iterable[dict[str, Any]], baseline_id: str | None, lang: str = "ko") -> pd.DataFrame:
    """Return one row per experiment and metric, preserving task boundaries."""
    items = list(results or [])
    baseline = next((item for item in items if item.get("id") == baseline_id), None)
    rows: list[dict[str, Any]] = []
    for result in items:
        metrics = sorted({key for fold in result.get("folds", []) for key in fold.get("metrics", {})})
        for metric in metrics:
            comparable, reason = (True, "baseline") if result is baseline else (
                _comparison_compatibility(result, baseline, metric) if baseline else (False, "baseline_not_found")
            )
            values = _result_values(result, metric)
            mean = float(np.mean(values)) if values else None
            sd = float(np.std(values, ddof=1)) if len(values) > 1 else None
            baseline_values = _result_values(baseline, metric) if comparable and baseline else []
            baseline_mean = float(np.mean(baseline_values)) if baseline_values else None
            delta = mean - baseline_mean if mean is not None and baseline_mean is not None else None
            improvement = (-delta if metric in LOWER_IS_BETTER else delta) if delta is not None else None
            relative = delta / abs(baseline_mean) * 100.0 if delta is not None and baseline_mean not in (None, 0) else None
            rows.append(
                {
                    "experiment_id": result.get("id"), "experiment": result.get("name", result.get("id")),
                    "task": result.get("task"), "phase": result.get("phase"), "metric": metric,
                    "mean": mean, "fold_sd": sd, "n_folds": len(values), "baseline_mean": baseline_mean,
                    "delta_raw": delta, "delta_percentage_points": delta * 100.0 if delta is not None and metric in RATIO_METRICS else None,
                    "relative_change_pct": relative, "improvement": improvement,
                    "higher_is_better": metric not in LOWER_IS_BETTER, "comparable": comparable,
                    "comparison_reason": reason, "duration_seconds": result.get("duration"),
                }
            )
    table = pd.DataFrame(rows)
    if not table.empty:
        labels = _labels(lang)
        table["task_display"] = table["task"].map(lambda value: labels.get(f"task_{value}", value))
        table["phase_display"] = table["phase"].map(lambda value: labels.get(f"phase_{value}", value))
        table["metric_display"] = table["metric"].map(lambda value: labels.get(f"metric_{value}", value))
        table["comparison_reason_display"] = table["comparison_reason"].map(lambda value: labels.get(f"reason_{value}", value))
    return table


def make_figure(
    kind: str, *, df: pd.DataFrame | None = None, results: Iterable[dict[str, Any]] | None = None,
    metric: str = "roc_auc", lang: str = "ko", options: dict[str, Any] | None = None,
    target: str | None = None, feature: str | None = None, baseline_id: str | None = None,
) -> Figure:
    options = dict(options or {})
    labels = _labels(lang)
    if df is not None and options.get("annotation") is None:
        scope = options.get("scope", "displayed")
        scope_label = labels.get(f"scope_{scope}", str(scope))
        options["annotation"] = f"{scope_label} · {_tr(labels, 'n', n=len(df))}"
    result_list = list(results or [])
    fig, ax = _fig(options)
    color = options.get("color", PALETTE[0])

    if kind == "correlation":
        method = options.get("corr_method", "pearson")
        numeric = df.select_dtypes(include=np.number) if df is not None else pd.DataFrame()
        if numeric.shape[1] < 2:
            _no_data(ax, labels)
            return fig
        correlation = numeric.corr(method=method)
        image = ax.imshow(correlation, vmin=-1, vmax=1, cmap="RdBu_r", aspect="auto")
        ax.set_xticks(range(len(correlation.columns)), correlation.columns, rotation=45, ha="right")
        ax.set_yticks(range(len(correlation.index)), correlation.index)
        if len(correlation.columns) <= 12:
            for row in range(len(correlation.index)):
                for column in range(len(correlation.columns)):
                    ax.text(column, row, f"{correlation.iloc[row, column]:.2f}", ha="center", va="center", fontsize=8)
        fig.colorbar(image, ax=ax, label=method.title())
        _finish(ax, options, _tr(labels, "correlation_title", method=method.title()))
    elif kind == "missing":
        if df is None or df.shape[1] == 0:
            _no_data(ax, labels); return fig
        missing = df.isna().mean().mul(100).sort_values(ascending=False)
        ax.bar(range(len(missing)), missing.values, color=color, edgecolor="black", linewidth=.4)
        ax.set_xticks(range(len(missing)), missing.index, rotation=45, ha="right")
        _finish(ax, options, labels["missing_title"], labels["experiment"], labels["missing_y"])
    elif kind == "histogram":
        if df is None or feature not in df or not pd.api.types.is_numeric_dtype(df[feature]):
            _no_data(ax, labels); return fig
        values = pd.to_numeric(df[feature], errors="coerce").dropna()
        ax.hist(values, bins="auto", color=color, alpha=.75, edgecolor="white")
        _finish(ax, options, _tr(labels, "histogram_title", feature=feature), feature, labels["count"])
    elif kind == "box":
        if df is None or feature not in df:
            _no_data(ax, labels); return fig
        if target and target in df and (options.get("task") == "classification" or not pd.api.types.is_numeric_dtype(df[target])):
            groups, names = [], []
            for name, group in df[[feature, target]].dropna().groupby(target, observed=True):
                values = pd.to_numeric(group[feature], errors="coerce").dropna().to_numpy()
                if len(values): groups.append(values); names.append(str(name))
            if groups:
                ax.boxplot(groups, tick_labels=names, showfliers=True)
        else:
            values = pd.to_numeric(df[feature], errors="coerce").dropna().to_numpy()
            if len(values): ax.boxplot([values], tick_labels=[feature], showfliers=True)
        if not ax.lines and not ax.patches:
            _no_data(ax, labels); return fig
        _finish(ax, options, _tr(labels, "box_title", feature=feature), target or "", feature)
    elif kind == "target":
        if df is None or not target or target not in df:
            _no_data(ax, labels); return fig
        values = df[target].dropna()
        continuous = options.get("task") == "regression" or (
            options.get("task") not in {"classification", "regression"}
            and pd.api.types.is_numeric_dtype(values) and values.nunique() > min(20, max(5, len(values) // 10))
        )
        if continuous:
            ax.hist(values, bins="auto", color=color, alpha=.75, edgecolor="white")
            xlabel, ylabel = target, labels["count"]
        else:
            counts = values.value_counts(dropna=False)
            ax.bar(range(len(counts)), counts.values, color=color, edgecolor="black", linewidth=.4)
            ax.set_xticks(range(len(counts)), [str(value) for value in counts.index], rotation=30, ha="right")
            xlabel, ylabel = target, labels["count"]
        _finish(ax, options, _tr(labels, "target_title", target=target), xlabel, ylabel)
    elif kind == "scatter":
        if df is None or feature not in df or not target or target not in df:
            _no_data(ax, labels); return fig
        values = df[[feature, target]].apply(pd.to_numeric, errors="coerce").dropna()
        ax.scatter(values[feature], values[target], alpha=.55, s=24, color=color, edgecolors="none", rasterized=len(values) > 5000)
        target_label = f"{target} ({options.get('target_unit')})" if options.get("target_unit") else target
        _finish(ax, options, _tr(labels, "scatter_title", feature=feature, target=target), feature, target_label)
    elif kind in {"comparison", "delta"}:
        baseline = next((item for item in result_list if item.get("id") == baseline_id), None)
        if kind == "delta" and baseline is None:
            _no_data(ax, labels); return fig
        reference = baseline or (result_list[0] if result_list else None)
        selected_task = baseline.get("task") if baseline else (result_list[0].get("task") if result_list else None)
        usable = [item for item in result_list if item.get("task") == selected_task]
        if reference:
            usable = [item for item in usable if _comparison_compatibility(item, reference, metric)[0]]
        if kind == "delta":
            baseline_folds = {fold.get("fold"): fold for fold in baseline.get("folds", [])}
            usable_values = []
            for item in usable:
                paired = []
                for fold in item.get("folds", []):
                    reference_fold = baseline_folds.get(fold.get("fold"))
                    if reference_fold is None or fold.get("valid_rows") != reference_fold.get("valid_rows"):
                        continue
                    value = fold.get("metrics", {}).get(metric)
                    reference_value = reference_fold.get("metrics", {}).get(metric)
                    if value is not None and reference_value is not None and np.isfinite(value) and np.isfinite(reference_value):
                        paired.append(float(value) - float(reference_value))
                if paired:
                    usable_values.append((item, paired))
        else:
            values = [_result_values(item, metric) for item in usable]
            usable_values = [(item, item_values) for item, item_values in zip(usable, values) if item_values]
        if not usable_values:
            _no_data(ax, labels); return fig
        base_mean = np.mean(_result_values(baseline, metric)) if baseline and _result_values(baseline, metric) else 0.0
        for index, (item, fold_values) in enumerate(usable_values):
            plotted = np.asarray(fold_values)
            mean = float(np.mean(plotted)); sd = float(np.std(plotted, ddof=1)) if len(plotted) > 1 else 0.0
            ax.errorbar(index, mean, yerr=sd if len(plotted) > 1 else None, fmt="s", capsize=5, color=PALETTE[index % len(PALETTE)])
            jitter = np.linspace(-.08, .08, len(plotted)) if len(plotted) > 1 else np.array([0.0])
            ax.scatter(index + jitter, plotted, color=PALETTE[index % len(PALETTE)], alpha=.75, marker="o")
        ax.set_xticks(range(len(usable_values)), [str(item.get("name", item.get("id"))) for item, _ in usable_values], rotation=30, ha="right")
        ax.axhline(0 if kind == "delta" else base_mean, linestyle="--", color="#666666", linewidth=1)
        default_title = _tr(labels, "delta_title" if kind == "delta" else "comparison_title", metric=labels.get(f"metric_{metric}", metric))
        displayed_rows = {
            prediction.get("row_id") for item, _ in usable_values for prediction in item.get("predictions", [])
        }
        meaning = labels["paired_fold_values"] if kind == "delta" else labels["fold_values"]
        annotation = f"{meaning} · {_tr(labels, 'n_fold', n=len(displayed_rows), folds=max(len(v) for _, v in usable_values))}"
        if options.get("annotation") is None: options["annotation"] = annotation
        _finish(ax, options, default_title, labels["experiment"], labels["delta"] if kind == "delta" else labels["score"])
    elif kind in {"roc", "pr"}:
        if not result_list:
            _no_data(ax, labels); return fig
        drew = False
        for result_index, result in enumerate(result_list):
            predictions = _prediction_frame(result)
            classes = result.get("classes", [])
            if predictions.empty or "probabilities" not in predictions or not classes:
                continue
            probs = np.asarray(predictions["probabilities"].tolist(), dtype=float)
            actual = predictions["y_true"].to_numpy()
            if probs.ndim != 2 or probs.shape[1] != len(classes):
                continue
            if len(classes) == 2:
                positive = result.get("config", {}).get("positive_class", classes[-1])
                try: pos_index = list(classes).index(positive)
                except ValueError: continue
                binary = (actual == positive).astype(int)
                if len(np.unique(binary)) < 2: continue
                if kind == "roc":
                    x, y, _ = roc_curve(binary, probs[:, pos_index])
                else:
                    precision_values, recall_values, _ = precision_recall_curve(binary, probs[:, pos_index])
                    x, y = recall_values, precision_values
                line_color = PALETTE[result_index % len(PALETTE)]
                ax.plot(x, y, color=line_color, label=str(result.get("name", result.get("id"))))
                threshold = float(result.get("config", {}).get("threshold", .5))
                predicted_positive = probs[:, pos_index] >= threshold
                true_positive = np.sum(predicted_positive & (binary == 1))
                if kind == "roc":
                    operating_x = np.sum(predicted_positive & (binary == 0)) / max(1, np.sum(binary == 0))
                    operating_y = true_positive / max(1, np.sum(binary == 1))
                else:
                    operating_x = true_positive / max(1, np.sum(binary == 1))
                    operating_y = true_positive / max(1, np.sum(predicted_positive))
                ax.scatter([operating_x], [operating_y], color=line_color, marker="X", s=55,
                           label=_tr(labels, "threshold", value=threshold))
                drew = True
            else:
                binary = label_binarize(actual, classes=classes)
                for class_index, class_name in enumerate(classes):
                    if len(np.unique(binary[:, class_index])) < 2: continue
                    if kind == "roc":
                        x, y, _ = roc_curve(binary[:, class_index], probs[:, class_index])
                    else:
                        precision_values, recall_values, _ = precision_recall_curve(binary[:, class_index], probs[:, class_index])
                        x, y = recall_values, precision_values
                    ax.plot(x, y, color=PALETTE[class_index % len(PALETTE)], label=f"{result.get('name', result.get('id'))}: {class_name}")
                    drew = True
        if not drew:
            _no_data(ax, labels); return fig
        if kind == "roc":
            ax.plot([0, 1], [0, 1], "--", color="#777777", label=labels["random"])
            _finish(ax, options, labels["roc_title"], labels["fpr"], labels["tpr"])
        else:
            _finish(ax, options, labels["pr_title"], labels["recall"], labels["precision"])
        if options.get("annotation") is None:
            total = sum(len(result.get("predictions", [])) for result in result_list)
            ax.text(.01, -.17, _tr(labels, "n", n=total), transform=ax.transAxes,
                    fontsize=max(7, float(options.get("font_size", 11)) - 2), va="top")
        _legend(ax, options, fontsize=max(7, float(options.get("font_size", 11)) - 2))
    elif kind == "confusion":
        if not result_list:
            _no_data(ax, labels); return fig
        result = result_list[0]; predictions = _prediction_frame(result); classes = result.get("classes", [])
        if predictions.empty or not classes:
            _no_data(ax, labels); return fig
        matrix = confusion_matrix(predictions["y_true"], predictions["y_pred"], labels=classes)
        image = ax.imshow(matrix, cmap="Blues")
        ax.set_xticks(range(len(classes)), [str(value) for value in classes], rotation=30, ha="right")
        ax.set_yticks(range(len(classes)), [str(value) for value in classes])
        for row in range(len(classes)):
            for column in range(len(classes)):
                ax.text(column, row, str(matrix[row, column]), ha="center", va="center")
        fig.colorbar(image, ax=ax)
        _finish(ax, options, labels["confusion_title"], labels["predicted"], labels["actual"])
        if options.get("annotation") is None:
            threshold = result.get("config", {}).get("threshold")
            parts = [_tr(labels, "n", n=len(predictions))]
            if threshold is not None and len(classes) == 2:
                parts.append(_tr(labels, "threshold", value=float(threshold)))
            ax.text(.01, -.17, " · ".join(parts), transform=ax.transAxes,
                    fontsize=max(7, float(options.get("font_size", 11)) - 2), va="top")
    elif kind in {"predicted", "residuals"}:
        if not result_list:
            _no_data(ax, labels); return fig
        predictions = _prediction_frame(result_list[0])
        if predictions.empty:
            _no_data(ax, labels); return fig
        actual = pd.to_numeric(predictions["y_true"], errors="coerce"); predicted = pd.to_numeric(predictions["y_pred"], errors="coerce")
        valid = actual.notna() & predicted.notna(); actual, predicted = actual[valid], predicted[valid]
        result = result_list[0]
        unit = options.get("target_unit") or result.get("config", {}).get("target_unit")
        actual_label = f"{labels['actual']} ({unit})" if unit else labels["actual"]
        predicted_label = f"{labels['predicted']} ({unit})" if unit else labels["predicted"]
        residual_label = f"{labels['residual']} ({unit})" if unit else labels["residual"]
        if kind == "predicted":
            ax.scatter(actual, predicted, alpha=.55, color=color, edgecolors="none")
            low, high = min(actual.min(), predicted.min()), max(actual.max(), predicted.max())
            ax.plot([low, high], [low, high], "--", color="#555555")
            _finish(ax, options, labels["predicted_title"], actual_label, predicted_label)
        else:
            ax.scatter(predicted, actual - predicted, alpha=.55, color=color, edgecolors="none")
            ax.axhline(0, linestyle="--", color="#555555")
            _finish(ax, options, labels["residual_title"], predicted_label, residual_label)
        if options.get("annotation") is None:
            folds = predictions["fold"].nunique() if "fold" in predictions else len(result.get("folds", []))
            ax.text(.01, -.17, _tr(labels, "n_fold", n=len(predictions), folds=folds), transform=ax.transAxes,
                    fontsize=max(7, float(options.get("font_size", 11)) - 2), va="top")
    elif kind == "learning":
        if not result_list:
            _no_data(ax, labels); return fig
        history = pd.DataFrame(result_list[0].get("history", []))
        if history.empty or "epoch" not in history:
            _no_data(ax, labels); return fig
        for column, name, line_color in [("train_loss", labels["train"], PALETTE[0]), ("valid_loss", labels["validation"], PALETTE[1])]:
            if column not in history or history[column].dropna().empty: continue
            grouped = history.groupby("epoch")[column]
            mean, sd = grouped.mean(), grouped.std().fillna(0)
            ax.plot(mean.index, mean.values, label=name, color=line_color)
            if history["fold"].nunique() > 1:
                ax.fill_between(mean.index, mean - sd, mean + sd, color=line_color, alpha=.18, label=f"{name} fold SD")
        loss_metric = next((value for value in history.get("loss_metric", pd.Series(dtype=object)).dropna().unique()), None)
        loss_label = labels.get(f"loss_{loss_metric}", labels["loss"])
        _finish(ax, options, labels["learning_title"], labels["epoch"], loss_label)
        _legend(ax, options)
    else:
        raise ValueError(f"unsupported_figure_kind:{kind}")
    ax.grid(True, alpha=.2)
    return fig


def export_figure(fig: Figure, path: str | Path, dpi: int = 300) -> None:
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() not in {".png", ".pdf", ".svg"}:
        raise ValueError("figure_format_must_be_png_pdf_or_svg")
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=dpi if target.suffix.lower() == ".png" else None, bbox_inches="tight", facecolor="white")


def export_tables(
    results: Iterable[dict[str, Any]], path: str | Path, baseline_id: str | None = None, lang: str = "ko"
) -> None:
    items = list(results or [])
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    comparison = comparison_table(items, baseline_id, lang)
    if not comparison.empty:
        comparison = comparison.rename(columns=_labels(lang).get("table_headers", {}))
    folds, predictions, history = [], [], []
    for result in items:
        for fold in result.get("folds", []):
            folds.append({"experiment_id_raw": result.get("id"), "experiment": result.get("name"), "task_key": result.get("task"), "fold": fold.get("fold"), **fold.get("metrics", {})})
        for prediction in result.get("predictions", []):
            predictions.append({"experiment_id_raw": result.get("id"), "experiment": result.get("name"), **prediction})
        for event in result.get("history", []):
            history.append({"experiment_id_raw": result.get("id"), "experiment": result.get("name"), **event})
    labels = _labels(lang)
    metric_columns = sorted({key for row in folds for key in row if key not in {"experiment_id_raw", "experiment", "task_key", "fold"}})
    fold_table = pd.DataFrame(folds, columns=["experiment_id_raw", "experiment", "task_key", "fold", *metric_columns]).rename(columns=labels.get("fold_headers", {}))
    prediction_table = pd.DataFrame(predictions, columns=["experiment_id_raw", "experiment", "row_id", "fold", "y_true", "y_pred", "probabilities"]).rename(columns=labels.get("prediction_headers", {}))
    history_columns = ["experiment_id_raw", "experiment", "fold", "epoch", "train_loss", "valid_loss", "optimizer_loss", "loss_metric"]
    history_table = pd.DataFrame(history, columns=history_columns).rename(columns=labels.get("history_headers", {}))
    if target.suffix.lower() == ".csv":
        comparison.to_csv(target, index=False, encoding="utf-8-sig")
        base = target.with_suffix("")
        fold_table.to_csv(base.with_name(base.name + "_folds.csv"), index=False, encoding="utf-8-sig")
        prediction_table.to_csv(base.with_name(base.name + "_predictions.csv"), index=False, encoding="utf-8-sig")
        history_table.to_csv(base.with_name(base.name + "_history.csv"), index=False, encoding="utf-8-sig")
        return
    if target.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("table_format_must_be_csv_or_xlsx")
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        comparison.to_excel(writer, sheet_name="comparison" if lang != "ko" else "실험 비교", index=False)
        fold_table.to_excel(writer, sheet_name="fold_metrics" if lang != "ko" else "fold 지표", index=False)
        prediction_table.to_excel(writer, sheet_name="predictions" if lang != "ko" else "예측값", index=False)
        history_table.to_excel(writer, sheet_name="learning_history" if lang != "ko" else "학습 이력", index=False)

