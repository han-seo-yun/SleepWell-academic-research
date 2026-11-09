"""Native Matplotlib views and exports for the preserved SleepWell research results."""

from __future__ import annotations
from .result_adapter import model_color

import json
from pathlib import Path
from typing import Any, Iterable

import matplotlib as mpl
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from .plotting import export_figure


mpl.rcParams["font.family"] = "sans-serif"
mpl.rcParams["font.sans-serif"] = ["Malgun Gothic", "DejaVu Sans"]
mpl.rcParams["axes.unicode_minus"] = False

PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]

OBSERVED_COLUMNS = [
    "actual_onset_offset_min",
    "actual_wake_offset_min",
    "actual_interval_min",
]
PERSONALIZATION_FIELDS = {
    "personal_weight": "personal_weight",
    "sleep_threshold": "sleep_threshold",
    "wake_threshold": "wake_threshold",
    "phase_min": "phase_min",
    "habit_bed_offset": "habit_bed_offset",
    "habit_wake_offset": "habit_wake_offset",
}


def _labels(lang: str) -> dict[str, str]:
    catalog = json.loads(
        (Path(__file__).with_name("locales") / "academic_plots.json").read_text(encoding="utf-8")
    )
    return catalog["ko" if lang == "ko" else "en"]


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    """Accept an academic envelope, a persisted run record, or canonical raw output."""
    value = result.get("result", result)
    if "raw" in value or "summary" in value and isinstance(value.get("summary"), list):
        return value
    raw = value
    return {
        "mode": raw.get("mode", "retrospective"),
        "raw": raw,
        "summary": raw.get("metrics", []),
        "provenance": raw.get("provenance", {}),
        "warnings": raw.get("warnings", []),
    }


def _raw(result: dict[str, Any]) -> dict[str, Any]:
    return _envelope(result).get("raw") or {}


def _summary(result: dict[str, Any]) -> list[dict[str, Any]]:
    envelope = _envelope(result)
    rows = envelope.get("summary")
    if not isinstance(rows, list):
        rows = (envelope.get("raw") or {}).get("metrics", [])
    return [dict(row) for row in rows if isinstance(row, dict)]


