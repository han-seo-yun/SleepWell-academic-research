"""Matplotlib-only figures for comparing SleepWell timing regressors."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from matplotlib.figure import Figure

from .academic_plotting import model_label
from .result_adapter import model_color


def _text(lang: str) -> dict[str, str]:
    return ({
        "mae": "평균 절대 오차(MAE)", "rmse": "큰 오차 강조(RMSE)",
        "bias": "평균 편향(예측-실제)", "tolerance": "허용오차 내 비율",
        "predictions": "실제값과 예측값", "errors": "야간별 오차 분포",
        "participants": "참여자별 MAE 분포", "minutes": "분", "rate": "비율(%)",
        "actual": "실제값(분)", "predicted": "예측값(분)", "error": "예측-실제(분)",
        "no_data": "비교 가능한 공통 예측값이 없습니다.",
        "target_onset": "취침 시각", "target_wake": "기상 시각", "target_interval": "수면 구간",
        "participant_equal": "참여자 균등", "night_equal": "야간 균등",
        "sample": "n={n}밤, {people}명", "night_points": "야간 단위 점",
        "participant_mae": "참여자별 MAE", "participant_rmse_mean": "참여자별 RMSE의 평균",
        "pooled_rmse": "야간 통합 RMSE", "tolerance_title": "허용오차 내 비율 (±{value:g}분)",
    } if lang == "ko" else {
        "mae": "Mean absolute error (MAE)", "rmse": "Large-error-sensitive RMSE",
        "bias": "Mean bias (predicted - actual)", "tolerance": "Within-tolerance rate",
        "predictions": "Observed versus predicted", "errors": "Night-level error distribution",
        "participants": "Participant-level MAE distribution", "minutes": "Minutes", "rate": "Rate (%)",
        "actual": "Observed (min)", "predicted": "Predicted (min)", "error": "Predicted - observed (min)",
        "no_data": "No common comparable predictions are available.",
        "target_onset": "Sleep onset", "target_wake": "Wake time", "target_interval": "Sleep interval",
        "participant_equal": "Participant equal", "night_equal": "Night equal",
        "sample": "n={n} nights, {people} participants", "night_points": "Night-level points",
        "participant_mae": "Participant-level MAE", "participant_rmse_mean": "Mean participant RMSE",
        "pooled_rmse": "Pooled-night RMSE", "tolerance_title": "Within-tolerance rate (±{value:g} min)",
    })


def _footer(figure: Figure, data: dict[str, Any], labels: dict[str, str], kind: str) -> None:
    target = labels[f"target_{data['target']}"]
    sample = labels["sample"].format(n=data["n_nights"], people=data["n_participants"])
    if kind in {"predictions", "errors"}:
        unit = labels["night_points"]
    elif kind == "participants":
        unit = labels["participant_mae"]
    elif kind == "rmse":
        unit = (labels["participant_rmse_mean"] if data["averaging"] == "participant_equal"
                else labels["pooled_rmse"])
    else:
        unit = labels[data["averaging"]]
    note = f"{target} · {sample} · {unit} · Matplotlib/Python"
    figure.text(.5, .10 / figure.get_figheight(), note, ha="center", va="bottom", fontsize=8, color="#34495e")


def _empty(data: dict[str, Any], labels: dict[str, str]) -> Figure:
    figure = Figure(figsize=(9.2, 5.4), dpi=100, facecolor="white")
    ax = figure.subplots()
    ax.text(.5, .5, labels["no_data"], ha="center", va="center", transform=ax.transAxes)
    ax.set_axis_off()
    return figure


def _bar(data: dict[str, Any], kind: str, labels: dict[str, str], lang: str) -> Figure:
    values = [(row["model_id"], float(row["within_tolerance"] * 100 if kind == "tolerance" else row[kind]))
              for row in data["rows"]]
    values.sort(key=lambda item: item[1], reverse=kind == "tolerance")
    height = max(5.4, 0.48 * len(values) + 2.2)
    figure = Figure(figsize=(9.2, height), dpi=100, facecolor="white")
    ax = figure.subplots()
    positions = np.arange(len(values))
    bars = ax.barh(positions, [value for _model, value in values],
                   color=[model_color(model) for model, _value in values], alpha=.85)
    ax.set_yticks(positions, [model_label(model, lang) for model, _value in values])
    ax.invert_yaxis()
    title = (labels["tolerance_title"].format(value=data["tolerance"])
             if kind == "tolerance" else labels[kind])
    ax.set_title(title, fontweight="bold")
    ax.set_xlabel(labels["rate"] if kind == "tolerance" else labels["minutes"])
    ax.grid(axis="x", alpha=.22)
    if kind == "tolerance":
        ax.set_xlim(0, 105)
    elif kind == "bias":
        limit = max((abs(value) for _model, value in values), default=1) * 1.18 or 1
        ax.set_xlim(-limit, limit)
        ax.axvline(0, color="#333333", linewidth=1)
    else:
        maximum = max((value for _model, value in values), default=1)
        ax.set_xlim(0, maximum * 1.16 if maximum > 0 else 1)
    for bar, (_model, value) in zip(bars, values):
        ax.text(value, bar.get_y() + bar.get_height()/2,
                f" {value:.1f}{'%' if kind == 'tolerance' else ''}", va="center", fontsize=8)
    figure.subplots_adjust(left=.28, right=.94, top=.88, bottom=.13)
    _footer(figure, data, labels, kind)
    return figure


def _predictions(data: dict[str, Any], labels: dict[str, str], lang: str) -> Figure:
    models = data["model_ids"]
    columns = min(2 if len(models) <= 4 else 3, max(1, len(models)))
    rows = math.ceil(len(models) / columns)
    height = 3.6 * rows + .9
    figure = Figure(figsize=(4.0 * columns, height), dpi=100, facecolor="white")
    axes = np.atleast_1d(figure.subplots(rows, columns, squeeze=False)).ravel()
    values = [float(row[key]) for row in data["predictions"] for key in ("actual", "predicted")]
    low, high = min(values), max(values)
    margin = max(5.0, (high - low) * .04)
    for ax, model in zip(axes, models):
        subset = [row for row in data["predictions"] if row["model_id"] == model]
        ax.scatter([row["actual"] for row in subset], [row["predicted"] for row in subset],
                   color=model_color(model), alpha=.58, s=22, edgecolors="none")
        ax.plot([low-margin, high+margin], [low-margin, high+margin], "--", color="#444444", linewidth=1)
        ax.set(xlim=(low-margin, high+margin), ylim=(low-margin, high+margin),
               xlabel=labels["actual"], ylabel=labels["predicted"])
        ax.set_title(model_label(model, lang), fontsize=10, fontweight="bold")
        ax.grid(alpha=.18)
    for ax in axes[len(models):]:
        ax.set_axis_off()
    figure.suptitle(labels["predictions"], fontweight="bold")
    # Reserve physical space for both the figure title and the footer, including
    # the single-row case where fractional margins used to overlap axis labels.
    figure.subplots_adjust(left=.10, right=.97, top=1-.74/height, bottom=.70/height,
                           wspace=.34, hspace=.48)
    _footer(figure, data, labels, "predictions")
    return figure


def _box(data: dict[str, Any], kind: str, labels: dict[str, str], lang: str) -> Figure:
    models = data["model_ids"]
    if kind == "errors":
        groups = [[row["error"] for row in data["predictions"] if row["model_id"] == model] for model in models]
        ylabel = labels["error"]
    else:
        groups = [[row["mae"] for row in data["participant_rows"] if row["model_id"] == model] for model in models]
        ylabel = labels["minutes"]
    figure = Figure(figsize=(max(9.2, .9 * len(models) + 2.5), 5.8), dpi=100, facecolor="white")
    ax = figure.subplots()
    boxes = ax.boxplot(groups, patch_artist=True, showmeans=True,
                       meanprops={"marker": "D", "markerfacecolor": "white", "markeredgecolor": "black", "markersize": 4})
    for patch, model in zip(boxes["boxes"], models):
        patch.set_facecolor(model_color(model)); patch.set_alpha(.68)
    if kind == "participants":
        for position, values in enumerate(groups, start=1):
            offsets = np.linspace(-.09, .09, len(values)) if len(values) > 1 else np.zeros(len(values))
            ax.scatter(position + offsets, values, s=28, marker="o", facecolors="white",
                       edgecolors="#202020", linewidths=.8, zorder=3)
    ax.set_xticks(range(1, len(models)+1), [model_label(model, lang) for model in models], rotation=27, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(labels[kind], fontweight="bold")
    if kind == "errors":
        ax.axhline(0, color="#333333", linewidth=1)
    else:
        ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=.2)
    figure.subplots_adjust(left=.09, right=.98, top=.88, bottom=.30)
    _footer(figure, data, labels, kind)
    return figure


def make_comparison_figure(data: dict[str, Any], kind: str = "mae", lang: str = "ko") -> Figure:
    """Create one comparison Figure using only Matplotlib's object-oriented API."""
    if kind not in {"mae", "rmse", "bias", "tolerance", "predictions", "errors", "participants"}:
        raise ValueError("unsupported comparison figure kind")
    labels = _text(lang)
    if not data.get("available"):
        return _empty(data, labels)
    if kind in {"mae", "rmse", "bias", "tolerance"}:
        return _bar(data, kind, labels, lang)
    if kind == "predictions":
        return _predictions(data, labels, lang)
    return _box(data, kind, labels, lang)


__all__ = ["make_comparison_figure"]
