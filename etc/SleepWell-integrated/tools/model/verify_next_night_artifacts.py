#!/usr/bin/env python3
"""Independently verify saved next-night artifacts and portable runtime parity.

The verifier reads an already completed run. It never rewrites predictions,
models, features, or experiment summaries; its only output is verification.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-app"
sys.path.insert(0, str(WEB))

from night_core import DATA_FEATURES, PHYSICAL_FEATURES, constrain, local_time  # noqa: E402
from night_forecast import NightModel  # noqa: E402
from personalization import load_population_prior  # noqa: E402


TOLERANCE_MIN = 1e-7
COHORTS = ("cmi", "mesa", "samsung")


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def timestamps_equal(left, right) -> bool:
    a = pd.to_datetime(left, utc=True, errors="coerce")
    b = pd.to_datetime(right, utc=True, errors="coerce")
    return bool(pd.notna(a).all() and pd.notna(b).all() and np.array_equal(a.to_numpy(), b.to_numpy()))


def maximum_error(actual, expected) -> float:
    a = np.asarray(actual, dtype=float)
    b = np.asarray(expected, dtype=float)
    require(a.shape == b.shape, "prediction array shape mismatch")
    require(np.isfinite(a).all() and np.isfinite(b).all(), "non-finite prediction value")
    return float(np.max(np.abs(a - b))) if a.size else 0.0


def portable_prediction(model: NightModel, arm: str, features: pd.DataFrame) -> np.ndarray:
    payload = model.raw["models"][arm]
    names = payload["feature_order"]
    values = features.loc[:, names].to_numpy(float)
    mean = np.asarray(payload["mean"], dtype=float)
    scale = np.asarray(payload["scale"], dtype=float)
    coefficients = np.asarray(payload["coefficients"], dtype=float)
    intercepts = np.asarray(payload["intercepts"], dtype=float)
    raw = (values - mean) / scale @ coefficients.T + intercepts
    return np.asarray([constrain(bed, wake) for bed, wake in raw], dtype=float)


def physical_prediction(features: pd.DataFrame) -> np.ndarray:
    values = features.loc[:, ["physical_bed", "physical_wake"]].to_numpy(float)
    return np.asarray([constrain(bed, wake) for bed, wake in values], dtype=float)


def align_saved_frames(cohort_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    features = pd.read_parquet(cohort_dir / "features.parquet")
    episodes = pd.read_parquet(cohort_dir / "episodes.parquet")
    require(len(features) == len(episodes), "features and episodes row counts differ")
    require(features["row_id"].is_unique and episodes["row_id"].is_unique, "row_id is not unique")
    require(np.array_equal(features["row_id"].to_numpy(), episodes["row_id"].to_numpy()),
            "features and episodes row order differs")
    require(features["subject_id"].astype(str).equals(episodes["subject_id"].astype(str)),
            "features and episodes subject alignment differs")
    require(timestamps_equal(features["cutoff"], episodes["cutoff"]),
            "features and episodes cutoff alignment differs")
    test_features = features.loc[features["split"].eq("test")].reset_index(drop=True)
    test_episodes = episodes.loc[episodes["split"].eq("test")].reset_index(drop=True)
    require(len(test_features) > 0, "cohort has no Test rows")
    return features, episodes, test_features.merge(
        test_episodes.drop(columns=[c for c in ("subject_id", "split", "cutoff") if c in test_episodes]),
        on="row_id", how="inner", validate="one_to_one", suffixes=("", "_episode"), sort=False,
    )


def compare_saved_predictions(cohort_dir: Path, model: NightModel, test_frame: pd.DataFrame) -> dict:
    errors = {}
    for arm in ("data", "hybrid", "physical"):
        saved = pd.read_csv(cohort_dir / f"test_{arm}.csv")
        require(len(saved) == len(test_frame), f"{arm} Test row count differs")
        require(saved["subject_id"].astype(str).equals(test_frame["subject_id"].astype(str)),
                f"{arm} Test subject order differs")
        require(timestamps_equal(saved["cutoff"], test_frame["cutoff"]), f"{arm} Test cutoff order differs")
        require(maximum_error(saved[["bed_offset", "wake_offset"]],
                              test_frame[["bed_offset", "wake_offset"]]) <= TOLERANCE_MIN,
                f"{arm} Test target alignment differs")
        predicted = physical_prediction(test_frame) if arm == "physical" else portable_prediction(model, arm, test_frame)
        error = maximum_error(predicted, saved[["predicted_bed_offset", "predicted_wake_offset"]])
        require(error <= TOLERANCE_MIN, f"{arm} portable predictions differ")
        errors[arm] = error
    return errors


def representative_positions(cohort_dir: Path, count: int) -> tuple[list[int], bool]:
    detail = pd.read_csv(cohort_dir / "test_data.csv")
    require(len(detail) == count, "representative source row count differs")
    positions = [0, count - 1]
    after = np.flatnonzero(pd.to_numeric(detail["history_days"], errors="coerce").to_numpy() >= 14)
    has_after_14 = len(after) > 0
    if has_after_14:
        positions.append(int(after[0]))
    return list(dict.fromkeys(positions)), has_after_14


def build_prior_history(episodes: pd.DataFrame, target: pd.Series, cohort: str) -> tuple[list[dict], str]:
    cutoff = local_time(str(target["cutoff"]))
    enrolled_at = str(target["enrolled_at"])
    enrolled = local_time(enrolled_at)
    same_person = episodes["subject_id"].astype(str).eq(str(target["subject_id"]))
    prior = episodes.loc[same_person].copy()
    prior_cutoffs = pd.to_datetime(prior["cutoff"], utc=True)
    mask = (prior_cutoffs >= pd.Timestamp(enrolled).tz_convert("UTC")) & (prior_cutoffs < pd.Timestamp(cutoff).tz_convert("UTC"))
    if cohort == "samsung":
        mask &= prior["split"].eq(target["split"])
    prior = prior.loc[mask].sort_values("cutoff")
    records = []
    for _, row in prior.iterrows():
        wake = local_time(str(row["wake"]))
        observed = local_time(str(row["observed_at"]))
        if not (wake < cutoff and observed < cutoff):
            continue
        records.append({"bed": str(row["bed"]), "wake": str(row["wake"]),
                        "observed_at": str(row["observed_at"]), "source": "research"})
    return records, enrolled_at


def runtime_parity(cohort_dir: Path, cohort: str, model: NightModel,
                   episodes: pd.DataFrame, test_frame: pd.DataFrame) -> dict:
    positions, has_after_14 = representative_positions(cohort_dir, len(test_frame))
    maximum_feature_error = 0.0
    maximum_prediction_error = {arm: 0.0 for arm in ("physical", "data", "hybrid")}
    runtime_personalized = 0
    for position in positions:
        target = test_frame.iloc[position]
        records, enrolled_at = build_prior_history(episodes, target, cohort)
        cutoff = local_time(str(target["cutoff"]))
        result = model.predict({"created_at": enrolled_at, "link_history": True}, records, cutoff,
                               research_context=True)
        require(result.get("available") is True, "runtime NightModel refused representative Test row")
        snapshot = result["feature_snapshot"]
        for name in DATA_FEATURES + PHYSICAL_FEATURES:
            error = abs(float(snapshot[name]) - float(target[name]))
            maximum_feature_error = max(maximum_feature_error, error)
        require(maximum_feature_error <= TOLERANCE_MIN, "runtime feature snapshot differs from saved features")
        runtime_personalized += int(result["personalization"]["personal_weight"] > 0)
        for arm in ("physical", "data", "hybrid"):
            predicted = result["predictions"][arm]
            actual_offsets = np.array([
                (local_time(predicted["bed"]) - cutoff).total_seconds() / 60,
                (local_time(predicted["wake"]) - cutoff).total_seconds() / 60,
            ])
            expected = portable_prediction(model, arm, test_frame.iloc[[position]])[0] if arm != "physical" else physical_prediction(test_frame.iloc[[position]])[0]
            error = maximum_error(actual_offsets, expected)
            maximum_prediction_error[arm] = max(maximum_prediction_error[arm], error)
            require(error <= TOLERANCE_MIN, f"runtime {arm} prediction differs from saved-feature prediction")
    return {
        "representative_rows": len(positions),
        "after_14_available": has_after_14,
        "personalized_representatives": runtime_personalized,
        "maximum_feature_error": maximum_feature_error,
        "maximum_prediction_error_min": maximum_prediction_error,
    }


def verify_cohort(run: Path, cohort: str) -> tuple[dict, pd.DataFrame]:
    directory = run / cohort
    required = ["features.parquet", "episodes.parquet", "test_data.csv", "test_hybrid.csv",
                "test_physical.csv", "night_model.json", "validation_selection.json"]
    require(all((directory / name).is_file() for name in required), f"{cohort} artifact set is incomplete")
    model = NightModel(directory / "night_model.json")
    require(model.raw["cohort"] == cohort, f"{cohort} model cohort differs")
    selection = json.loads((directory / "validation_selection.json").read_text(encoding="utf-8"))
    require(model.raw["selected"] == selection["selected"], f"{cohort} selected models differ")
    require(model.raw["validation_winner"] == selection["winner"], f"{cohort} validation winner differs")
    _, episodes, test_frame = align_saved_frames(directory)
    prediction_errors = compare_saved_predictions(directory, model, test_frame)
    runtime = runtime_parity(directory, cohort, model, episodes, test_frame)
    return {
        "artifact_loaded": True,
        "rows": int(len(episodes)),
        "test_rows": int(len(test_frame)),
        "people": int(episodes["subject_id"].astype(str).nunique()),
        "portable_maximum_error_min": prediction_errors,
        "runtime": runtime,
    }, episodes


def verify_split_boundaries(frames: dict[str, pd.DataFrame]) -> dict:
    for cohort in ("cmi", "mesa"):
        per_person = frames[cohort].groupby(frames[cohort]["subject_id"].astype(str))["split"].nunique()
        require(bool((per_person <= 1).all()), f"{cohort} participant split overlap")
    cmi_people = set(frames["cmi"]["subject_id"].astype(str))
    mesa_people = set(frames["mesa"]["subject_id"].astype(str))
    require(not cmi_people.intersection(mesa_people), "CMI and MESA participant identifiers overlap")
    samsung = frames["samsung"].copy()
    cutoff = pd.to_datetime(samsung["cutoff"], utc=True)
    bounds = {split: (cutoff[samsung["split"].eq(split)].min(), cutoff[samsung["split"].eq(split)].max())
              for split in ("train", "validation", "test")}
    require(all(pd.notna(value) for pair in bounds.values() for value in pair), "Samsung split is empty")
    require(bounds["train"][1] < bounds["validation"][0] and bounds["validation"][1] < bounds["test"][0],
            "Samsung chronological split overlap")
    return {"cmi_subject_disjoint": True, "mesa_subject_disjoint": True,
            "cross_cohort_identifier_disjoint": True, "samsung_chronological": True}


def verify(run: Path) -> dict:
    require(run.is_dir(), "run directory does not exist")
    cohorts, frames = {}, {}
    for cohort in COHORTS:
        cohorts[cohort], frames[cohort] = verify_cohort(run, cohort)
    split_checks = verify_split_boundaries(frames)
    population = load_population_prior(run / "population_prior.json")
    require(population.get("available") is True, "population prior failed production loader validation")
    artifact = population["artifact"]
    return {
        "schema_version": 1,
        "verified": True,
        "tolerance_minutes": TOLERANCE_MIN,
        "cohorts": cohorts,
        "split_checks": split_checks,
        "population_prior": {"available": True, "version": artifact["version"],
                             "groups": len(artifact["groups"]), "global_n_people": artifact["global"]["n_people"]},
        "privacy": "aggregate verification only; participant identifiers omitted",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="completed next-night experiment directory")
    args = parser.parse_args()
    run = args.run.resolve()
    output = run / "verification.json"
    try:
        report = verify(run)
        dump(output, report)
        print(json.dumps({"verified": True, "cohorts": {key: value["test_rows"] for key, value in report["cohorts"].items()},
                          "population_prior_available": True}, ensure_ascii=False))
    except Exception as error:
        failure = {"schema_version": 1, "verified": False, "error_type": type(error).__name__,
                   "error": str(error), "privacy": "participant identifiers omitted"}
        if run.is_dir():
            dump(output, failure)
        print(json.dumps({"verified": False, "error_type": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