def _predictions(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _raw(result)
    rows = raw.get("predictions") or raw.get("forecast_predictions") or []
    if rows:
        return [dict(row) for row in rows if isinstance(row, dict)]
    # Prospective demonstrations retain immutable forecasts and assessments
    # separately. Join them by forecast id without changing either record.
    assessments = {
        row.get("forecast_id"): row for row in raw.get("assessments", [])
        if isinstance(row, dict) and row.get("eligible")
    }
    flattened = []
    for forecast in raw.get("forecasts", []):
        if not isinstance(forecast, dict):
            continue
        assessment = assessments.get(forecast.get("id"))
        actual = (assessment or {}).get("outcome_snapshot") or {}
        for model_id, prediction in (forecast.get("predictions") or {}).items():
            flattened.append({
                "participant_id": forecast.get("enrollment_identity", "prospective"),
                "cutoff": forecast.get("cutoff"), "model_id": model_id,
                **dict(prediction),
                "actual_onset_offset_min": actual.get("onset_offset_min"),
                "actual_wake_offset_min": actual.get("wake_offset_min"),
                "actual_interval_min": actual.get("interval_min"),
            })
        online = (forecast.get("online_personalization") or {}).get("predictions") or {}
        for model_id, prediction in online.items():
            flattened.append({
                "participant_id": forecast.get("enrollment_identity", "prospective"),
                "cutoff": forecast.get("cutoff"), "model_id": f"online_{model_id}",
                **dict(prediction),
                "actual_onset_offset_min": actual.get("onset_offset_min"),
                "actual_wake_offset_min": actual.get("wake_offset_min"),
                "actual_interval_min": actual.get("interval_min"),
            })
    return flattened


def _personalization_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _raw(result)
    rows = [dict(row) for row in raw.get("personalization", []) if isinstance(row, dict)]
    for forecast in raw.get("forecasts", []):
        if not isinstance(forecast, dict):
            continue
        online = forecast.get("online_personalization") or {}
        for model_id, adjustment in (online.get("adjustments") or {}).items():
            rows.append({
                "cutoff": forecast.get("cutoff"), "model_id": f"online_{model_id}",
                "policy_version": online.get("policy_version"), **dict(adjustment or {}),
            })
    return rows


def _observed_nights(result: dict[str, Any]) -> pd.DataFrame:
    """Return one complete observed record per participant and cutoff.

    Academic prediction output repeats the same observed night for every model
    arm.  Correlation is descriptive of observed variables, so those model-arm
    repetitions must not change the sample.
    """
    rows = []
    for row in _predictions(result):
        values = {column: _finite(row.get(column)) for column in OBSERVED_COLUMNS}
        if any(value is None for value in values.values()):
            continue
        participant = row.get("participant_id")
        cutoff = row.get("cutoff")
        if participant is None or cutoff is None:
            continue
        rows.append({
            "participant_id": str(participant),
            "cutoff": str(cutoff),
            **values,
        })
    if not rows:
        return pd.DataFrame(columns=["participant_id", "cutoff", *OBSERVED_COLUMNS])
    return pd.DataFrame(rows).drop_duplicates(
        subset=["participant_id", "cutoff"], keep="first"
    ).reset_index(drop=True)


def _model_label(model_id: str, labels: dict[str, str]) -> str:
    model_id = str(model_id)
    if model_id == "baseline.recent7":
        return labels["recent7"]
    if model_id in labels:
        return labels[model_id]
    parts = model_id.split(".")
    cohort = parts[0].upper() if len(parts) > 1 and parts[0] in {"cmi", "mesa"} else ""
    if len(parts) > 1 and parts[0] == "macro":
        cohort = labels["macro_cohort"]
    kind = parts[-1]
    for suffix in ("_cmi", "_mesa"):
        if kind.endswith(suffix):
            cohort = suffix[1:].upper()
            kind = kind[:-len(suffix)]
            break
    online = kind.startswith("online_")
    if online:
        kind = kind[len("online_"):]
    display = labels.get(kind, kind.replace("_", " "))
    if online:
        display = labels["online_prefix"].format(model=display)
    return f"{cohort} · {display}" if cohort else display


def model_label(model_id: str, lang: str = "ko") -> str:
    """Return the same localized arm name used by figures and result tables."""
    return _model_label(model_id, _labels(lang))


def _figure(options: dict[str, Any]) -> tuple[Figure, Any]:
    figure = Figure(
        figsize=(float(options.get("width", 9.2)), float(options.get("height", 5.8))),
        dpi=int(options.get("dpi", 100)), facecolor="white",
    )
    return figure, figure.subplots()


def _add_footer(figure: Figure, options: dict[str, Any]) -> None:
    lines = [str(value) for value in
             (options.get("annotation"), options.get("_evidence_caption")) if value]
    if not lines:
        figure.subplots_adjust(bottom=.16, left=.11, right=.97, top=.90)
        return
    # The footer belongs to the Figure, not the Axes.  A reserved band keeps it
    # independent of rotated tick labels and the x-axis label in every export.
    # Reserve enough separation for rotated English model labels plus the
    # x-axis label at short, wide in-app canvas sizes (for example 1230x550).
    # The footer remains in its own figure band and never competes for Axes
    # layout space.
    figure.subplots_adjust(bottom=.39, left=.11, right=.97, top=.90)
    figure.text(.5, .025, "\n".join(lines), ha="center", va="bottom",
                fontsize=max(7, float(options.get("font_size", 11)) - 2), wrap=True)


def _finish(ax: Any, options: dict[str, Any], title: str, xlabel: str, ylabel: str) -> None:
    title = title if options.get("title") is None else str(options.get("title", ""))
    xlabel = xlabel if options.get("xlabel") is None else str(options.get("xlabel", ""))
    ylabel = ylabel if options.get("ylabel") is None else str(options.get("ylabel", ""))
    size = float(options.get("font_size", 11))
    ax.set_title(title, fontsize=size + 2, fontweight="bold")
    ax.set_xlabel(xlabel, fontsize=size)
    ax.set_ylabel(ylabel, fontsize=size)
    ax.tick_params(labelsize=max(7, size - 1))
    _add_footer(ax.figure, options)


def _empty(ax: Any, labels: dict[str, str], options: dict[str, Any]) -> None:
    ax.text(.5, .5, labels["no_data"], ha="center", va="center", transform=ax.transAxes)
    ax.set_axis_off()
    _add_footer(ax.figure, options)


def _evidence_caption(result: dict[str, Any], labels: dict[str, str]) -> str | None:
    envelope = _envelope(result)
    provenance = envelope.get("provenance") or {}
    if provenance.get("input_synthetic") is True or envelope.get("mode") == "prospective_demo":
        return labels["synthetic_caption"]
    if envelope.get("mode") == "pooled_retraining":
        return labels["pooled_evidence_caption"]
    if envelope.get("mode") == "controlled_comparison":
        return labels["controlled_evidence_caption"]
    if envelope.get("mode") == "cross_cohort_evaluation":
        return labels["cross_evidence_caption"]
    if envelope.get("mode") in {"participant_revalidation", "personalization_comparison"}:
        return labels["revalidation_evidence_caption"]
    stored_reports = {
        "continuous_constants_validation_v2", "full_validity_tables_v1",
        "personalization_validation_v1", "target_validity_validation_v1",
    }
    if envelope.get("mode") == "report" and envelope.get("report_type") in stored_reports:
        return labels["stored_evidence_caption"]
    return None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _metric_prediction_columns(metric: str) -> tuple[str, str]:
    if metric.startswith("wake"):
        return "actual_wake_offset_min", "wake_offset_min"
    return "actual_onset_offset_min", "onset_offset_min"


def _study_model_name(
    row: dict[str, Any], labels: dict[str, str], *, controlled: bool = False,
) -> str:
    model = str(row.get("model", ""))
    if controlled and model in {"pooled_data", "pooled_hybrid"}:
        return labels[f"controlled_{model}"]
    if model in labels:
        return labels[model]
    display = _model_label(row.get("model_id", model), labels)
    return display.split(" · ", 1)[-1]


def _faceted_study_comparison(
    result: dict[str, Any], metric: str, labels: dict[str, str], options: dict[str, Any],
) -> Figure:
    rows = [(row, _finite(row.get(metric))) for row in _summary(result)]
    rows = [(row, value) for row, value in rows if value is not None]
    figure = Figure(
        figsize=(max(12.6, float(options.get("width", 9.2))),
                 max(5.8, float(options.get("height", 5.8)))),
        dpi=int(options.get("dpi", 100)), facecolor="white",
    )
    axes = figure.subplots(1, 3, sharey=True)
    cohorts = (("CMI", "CMI"), ("MESA", "MESA"), ("MACRO", labels["macro_cohort"]))
    joint_ci = metric == "joint_mae_min"
    mode = _envelope(result).get("mode")
    controlled = mode == "controlled_comparison"
    model_order = (
        ["separate_data", "separate_hybrid", "separate_shared_data", "separate_shared_hybrid",
         "pooled_data", "pooled_hybrid", "pooled_source_data", "pooled_source_hybrid"]
        if controlled else
        ["cmi_data", "mesa_data", "pooled_data", "mean50_data", "known_source_data",
         "recent_median", "cmi_hybrid", "mesa_hybrid", "pooled_hybrid", "mean50_hybrid",
         "known_source_hybrid", "recent_median_cmi", "recent_median_mesa"]
        if mode == "cross_cohort_evaluation" else
        ["cmi_data", "mesa_data", "pooled_data", "mean50_data", "known_source_data",
         "recent_median", "pooled_source_data", "separate_shared_data",
         "cmi_hybrid", "mesa_hybrid", "pooled_hybrid", "mean50_hybrid",
         "known_source_hybrid", "recent_median_cmi", "recent_median_mesa",
         "pooled_source_hybrid", "separate_shared_hybrid"]
        if mode == "participant_revalidation" else
        ["data33", "data33_residual", "hybrid39", "hybrid39_personalized"]
        if mode == "personalization_comparison" else
        ["existing_data", "existing_hybrid", "existing_physical", "recent_median",
         "pooled_data", "pooled_hybrid"]
    )
    order_index = {model: index for index, model in enumerate(model_order)}
    plotted = False
    global_top = 0.0
    for axis, (cohort, title) in zip(axes, cohorts):
        group = [(row, value) for row, value in rows
                 if str(row.get("cohort", "")).upper() == cohort]
        group.sort(key=lambda item: order_index.get(str(item[0].get("model", "")), len(order_index)))
        if not group:
            axis.text(.5, .5, labels["no_data"], ha="center", va="center", transform=axis.transAxes)
            axis.set_title(title, fontweight="bold")
            continue
        plotted = True
        names = [_study_model_name(row, labels, controlled=controlled) for row, _ in group]
        values = np.asarray([value for _, value in group], dtype=float)
        colors = [PALETTE[order_index.get(str(row.get("model", "")), 0) % len(PALETTE)]
                  for row, _ in group]
        error = None
        if joint_ci:
            lows = np.asarray([_finite(row.get("ci95_low")) for row, _ in group], dtype=object)
            highs = np.asarray([_finite(row.get("ci95_high")) for row, _ in group], dtype=object)
            if all(value is not None for value in lows) and all(value is not None for value in highs):
                low_values, high_values = lows.astype(float), highs.astype(float)
                error = np.vstack([values - low_values, high_values - values])
        x = np.arange(len(group))
        axis.bar(x, values, yerr=error, capsize=4 if error is not None else 0,
                 color=colors, edgecolor="black", linewidth=.4)
        axis.set_xticks(x, names, rotation=28, ha="right")
        axis.set_title(title, fontweight="bold")
        for index, value in enumerate(values):
            upper = error[1, index] if error is not None else 0
            global_top = max(global_top, float(value + upper))
            axis.text(index, value + upper, f"{value:.2f}", ha="center", va="bottom", fontsize=8)
    if not plotted:
        for axis in axes:
            axis.set_axis_off()
    else:
        axes[0].set_ylim(0, global_top * 1.08 if global_top > 0 else 1)
    size = float(options.get("font_size", 11))
    figure.suptitle(
        (str(options["title"]) if options.get("title") is not None
         else labels["comparison_title"].format(metric=labels.get(metric, metric))),
        fontsize=size + 3, fontweight="bold",
    )
    axes[0].set_ylabel(str(options.get("ylabel") or labels["value"]), fontsize=size)
    for axis in axes:
        axis.set_xlabel(str(options.get("xlabel") or labels["model"]), fontsize=size)
        axis.tick_params(labelsize=max(7, size - 2))
    if options.get("annotation") is None:
        mode = _envelope(result).get("mode")
        scope = (labels["revalidation_scope"] if mode in {"participant_revalidation", "personalization_comparison"} else
                 labels["controlled_scope"] if mode == "controlled_comparison" else
                 labels["cross_scope"] if mode == "cross_cohort_evaluation" else
                 labels["pooled_scope"])
        ci_note = labels["joint_ci_note"] if joint_ci else labels["endpoint_no_ci_note"]
        options["annotation"] = f"{scope} · {ci_note}"
    _add_footer(figure, options)
    return figure


def _paired_difference_figure(
    result: dict[str, Any], labels: dict[str, str], options: dict[str, Any],
) -> Figure:
    envelope = _envelope(result)
    mode = envelope.get("mode")
    raw = envelope.get("raw") or {}
    default_pair = (("separate_data", "pooled_data")
                    if mode == "controlled_comparison" else
                    ("data33", "data33_residual")
                    if mode == "personalization_comparison" else
                    ("recent_median", "pooled_data")
                    if mode == "participant_revalidation" else
                    ("recent_median", "mean50_data")
                    if mode == "cross_cohort_evaluation" else
                    ("existing_data", "pooled_data"))
    primary_pair = raw.get("primary_pair") or default_pair
    reference, model = map(str, primary_pair)
    comparisons = raw.get("paired_comparisons", [])
    selected = {str(row.get("cohort", "")).upper(): row for row in comparisons
                if row.get("reference") == reference and row.get("model") == model}
    figure, axis = _figure(options)
    cohorts = [cohort for cohort in ("CMI", "MESA", "MACRO") if cohort in selected]
    if not cohorts:
        _empty(axis, labels, options)
        return figure
    increase = np.asarray([-float(selected[c]["improvement_min"]) for c in cohorts])
    low = np.asarray([-float(selected[c]["ci95_high"]) for c in cohorts])
    high = np.asarray([-float(selected[c]["ci95_low"]) for c in cohorts])
    error = np.vstack([increase - low, high - increase])
    y = np.arange(len(cohorts))
    for index, cohort in enumerate(cohorts):
        axis.errorbar(increase[index], y[index],
                      xerr=np.asarray([[error[0, index]], [error[1, index]]]),
                      fmt="o", markersize=7, capsize=5, linewidth=2,
                      color=PALETTE[index])
        axis.text(high[index] + .04, y[index],
                  f"{increase[index]:+.2f} [{low[index]:+.2f}, {high[index]:+.2f}]",
                  va="center", fontsize=8)
    axis.axvline(0, color="#333333", linestyle="--", linewidth=1)
    axis.set_yticks(y, [labels["macro_cohort"] if c == "MACRO" else c for c in cohorts])
    axis.invert_yaxis()
    margin = max(.25, float(high.max() - low.min()) * .15)
    axis.set_xlim(min(0, float(low.min())) - margin, float(high.max()) + 2 * margin)
    if options.get("annotation") is None:
        options["annotation"] = labels["paired_difference_note"]
    title = (labels["personalization_paired_difference_title"]
             if mode == "personalization_comparison" else
             labels["revalidation_paired_difference_title"]
             if mode == "participant_revalidation" else
             labels["controlled_paired_difference_title"]
             if mode == "controlled_comparison" else
             labels["cross_paired_difference_title"]
             if mode == "cross_cohort_evaluation" else
             labels["pooled_paired_difference_title"])
    _finish(axis, options, title, "", labels["cohort"])
    axis.set_xlabel(labels["mae_increase"])
    return figure


def make_academic_figure(
    result: dict[str, Any], kind: str = "comparison", metric: str = "joint_mae_min",
    lang: str = "ko", options: dict[str, Any] | None = None,
) -> Figure:
    """Build a figure exclusively from recorded academic model output."""
    options = dict(options or {})
    labels = _labels(lang)
    options["_evidence_caption"] = _evidence_caption(result, labels)
    if kind == "comparison" and _envelope(result).get("mode") in {
        "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
        "participant_revalidation", "personalization_comparison",
    }:
        return _faceted_study_comparison(result, metric, labels, options)
    if kind == "paired_difference":
        return _paired_difference_figure(result, labels, options)
    figure, ax = _figure(options)

    if kind == "comparison":
        rows = [(row, _finite(row.get(metric))) for row in _summary(result)]
        rows = [(row, value) for row, value in rows if value is not None]
        rows.sort(key=lambda item:item[1])
        if not rows:
            _empty(ax, labels, options); return figure
        names = [_model_label(row.get("model_id", ""), labels) for row, _ in rows]
        values = [value for _, value in rows]
        colors = [model_color(row.get('model_id','')) for row,_ in rows]
        ax.bar(range(len(rows)), values, color=colors, edgecolor="black", linewidth=.4)
        ax.set_xticks(range(len(rows)), names, rotation=35, ha="right")
        for index, value in enumerate(values):
            ax.text(index, value, f"{value:.2f}", ha="center", va="bottom", fontsize=8)
        scope = labels["common_scope"] if _raw(result).get("metrics") else labels["available_scope"]
        if options.get("annotation") is None:
            counts = rows[0][0]
            n = counts.get("n_nights", counts.get("n"))
            participants = counts.get("n_participants")
            count_note = (labels["n_note"].format(n=n, participants=participants)
                          if n is not None and participants is not None else "")
            options["annotation"] = " · ".join(
                value for value in (scope, count_note, labels["fold_note"]) if value
            )
            if _envelope(result).get("mode") == "pooled_retraining":
                options["annotation"] = labels["pooled_scope"]
        _finish(ax, options, labels["comparison_title"].format(metric=labels.get(metric, metric)),
                labels["model"], labels["value"])
        return figure

    predictions = _predictions(result)
    if kind == "predictions":
        actual_key, predicted_key = _metric_prediction_columns(metric)
        by_model: dict[str, list[tuple[float, float]]] = {}
        for row in predictions:
            actual, predicted = _finite(row.get(actual_key)), _finite(row.get(predicted_key))
            if actual is not None and predicted is not None:
                by_model.setdefault(str(row.get("model_id", "model")), []).append((actual, predicted))
        if not by_model:
            _empty(ax, labels, options); return figure
        all_values = []
        for index, (model_id, pairs) in enumerate(by_model.items()):
            x, y = zip(*pairs)
            all_values.extend(x); all_values.extend(y)
            ax.scatter(x, y, alpha=.58, s=24, color=model_color(model_id),
                       label=_model_label(model_id, labels))
        low, high = min(all_values), max(all_values)
        ax.plot([low, high], [low, high], "--", color="#555555", linewidth=1)
        if options.get("annotation") is None:
            options["annotation"] = labels["prediction_note"]
        _finish(ax, options, labels["predictions_title"], labels["actual"], labels["predicted"])
        if options.get("legend", "best") not in {None, False, "none"}:
            ax.legend(loc=str(options.get("legend", "best")), fontsize=8)
        return figure

    if kind == "correlation":
        observed = _observed_nights(result)
        if observed.empty:
            _empty(ax, labels, options); return figure
        method = str(options.get("corr_method", "pearson")).lower()
        method = method if method in {"pearson", "spearman"} else "pearson"
        matrix = observed[OBSERVED_COLUMNS].corr(method=method)
        image = ax.imshow(matrix.to_numpy(), cmap="RdBu_r", vmin=-1, vmax=1,
                          interpolation="nearest", aspect="auto")
        display_names = [labels[column] for column in OBSERVED_COLUMNS]
        ax.set_xticks(range(len(display_names)), display_names, rotation=25, ha="right")
        ax.set_yticks(range(len(display_names)), display_names)
        for row_index in range(len(display_names)):
            for column_index in range(len(display_names)):
                value = matrix.iat[row_index, column_index]
                color = "white" if np.isfinite(value) and abs(value) >= .55 else "black"
                text = f"{value:.2f}" if np.isfinite(value) else labels["not_calculable"]
                ax.text(column_index, row_index, text, ha="center", va="center",
                        color=color, fontsize=max(7, float(options.get("font_size", 11)) - 1))
        colorbar = figure.colorbar(image, ax=ax, fraction=.046, pad=.04)
        colorbar.set_label(labels["correlation_coefficient"])
        if options.get("annotation") is None:
            options["annotation"] = labels["correlation_note"].format(
                method=labels[f"corr_{method}"], n=len(observed)
            )
        _finish(ax, options,
                labels["correlation_title"].format(method=labels[f"corr_{method}"]),
                "", "")
        # The full English observed-variable labels need more room than the
        # compact labels used by other figure kinds at the in-app 1230px width.
        figure.subplots_adjust(left=.16)
        return figure

    if kind == "personalization":
        rows = _personalization_records(result)
        series: dict[tuple[str, str], list[tuple[str, float]]] = {}
        field = str(options.get("personalization_field", "personal_weight"))
        field = field if field in PERSONALIZATION_FIELDS else "personal_weight"
        for row in rows:
            state = row.get("state") or {}
            key = (str(row.get("participant_id", "")), str(row.get("cohort", "")))
            value = _finite(state.get(field))
            if field == "personal_weight" and value is None:
                value = _finite(state.get("personalization_weight"))
            if value is not None:
                series.setdefault(key, []).append((str(row.get("cutoff", "")), value))
        # Prospective online personalization exposes its recorded adjustment
        # weights at each immutable forecast cutoff.
        if field == "personal_weight":
            for row in rows:
                if not row.get("model_id"):
                    continue
                weight = _finite(row.get("weight"))
                if weight is not None:
                    series.setdefault((str(row["model_id"]), "online"), []).append(
                        (str(row.get("cutoff", "")), weight)
                    )
        if not series:
            _empty(ax, labels, options); return figure
        for index, (key, points) in enumerate(series.items()):
            points.sort(key=lambda item: item[0])
            ax.plot(range(len(points)), [item[1] for item in points], marker="o", markersize=3,
                    color=PALETTE[index % len(PALETTE)], label=f"{key[0]} · {key[1].upper()}")
        if options.get("annotation") is None:
            options["annotation"] = labels["personalization_note"]
        ylabel = labels[PERSONALIZATION_FIELDS[field]]
        _finish(ax, options, labels["personalization_title"], labels["night"], ylabel)
        if options.get("legend", "best") not in {None, False, "none"}:
            ax.legend(loc=str(options.get("legend", "best")), fontsize=8)
        return figure

    if kind == "errors":
        grouped: dict[str, list[float]] = {}
        for row in predictions:
            onset = (_finite(row.get("actual_onset_offset_min")), _finite(row.get("onset_offset_min")))
            wake = (_finite(row.get("actual_wake_offset_min")), _finite(row.get("wake_offset_min")))
            values = [abs(actual - predicted) for actual, predicted in (onset, wake)
                      if actual is not None and predicted is not None]
            if values: grouped.setdefault(str(row.get("model_id", "model")), []).append(float(np.mean(values)))
        if not grouped:
            _empty(ax, labels, options); return figure
        arrays = list(grouped.values())
        positions = range(1, len(arrays) + 1)
        box_positions = [index for index, values in enumerate(arrays, 1) if len(values) >= 2]
        box_arrays = [values for values in arrays if len(values) >= 2]
        if box_arrays:
            ax.boxplot(box_arrays, positions=box_positions, showfliers=True)
        for index, values in enumerate(arrays, 1):
            jitter = np.linspace(-.10, .10, len(values)) if len(values) > 1 else [0]
            ax.scatter(np.asarray(jitter) + index, values, s=13, alpha=.55, color=model_color(list(grouped)[index-1]))
        ax.set_xticks(list(positions), [_model_label(model, labels) for model in grouped], rotation=35, ha="right")
        if options.get("annotation") is None:
            options["annotation"] = labels["errors_note"].format(n=sum(map(len, arrays)))
        _finish(ax, options, labels["errors_title"], labels["model"], labels["value"])
        return figure

    if kind == "source_report":
        normalized = _envelope(result).get("normalized_tables") or []
        if normalized:
            report = normalized[0]
            columns = [str(value) for value in report.get("columns", [])]
            rows = [[str(value) for value in row] for row in report.get("rows", [])]
            ax.axis("off")
            table = ax.table(cellText=rows[:30], colLabels=columns, cellLoc="left",
                             colLoc="left", loc="center")
            table.auto_set_font_size(False)
            table.set_fontsize(max(6, float(options.get("font_size", 10)) - 2))
            table.scale(1, 1.25)
            title = str(options.get("title") if options.get("title") is not None
                        else report.get("title") or labels["source_report_title"])
            ax.set_title(title, fontsize=float(options.get("font_size", 10)) + 2,
                         fontweight="bold")
            _add_footer(figure, options)
            return figure
        provenance = _envelope(result).get("provenance") or {}
        if not provenance:
            provenance = {key: value for key, value in _raw(result).items()
                          if key in {"run_id", "evidence_label", "runtime_model_version", "personalization_policy"}}
        if not provenance:
            _empty(ax, labels, options); return figure
        ax.axis("off")
        rows = [[str(key), json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)]
                for key, value in provenance.items()]
        table = ax.table(cellText=rows, colLabels=[labels["report_key"], labels["report_value"]],
                         cellLoc="left", colLoc="left", loc="center", colWidths=[.28, .72])
        table.auto_set_font_size(False); table.set_fontsize(max(7, float(options.get("font_size", 10)) - 1))
        table.scale(1, 1.35)
        ax.set_title(labels["source_report_title"], fontsize=float(options.get("font_size", 10)) + 2,
                     fontweight="bold")
        _add_footer(figure, options)
        return figure

    raise ValueError(f"unsupported academic figure kind: {kind}")


