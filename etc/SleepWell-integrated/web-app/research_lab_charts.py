"""In-memory Matplotlib charts for the stateless researcher experiment lab."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import csv
import hashlib
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
import random
import threading
from typing import Any
import zipfile

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import fontManager
from matplotlib.lines import Line2D
from matplotlib.text import Text
from matplotlib.ticker import MaxNLocator


_MAX_POINTS = 365
_MAX_MODELS = 16
_MAX_DIAGNOSTIC_PARTICIPANTS = 10
_MAX_EXPORT_SOURCE_ROWS = 50_000
_RENDER_LOCK = threading.Lock()
_PUBLICATION_EXPORT = False

_PUBLICATION_WIDTH_MM = 180.0
_PUBLICATION_HEIGHTS_MM = {
    "prediction-timeline": 160.0,
    "model-mae-comparison": 180.0,
    "personalization-traces": 200.0,
    "participant-mae-heatmap": 135.0,
    "joint-error-distribution": 135.0,
    "signed-residual-bias": 160.0,
    "sleep-actogram": 170.0,
    "data-completeness": 150.0,
}
_PUBLICATION_CAPTIONS = {
    "prediction-timeline": (
        "Observed and predicted sleep onset and wake time on the selected participant's chronological nights. "
        "Lines stop at missing observations; they do not bridge unobserved nights."
    ),
    "model-mae-comparison": (
        "Participant-equal joint mean absolute error (MAE) on identical nights and paired within-participant "
        "difference from the recent-7-night baseline. Whiskers are deterministic participant-bootstrap 95% "
        "percentile intervals (2,000 draws) and are omitted when fewer than two participants are available."
    ),
    "personalization-traces": (
        "Recorded personalization state and model inputs for the selected participant. Missing state values "
        "remain gaps; fixed model constants are assumptions, not participant-specific physiological estimates."
    ),
    "participant-mae-heatmap": (
        "Within-participant joint MAE by model on the common-night subset. Each cell represents one participant "
        "rather than a pooled collection of nights."
    ),
    "joint-error-distribution": (
        "Distribution of within-participant common-night joint MAE. Each point is one participant-model value; "
        "the box summarizes participants and does not treat nights as independent replicates."
    ),
    "signed-residual-bias": (
        "Within-participant mean signed onset and wake residuals on common nights. Positive values indicate late "
        "predictions; stars are participant-equal model means."
    ),
    "sleep-actogram": (
        "Double-plotted actogram of observed main-sleep intervals for the selected participant, each row spanning "
        "two consecutive days from the 20:00 cutoff. Grey rows lack an observed outcome; thin lines show the "
        "fusion-model prediction. Naps are not part of the input and are not shown."
    ),
    "data-completeness": (
        "Data completeness by participant and cutoff date. Observed nights are split by whether the outcome was "
        "recorded before the next 20:00 decision; prediction-only nights and days without a row are shown separately."
    ),
}
_PUBLICATION_SOURCE_FIELDS = {
    "prediction-timeline": (
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "model-mae-comparison": (
        "metrics.model_id", "metrics.joint_mae_min", "metrics.n", "metrics.n_participants",
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "personalization-traces": (
        "personalization.participant_id", "personalization.cutoff", "personalization.cohort",
        "personalization.history_count", "personalization.state", "personalization.features",
    ),
    "participant-mae-heatmap": (
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "joint-error-distribution": (
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "signed-residual-bias": (
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "sleep-actogram": (
        "predictions.participant_id", "predictions.cutoff", "predictions.model_id",
        "predictions.onset_offset_min", "predictions.wake_offset_min",
        "predictions.actual_onset_offset_min", "predictions.actual_wake_offset_min",
    ),
    "data-completeness": (
        "observations.completeness.participant_id", "observations.completeness.grid",
        "observations.completeness.labeled_coverage",
    ),
}

_COHORT_ORDER = ("cmi", "mesa")
_MODEL_COLORS = (
    "#4C78A8",
    "#72B7B2",
    "#54A24B",
    "#B279A2",
    "#F58518",
    "#E45756",
    "#FF9DA6",
    "#9D755D",
    "#79706E",
    "#BAB0AC",
)
_COHORT_COLORS = {"cmi": "#2563A6", "mesa": "#D2691E"}
_METRIC_FIELDS = (
    ("onset_mae_min", "Onset MAE", "#4C78A8"),
    ("wake_mae_min", "Wake MAE", "#F58518"),
    ("joint_mae_min", "Joint MAE", "#B279A2"),
)
_EXPORT_METRIC_FIELDS = _METRIC_FIELDS[:2] + (
    ("interval_mae_min", "Interval MAE", "#54A24B"),
    _METRIC_FIELDS[2],
)
_KOREAN_FONT_NAMES = (
    "Malgun Gothic",
    "AppleGothic",
    "NanumGothic",
    "Noto Sans CJK KR",
    "Noto Sans KR",
    "Arial Unicode MS",
)


def _installed_korean_font() -> str | None:
    entries = sorted(fontManager.ttflist, key=lambda entry: (entry.name.casefold(), entry.fname))
    by_name: dict[str, Any] = {}
    for entry in entries:
        by_name.setdefault(entry.name.casefold(), entry)
    for name in _KOREAN_FONT_NAMES:
        entry = by_name.get(name.casefold())
        if entry is not None:
            return entry.name
    return None


_KOREAN_FONT = _installed_korean_font()


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _safe_text(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _plot_text(english: str, korean: str) -> str:
    return korean if _KOREAN_FONT is not None and not _PUBLICATION_EXPORT else english


def _mode_banner(result: Mapping[str, Any]) -> str:
    mode = _safe_text(result.get("mode"), 24).casefold()
    label = _safe_text(result.get("evidence_label"), 180) or "unspecified evidence label"
    if _PUBLICATION_EXPORT and mode == "synthetic":
        return "SYNTHETIC DATA — demonstration only; not accuracy evidence"
    if _PUBLICATION_EXPORT and mode == "real":
        return "RETROSPECTIVE REPLAY — not prospective or clinical evidence"
    if _PUBLICATION_EXPORT:
        return "EVIDENCE SCOPE NOT ESTABLISHED"
    if mode == "synthetic":
        prefix = "SYNTHETIC DEMONSTRATION - NOT REAL-WORLD ACCURACY EVIDENCE"
    elif mode == "real":
        prefix = "RETROSPECTIVE USER-INPUT REPLAY - NOT PROSPECTIVE OR CLINICAL EVIDENCE"
    else:
        prefix = f"MODE {mode.upper() or 'UNKNOWN'} - EVIDENCE SCOPE NOT ESTABLISHED"
    return f"{prefix} | {label}"


def _new_figure(size: tuple[float, float]) -> Figure:
    figure = Figure(figsize=size, dpi=100, facecolor="white", constrained_layout=True)
    # Reserve fixed page space for the evidence banner, captions, and tick extents.
    # This is intentional page composition, not a post-save tight crop.
    figure.get_layout_engine().set(rect=(0.035, 0.105, 0.93, 0.815))
    FigureCanvasAgg(figure)
    return figure


def _apply_local_font(figure: Figure) -> None:
    if _KOREAN_FONT is None:
        return
    family = ["DejaVu Sans", _KOREAN_FONT] if _PUBLICATION_EXPORT else [_KOREAN_FONT, "DejaVu Sans"]
    for text in figure.findobj(match=Text):
        text.set_fontfamily(family)


def _add_heading(figure: Figure, title: str, banner: str) -> None:
    # The evidence-scope banner is no longer drawn on figures at the researcher's request;
    # mode and evidence_label remain recorded in result.json and figure-manifest.json.
    figure.suptitle(title, fontsize=15, fontweight="bold", y=0.995)


def _empty_panel(axis: Any, heading: str, detail: str) -> None:
    axis.set_axis_off()
    axis.text(
        0.5,
        0.57,
        heading,
        transform=axis.transAxes,
        ha="center",
        va="center",
        fontsize=14,
        fontweight="bold",
        color="#374151",
    )
    axis.text(
        0.5,
        0.43,
        detail,
        transform=axis.transAxes,
        ha="center",
        va="center",
        fontsize=10,
        color="#4B5563",
        wrap=True,
    )


def _finite_limits(values: list[float | None], minimum_pad: float = 1.0) -> tuple[float, float] | None:
    finite = [value for value in values if value is not None and math.isfinite(value)]
    if not finite:
        return None
    low, high = min(finite), max(finite)
    if low == high:
        pad = max(abs(low) * 0.06, minimum_pad)
    else:
        pad = max((high - low) * 0.08, minimum_pad)
    return low - pad, high + pad


def _date_label(cutoff: str) -> str:
    date = cutoff.split("T", 1)[0]
    if len(date) == 10 and date[4] == "-" and date[7] == "-":
        return date[5:]
    return _safe_text(cutoff, 16)


def _tick_positions(count: int, maximum: int = 8) -> list[int]:
    if count <= 0:
        return []
    if count <= maximum:
        return list(range(count))
    positions = {round(index * (count - 1) / (maximum - 1)) for index in range(maximum)}
    return sorted(positions)


def _model_maps(result: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    models: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    for row in _rows(result.get("models")):
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in models:
            continue
        models[model_id] = row
        order.append(model_id)
    return models, order


def _model_label(model_id: str, models: Mapping[str, Mapping[str, Any]]) -> str:
    model = models.get(model_id, {})
    if _PUBLICATION_EXPORT:
        kind = _safe_text(model.get("kind"), 32).casefold()
        label = {
            "baseline_recent7": "Recent 7-night baseline",
            "physical_fixed": "Fixed physics",
            "physical_personal": "Personalized physics",
            "data": "Data model",
            "hybrid": "Fusion model",
        }.get(kind, _safe_text(model_id, 44))
    else:
        label = _safe_text(model.get("label"), 44) or _safe_text(model_id, 44)
    cohort = _safe_text(model.get("cohort"), 12).upper()
    if model_id == "baseline.recent7" or not cohort or cohort in {"SHARED", "ALL"}:
        return label
    return f"{cohort} · {label}"


def _model_color(model_id: str, index: int, models: Mapping[str, Mapping[str, Any]]) -> str:
    if model_id == "baseline.recent7":
        return "#1F2937"
    cohort = _safe_text(models.get(model_id, {}).get("cohort"), 12).casefold()
    kind = _safe_text(models.get(model_id, {}).get("kind"), 32).casefold()
    if cohort == "cmi":
        return {
            "physical_fixed": "#1D4E89",
            "physical_personal": "#3B82B5",
            "data": "#60A5C4",
            "hybrid": "#7C3AED",
        }.get(kind, "#2563A6")
    if cohort == "mesa":
        return {
            "physical_fixed": "#B45309",
            "physical_personal": "#EA7C25",
            "data": "#E45756",
            "hybrid": "#A23E48",
        }.get(kind, "#D2691E")
    stable_index = hashlib.sha256(model_id.encode("utf-8")).digest()[0] % len(_MODEL_COLORS)
    return _MODEL_COLORS[stable_index]


def _timeline_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    participant_id = summary.get("chart_participant_id")
    selected = participant_id if isinstance(participant_id, str) else ""
    banner = _mode_banner(result)
    title = _plot_text("Observed and predicted sleep timing", "관측·예측 수면 시각")
    if selected and not _PUBLICATION_EXPORT:
        title = f"{title} - {_safe_text(selected, 48)}"

    predictions = [
        row
        for row in _rows(result.get("predictions"))
        if selected and row.get("participant_id") == selected and isinstance(row.get("cutoff"), str)
    ]
    cutoffs = sorted({str(row["cutoff"]) for row in predictions})[:_MAX_POINTS]
    cutoff_set = set(cutoffs)
    predictions = [row for row in predictions if row.get("cutoff") in cutoff_set]

    figure = _new_figure((12.0, 10.0))
    _add_heading(figure, title, banner)
    if not cutoffs:
        axis = figure.add_subplot(1, 1, 1)
        _empty_panel(
            axis,
            "No timeline available",
            "No prediction rows are available for summary.chart_participant_id.",
        )
        alt = (
            f"관측·예측 수면 시각. 선택 참가자 {_safe_text(selected) or '없음'}에 표시할 예측 행이 없습니다. "
            f"{banner}."
        )
        return figure, alt

    models, model_order = _model_maps(result)
    prediction_model_ids = {
        str(row.get("model_id"))
        for row in predictions
        if isinstance(row.get("model_id"), str) and row.get("model_id")
    }
    ordered_ids = [model_id for model_id in model_order if model_id in prediction_model_ids]
    ordered_ids.extend(sorted(prediction_model_ids.difference(ordered_ids)))
    ordered_ids = ordered_ids[:_MAX_MODELS]

    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    by_cutoff: dict[str, list[Mapping[str, Any]]] = {cutoff: [] for cutoff in cutoffs}
    for row in predictions:
        cutoff = str(row["cutoff"])
        model_id = row.get("model_id")
        by_cutoff[cutoff].append(row)
        if isinstance(model_id, str):
            by_key.setdefault((cutoff, model_id), row)

    actual: dict[str, list[float | None]] = {}
    for field in ("actual_onset_offset_min", "actual_wake_offset_min"):
        values: list[float | None] = []
        for cutoff in cutoffs:
            value = None
            for row in by_cutoff[cutoff]:
                value = _finite(row.get(field))
                if value is not None:
                    break
            values.append(value)
        actual[field] = values

    onset_axis = figure.add_subplot(2, 1, 1)
    wake_axis = figure.add_subplot(2, 1, 2, sharex=onset_axis)
    figure.get_layout_engine().set(rect=(0.035, 0.255, 0.93, 0.665))
    x = list(range(len(cutoffs)))
    endpoints = (
        (onset_axis, "onset_offset_min", "actual_onset_offset_min", _plot_text("Sleep onset", "수면 시작")),
        (wake_axis, "wake_offset_min", "actual_wake_offset_min", _plot_text("Final wake", "최종 기상")),
    )
    any_actual = False
    for axis, prediction_field, actual_field, endpoint_title in endpoints:
        plotted_values: list[float | None] = []
        actual_values = actual[actual_field]
        if any(value is not None for value in actual_values):
            any_actual = True
            axis.plot(
                x,
                actual_values,
                color="#111827",
                linewidth=2.4,
                marker="o",
                markersize=3.5,
                label=_plot_text("Observed", "관측"),
                zorder=5,
            )
            plotted_values.extend(actual_values)
        for index, model_id in enumerate(ordered_ids):
            values = [
                _finite(by_key.get((cutoff, model_id), {}).get(prediction_field))
                for cutoff in cutoffs
            ]
            if not any(value is not None for value in values):
                continue
            plotted_values.extend(values)
            axis.plot(
                x,
                values,
                color=_model_color(model_id, index, models),
                linewidth=1.35,
                alpha=0.9,
                label=_model_label(model_id, models),
            )
        limits = _finite_limits(plotted_values, minimum_pad=5.0)
        axis.set_title(endpoint_title, loc="left", fontsize=11, fontweight="bold")
        axis.set_ylabel(
            _plot_text(
                "Minutes after 20:00 cutoff\n(240 = 00:00 next day)",
                "20:00 기준 경과 분\n(240 = 다음 날 00:00)",
            ),
            fontsize=9,
        )
        axis.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.75)
        if limits is None:
            axis.text(
                0.5,
                0.5,
                "No finite values",
                transform=axis.transAxes,
                ha="center",
                va="center",
                color="#6B7280",
            )
        else:
            axis.set_ylim(*limits)
            if limits[0] <= 240.0 <= limits[1]:
                axis.axhline(240.0, color="#6B7280", linewidth=0.8, linestyle=":", zorder=0)
                axis.text(
                    0.995,
                    240.0,
                    " 00:00 (+1 day)",
                    transform=axis.get_yaxis_transform(),
                    ha="right",
                    va="bottom",
                    fontsize=7,
                    color="#6B7280",
                )
    handles, labels = onset_axis.get_legend_handles_labels()
    if handles:
        figure.legend(
            handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.075),
            ncol=3, fontsize=7, frameon=False, title=_plot_text("Series", "계열"),
            title_fontsize=8,
        )

    positions = _tick_positions(len(cutoffs))
    wake_axis.set_xticks(positions, [_date_label(cutoffs[position]) for position in positions])
    wake_axis.set_xlabel(_plot_text("Cutoff date (20:00, source timezone)", "기준일(20:00, 입력 시간대)"))
    onset_axis.tick_params(axis="x", labelbottom=False)
    if not any_actual:
        figure.text(
            0.5,
            0.015,
            "Observed outcomes unavailable for this participant; displayed curves are predictions only.",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#8B1E1E",
        )
    actual_note = (
        "관측값은 결측 지점에서 선을 잇지 않습니다."
        if any_actual
        else "관측 결과가 없어 예측선만 표시합니다."
    )
    alt = (
        f"관측·예측 수면 시각. 참가자 {_safe_text(selected)}의 20시 이후 분 단위 수면 시작과 기상 시각; "
        f"자정은 240분입니다. {actual_note} {banner}."
    )
    return figure, alt


def _valid_primary_metrics(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    valid: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for row in _rows(result.get("metrics")):
        model_id = row.get("model_id")
        count = _finite(row.get("n"))
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        if count is None or count <= 0:
            continue
        if not any(_finite(row.get(field)) is not None for field, _, _ in _METRIC_FIELDS):
            continue
        seen.add(model_id)
        valid.append(row)
    return valid


def _count_label(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "?"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _common_diagnostic_data(result: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the primary identical-night subset from immutable prediction rows."""

    models, model_order = _model_maps(result)
    predictions = _rows(result.get("predictions"))
    prediction_model_ids = {
        str(row.get("model_id"))
        for row in predictions
        if isinstance(row.get("model_id"), str) and row.get("model_id")
    }
    # Model metadata is the evaluation contract. Falling back to prediction IDs
    # keeps malformed-but-readable results safe without silently dropping a
    # declared model from an otherwise "common" comparison.
    model_ids = list(model_order) if model_order else sorted(prediction_model_ids)
    model_ids = model_ids[:_MAX_MODELS]

    by_model: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = {
        model_id: {} for model_id in model_ids
    }
    for row in predictions:
        model_id = row.get("model_id")
        participant_id = row.get("participant_id")
        cutoff = row.get("cutoff")
        if (
            model_id not in by_model
            or not isinstance(participant_id, str)
            or not participant_id
            or not isinstance(cutoff, str)
            or not cutoff
        ):
            continue
        if not all(
            _finite(row.get(field)) is not None
            for field in (
                "onset_offset_min",
                "wake_offset_min",
                "actual_onset_offset_min",
                "actual_wake_offset_min",
            )
        ):
            continue
        by_model[str(model_id)].setdefault((participant_id, cutoff), row)

    if not model_ids:
        common_keys: set[tuple[str, str]] = set()
    else:
        common_keys = set.intersection(*(set(by_model[model_id]) for model_id in model_ids))

    # Actuals are repeated on every model row. Conflicting repeats are not comparable.
    consistent_keys: list[tuple[str, str]] = []
    for key in sorted(common_keys):
        onset_actuals = [
            _finite(by_model[model_id][key].get("actual_onset_offset_min"))
            for model_id in model_ids
        ]
        wake_actuals = [
            _finite(by_model[model_id][key].get("actual_wake_offset_min"))
            for model_id in model_ids
        ]
        if (
            all(value is not None for value in onset_actuals + wake_actuals)
            and max(onset_actuals) - min(onset_actuals) <= 1e-9
            and max(wake_actuals) - min(wake_actuals) <= 1e-9
        ):
            consistent_keys.append(key)

    total_common_nights = len(consistent_keys)
    total_participants = len({participant_id for participant_id, _cutoff in consistent_keys})
    keys = consistent_keys[:_MAX_POINTS]
    participant_counts: dict[str, int] = {}
    for participant_id, _cutoff in keys:
        participant_counts[participant_id] = participant_counts.get(participant_id, 0) + 1
    all_participants = sorted(participant_counts, key=lambda item: (-participant_counts[item], item))
    participants = all_participants[:_MAX_DIAGNOSTIC_PARTICIPANTS]
    return {
        "models": models,
        "model_ids": model_ids,
        "by_model": by_model,
        "keys": keys,
        "total_common_nights": total_common_nights,
        "participants": participants,
        "total_participants": total_participants,
    }


