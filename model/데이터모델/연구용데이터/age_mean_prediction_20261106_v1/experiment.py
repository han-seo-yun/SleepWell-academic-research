"""Prespecified MESA age-prior comparison; never edits app or source data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.multioutput import MultiOutputRegressor
from threadpoolctl import threadpool_limits


ROOT = Path(r"E:\학술제")
HERE = Path(__file__).resolve().parent
APP = ROOT / "앱_소빈/통합 v8"
ASSETS = APP / "app/src/main/assets/models"
PRIOR = ASSETS / "population_prior.json"
SOURCE_RUN = ROOT / "데이터모델/연구용데이터/mesa_work_schedule_20261106_v1"
REPLAY = SOURCE_RUN / "private_replay.parquet"
EPISODES = ROOT / "데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa_episodes.csv"
MESA_SOURCE = ROOT / "데이터셋/MESA/datasets/mesa-sleep-dataset-0.8.0.csv"
WEB = ROOT / "기타/SleepWell-integrated/web-app"
sys.path.insert(0, str(WEB))
from night_core import constrain

SEED = 20261106
ARMS = ("control", "age", "band")
CANDIDATE = "age"
DIAGNOSTIC = "band"
TARGETS = ["bedtime_min", "wake_min"]
SCOPES = {"scientific_54_80": (54, 80), "deployable_60_80": (60, 80)}
EXPECTED_SPLITS = {"train": (1525, 10524), "validation": (299, 2062), "test": (315, 2175)}
EXPECTED_REPLAY_SHA256 = "066bf861a1d7b7dda72bcdf47f5680492baebd589feb226986a3e8d58c83f53e"
EXPECTED_INPUT_HASHES = {
    ROOT / "데이터셋/MESA/datasets/mesa-sleep-harmonized-dataset-0.8.0.csv": "6deb1dfcb24369d347d9901d3d18c508f282ed4e9e2346cecbdfd3db08815beb",
    MESA_SOURCE: "7e2df76c613cae9838919d49feeea85cdd32f697b72cb3415bc0d364c8d33799",
    EPISODES: "06263f30c94c69d4800cf209a819935f79acd596741e160a42e85533240bcee0",
    ASSETS / "robust_mesa_v2.json": "757b939d756c9174eab9304e0d8e872f4ed9eeae34e99a3d921f29284207c620",
    ASSETS / "robust_cmi_v2.json": "add8ba4e23f9c4c9337b821406386664a054580d86c6e53c9b5e6f189bdfa2c7",
    WEB / "fusion_forecast.py": "15591d72dedf55b0d9365bc57e13228f8847fff8902f12570cdd5e657c4fc34b",
    WEB / "night_core.py": "29ee62d4b9e73b7b148e62b90c9fbb21875d0a84101bfc1a60c51450071f6e5c",
    WEB / "robust_features.py": "9395964e91c816b0832650a2a3dde203fff1acedf835af48fcddf5073fb2853e",
}


def sha(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(path: Path, value) -> None:
    def convert(item):
        if isinstance(item, np.generic):
            return item.item()
        if pd.isna(item):
            return None
        raise TypeError(type(item).__name__)

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=convert,
                               allow_nan=False) + "\n", encoding="utf-8")


def feature_order() -> list[str]:
    support = json.loads((SOURCE_RUN / "support_lock.json").read_text(encoding="utf-8"))
    if support["replay_sha256"] != EXPECTED_REPLAY_SHA256:
        raise ValueError("Upstream replay support lock is not the frozen replay")
    return list(support["feature_order"])


FEATURES = feature_order()


def procedure_paths() -> list[Path]:
    return [HERE / "experiment.py", HERE / "test_experiment.py", HERE / "PROTOCOL.md",
            HERE / "verify_replay.py", HERE / "verify_replay.json",
            HERE / "baseline_parity.json"]


def verify_original_inputs() -> None:
    if sha(REPLAY) != EXPECTED_REPLAY_SHA256:
        raise ValueError("Frozen causal replay changed")
    for path, expected in EXPECTED_INPUT_HASHES.items():
        if sha(path) != expected:
            raise ValueError(f"Frozen input changed: {path}")


def verify_preparation_evidence() -> None:
    replay_report = json.loads((HERE / "verify_replay.json").read_text(encoding="utf-8"))
    parity_report = json.loads((HERE / "baseline_parity.json").read_text(encoding="utf-8"))
    if replay_report.get("passed") is not True:
        raise ValueError("Independent replay/age-link verification did not pass")
    if parity_report.get("passed") is not True:
        raise ValueError("Current reference baseline parity did not pass")
    if (parity_report.get("cases_passed") != 9 or
            parity_report.get("numeric_comparisons") != 1008 or
            parity_report.get("maximum_absolute_difference") != 0):
        raise ValueError("Baseline parity evidence is incomplete")


def verify_manifest(out: Path) -> None:
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    for path, expected in manifest["sha256"].items():
        if sha(path) != expected:
            raise ValueError(f"Locked input/procedure changed: {path}")
    verify_original_inputs()
    verify_preparation_evidence()


def verify_split(frame: pd.DataFrame, expected: dict = EXPECTED_SPLITS) -> None:
    actual = {name: (part.subject_id.nunique(), len(part))
              for name, part in frame.groupby("split")}
    if actual != expected:
        raise ValueError(f"Participant split changed: {actual}")
    if frame.groupby("subject_id").split.nunique().max() != 1:
        raise ValueError("A participant crosses frozen splits")
    if frame.duplicated(["subject_id", "cutoff"]).any():
        raise ValueError("Duplicate participant/cutoff rows")


def load_prior() -> dict:
    raw = json.loads(PRIOR.read_text(encoding="utf-8"))
    age_only = {row["age_group"]: float(row["mean_sleep_min"])
                for row in raw["groups"]
                if row["sex"] == "any" and row["activity_band"] == "any"}
    required = {"18-29", "30-44", "45-59", "60-80"}
    if set(age_only) != required:
        raise ValueError("Population prior age-only groups changed")
    return {"version": raw["version"], "source": raw["source"],
            "source_sha256": raw["source_sha256"],
            "global_mean": float(raw["global"]["mean_sleep_min"]),
            "age_only": age_only}


def age_group(age: float) -> str | None:
    if not np.isfinite(age):
        return None
    for low, high, label in ((18, 29, "18-29"), (30, 44, "30-44"),
                             (45, 59, "45-59"), (60, 80, "60-80")):
        if low <= age <= high:
            return label
    return None


def age_context(frame: pd.DataFrame, prior: dict, arm: str) -> np.ndarray:
    ages = pd.to_numeric(frame.age, errors="coerce").to_numpy(float, na_value=np.nan)
    if arm == "control":
        return np.full(len(frame), prior["global_mean"] / 60.0)
    groups = [age_group(value) for value in ages]
    if arm == "age":
        return np.asarray([prior["age_only"].get(group, prior["global_mean"]) / 60.0
                           for group in groups], dtype=float)
    if arm == "band":
        values = {**prior["age_only"], "__global__": prior["global_mean"]}
        ordered = {label: float(rank + 1)
                   for rank, (label, _) in enumerate(sorted(values.items(), key=lambda item: item[1]))}
        return np.asarray([ordered.get(group, ordered["__global__"]) for group in groups], dtype=float)
    raise ValueError(f"Unknown arm: {arm}")


def fit_transform_spec(x: np.ndarray) -> dict:
    median = np.nanmedian(x, axis=0)
    if not np.isfinite(median).all():
        raise ValueError("Train contains an entirely missing base feature")
    imputed = np.where(np.isnan(x), median, x)
    return {"median": median, "low": np.quantile(imputed, .01, axis=0),
            "high": np.quantile(imputed, .99, axis=0)}


def matrix(x: np.ndarray, frame: pd.DataFrame, spec: dict, prior: dict,
           arm: str) -> np.ndarray:
    base = np.clip(np.where(np.isnan(x), spec["median"], x), spec["low"], spec["high"])
    return np.column_stack([base, age_context(frame, prior, arm)])


def eligibility(frame: pd.DataFrame, scope: str) -> np.ndarray:
    low, high = SCOPES[scope]
    return pd.to_numeric(frame.age, errors="coerce").between(low, high).fillna(False).to_numpy(bool)


def project(prediction: np.ndarray) -> np.ndarray:
    return np.asarray([constrain(*row) for row in prediction], dtype=float)


def baseline(frame: pd.DataFrame, cohort: str) -> np.ndarray:
    return frame[[f"baseline_{cohort}_bed", f"baseline_{cohort}_wake"]].to_numpy(float)


def predictions(frame: pd.DataFrame, models: dict, spec: dict, prior: dict,
                scope: str) -> dict[str, np.ndarray]:
    old_mesa, cmi = baseline(frame, "mesa"), baseline(frame, "cmi")
    current = (old_mesa + cmi) / 2.0
    mask = eligibility(frame, scope)
    x = frame[[f"f__{name}" for name in FEATURES]].to_numpy(float)
    result = {"current_v8": current}
    for arm, model in models.items():
        new_mesa = project(model.predict(matrix(x, frame, spec, prior, arm)))
        new_mesa[~mask] = old_mesa[~mask]
        result[arm] = (new_mesa + cmi) / 2.0
        if not np.array_equal(result[arm][~mask], current[~mask]):
            raise AssertionError("Out-of-scope fallback is not bit-exact")
    return result


def participant_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("subject_id").subject_id.transform("size").to_numpy(float)
    weights = 1.0 / counts
    return weights / weights.mean()


def subject_losses(frame: pd.DataFrame, prediction: np.ndarray) -> pd.DataFrame:
    error = prediction - frame[TARGETS].to_numpy(float)
    losses = pd.DataFrame({"subject_id": frame.subject_id.to_numpy(),
                           "onset": np.abs(error[:, 0]),
                           "wake": np.abs(error[:, 1]),
                           "joint": np.abs(error).mean(axis=1),
                           "interval": np.abs(error[:, 1] - error[:, 0])})
    return losses.groupby("subject_id", sort=True).mean()


def summary(frame: pd.DataFrame, prediction: np.ndarray) -> dict:
    losses = subject_losses(frame, prediction)
    return {"people": len(losses), "nights": len(frame), **losses.mean().to_dict()}


def paired(frame: pd.DataFrame, comparator: np.ndarray, candidate: np.ndarray) -> dict:
    differences = subject_losses(frame, comparator) - subject_losses(frame, candidate)
    joint = differences.joint.to_numpy()
    rng = np.random.default_rng(SEED)
    draws = joint[rng.integers(0, len(joint), size=(5000, len(joint)))].mean(axis=1)
    return {"people": len(joint), "improvement_min": float(joint.mean()),
            "ci95": np.quantile(draws, [.025, .975]).tolist(),
            "endpoint_improvement_min": differences.mean().to_dict(),
            "method": "paired participant bootstrap; positive=comparator-candidate; 5000 replicates"}


def prepare(out: Path) -> None:
    if (out / "input_manifest.json").exists():
        raise ValueError("Preparation already exists; use a new output directory")
    out.mkdir(parents=True, exist_ok=True)
    verify_original_inputs()
    verify_preparation_evidence()
    for path in procedure_paths():
        if not path.is_file():
            raise FileNotFoundError(path)
    frame = pd.read_parquet(REPLAY, columns=["subject_id", "split", "cutoff", "age"])
    verify_split(frame)
    prior = load_prior()
    people = frame.drop_duplicates("subject_id")
    counts = {split: {scope: int(eligibility(part, scope).sum())
                      for scope in SCOPES}
              for split, part in people.groupby("split")}
    paths = [REPLAY, PRIOR, SOURCE_RUN / "experiment.py", SOURCE_RUN / "support_lock.json",
             *EXPECTED_INPUT_HASHES.keys(), *procedure_paths()]
    dump(out / "input_manifest.json", {"prepared_utc": stamp(), "seed": SEED,
         "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
         "sklearn": sklearn.__version__, "sha256": {str(path): sha(path) for path in paths}})
    dump(out / "support_lock.json", {"locked_utc": stamp(), "replay_sha256": sha(REPLAY),
         "participant_splits": {key: {"people": value[0], "nights": value[1]}
                                for key, value in EXPECTED_SPLITS.items()},
         "age_range": [float(people.age.min()), float(people.age.max())],
         "age_observed_people": int(people.age.notna().sum()), "scope_people": counts,
         "scopes": SCOPES, "feature_order": FEATURES, "arms": ARMS,
         "candidate": CANDIDATE, "diagnostic_not_selectable": DIAGNOSTIC,
         "population_prior": prior,
         "outcomes_not_read_during_support_lock": True})
    verify_manifest(out)
    print("Input and label-free support lock complete", flush=True)


def seal(out: Path) -> None:
    verify_manifest(out)
    path = out / "prefit_seal.json"
    if path.exists():
        raise ValueError("Prefit seal already exists")
    names = ["input_manifest.json", "support_lock.json"]
    dump(path, {"sealed_utc": stamp(), "sha256": {name: sha(out / name) for name in names},
                "procedure_sha256": {str(p): sha(p) for p in procedure_paths()},
                "test_outcomes_seen": False})
    print("Prefit procedure seal complete", flush=True)


def verify_prefit(out: Path) -> None:
    verify_manifest(out)
    seal_data = json.loads((out / "prefit_seal.json").read_text(encoding="utf-8"))
    for name, expected in seal_data["sha256"].items():
        if sha(out / name) != expected:
            raise ValueError(f"Prefit artifact changed: {name}")
    for path, expected in seal_data["procedure_sha256"].items():
        if sha(path) != expected:
            raise ValueError(f"Procedure changed after seal: {path}")


def new_estimator() -> MultiOutputRegressor:
    return MultiOutputRegressor(HistGradientBoostingRegressor(
        loss="absolute_error", max_leaf_nodes=7, min_samples_leaf=20,
        learning_rate=.05, max_iter=120, l2_regularization=10.0,
        early_stopping=False, random_state=SEED))


def fit(out: Path) -> None:
    verify_prefit(out)
    if (out / "validation_lock.json").exists():
        raise ValueError("Validation/model lock already exists")
    needed = ["subject_id", "split", "cutoff", "age", "history_count", *TARGETS,
              "baseline_mesa_bed", "baseline_mesa_wake",
              "baseline_cmi_bed", "baseline_cmi_wake",
              *[f"f__{name}" for name in FEATURES]]
    replay = pd.read_parquet(REPLAY, columns=needed,
                             filters=[("split", "in", ["train", "validation"])])
    verify_split(replay, {key: EXPECTED_SPLITS[key] for key in ("train", "validation")})
    train = replay.loc[replay.split.eq("train")].copy()
    validation = replay.loc[replay.split.eq("validation")].copy()
    x_train = train[[f"f__{name}" for name in FEATURES]].to_numpy(float)
    spec, prior = fit_transform_spec(x_train), load_prior()
    y_train = train[TARGETS].to_numpy(float)
    weights = participant_weights(train)
    models = {}
    with threadpool_limits(limits=2):
        for arm in ARMS:
            model = new_estimator()
            model.fit(matrix(x_train, train, spec, prior, arm), y_train,
                      sample_weight=weights)
            models[arm] = model
            print(f"Fitted prespecified {arm} arm on Train only", flush=True)
    bundle_path = out / "research_models.joblib"
    joblib.dump({"models": models, "spec": spec, "prior": prior}, bundle_path)
    metrics = {}
    comparisons = {}
    with threadpool_limits(limits=2):
        for scope in SCOPES:
            prediction = predictions(validation, models, spec, prior, scope)
            mask = eligibility(validation, scope)
            metrics[scope] = {
                "all_with_exact_fallback": {arm: summary(validation, values)
                                            for arm, values in prediction.items()},
                "eligible_only": {arm: summary(validation.loc[mask], values[mask])
                                  for arm, values in prediction.items()},
            }
            comparisons[scope] = {
                view: {comparator: paired(validation if view == "all_with_exact_fallback" else validation.loc[mask],
                                          prediction[comparator] if view == "all_with_exact_fallback" else prediction[comparator][mask],
                                          prediction[CANDIDATE] if view == "all_with_exact_fallback" else prediction[CANDIDATE][mask])
                       for comparator in ("current_v8", "control")}
                for view in ("all_with_exact_fallback", "eligible_only")}
    dump(out / "validation_lock.json", {"locked_before_test_utc": stamp(),
         "candidate_fixed_before_results": CANDIDATE,
         "diagnostic_excluded_from_selection": DIAGNOSTIC,
         "validation_metrics": metrics, "validation_comparisons": comparisons,
         "model_sha256": sha(bundle_path), "prefit_seal_sha256": sha(out / "prefit_seal.json"),
         "automatic_app_promotion": False})
    (out / "validation_lock.sha256").write_text(sha(out / "validation_lock.json"), encoding="ascii")
    verify_prefit(out)
    print("Models and Validation results locked before Test", flush=True)


def stratum_masks(frame: pd.DataFrame, scope: str) -> dict[str, np.ndarray]:
    eligible = eligibility(frame, scope)
    return {"eligible": eligible,
            "eligible_history_0": eligible & frame.history_count.eq(0).to_numpy(),
            "eligible_history_1_2": eligible & frame.history_count.between(1, 2).to_numpy(),
            "eligible_history_3_plus": eligible & frame.history_count.ge(3).to_numpy(),
            "eligible_age_54_59": eligible & frame.age.between(54, 59).to_numpy(),
            "eligible_age_60_80": eligible & frame.age.between(60, 80).to_numpy()}


def evaluate_split(frame: pd.DataFrame, bundle: dict, scope: str) -> tuple[dict, pd.DataFrame]:
    prediction = predictions(frame, bundle["models"], bundle["spec"], bundle["prior"], scope)
    eligible = eligibility(frame, scope)
    views = {"all_with_exact_fallback": np.ones(len(frame), dtype=bool), "eligible_only": eligible}
    result = {"views": {}, "strata": {}}
    for name, mask in views.items():
        selected = frame.loc[mask]
        result["views"][name] = {
            "metrics": {arm: summary(selected, values[mask]) for arm, values in prediction.items()},
            "age_vs": {comparator: paired(selected, prediction[comparator][mask], prediction[CANDIDATE][mask])
                       for comparator in ("current_v8", "control")}}
    for name, mask in stratum_masks(frame, scope).items():
        if not mask.any():
            continue
        selected = frame.loc[mask]
        result["strata"][name] = {
            "metrics": {arm: summary(selected, values[mask]) for arm, values in prediction.items()},
            "age_vs": {comparator: paired(selected, prediction[comparator][mask], prediction[CANDIDATE][mask])
                       for comparator in ("current_v8", "control")}}
    saved = frame[["subject_id", "split", "cutoff", "age", "history_count", *TARGETS]].copy()
    saved["scope"] = scope
    saved["eligible"] = eligible
    for arm, values in prediction.items():
        saved[f"{arm}_bed"], saved[f"{arm}_wake"] = values[:, 0], values[:, 1]
    return result, saved


def gate(result: dict) -> dict:
    checks = {}
    deployable = result["scopes"]["deployable_60_80"]
    for split in ("validation", "test"):
        split_result = deployable[split]
        for view in ("all_with_exact_fallback", "eligible_only"):
            metrics = split_result["views"][view]["metrics"]
            comparisons = split_result["views"][view]["age_vs"]
            checks[f"{split}_{view}_gain_vs_current_at_least_1min"] = (
                metrics["current_v8"]["joint"] - metrics[CANDIDATE]["joint"] >= 1.0)
            checks[f"{split}_{view}_not_worse_than_control"] = (
                metrics[CANDIDATE]["joint"] <= metrics["control"]["joint"])
            if split == "test":
                for comparator in ("current_v8", "control"):
                    checks[f"test_{view}_ci_above_zero_vs_{comparator}"] = (
                        comparisons[comparator]["ci95"][0] > 0.0)
            for comparator in ("current_v8", "control"):
                for endpoint in ("onset", "wake"):
                    checks[f"{split}_{view}_{endpoint}_harm_at_most_1min_vs_{comparator}"] = (
                        metrics[CANDIDATE][endpoint] - metrics[comparator][endpoint] <= 1.0)
        for label, details in split_result["strata"].items():
            if details["metrics"][CANDIDATE]["people"] < 30:
                continue
            for comparator in ("current_v8", "control"):
                checks[f"{split}_{label}_harm_at_most_1min_vs_{comparator}"] = (
                    details["metrics"][CANDIDATE]["joint"] -
                    details["metrics"][comparator]["joint"] <= 1.0)
    passed = all(checks.values())
    return {"engineering_threshold_min": 1.0, "checks": checks,
            "performance_pass": passed,
            "decision": "eligible_for_app_integration_review" if passed else "do_not_integrate",
            "failed": [name for name, value in checks.items() if not value],
            "clinical_or_recommendation_effect_claim": False}


def evaluate(out: Path) -> None:
    verify_prefit(out)
    if (out / "evaluation.json").exists():
        raise ValueError("Test evaluation already exists; never overwrite it")
    if (out / "test_evaluation_started.json").exists():
        raise ValueError("Test evaluation was already started; do not rerun it")
    lock_path = out / "validation_lock.json"
    if sha(lock_path) != (out / "validation_lock.sha256").read_text(encoding="ascii"):
        raise ValueError("Validation lock changed")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if sha(out / "prefit_seal.json") != lock["prefit_seal_sha256"]:
        raise ValueError("Prefit seal changed after Validation")
    if sha(out / "research_models.joblib") != lock["model_sha256"]:
        raise ValueError("Locked model changed")
    bundle = joblib.load(out / "research_models.joblib")
    dump(out / "test_evaluation_started.json", {"started_utc": stamp(),
         "validation_lock_sha256": sha(lock_path), "model_sha256": sha(out / "research_models.joblib"),
         "single_test_evaluation": True})
    replay = pd.read_parquet(REPLAY)
    verify_split(replay)
    result = {"evaluated_utc": stamp(), "candidate": CANDIDATE,
              "diagnostic_arm": DIAGNOSTIC,
              "endpoint": "next main-sleep ACT-derived onset/final-wake clock-minute MAE",
              "recommendation_effect_evaluated": False, "scopes": {},
              "limitations": ["MESA ages 54-90 only; no young-adult validation",
                  "NHANES prior is self-reported usual weekday interval, not optimal sleep need",
                  "historical Test split has been used in earlier project analyses",
                  "retrospective age availability is assumed at enrollment",
                  "prediction error does not establish benefit of following a recommendation"]}
    saved = []
    for scope in SCOPES:
        result["scopes"][scope] = {}
        for split in ("validation", "test"):
            frame = replay.loc[replay.split.eq(split)].copy()
            with threadpool_limits(limits=2):
                details, rows = evaluate_split(frame, bundle, scope)
            if split == "validation":
                for view in ("all_with_exact_fallback", "eligible_only"):
                    if details["views"][view]["metrics"] != lock["validation_metrics"][scope][view]:
                        raise ValueError("Recomputed Validation metrics differ from the pre-Test lock")
                    if details["views"][view]["age_vs"] != lock["validation_comparisons"][scope][view]:
                        raise ValueError("Recomputed Validation comparison differs from the pre-Test lock")
            result["scopes"][scope][split] = details
            saved.append(rows)
    result["gate"] = gate(result)
    pd.concat(saved, ignore_index=True).to_parquet(out / "private_predictions.parquet", index=False)
    verify_prefit(out)
    result["input_integrity_verified"] = True
    dump(out / "evaluation.json", result)
    dump(out / "output_manifest.json", {"created_utc": stamp(), "sha256": {
        path.name: sha(path) for path in out.iterdir()
        if path.is_file() and path.name != "output_manifest.json"}})
    print(json.dumps({"gate": result["gate"], "recommendation_effect_evaluated": False},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "seal", "fit", "evaluate"])
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    globals()[arguments.phase](arguments.output)