def _flatten_records(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    return pd.json_normalize(list(rows), sep=".") if rows else pd.DataFrame()


def export_academic_tables(result: dict[str, Any], path: str | Path, lang: str = "ko") -> None:
    """Export recorded summaries, predictions, personalization and provenance."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    envelope = _envelope(result)
    raw = envelope.get("raw") or {}
    mode = envelope.get("mode")
    controlled_study = mode in {
        "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
        "participant_revalidation", "personalization_comparison",
    }
    participant_revalidation = mode in {"participant_revalidation", "personalization_comparison"}
    summary = _flatten_records(_summary(result))
    predictions = _flatten_records(_predictions(result))
    personalization = _flatten_records(_personalization_records(result))
    models = _flatten_records(raw.get("models", []))
    provenance = _flatten_records([envelope.get("provenance") or {}])
    metadata = _flatten_records([{
        "mode": mode, "report_type": envelope.get("report_type"),
        "run_id": raw.get("run_id"), "evidence_label": raw.get("evidence_label"),
        "runtime_model_version": raw.get("runtime_model_version"),
        "personalization_policy": raw.get("personalization_policy"),
        "evidence": (envelope.get("provenance") or {}).get("evidence"),
        "evaluation_status": raw.get("status"),
        "metric_unit": "minutes" if controlled_study else None,
        "primary_pair": (json.dumps(raw.get("primary_pair"), ensure_ascii=False)
                         if raw.get("primary_pair") else None),
        "test_reuse": ("not a Test holdout; all-participant grouped out-of-fold estimates"
                       if participant_revalidation else
                       "reused historical Test" if controlled_study else None),
        "ci_scope": ("participant-cluster bootstrap 95%; conditional on the fixed CV procedure; "
                     "excludes prior procedure-selection uncertainty" if participant_revalidation else
                     "participant-cluster bootstrap 95%; conditional on fitted models; "
                     "excludes training/selection uncertainty") if controlled_study else None,
        "original_frozen_models_replaced": False if controlled_study else None,
    }])
    warnings = pd.DataFrame({"warning": envelope.get("warnings", [])}) if isinstance(envelope.get("warnings"), list) else pd.DataFrame({"warning": [envelope.get("warnings")]})
    tables = {
        "summary" if lang != "ko" else "성능 요약": summary,
        "predictions" if lang != "ko" else "예측값": predictions,
        "personalization" if lang != "ko" else "개인화": personalization,
        "models" if lang != "ko" else "모델 정보": models,
        "provenance" if lang != "ko" else "출처 정보": provenance,
        "metadata" if lang != "ko" else "실행 메타데이터": metadata,
        "warnings" if lang != "ko" else "주의사항": warnings,
    }
    if participant_revalidation:
        grouped_tables = [
            ("paired comparisons", "짝지은 비교", "paired_comparisons"),
            ("fold metrics", "Fold 성능", "fold_metrics"),
            ("history metrics", "이력별 성능", "history_metrics"),
            ("history paired comparisons", "이력별 짝지은 비교", "history_paired_comparisons"),
            ("repeat metrics", "반복별 성능", "repeat_metrics"),
        ]
        if mode == "participant_revalidation":
            grouped_tables.extend([
                ("untouched holdout metrics", "미사용 MESA holdout 성능", "untouched_holdout_metrics"),
                ("untouched holdout paired comparisons", "미사용 MESA holdout 짝지은 비교", "untouched_holdout_paired_comparisons"),
            ])
        else:
            grouped_tables.append(("personalization diagnostics", "개인화 진단", "personalization_diagnostics"))
        for english, korean, key in grouped_tables:
            tables[english if lang != "ko" else korean] = _flatten_records(raw.get(key, []))
        selection = raw.get("selection")
        if isinstance(selection, dict):
            selection_row = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in selection.items()
            }
            tables["selection" if lang != "ko" else "발표 모델 선정"] = _flatten_records([selection_row])
            if mode == "personalization_comparison":
                tables["selection candidates" if lang != "ko" else "선정 후보 비교"] = _flatten_records(
                    selection.get("candidates") or []
                )
    for index, report in enumerate(envelope.get("normalized_tables") or [], 1):
        columns = report.get("columns") or []
        rows = report.get("rows") or []
        name = str(report.get("table_id") or f"report_{index}").replace(".", "_")
        tables[name[:31]] = pd.DataFrame(rows, columns=columns)
    if target.suffix.lower() == ".csv":
        summary.to_csv(target, index=False, encoding="utf-8-sig")
        base = target.with_suffix("")
        for name, table in tables.items():
            if table is summary or table.empty: continue
            safe = "_".join(name.split())
            table.to_csv(base.with_name(base.name + "_" + safe + ".csv"), index=False, encoding="utf-8-sig")
        return
    if target.suffix.lower() != ".xlsx":
        raise ValueError("academic table export supports .csv and .xlsx")
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        for name, table in tables.items():
            table.to_excel(writer, sheet_name=name[:31], index=False)


__all__ = ["make_academic_figure", "model_label", "export_academic_tables", "export_figure"]