def _signed_errors(row: Mapping[str, Any]) -> tuple[float, float]:
    onset = _finite(row.get("onset_offset_min"))
    wake = _finite(row.get("wake_offset_min"))
    actual_onset = _finite(row.get("actual_onset_offset_min"))
    actual_wake = _finite(row.get("actual_wake_offset_min"))
    if onset is None or wake is None or actual_onset is None or actual_wake is None:
        raise ValueError("diagnostic row must contain finite prediction and actual offsets")
    return onset - actual_onset, wake - actual_wake


def _participant_joint_error_map(data: Mapping[str, Any], model_id: str) -> dict[str, float]:
    """One joint-MAE observation per participant on the shared-night subset."""

    by_model = data["by_model"]
    keys = data["keys"]
    participant_ids = sorted({participant_id for participant_id, _cutoff in keys})
    values: dict[str, float] = {}
    for participant_id in participant_ids:
        participant_errors = []
        for key in keys:
            if key[0] != participant_id:
                continue
            onset_error, wake_error = _signed_errors(by_model[model_id][key])
            participant_errors.append((abs(onset_error) + abs(wake_error)) / 2.0)
        if participant_errors:
            values[participant_id] = sum(participant_errors) / len(participant_errors)
    return values


def _participant_joint_errors(data: Mapping[str, Any], model_id: str) -> list[float]:
    return list(_participant_joint_error_map(data, model_id).values())


