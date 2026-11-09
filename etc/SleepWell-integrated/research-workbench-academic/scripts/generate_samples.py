"""Generate deterministic, synthetic demonstration datasets.

The files contain no real participants or sensitive data. They deliberately include
numeric/categorical columns, groups, dates and a small amount of missingness.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples"


def _common(rng: np.random.Generator, n: int) -> pd.DataFrame:
    group = np.repeat(np.arange((n + 2) // 3), 3)[:n]
    age = np.clip(rng.normal(48, 13, n), 18, 85).round(0)
    biomarker = rng.normal(0, 1, n)
    treatment = rng.choice(["control", "A", "B"], n, p=[0.4, 0.3, 0.3])
    site = rng.choice(["Seoul", "Busan", "Daejeon"], n)
    frame = pd.DataFrame(
        {
            "participant_id": [f"P{i:04d}" for i in range(n)],
            "group_id": [f"G{x:03d}" for x in group],
            "visit_date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "age": age,
            "biomarker": biomarker.round(4),
            "treatment": treatment,
            "site": site,
        }
    )
    frame.loc[rng.choice(n, max(1, n // 20), replace=False), "biomarker"] = np.nan
    frame.loc[rng.choice(n, max(1, n // 25), replace=False), "site"] = None
    return frame


def generate() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(20261101)
    binary = _common(rng, 180)
    logit = (
        -1.1
        + 0.025 * (binary["age"] - 45)
        + 1.0 * binary["biomarker"].fillna(0)
        + 0.65 * (binary["treatment"] == "B")
    )
    probability = 1 / (1 + np.exp(-logit))
    binary["response"] = np.where(rng.random(len(binary)) < probability, "yes", "no")
    binary.to_csv(OUT / "classification_mixed.csv", index=False, encoding="utf-8-sig")

    regression = _common(rng, 180)
    treatment_effect = regression["treatment"].map({"control": 0.0, "A": 2.0, "B": 4.0})
    regression["outcome_score"] = (
        18
        + 0.18 * regression["age"]
        + 3.2 * regression["biomarker"].fillna(0)
        + treatment_effect
        + rng.normal(0, 2.2, len(regression))
    ).round(3)
    regression.to_excel(OUT / "regression_mixed.xlsx", index=False, engine="openpyxl")

    dual = regression.copy()
    dual["clinical_response"] = np.where(dual["outcome_score"] >= 30, "responder", "non-responder")
    dual.to_csv(OUT / "dual_tasks.csv", index=False, encoding="utf-8-sig")

    multi = _common(rng, 210)
    score = multi["biomarker"].fillna(0) + 0.25 * (multi["treatment"] == "B") + rng.normal(0, 0.7, len(multi))
    multi["risk_group"] = pd.cut(score, [-np.inf, -0.45, 0.55, np.inf], labels=["low", "medium", "high"]).astype(str)
    multi.to_csv(OUT / "multiclass_mixed.csv", index=False, encoding="utf-8-sig")

    print(f"Generated 4 sample datasets in {OUT}")


if __name__ == "__main__":
    generate()