def _bootstrap_mean_ci(values: list[float], seed_text: str) -> tuple[float, float] | None:
    """Deterministic participant bootstrap percentile interval; unavailable for n < 2."""

    if len(values) < 2:
        return None
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    count = len(values)
    means = sorted(
        sum(values[generator.randrange(count)] for _index in range(count)) / count
        for _draw in range(2000)
    )
    return means[49], means[1949]


def _diagnostic_empty_figure(
    result: Mapping[str, Any], title: str, detail: str, alt_prefix: str
) -> tuple[Figure, str]:
    banner = _mode_banner(result)
    figure = _new_figure((12.0, 6.4))
    _add_heading(figure, title, banner)
    axis = figure.add_subplot(1, 1, 1)
    _empty_panel(axis, "Accuracy unavailable", detail)
    return figure, f"{alt_prefix}. Accuracy unavailable: {detail} {banner}."


def _participant_display_map(result: Mapping[str, Any]) -> dict[str, str]:
    data = _common_diagnostic_data(result)
    return {
        participant_id: f"P{index:02d}"
        for index, participant_id in enumerate(data["participants"], start=1)
    }


def _participant_mae_heatmap_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    title = _plot_text(
        "Common-night joint MAE by model and participant",
        "모델·참가자별 공통 밤 joint MAE",
    )
    data = _common_diagnostic_data(result)
    model_ids = data["model_ids"]
    participants = data["participants"]
    keys = data["keys"]
    if not model_ids or not participants or not keys:
        return _diagnostic_empty_figure(
            result,
            title,
            "No finite outcomes shared by every displayed model; no placeholder heatmap is drawn.",
            "모델·참가자별 공통 밤 joint MAE",
        )

    by_model = data["by_model"]
    models = data["models"]
    keys_by_participant: dict[str, list[tuple[str, str]]] = {
        participant_id: [key for key in keys if key[0] == participant_id]
        for participant_id in participants
    }
    matrix: list[list[float]] = []
    for model_id in model_ids:
        row_values = []
        for participant_id in participants:
            errors = []
            for key in keys_by_participant[participant_id]:
                onset_error, wake_error = _signed_errors(by_model[model_id][key])
                errors.append((abs(onset_error) + abs(wake_error)) / 2.0)
            row_values.append(sum(errors) / len(errors))
        matrix.append(row_values)

    banner = _mode_banner(result)
    figure = _new_figure((12.0, max(6.4, 0.46 * len(model_ids) + 2.5)))
    _add_heading(figure, title, banner)
    axis = figure.add_subplot(1, 1, 1)
    image = axis.imshow(matrix, aspect="auto", cmap="YlOrRd", interpolation="nearest")
    display_ids = _participant_display_map(result)
    axis.set_xticks(
        range(len(participants)),
        [
            f"{display_ids[participant_id]}\n(n={len(keys_by_participant[participant_id])})"
            for participant_id in participants
        ],
    )
    axis.set_yticks(
        range(len(model_ids)),
        [_model_label(model_id, models) for model_id in model_ids],
    )
    axis.set_xlabel(_plot_text("Participant (common nights)", "참가자(공통 평가 밤 수)"))
    axis.set_ylabel(_plot_text("Model", "모델"))
    axis.tick_params(axis="x", labelsize=8)
    axis.tick_params(axis="y", labelsize=8)
    if len(model_ids) * len(participants) <= 160:
        threshold = max(max(row) for row in matrix) * 0.58
        for row_index, row_values in enumerate(matrix):
            for column_index, value in enumerate(row_values):
                axis.text(
                    column_index,
                    row_index,
                    f"{value:.1f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="white" if value >= threshold and threshold > 0.0 else "#111827",
                )
    colorbar = figure.colorbar(image, ax=axis, shrink=0.86)
    colorbar.set_label(_plot_text("Within-participant joint MAE (min)", "참가자 내 joint MAE(분)"))
    figure.text(
        0.5,
        0.025,
        (
            "Each cell is a within-participant mean on the identical-night subset.\n"
            "Participants receive equal weight only when cells are aggregated."
        ),
        ha="center",
        va="bottom",
        fontsize=8,
        color="#4B5563",
    )
    shown = len(participants)
    total = data["total_participants"]
    night_note = (
        f"공통 평가 밤 {len(keys)}/{data['total_common_nights']}개"
        if len(keys) != data["total_common_nights"]
        else f"공통 평가 밤 {len(keys)}개"
    )
    alt = (
        f"모델·참가자별 공통 밤 joint MAE heatmap. 참가자 표시 {shown}/{total}명(최대 "
        f"{_MAX_DIAGNOSTIC_PARTICIPANTS}명), {night_note}. 각 셀은 참가자 내 평균이며 참가자 간 집계 시 동일 가중치입니다. "
        f"{banner}."
    )
    return figure, alt


def _joint_error_boxplot_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    title = _plot_text(
        "Participant-level common-night joint MAE",
        "참가자별 공통 밤 joint MAE 분포",
    )
    data = _common_diagnostic_data(result)
    model_ids = data["model_ids"]
    keys = data["keys"]
    if not model_ids or not keys:
        return _diagnostic_empty_figure(
            result,
            title,
            "No finite outcomes shared by every displayed model; no placeholder distribution is drawn.",
            "참가자별 공통 밤 joint MAE boxplot",
        )

    models = data["models"]
    distributions: list[list[float]] = []
    for model_id in model_ids:
        distributions.append(_participant_joint_errors(data, model_id))

    banner = _mode_banner(result)
    figure = _new_figure((12.0, 7.0))
    _add_heading(figure, title, banner)
    axis = figure.add_subplot(1, 1, 1)
    summarized = [(index, values) for index, values in enumerate(distributions) if len(values) >= 2]
    if summarized:
        boxplot_options: dict[str, Any] = {
            "patch_artist": True,
            "showfliers": True,
            "medianprops": {"color": "#111827", "linewidth": 1.6},
            "whiskerprops": {"color": "#4B5563"},
            "capprops": {"color": "#4B5563"},
            "positions": [index + 1 for index, _values in summarized],
        }
        artists = axis.boxplot([values for _index, values in summarized], **boxplot_options)
    else:
        artists = {"boxes": []}
    for (model_index, _values), box in zip(summarized, artists["boxes"]):
        box.set_facecolor(_model_color(model_ids[model_index], model_index, models))
        box.set_alpha(0.72)
    # Raw participant observations make the replication unit visible. The small,
    # deterministic horizontal offsets avoid concealing coincident values.
    for model_index, values in enumerate(distributions, start=1):
        offsets = [((index % 5) - 2) * 0.025 for index in range(len(values))]
        axis.scatter(
            [model_index + offset for offset in offsets],
            values,
            s=18,
            facecolors="white",
            edgecolors="#111827",
            linewidths=0.65,
            zorder=4,
        )
    axis.set_ylabel(_plot_text("Participant joint MAE (minutes)", "참가자별 joint MAE(분)"))
    axis.set_xlabel(_plot_text("Model", "모델"))
    axis.set_xticks(range(1, len(model_ids) + 1), [_model_label(model_id, models) for model_id in model_ids])
    axis.tick_params(axis="x", labelrotation=28, labelsize=8)
    axis.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.75)
    distribution_note = (
        "Boxes summarize the participant-equal distribution."
        if summarized
        else "Only one participant is available; distribution summaries are unavailable and points are shown alone."
    )
    figure.text(
        0.5,
        0.025,
        (
            "One observation per participant: within-participant mean over identical common nights.\n"
            + distribution_note
        ),
        ha="center",
        va="bottom",
        fontsize=8,
        color="#4B5563",
    )
    alt = (
        f"참가자별 공통 밤 joint MAE boxplot. 점 하나는 참가자 한 명의 공통 밤 내 평균이고 상자는 참가자 분포입니다. "
        f"참가자 n={len(distributions[0])}; 사용한 공통 밤 {len(keys)}/{data['total_common_nights']}개. "
        + ("n=1이므로 분포 요약 없이 점만 표시합니다. " if not summarized else "")
        + f"{banner}."
    )
    return figure, alt


def _signed_residual_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    title = _plot_text(
        "Participant-level signed onset and wake bias",
        "참가자별 signed onset·wake 잔차",
    )
    data = _common_diagnostic_data(result)
    model_ids = data["model_ids"]
    participants = data["participants"]
    keys = data["keys"]
    if not model_ids or not participants or not keys:
        return _diagnostic_empty_figure(
            result,
            title,
            "No finite outcomes shared by every displayed model; no placeholder residuals are drawn.",
            "참가자별 signed onset·wake 잔차 scatter",
        )

    by_model = data["by_model"]
    models = data["models"]
    keys_by_participant = {
        participant_id: [key for key in keys if key[0] == participant_id]
        for participant_id in participants
    }
    banner = _mode_banner(result)
    figure = _new_figure((12.0, 9.5))
    _add_heading(figure, title, banner)
    figure.get_layout_engine().set(rect=(0.035, 0.235, 0.93, 0.685))
    axis = figure.add_subplot(1, 1, 1)
    all_values: list[float] = []
    for model_index, model_id in enumerate(model_ids):
        onset_means = []
        wake_means = []
        for participant_id in participants:
            residuals = [_signed_errors(by_model[model_id][key]) for key in keys_by_participant[participant_id]]
            onset_means.append(sum(value[0] for value in residuals) / len(residuals))
            wake_means.append(sum(value[1] for value in residuals) / len(residuals))
        all_values.extend(onset_means)
        all_values.extend(wake_means)
        color = _model_color(model_id, model_index, models)
        axis.scatter(
            onset_means,
            wake_means,
            s=38,
            color=color,
            alpha=0.62,
            edgecolors="white",
            linewidths=0.5,
            label=_model_label(model_id, models),
        )
        axis.scatter(
            [sum(onset_means) / len(onset_means)],
            [sum(wake_means) / len(wake_means)],
            s=125,
            color=color,
            edgecolors="#111827",
            linewidths=0.8,
            marker="*",
            zorder=5,
        )

    bound = max(5.0, max(abs(value) for value in all_values) * 1.12)
    axis.set_xlim(-bound, bound)
    axis.set_ylim(-bound, bound)
    axis.xaxis.set_major_locator(MaxNLocator(nbins=7, symmetric=True, prune="both"))
    axis.yaxis.set_major_locator(MaxNLocator(nbins=7, symmetric=True, prune="both"))
    axis.axhline(0.0, color="#6B7280", linewidth=0.9)
    axis.axvline(0.0, color="#6B7280", linewidth=0.9)
    axis.plot([-bound, bound], [-bound, bound], color="#9CA3AF", linewidth=0.8, linestyle=":")
    axis.set_aspect("equal", adjustable="box")
    axis.grid(color="#E5E7EB", linewidth=0.6, alpha=0.75)
    axis.set_xlabel(_plot_text("Mean signed onset residual (min; + = late)", "평균 signed onset 잔차(분; + = 늦음)"))
    axis.set_ylabel(_plot_text("Mean signed wake residual (min; + = late)", "평균 signed wake 잔차(분; + = 늦음)"))
    handles, labels = axis.get_legend_handles_labels()
    if handles:
        figure.legend(
            handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.065),
            fontsize=7, ncol=3, frameon=False, title=_plot_text("Model", "모델"),
            title_fontsize=8,
        )
    figure.text(
        0.5,
        0.015,
        "Circles are within-participant mean residuals; stars are participant-equal model means.",
        ha="center",
        va="bottom",
        fontsize=8,
        color="#4B5563",
    )
    alt = (
        f"참가자별 signed onset·wake 잔차 scatter. 원은 참가자 내 공통 밤 평균, 별은 모델별 participant_equal 평균입니다. "
        f"양수는 예측이 늦음을 뜻합니다. 참가자 표시 {len(participants)}/{data['total_participants']}명(최대 "
        f"{_MAX_DIAGNOSTIC_PARTICIPANTS}명), 공통 밤 {len(keys)}/{data['total_common_nights']}개. {banner}."
    )
    return figure, alt


def _metric_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    banner = _mode_banner(result)
    title = _plot_text("Primary same-night model MAE", "동일 밤 기준 모델 MAE 비교")
    metrics = _valid_primary_metrics(result)
    models, model_order = _model_maps(result)
    order_index = {model_id: index for index, model_id in enumerate(model_order)}
    metrics.sort(key=lambda row: order_index.get(str(row.get("model_id")), len(order_index)))

    if not metrics:
        figure = _new_figure((12.0, 6.4))
        _add_heading(figure, title, banner)
        axis = figure.add_subplot(1, 1, 1)
        _empty_panel(
            axis,
            "Accuracy unavailable",
            "No known outcomes are present in the primary same-night subset; no zero or placeholder bars are drawn.",
        )
        alt = (
            "동일 밤 모델 오차 비교. Accuracy unavailable: 알려진 결과가 없어 정확도를 계산할 수 없으며 "
            f"가짜 0 막대를 표시하지 않습니다. {banner}."
        )
        return figure, alt

    diagnostic = _common_diagnostic_data(result)
    participant_maps = {
        model_id: _participant_joint_error_map(diagnostic, model_id)
        for model_id in diagnostic["model_ids"]
    }
    joint_intervals = {
        model_id: _bootstrap_mean_ci(list(values.values()), f"sleepwell-joint-mae:{model_id}")
        for model_id, values in participant_maps.items()
    }
    baseline_id = "baseline.recent7"
    baseline_map = participant_maps.get(baseline_id, {})

    figure = _new_figure((12.0, 11.5))
    _add_heading(figure, title, banner)
    top_axis = figure.add_subplot(2, 1, 1)
    delta_axis = figure.add_subplot(2, 1, 2)
    top_values: list[float] = []
    labels = []
    for row_index, row in enumerate(metrics):
        model_id = str(row.get("model_id"))
        labels.append(
            f"{_model_label(model_id, models)}\n"
            f"(nights={_count_label(row.get('n'))}; p={_count_label(row.get('n_participants'))})"
        )
        value = _finite(row.get("joint_mae_min"))
        if value is None:
            continue
        color = _model_color(model_id, row_index, models)
        top_values.append(value)
        top_axis.scatter([value], [row_index], marker="D", s=38, color=color,
                         edgecolors="#111827", linewidths=0.45, zorder=4)
        interval = joint_intervals.get(model_id)
        if interval is not None:
            top_values.extend(interval)
            top_axis.errorbar(
                [value], [row_index],
                xerr=[[max(0.0, value - interval[0])], [max(0.0, interval[1] - value)]],
                fmt="none", ecolor=color, elinewidth=1.1, capsize=2.2, zorder=3,
            )
    top_axis.set_yticks(range(len(metrics)), labels)
    top_axis.invert_yaxis()
    top_axis.set_xlim(0.0, max(1.0, max(top_values, default=0.0) * 1.2 + 0.5))
    top_axis.xaxis.set_major_locator(MaxNLocator(nbins=6, prune="upper"))
    top_axis.set_xlabel(_plot_text("Participant-equal joint MAE (min)", "참가자 동일가중 joint MAE(분)"))
    top_axis.set_title(_plot_text("Absolute error", "절대 오차"), loc="left", fontsize=10, fontweight="bold")
    top_axis.grid(axis="x", color="#D1D5DB", linewidth=0.7, alpha=0.75)
    top_axis.tick_params(axis="y", labelsize=6.8)

    deltas: list[tuple[str, float, tuple[float, float] | None, int]] = []
    for row in metrics:
        model_id = str(row.get("model_id"))
        if model_id == baseline_id:
            continue
        model_map = participant_maps.get(model_id, {})
        shared = sorted(set(baseline_map).intersection(model_map))
        paired = [model_map[participant_id] - baseline_map[participant_id] for participant_id in shared]
        if paired:
            deltas.append((model_id, sum(paired) / len(paired),
                           _bootstrap_mean_ci(paired, f"sleepwell-paired-delta:{model_id}"), len(paired)))
    if not deltas:
        _empty_panel(delta_axis, "Paired difference unavailable",
                     "Participant-level model and recent-7 baseline values are not jointly available.")
    else:
        delta_limits: list[float] = []
        for row_index, (model_id, value, interval, _paired_n) in enumerate(deltas):
            color = _model_color(model_id, row_index, models)
            delta_limits.append(value)
            delta_axis.scatter([value], [row_index], marker="o", s=36, color=color,
                               edgecolors="#111827", linewidths=0.45, zorder=4)
            if interval is not None:
                delta_limits.extend(interval)
                delta_axis.errorbar(
                    [value], [row_index],
                    xerr=[[max(0.0, value - interval[0])], [max(0.0, interval[1] - value)]],
                    fmt="none", ecolor=color, elinewidth=1.1, capsize=2.2, zorder=3,
                )
        bound = max(1.0, max(abs(value) for value in delta_limits) * 1.2)
        delta_axis.set_xlim(-bound, bound)
        delta_axis.xaxis.set_major_locator(MaxNLocator(nbins=6, symmetric=True, prune="both"))
        delta_axis.axvline(0.0, color="#4B5563", linewidth=0.9)
        delta_axis.set_yticks(
            range(len(deltas)),
            [f"{_model_label(item[0], models)}\n(paired p={item[3]})" for item in deltas],
        )
        delta_axis.invert_yaxis()
        delta_axis.set_title(_plot_text("Paired difference from baseline", "기준선 대비 paired 차이"), loc="left", fontsize=10, fontweight="bold")
        delta_axis.set_xlabel(_plot_text("Δ joint MAE vs recent-7 (min; <0 better)",
                                         "recent-7 대비 Δ joint MAE(분; <0 개선)"))
        delta_axis.grid(axis="x", color="#D1D5DB", linewidth=0.7, alpha=0.75)
        delta_axis.tick_params(axis="y", labelsize=6.8)

    figure.text(
        0.5, 0.025,
        "Identical-night subset; one mean per participant. Whiskers: participant-bootstrap 95% intervals\n"
        "(2,000 deterministic draws; unavailable for p < 2). Δ is paired within participant.",
        ha="center", va="bottom", fontsize=8, color="#4B5563",
    )
    alt = (
        "동일 밤 모델 joint MAE 비교. 위 패널은 참가자 동일가중 joint MAE, 아래 패널은 같은 참가자 내 recent-7 "
        "기준선 대비 paired 차이(음수=개선)입니다. 참가자 bootstrap 95% 구간(2,000회, 고정 시드)은 참가자 2명 "
        f"이상일 때만 표시합니다. onset·wake 세부값은 결과 표와 잔차 그림에 유지합니다. {banner}."
    )
    return figure, alt


def _personalization_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    participant_id = summary.get("chart_participant_id")
    selected = participant_id if isinstance(participant_id, str) else ""
    banner = _mode_banner(result)
    title = _plot_text("Personalization state and model inputs", "개인화 상태·모델 입력 추적")
    if selected and not _PUBLICATION_EXPORT:
        title = f"{title} - {_safe_text(selected, 48)}"

    rows = [
        row
        for row in _rows(result.get("personalization"))
        if selected
        and row.get("participant_id") == selected
        and isinstance(row.get("cutoff"), str)
        and isinstance(row.get("cohort"), str)
    ]
    cutoffs = sorted({str(row["cutoff"]) for row in rows})[:_MAX_POINTS]
    cutoff_set = set(cutoffs)
    rows = [row for row in rows if row.get("cutoff") in cutoff_set]
    policy = result.get("personalization_policy", "legacy14")
    if policy == "gradual_v1":
        policy_note = (
            "gradual_v1: weight=n/(n+14) from the first valid record; 14 is shrinkage strength, "
            "not a waiting period. Changed inference policy with frozen weights requires separate validation."
        )
        policy_alt = (
            "gradual_v1은 첫 유효 기록부터 n/(n+14)로 반영하며 14는 대기기간이 아닌 수축 강도입니다. "
            "기존 동결 가중치에 추론 개인화 정책을 변경했으므로 별도 검증이 필요합니다."
        )
    else:
        policy_note = (
            "legacy14: personalization requires at least 14 valid records and 14 elapsed days; "
            "then weight=n/(n+14). This preserves the original frozen-model policy."
        )
        policy_alt = (
            "legacy14는 유효 기록 14개 이상과 경과일 14일 이상에서 n/(n+14)를 적용하는 "
            "기존 동결 모델 정책입니다."
        )
    fixed_note = (
        "Fixed model assumptions: tau_wake=18.2 h; tau_sleep=4.2 h; circadian amplitude=0.15. "
        f"They are not participant-identified physiology. {policy_note}"
    )

    if not rows or not cutoffs:
        figure = _new_figure((12.0, 6.4))
        _add_heading(figure, title, banner)
        axis = figure.add_subplot(1, 1, 1)
        _empty_panel(axis, "No personalization trace available", fixed_note)
        alt = (
            "개인화 상태·모델 입력 추적. 선택 참가자의 상태 스냅샷이 없습니다. "
            "고정 가정은 tau_wake 18.2시간, tau_sleep 4.2시간, 진폭 0.15이며 개인 생리 추정값이 아닙니다. "
            f"{policy_alt} {banner}."
        )
        return figure, alt

    cohorts = [
        cohort
        for cohort in _COHORT_ORDER
        if any(_safe_text(row.get("cohort"), 12).casefold() == cohort for row in rows)
    ]
    extras = sorted(
        {
            _safe_text(row.get("cohort"), 12).casefold()
            for row in rows
            if _safe_text(row.get("cohort"), 12).casefold() not in cohorts
        }
    )
    cohorts.extend(extras)
    cohorts = cohorts[:4]
    row_map: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        cohort = _safe_text(row.get("cohort"), 12).casefold()
        if cohort in cohorts:
            row_map.setdefault((str(row["cutoff"]), cohort), row)

    figure = _new_figure((12.0, 12.0))
    _add_heading(figure, title, banner)
    grid = figure.add_gridspec(5, 1, height_ratios=(1.0, 1.0, 1.45, 1.0, 1.35))
    axes = [figure.add_subplot(grid[index, 0]) for index in range(4)]
    note_axis = figure.add_subplot(grid[4, 0])
    note_axis.set_axis_off()
    note_axis.text(0.01, 0.32, fixed_note, transform=note_axis.transAxes, ha="left", va="center", fontsize=8.5, wrap=True)
    note_axis.text(
        0.01,
        0.04,
        "Only result state/features are plotted; missing values remain gaps and are never replaced with zero.",
        transform=note_axis.transAxes,
        ha="left",
        va="center",
        fontsize=8,
        color="#4B5563",
    )

    x = list(range(len(cutoffs)))
    axis_specs = (
        (axes[0], "personal_weight", "state", _plot_text("Personal weight", "개인 가중치"), "weight"),
        (axes[1], "phase_min", "state", _plot_text("Phase (minutes)", "위상(분)"), "phase"),
        (axes[3], "history_count", "row", _plot_text("Valid history count", "유효 이력 수"), "history"),
    )
    for axis, field, source, axis_title, _kind in axis_specs:
        values_for_limits: list[float | None] = []
        plotted = False
        for cohort_index, cohort in enumerate(cohorts):
            values: list[float | None] = []
            for cutoff in cutoffs:
                row = row_map.get((cutoff, cohort), {})
                if source == "state":
                    state = row.get("state") if isinstance(row.get("state"), Mapping) else {}
                    value = _finite(state.get(field))
                else:
                    value = _finite(row.get(field))
                values.append(value)
            values_for_limits.extend(values)
            if not any(value is not None for value in values):
                continue
            plotted = True
            axis.plot(
                x,
                values,
                color=_COHORT_COLORS.get(cohort, _MODEL_COLORS[cohort_index % len(_MODEL_COLORS)]),
                linewidth=1.7,
                label=cohort.upper(),
                drawstyle="steps-post" if field == "history_count" else "default",
            )
        axis.set_title(axis_title, loc="left", fontsize=10, fontweight="bold")
        axis.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.75)
        limits = _finite_limits(values_for_limits, minimum_pad=0.05 if field == "personal_weight" else 1.0)
        if limits is not None:
            axis.set_ylim(*limits)
        if not plotted:
            axis.text(0.5, 0.5, "Not available in result", transform=axis.transAxes, ha="center", va="center", color="#6B7280")

    process_axis = axes[2]
    process_styles = (
        ("current_s", "features", "S", "-"),
        ("current_c", "features", "C", "--"),
        ("sleep_threshold", "state", "sleep threshold", ":"),
        ("wake_threshold", "state", "wake threshold", "-."),
    )
    process_values: list[float | None] = []
    process_plotted = False
    for cohort_index, cohort in enumerate(cohorts):
        color = _COHORT_COLORS.get(cohort, _MODEL_COLORS[cohort_index % len(_MODEL_COLORS)])
        for field, source, field_label, linestyle in process_styles:
            values = []
            for cutoff in cutoffs:
                row = row_map.get((cutoff, cohort), {})
                container = row.get(source) if isinstance(row.get(source), Mapping) else {}
                values.append(_finite(container.get(field)))
            process_values.extend(values)
            if not any(value is not None for value in values):
                continue
            process_plotted = True
            process_axis.plot(
                x,
                values,
                color=color,
                linestyle=linestyle,
                linewidth=1.45,
                label=f"{cohort.upper()} {field_label}",
            )
    process_axis.set_title(
        _plot_text("Current S/C and transition thresholds", "현재 S/C와 전이 임계값"),
        loc="left",
        fontsize=10,
        fontweight="bold",
    )
    process_axis.grid(axis="y", color="#D1D5DB", linewidth=0.7, alpha=0.75)
    process_limits = _finite_limits(process_values, minimum_pad=0.05)
    if process_limits is not None:
        process_axis.set_ylim(*process_limits)
    if not process_plotted:
        process_axis.text(
            0.5,
            0.5,
            "S/C and thresholds not available in result",
            transform=process_axis.transAxes,
            ha="center",
            va="center",
            color="#6B7280",
        )

    legend_handles = [
        Line2D([0], [0], color=_COHORT_COLORS.get(cohort, _MODEL_COLORS[index]), linewidth=2,
               label=f"Cohort: {cohort.upper()}")
        for index, cohort in enumerate(cohorts)
    ]
    if process_plotted:
        legend_handles.extend(
            Line2D([0], [0], color="#374151", linestyle=linestyle, linewidth=1.5, label=field_label)
            for _field, _source, field_label, linestyle in process_styles
        )
    if legend_handles:
        note_axis.legend(
            handles=legend_handles, loc="upper left", bbox_to_anchor=(0.0, 1.02),
            ncol=3, fontsize=7.5, frameon=False,
        )

    positions = _tick_positions(len(cutoffs))
    for axis in axes[:-1]:
        axis.tick_params(axis="x", labelbottom=False)
    axes[-1].set_xticks(positions, [_date_label(cutoffs[position]) for position in positions])
    axes[-1].set_xlabel(_plot_text("Cutoff date (20:00, source timezone)", "기준일(20:00, 입력 시간대)"))
    alt = (
        "개인화 상태·모델 입력 추적. 선택 참가자와 코호트별 personal_weight, phase_min, current_s, current_c, "
        "수면·기상 임계값, history_count를 실제 결과 스냅샷에서 표시합니다. 고정 가정 tau_wake 18.2시간, "
        "tau_sleep 4.2시간, 진폭 0.15는 개인 생리 추정값이 아닙니다. "
        f"{policy_alt} {banner}."
    )
    return figure, alt


_COMPLETENESS_STATUSES = (
    ("timely", "Observed, available by next 20:00", "관측 · 다음 20시 전 입력", "#2F855A"),
    ("late", "Observed, reported after next 20:00", "관측 · 다음 20시 이후 입력", "#D69E2E"),
    ("prediction_only", "Prediction only (outcome unavailable)", "예측만 · 결과 없음", "#90CDF4"),
    ("missing", "No row", "행 없음", "#CBD5E0"),
)


def _observation_display_ids(result: Mapping[str, Any], participants: list[str]) -> dict[str, str]:
    known = _participant_display_map(result)
    display: dict[str, str] = {}
    extra = len(known)
    for participant in participants:
        if participant in known:
            display[participant] = known[participant]
        else:
            extra += 1
            display[participant] = f"P{extra:02d}"
    return display


def _hour_ticks(axis: Any, hours: int) -> None:
    positions = list(range(0, hours * 60 + 1, 240))
    axis.set_xticks(positions, [f"{(20 + position // 60) % 24:02d}:00" for position in positions])
    axis.set_xlim(0, hours * 60)


def _actogram_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    """Double-plotted sleep actogram (GGIR / pyActigraphy convention) for the chart participant."""
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    participant_id = summary.get("chart_participant_id")
    selected = participant_id if isinstance(participant_id, str) else ""
    banner = _mode_banner(result)
    title = _plot_text("Double-plotted sleep actogram", "이중 플롯 수면 액토그램")
    if selected and not _PUBLICATION_EXPORT:
        title = f"{title} - {_safe_text(selected, 48)}"
    figure = _new_figure((12.0, 10.0))
    _add_heading(figure, title, banner)

    models, model_order = _model_maps(result)
    reference = next((model_id for model_id in model_order if model_id.endswith(".hybrid")), None)
    nights: dict[str, dict[str, Any]] = {}
    for row in _rows(result.get("predictions")):
        cutoff = row.get("cutoff")
        if row.get("participant_id") != selected or not isinstance(cutoff, str):
            continue
        night = nights.setdefault(cutoff, {"actual": None, "predicted": None})
        onset, wake = _finite(row.get("actual_onset_offset_min")), _finite(row.get("actual_wake_offset_min"))
        if onset is not None and wake is not None:
            night["actual"] = (onset, wake)
        if row.get("model_id") == reference:
            onset, wake = _finite(row.get("onset_offset_min")), _finite(row.get("wake_offset_min"))
            if onset is not None and wake is not None:
                night["predicted"] = (onset, wake)
    cutoffs = sorted(nights)[:_MAX_POINTS]
    if not cutoffs:
        axis = figure.add_subplot(1, 1, 1)
        _empty_panel(axis, "No actogram available", "No rows are available for summary.chart_participant_id.")
        return figure, f"이중 플롯 수면 액토그램. 선택 참가자 {_safe_text(selected) or '없음'}의 행이 없습니다. {banner}."

    axis = figure.add_subplot(1, 1, 1)
    figure.get_layout_engine().set(rect=(0.035, 0.13, 0.93, 0.79))
    observed_color = "#5B3FA6"
    predicted_color = "#E8590C"
    unknown_color = "#E5E7EB"
    for index, cutoff in enumerate(cutoffs):
        # Left half: this night; right half: the following calendar night (double plot).
        for half, key in ((0, cutoff), (1, cutoffs[index + 1] if index + 1 < len(cutoffs) else None)):
            if key is None:
                continue
            offset = half * 1440
            night = nights[key]
            if night["actual"] is None:
                axis.broken_barh([(offset, 1440)], (index - 0.4, 0.8), facecolors=unknown_color,
                                 edgecolors="none", zorder=1)
            else:
                start, end = night["actual"]
                axis.broken_barh([(offset + start, end - start)], (index - 0.3, 0.52),
                                 facecolors=observed_color, edgecolors="none", zorder=2)
            if night["predicted"] is not None:
                start, end = night["predicted"]
                axis.plot([offset + start, offset + end], [index + 0.36, index + 0.36],
                          color=predicted_color, linewidth=1.1, solid_capstyle="butt", zorder=3)
    axis.set_ylim(len(cutoffs) - 0.5, -0.5)
    positions = _tick_positions(len(cutoffs), maximum=12)
    axis.set_yticks(positions, [_date_label(cutoffs[position]) for position in positions])
    _hour_ticks(axis, 48)
    axis.axvline(1440, color="#9CA3AF", linewidth=0.8)
    axis.grid(axis="x", color="#E5E7EB", linewidth=0.6)
    axis.set_xlabel(_plot_text("Clock time (two consecutive days, starting at the 20:00 cutoff)",
                               "시각 (20:00 기준일부터 연속 이틀)"))
    axis.set_ylabel(_plot_text("Cutoff date", "기준일"))
    handles = [
        Line2D([0], [0], color=observed_color, linewidth=6, label=_plot_text("Observed main sleep", "관측 주 수면")),
        Line2D([0], [0], color=unknown_color, linewidth=6, label=_plot_text("Outcome unavailable", "결과 없음")),
    ]
    if reference:
        handles.append(Line2D([0], [0], color=predicted_color, linewidth=1.6,
                              label=_plot_text("Prediction: ", "예측: ") + _model_label(reference, models)))
    figure.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.045), ncol=3,
                  fontsize=8, frameon=False)
    observed = sum(nights[cutoff]["actual"] is not None for cutoff in cutoffs)
    alt = (
        f"이중 플롯 수면 액토그램. 참가자 {_safe_text(selected)}의 {len(cutoffs)}개 기준일 중 관측 주 수면 {observed}밤을 "
        f"20시부터 이틀 폭으로 표시하고, 오른쪽 절반은 다음 날 밤을 반복합니다. 회색은 결과 없음, 가는 선은 "
        f"{_model_label(reference, models) if reference else '예측 없음'} 예측 구간입니다. 낮잠은 입력에 없어 표시되지 않습니다. {banner}."
    )
    return figure, alt


def _completeness_figure(result: Mapping[str, Any]) -> tuple[Figure, str]:
    """Participant x calendar-day availability grid (compliance dashboards of Beiwe / CentrePoint)."""
    banner = _mode_banner(result)
    title = _plot_text("Data completeness and reporting timeliness", "데이터 완결성·입력 시점")
    observations = result.get("observations") if isinstance(result.get("observations"), Mapping) else {}
    entries = [row for row in _rows(observations.get("completeness")) if isinstance(row.get("participant_id"), str)]
    entries = entries[:_MAX_DIAGNOSTIC_PARTICIPANTS]
    figure = _new_figure((12.0, min(8.0, 5.0 + 0.4 * len(entries))))
    _add_heading(figure, title, banner)
    dates = sorted({cell.get("date") for row in entries for cell in _rows(row.get("grid"))
                    if isinstance(cell.get("date"), str)})[:_MAX_POINTS + 31]
    if not entries or not dates:
        axis = figure.add_subplot(1, 1, 1)
        _empty_panel(axis, "No completeness data", "The result does not contain observation completeness rows.")
        return figure, f"데이터 완결성 그림. 표시할 행이 없습니다. {banner}."

    status_index = {status: index for index, (status, *_rest) in enumerate(_COMPLETENESS_STATUSES)}
    outside = len(_COMPLETENESS_STATUSES)
    column = {date: index for index, date in enumerate(dates)}
    matrix = [[outside] * len(dates) for _ in entries]
    for row_index, row in enumerate(entries):
        for cell in _rows(row.get("grid")):
            position = column.get(cell.get("date"))
            if position is not None and cell.get("status") in status_index:
                matrix[row_index][position] = status_index[cell["status"]]

    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    colors = [color for *_rest, color in _COMPLETENESS_STATUSES] + ["#FFFFFF"]
    colormap = ListedColormap(colors)
    norm = BoundaryNorm([value - 0.5 for value in range(len(colors) + 1)], colormap.N)
    axis = figure.add_subplot(1, 1, 1)
    figure.get_layout_engine().set(rect=(0.035, 0.24, 0.93, 0.62))
    axis.imshow(matrix, aspect="auto", interpolation="nearest", cmap=colormap, norm=norm)
    participants = [row["participant_id"] for row in entries]
    display = _observation_display_ids(result, participants)
    labels = [display[pid] if _PUBLICATION_EXPORT else _safe_text(pid, 24) for pid in participants]
    axis.set_yticks(range(len(entries)), labels, fontsize=8)
    positions = _tick_positions(len(dates), maximum=10)
    axis.set_xticks(positions, [dates[position][5:] for position in positions], fontsize=8)
    axis.set_xlabel(_plot_text("Cutoff date (20:00)", "기준일(20:00)"))
    for row_index, row in enumerate(entries):
        coverage = _finite(row.get("labeled_coverage"))
        if coverage is not None:
            axis.text(1.005, row_index, f"{coverage * 100:.0f}%", transform=axis.get_yaxis_transform(),
                      va="center", ha="left", fontsize=8, color="#374151")
    legend_labels = [_plot_text(english, korean) for _status, english, korean, _color in _COMPLETENESS_STATUSES]
    figure.legend(
        handles=[Patch(facecolor=color, edgecolor="none", label=label)
                 for (*_rest, color), label in zip(_COMPLETENESS_STATUSES, legend_labels)],
        loc="lower center", bbox_to_anchor=(0.5, 0.07), ncol=2 if _PUBLICATION_EXPORT else 4,
        fontsize=8, frameon=False,
    )
    figure.text(
        0.5, 0.02,
        _plot_text("Right-hand percentage: observed nights / days between first and last row.",
                   "오른쪽 백분율: 첫 행과 마지막 행 사이 날짜 중 관측된 밤의 비율"),
        ha="center", va="bottom", fontsize=8, color="#4B5563",
    )
    late = sum(int(row.get("late_nights") or 0) for row in entries)
    missing = sum(int(row.get("missing_nights") or 0) for row in entries)
    alt = (
        f"데이터 완결성 그림. 참가자 {len(entries)}명 × 기준일 {len(dates)}일 격자에서 다음 20시 전에 입력된 관측 밤, "
        f"늦게 입력된 밤 {late}개, 예측만 있는 밤, 행이 없는 날 {missing}개를 색으로 구분합니다. {banner}."
    )
    return figure, alt


def _encode_chart(figure: Figure, chart_id: str, title: str, alt: str) -> dict[str, str]:
    _apply_local_font(figure)
    try:
        with BytesIO() as buffer:
            canvas = figure.canvas
            if not isinstance(canvas, FigureCanvasAgg):
                canvas = FigureCanvasAgg(figure)
            canvas.print_png(
                buffer,
                metadata={
                    "Title": chart_id,
                    "Description": alt,
                    "Software": "SleepWell research lab in-memory Matplotlib renderer",
                },
            )
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    finally:
        figure.clear()
    return {
        "id": chart_id,
        "title": title,
        "alt": alt,
        "data_uri": f"data:image/png;base64,{encoded}",
    }


def _figure_specs(result: Mapping[str, Any]) -> list[tuple[str, str, Figure, str]]:
    definitions = (
        ("prediction-timeline", "관측·예측 수면 시각", _timeline_figure),
        ("model-mae-comparison", "동일 밤 모델 MAE 비교", _metric_figure),
        ("personalization-traces", "개인화 상태·모델 입력 추적", _personalization_figure),
        ("participant-mae-heatmap", "모델·참가자 공통 밤 MAE", _participant_mae_heatmap_figure),
        ("joint-error-distribution", "참가자별 joint MAE 분포", _joint_error_boxplot_figure),
        ("signed-residual-bias", "signed onset·wake 잔차", _signed_residual_figure),
        ("sleep-actogram", "이중 플롯 수면 액토그램", _actogram_figure),
        ("data-completeness", "데이터 완결성·입력 시점", _completeness_figure),
    )
    return [
        (chart_id, title, *builder(result))
        for chart_id, title, builder in definitions
    ]


def render_charts(result: dict) -> list[dict]:
    """Render the bounded research-lab figures without files or pyplot state."""

    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    with _RENDER_LOCK:
        return [
            _encode_chart(figure, chart_id, title, alt)
            for chart_id, title, figure, alt in _figure_specs(result)
        ]


def _safe_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite(value)
        return "" if number is None else f"{number:.12g}"
    text = str(value)
    # result.json remains lossless; the tabular companion is safe to open in a
    # spreadsheet without interpreting participant labels as formulas.
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _publication_source_csv(result: Mapping[str, Any]) -> bytes:
    columns = (
        "record_type", "participant_id", "display_participant", "cutoff", "model_id", "cohort",
        "variable", "value", "unit", "n_nights", "n_participants", "averaging",
    )
    rows: list[dict[str, Any]] = []
    display_ids = _participant_display_map(result)

    def add(**values: Any) -> None:
        if len(rows) >= _MAX_EXPORT_SOURCE_ROWS:
            raise ValueError("publication source-data row limit exceeded")
        rows.append(values)

    for metric in _rows(result.get("metrics")):
        for field, _label, _color in _EXPORT_METRIC_FIELDS:
            value = _finite(metric.get(field))
            if value is not None:
                add(
                    record_type="participant_equal_metric", model_id=metric.get("model_id"),
                    variable=field, value=value, unit="minutes",
                    n_nights=metric.get("n"), n_participants=metric.get("n_participants"),
                    averaging=metric.get("averaging"),
                )
    prediction_fields = (
        "onset_offset_min", "wake_offset_min", "interval_min",
        "actual_onset_offset_min", "actual_wake_offset_min", "actual_interval_min",
    )
    for prediction in _rows(result.get("predictions")):
        for field in prediction_fields:
            value = _finite(prediction.get(field))
            if value is not None:
                add(
                    record_type="prediction", participant_id=prediction.get("participant_id"),
                    display_participant=display_ids.get(str(prediction.get("participant_id")), ""),
                    cutoff=prediction.get("cutoff"), model_id=prediction.get("model_id"),
                    variable=field, value=value, unit="minutes_after_cutoff" if "offset" in field else "minutes",
                )
    personalization_fields = (
        ("row", "history_count", "count"),
        ("state", "personal_weight", "proportion"),
        ("state", "phase_min", "minutes"),
        ("state", "sleep_threshold", "model_state"),
        ("state", "wake_threshold", "model_state"),
        ("features", "current_s", "model_state"),
        ("features", "current_c", "model_state"),
    )
    for row in _rows(result.get("personalization")):
        for container_name, field, unit in personalization_fields:
            container = row if container_name == "row" else row.get(container_name)
            value = _finite(container.get(field)) if isinstance(container, Mapping) else None
            if value is not None:
                add(
                    record_type="personalization", participant_id=row.get("participant_id"),
                    display_participant=display_ids.get(str(row.get("participant_id")), ""),
                    cutoff=row.get("cutoff"), cohort=row.get("cohort"), variable=field,
                    value=value, unit=unit,
                )

    observations = result.get("observations") if isinstance(result.get("observations"), Mapping) else {}
    regularity_fields = (
        ("sri", "index_-100_100"), ("interdaily_stability", "ratio"), ("intradaily_variability", "ratio"),
        ("onset_sd_min", "minutes"), ("wake_sd_min", "minutes"), ("midpoint_sd_min", "minutes"),
        ("duration_mean_min", "minutes"), ("duration_sd_min", "minutes"),
        ("weekend_weekday_midpoint_gap_min", "minutes"),
    )
    for row in _rows(observations.get("regularity")):
        for field, unit in regularity_fields:
            value = _finite(row.get(field))
            if value is not None:
                add(
                    record_type="observed_regularity", participant_id=row.get("participant_id"),
                    display_participant=display_ids.get(str(row.get("participant_id")), ""),
                    variable=field, value=value, unit=unit, n_nights=row.get("labeled_nights"),
                )
    for row in _rows(observations.get("completeness")):
        for cell in _rows(row.get("grid")):
            add(
                record_type="observed_completeness", participant_id=row.get("participant_id"),
                display_participant=display_ids.get(str(row.get("participant_id")), ""),
                cutoff=cell.get("date"), variable=f"status:{cell.get('status')}",
                value=_finite(cell.get("report_delay_min")), unit="report_delay_minutes",
            )

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _safe_csv_value(row.get(column)) for column in columns})
    return output.getvalue().encode("utf-8-sig")


def _zip_write(bundle: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    bundle.writestr(info, content)


def _validate_publication_text_bounds(figure: Figure, chart_id: str) -> None:
    """Fail export when visible text falls outside the declared physical page."""

    canvas = figure.canvas
    if not isinstance(canvas, FigureCanvasAgg):
        canvas = FigureCanvasAgg(figure)
    canvas.draw()
    renderer = canvas.get_renderer()
    page = figure.bbox
    tolerance = 2.0
    overflow: list[str] = []
    for item in figure.findobj(match=Text):
        if not item.get_visible() or not item.get_text().strip():
            continue
        bounds = item.get_window_extent(renderer=renderer)
        if (
            bounds.x0 < page.x0 - tolerance
            or bounds.y0 < page.y0 - tolerance
            or bounds.x1 > page.x1 + tolerance
            or bounds.y1 > page.y1 + tolerance
        ):
            overflow.append(_safe_text(item.get_text(), 64))
    if overflow:
        raise ValueError(f"publication text outside canvas for {chart_id}: {overflow[:3]}")


def export_figures(result: dict) -> bytes:
    """Return a self-contained, general-purpose manuscript figure ZIP.

    This is a provisional export because no target journal or production phase
    has been specified. Vector text remains editable in SVG, PDF uses TrueType
    fonts, and the PNG copy is rendered at 300 dpi at the declared physical size.
    """

    if not isinstance(result, Mapping):
        raise TypeError("result must be a mapping")
    exact_result = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    source_csv = _publication_source_csv(result)
    files: list[dict[str, Any]] = []
    figure_records: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    global _PUBLICATION_EXPORT
    with _RENDER_LOCK:
        previous_mode = _PUBLICATION_EXPORT
        _PUBLICATION_EXPORT = True
        try:
            specifications = _figure_specs(result)
            with matplotlib.rc_context(
                {"pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none", "savefig.facecolor": "white"}
            ):
                for order, (chart_id, _ui_title, figure, alt) in enumerate(specifications, start=1):
                    try:
                        width_mm = _PUBLICATION_WIDTH_MM
                        height_mm = _PUBLICATION_HEIGHTS_MM[chart_id]
                        figure.set_size_inches(width_mm / 25.4, height_mm / 25.4, forward=True)
                        _apply_local_font(figure)
                        _validate_publication_text_bounds(figure, chart_id)
                        stem = f"figures/{order:02d}-{chart_id}"
                        formats = (
                            ("png", {"Title": chart_id, "Description": alt, "Software": "SleepWell/Matplotlib"}),
                            ("pdf", {"Title": chart_id, "Subject": alt, "Creator": "SleepWell/Matplotlib"}),
                            ("svg", {"Title": chart_id, "Description": alt, "Creator": "SleepWell/Matplotlib"}),
                        )
                        for extension, metadata in formats:
                            with BytesIO() as output:
                                figure.savefig(
                                    output, format=extension, dpi=300, facecolor="white",
                                    transparent=False, metadata=metadata,
                                )
                                content = output.getvalue()
                            filename = f"{stem}.{extension}"
                            payloads.append((filename, content))
                            files.append(
                                {
                                    "chart_id": chart_id, "path": filename, "format": extension,
                                    "sha256": hashlib.sha256(content).hexdigest(),
                                    "width_mm": width_mm, "height_mm": height_mm,
                                    "dpi": 300 if extension == "png" else None,
                                }
                            )
                        figure_record = {
                            "id": chart_id,
                            "title": _PUBLICATION_CAPTIONS[chart_id].split(". ", 1)[0],
                            "caption": _PUBLICATION_CAPTIONS[chart_id],
                            "alt": alt,
                            "width_mm": width_mm,
                            "height_mm": height_mm,
                            "source_fields": list(_PUBLICATION_SOURCE_FIELDS[chart_id]),
                            "paths": {
                                extension: f"{stem}.{extension}"
                                for extension in ("png", "pdf", "svg")
                            },
                            "text_bounds_checked": True,
                        }
                        if chart_id == "participant-mae-heatmap":
                            figure_record["display_participant_mapping"] = {
                                code: participant_id
                                for participant_id, code in _participant_display_map(result).items()
                            }
                        figure_records.append(figure_record)
                    finally:
                        figure.clear()
        finally:
            _PUBLICATION_EXPORT = previous_mode

    manifest = {
        "schema_version": 1,
        "status": "provisional_general_publication_export",
        "journal_profile": None,
        "mode": result.get("mode"),
        "run_id": result.get("run_id"),
        "matplotlib_version": matplotlib.__version__,
        "figure_count": len(figure_records),
        "source_data": "source-data.csv",
        "exact_result": "result.json",
        "estimands": {
            "primary": "participant-equal joint MAE on identical common nights",
            "uncertainty": "participant bootstrap 95% percentile interval; 2,000 draws; deterministic seed; unavailable for participant n < 2",
            "distribution": "one within-participant common-night joint MAE per participant and model",
            "signed_bias": "within-participant mean signed residual; participant-equal model mean",
        },
        "missing_data": "retained as gaps; never imputed as zero by the renderer",
        "limits": {"max_width_mm": _PUBLICATION_WIDTH_MM, "source_rows": _MAX_EXPORT_SOURCE_ROWS},
        "figures": figure_records,
        "files": files,
    }
    readme = (
        "SleepWell general-purpose manuscript figure bundle\n\n"
        "This export is provisional: a target journal, article type, submission phase, and final column width "
        "were not specified. PDF and SVG are vector outputs; PNG is 300 dpi at the physical size recorded in "
        "figure-manifest.json. result.json preserves the exact evaluation result. source-data.csv is a long-form "
        "table of plotted values; labels beginning with spreadsheet formula characters are prefixed with an "
        "apostrophe in CSV only. Missing values remain absent/gaps. The run mode and evidence label are "
        "recorded in result.json and figure-manifest.json rather than printed on the figures.\n\n"
        "Reproduction: install a compatible Python and Matplotlib, then run `python reproduce.py`.\n"
    ).encode("utf-8")
    reproduce = (
        "from io import BytesIO\n"
        "import json\n"
        "from pathlib import Path\n"
        "from zipfile import ZipFile\n"
        "from research_lab_charts import export_figures\n\n"
        "root = Path(__file__).resolve().parent\n"
        "result = json.loads((root / 'result.json').read_text(encoding='utf-8'))\n"
        "with ZipFile(BytesIO(export_figures(result))) as bundle:\n"
        "    for member in bundle.infolist():\n"
        "        if member.filename.startswith('figures/'):\n"
        "            target = root / member.filename\n"
        "            target.parent.mkdir(parents=True, exist_ok=True)\n"
        "            target.write_bytes(bundle.read(member))\n"
    ).encode("utf-8")
    module_source = Path(__file__).read_bytes()
    with BytesIO() as output:
        with zipfile.ZipFile(output, "w") as bundle:
            for name, content in payloads:
                _zip_write(bundle, name, content)
            _zip_write(bundle, "figure-manifest.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
            _zip_write(bundle, "source-data.csv", source_csv)
            _zip_write(bundle, "result.json", exact_result)
            _zip_write(bundle, "README.txt", readme)
            _zip_write(bundle, "reproduce.py", reproduce)
            _zip_write(bundle, "research_lab_charts.py", module_source)
        return output.getvalue()
