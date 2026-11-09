from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import platform
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
from catboost import CatBoostRegressor
from sklearn.model_selection import GroupKFold, TimeSeriesSplit
from sklearn.multioutput import MultiOutputRegressor
from threadpoolctl import threadpool_limits


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = SCRIPT_DIR.parents[2]


def _add_import_paths(workspace: Path) -> tuple[Path, Path]:
    tools = workspace / "기타" / "SleepWell-integrated" / "tools" / "model"
    web = workspace / "기타" / "SleepWell-integrated" / "web-app"
    # Both paths are explicit because the research helpers import web runtime code by module name.
    for path in (web, tools):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    return tools, web


DEFAULT_TOOLS, DEFAULT_WEB = _add_import_paths(DEFAULT_WORKSPACE)

import next_night_experiments as next_helpers  # noqa: E402
import night_features_v2 as feature_helpers  # noqa: E402
import robust_night_experiments as robust_helpers  # noqa: E402
from gate_model import (  # noqa: E402
    CONTEXT_COLUMNS,
    GATE_GRID,
    ConditionalGate,
    best_fixed_weight,
    make_context,
)
from night_core import PHYSICAL_FEATURES  # noqa: E402


SEED = 20261026
COHORTS = ("cmi", "mesa", "samsung")
SPLITS = ("train", "validation", "test")
TARGETS = tuple(next_helpers.TARGETS)
DATA_FEATURES = tuple(feature_helpers.DATA_V2)
HYBRID_FEATURES = tuple(feature_helpers.HYBRID_V2)
PHYSICAL_ENDPOINTS = ("physical_bed", "physical_wake")

BASELINE_ARMS = (
    "baseline_recent7",
    "baseline_recent14",
    "baseline_ew14",
    "baseline_weektype",
    "baseline_population_median",
    "physical",
)
FAMILIES = ("hgb", "catboost")
FAMILY_SUFFIXES = ("data", "hybrid", "fixed_blend", "gate_depth1", "gate_depth2")
FAMILY_ARMS = tuple(f"{family}_{suffix}" for family in FAMILIES for suffix in FAMILY_SUFFIXES)
FROZEN_ARMS = ("frozen_v2_data", "frozen_v2_hybrid")
ALL_ARMS = BASELINE_ARMS + FAMILY_ARMS + FROZEN_ARMS
GATE_ARMS = tuple(f"{family}_gate_depth{depth}" for family in FAMILIES for depth in (1, 2))
NONCONDITIONAL_ARMS = tuple(arm for arm in ALL_ARMS if arm not in GATE_ARMS)

ARM_COMPLEXITY = {
    "baseline_population_median": 0,
    "baseline_recent7": 1,
    "baseline_recent14": 1,
    "baseline_ew14": 1,
    "baseline_weektype": 1,
    "physical": 1,
    "frozen_v2_data": 2,
    "hgb_data": 2,
    "catboost_data": 2,
    "frozen_v2_hybrid": 3,
    "hgb_hybrid": 3,
    "catboost_hybrid": 3,
    "hgb_fixed_blend": 3,
    "catboost_fixed_blend": 3,
    "hgb_gate_depth1": 4,
    "catboost_gate_depth1": 4,
    "hgb_gate_depth2": 5,
    "catboost_gate_depth2": 5,
}

EXPECTED_COUNTS = {
    "cmi": {
        "rows": {"train": 3263, "validation": 691, "test": 673},
        "people": {"train": 186, "validation": 39, "test": 44},
    },
    "mesa": {
        "rows": {"train": 10524, "validation": 2062, "test": 2175},
        "people": {"train": 1525, "validation": 299, "test": 315},
    },
    "samsung": {
        "rows": {"train": 137, "validation": 29, "test": 30},
        "people": {"train": 1, "validation": 1, "test": 1},
    },
}
EXPECTED_HISTORY = {
    "cmi": {"max_valid_days": 34, "max_recent_count14": 14.0, "personal_weight_positive_rows": 1423},
    "mesa": {"max_valid_days": 12, "max_recent_count14": 12.0, "personal_weight_positive_rows": 0},
    "samsung": {"max_valid_days": 136, "max_recent_count14": 14.0, "personal_weight_positive_rows": 154},
}
REQUIRED_EPISODE_COLUMNS = {
    "cohort",
    "subject_id",
    "split",
    "cutoff",
    "bed",
    "wake",
    "observed_at",
    "row_id",
    "bed_offset",
    "wake_offset",
}


class ProtocolError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_values(values: Sequence[Any]) -> str:
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ProtocolError(f"Expected a JSON object: {path}")
    return value


def dump_json_new(path: Path, value: Any) -> None:
    path = Path(path)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def write_csv_new(path: Path, frame: pd.DataFrame) -> None:
    with Path(path).open("x", encoding="utf-8", newline="") as handle:
        frame.to_csv(handle, index=False)


def save_joblib_new(path: Path, value: Any) -> None:
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    joblib.dump(value, path)


def create_output_directory(path: Path) -> Path:
    output = Path(path).expanduser()
    output.mkdir(parents=True, exist_ok=False)
    return output.resolve()


def layout_paths(workspace: Path) -> dict[str, Path]:
    workspace = Path(workspace).expanduser().resolve()
    research = workspace / "데이터모델" / "연구용데이터"
    return {
        "workspace": workspace,
        "v1": research / "next_night_run_20261020_v1" / "analysis",
        "v2": research / "robust_next_night_run_20261020_v2" / "analysis",
        "tools": workspace / "기타" / "SleepWell-integrated" / "tools" / "model",
        "web": workspace / "기타" / "SleepWell-integrated" / "web-app",
    }


def validate_import_binding(layout: Mapping[str, Path]) -> None:
    expected = {
        robust_helpers: layout["tools"] / "robust_night_experiments.py",
        next_helpers: layout["tools"] / "next_night_experiments.py",
        feature_helpers: layout["tools"] / "night_features_v2.py",
    }
    for module, source in expected.items():
        actual = Path(inspect.getsourcefile(module) or "").resolve()
        if actual != source.resolve():
            raise ProtocolError(f"Imported {module.__name__} from {actual}, not workspace source {source}")


def locked_input_files(layout: Mapping[str, Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for cohort in COHORTS:
        result[f"v1/{cohort}/episodes.parquet"] = layout["v1"] / cohort / "episodes.parquet"
        result[f"v2/{cohort}/features.parquet"] = layout["v2"] / cohort / "features.parquet"
        result[f"v2/{cohort}/data.joblib"] = layout["v2"] / cohort / "data.joblib"
        result[f"v2/{cohort}/hybrid.joblib"] = layout["v2"] / cohort / "hybrid.joblib"
    return result


def validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("version") != "conditional-gate-20261026-v1":
        raise ProtocolError("Unexpected experiment protocol version")
    if protocol.get("status") != "prespecified_before_execution":
        raise ProtocolError("Protocol was not marked prespecified")
    if int(protocol.get("seed", -1)) != SEED:
        raise ProtocolError("Protocol seed mismatch")
    if tuple(protocol.get("arms", ())) != ALL_ARMS or len(set(ALL_ARMS)) != 18:
        raise ProtocolError("Protocol must lock exactly the 18 declared arms")
    features = protocol.get("features", {})
    if tuple(features.get("data", ())) != DATA_FEATURES:
        raise ProtocolError("Protocol DATA_V2 order differs from source")
    if tuple(features.get("hybrid_additions", ())) != tuple(PHYSICAL_FEATURES):
        raise ProtocolError("Protocol physical feature order differs from source")
    if HYBRID_FEATURES != DATA_FEATURES + tuple(PHYSICAL_FEATURES):
        raise ProtocolError("HYBRID_V2 must append exactly six physical features to DATA_V2")
    if len(DATA_FEATURES) != 33 or len(HYBRID_FEATURES) != 39:
        raise ProtocolError("Expected 33 data and 39 hybrid features")
    gate = protocol.get("gate", {})
    if tuple(float(value) for value in gate.get("grid", ())) != tuple(float(value) for value in GATE_GRID):
        raise ProtocolError("Gate grid mismatch")
    if tuple(gate.get("context_columns", ())) != tuple(CONTEXT_COLUMNS):
        raise ProtocolError("Gate context mismatch")
    if float(protocol.get("selection", {}).get("gate_threshold_minutes", -1)) != 5.0:
        raise ProtocolError("Selection threshold must be five minutes")


def validate_locked_inputs(layout: Mapping[str, Path], protocol: Mapping[str, Any]) -> dict[str, str]:
    expected = protocol["expected_input_sha256"]
    files = locked_input_files(layout)
    if set(expected) != set(files):
        raise ProtocolError("Protocol input hash keys differ from the locked source set")
    actual = {name: sha256_file(path) for name, path in files.items()}
    mismatches = {name: {"expected": expected[name], "actual": actual[name]}
                  for name in expected if expected[name] != actual[name]}
    if mismatches:
        raise ProtocolError(f"Locked input hash mismatch: {mismatches}")

    v2_protocol = load_json(layout["v2"] / "protocol.json")
    for cohort in COHORTS:
        expected_episode = expected[f"v1/{cohort}/episodes.parquet"]
        recorded = v2_protocol.get("input_hashes", {}).get(f"{cohort}/episodes.parquet")
        if recorded != expected_episode:
            raise ProtocolError(f"{cohort}: v2 protocol episode hash does not match this protocol")
        artifact = load_json(layout["v2"] / cohort / "artifact.json")
        for arm in ("data", "hybrid"):
            recorded_model = artifact.get("model_sha256", {}).get(arm)
            if recorded_model != expected[f"v2/{cohort}/{arm}.joblib"]:
                raise ProtocolError(f"{cohort}: trusted v2 {arm} artifact hash mismatch")
    return actual


def source_inventory(layout: Mapping[str, Path]) -> dict[str, str]:
    workspace = layout["workspace"]
    files: dict[str, Path] = {
        "experiment/run_experiment.py": SCRIPT_DIR / "run_experiment.py",
        "experiment/test_run_experiment.py": SCRIPT_DIR / "test_run_experiment.py",
        "experiment/protocol.json": SCRIPT_DIR / "protocol.json",
        "experiment/gate_model.py": SCRIPT_DIR / "gate_model.py",
        "experiment/test_gate_model.py": SCRIPT_DIR / "test_gate_model.py",
        "helpers/robust_night_experiments.py": layout["tools"] / "robust_night_experiments.py",
        "helpers/next_night_experiments.py": layout["tools"] / "next_night_experiments.py",
        "helpers/night_features_v2.py": layout["tools"] / "night_features_v2.py",
        "helpers/night_estimators.py": layout["tools"] / "night_estimators.py",
        "helpers/night_core.py": layout["web"] / "night_core.py",
        "v2/protocol.json": layout["v2"] / "protocol.json",
    }
    for name, path in locked_input_files(layout).items():
        files[f"inputs/{name}"] = path
    for cohort in COHORTS:
        files[f"inputs/v2/{cohort}/artifact.json"] = layout["v2"] / cohort / "artifact.json"
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required source files are missing: {missing}")
    return {name: sha256_file(path) for name, path in sorted(files.items())}


def environment_versions() -> dict[str, Any]:
    packages = (
        "numpy",
        "pandas",
        "scipy",
        "scikit-learn",
        "catboost",
        "pyarrow",
        "joblib",
        "threadpoolctl",
    )
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {name: importlib.metadata.version(name) for name in packages},
    }


def validate_environment(environment: Mapping[str, Any], protocol: Mapping[str, Any]) -> None:
    installed = environment["packages"]
    expected = protocol["expected_environment"]
    mismatches = {name: {"expected": version, "actual": installed.get(name)}
                  for name, version in expected.items() if installed.get(name) != version}
    if mismatches:
        raise ProtocolError(f"Environment differs from the locked versions: {mismatches}")
    if sklearn.__version__ != installed["scikit-learn"]:
        raise ProtocolError("Imported sklearn version differs from package metadata")


def _timestamp(value: Any) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        raise ProtocolError("Episode timestamps must be timezone-aware")
    return result


def _maximum_timestamp(frame: pd.DataFrame, columns: Sequence[str]) -> str | None:
    if frame.empty:
        return None
    values = [_timestamp(frame[column].max()) for column in columns]
    return max(values).isoformat()


def load_and_validate_cohort(layout: Mapping[str, Path], cohort: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    path = layout["v1"] / cohort / "episodes.parquet"
    frame = pd.read_parquet(path).copy()
    missing = sorted(REQUIRED_EPISODE_COLUMNS - set(frame.columns))
    if missing:
        raise ProtocolError(f"{cohort}: missing episode columns {missing}")
    if set(frame["cohort"].astype(str)) != {cohort}:
        raise ProtocolError(f"{cohort}: cohort column mismatch")
    if set(frame["split"].astype(str)) != set(SPLITS):
        raise ProtocolError(f"{cohort}: expected exactly train/validation/test")
    if frame["row_id"].duplicated().any() or frame["row_id"].tolist() != list(range(len(frame))):
        raise ProtocolError(f"{cohort}: row_id identity/order changed")
    if frame.duplicated(["subject_id", "cutoff"]).any():
        raise ProtocolError(f"{cohort}: duplicate subject/cutoff rows")
    if not np.isfinite(frame[list(TARGETS)].to_numpy(float)).all():
        raise ProtocolError(f"{cohort}: nonfinite targets")

    row_counts = {split: int(frame["split"].eq(split).sum()) for split in SPLITS}
    people_counts = {split: int(frame.loc[frame["split"].eq(split), "subject_id"].nunique()) for split in SPLITS}
    if row_counts != EXPECTED_COUNTS[cohort]["rows"] or people_counts != EXPECTED_COUNTS[cohort]["people"]:
        raise ProtocolError(
            f"{cohort}: partition counts changed: rows={row_counts}, people={people_counts}"
        )
    if (pd.to_datetime(frame["observed_at"], utc=True) < pd.to_datetime(frame["wake"], utc=True)).any():
        raise ProtocolError(f"{cohort}: outcome observed_at precedes wake")

    audit: dict[str, Any] = {
        "split_rows": row_counts,
        "split_people": people_counts,
        "row_id_sha256": sha256_values(frame["row_id"].tolist()),
        "subject_cutoff_unique": True,
        "all_validation_test_rows_preserved_at_load": True,
    }
    if cohort != "samsung":
        per_subject_splits = frame.groupby("subject_id")["split"].nunique()
        if int(per_subject_splits.max()) != 1:
            raise ProtocolError(f"{cohort}: participant outer-split leakage")
        audit["outer_participant_disjoint"] = True
        audit["chronology_claim"] = "none across participants; anonymized dates"
    else:
        if int(frame["subject_id"].nunique()) != 1:
            raise ProtocolError("Samsung protocol supports exactly one participant")
        temporal = {}
        for earlier, later in (("train", "validation"), ("validation", "test")):
            left = frame.loc[frame["split"].eq(earlier)]
            right = frame.loc[frame["split"].eq(later)]
            first_later = _timestamp(right["cutoff"].min())
            strict = (
                _timestamp(left["cutoff"].max()) < first_later
                and _timestamp(left["wake"].max()) < first_later
                and _timestamp(left["observed_at"].max()) < first_later
            )
            if not strict:
                raise ProtocolError(f"Samsung {earlier} labels are not available before {later}")
            temporal[f"{earlier}_before_{later}"] = {
                "first_later_cutoff": first_later.isoformat(),
                "max_earlier_availability": _maximum_timestamp(left, ("cutoff", "wake", "observed_at")),
                "strict": True,
            }
        audit["outer_temporal_availability"] = temporal
        audit["one_person"] = True
    return frame, audit


def training_prior(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty or not frame["split"].eq("train").all():
        raise ProtocolError("A prior may be fit only from a nonempty temporary TRAIN frame")
    prior = next_helpers.prior_from_train(frame)
    if int(prior["n_nights"]) != len(frame):
        raise ProtocolError("Prior night count does not equal fold/full TRAIN rows")
    if int(prior["n_people"]) != int(frame["subject_id"].nunique()):
        raise ProtocolError("Prior person count does not equal fold/full TRAIN participants")
    return prior


def strict_availability_purge(
    candidate_fit: pd.DataFrame, first_held_cutoff: Any
) -> tuple[pd.DataFrame, dict[str, Any]]:
    first = _timestamp(first_held_cutoff)
    mask = (
        pd.to_datetime(candidate_fit["cutoff"], utc=True).lt(first.tz_convert("UTC"))
        & pd.to_datetime(candidate_fit["wake"], utc=True).lt(first.tz_convert("UTC"))
        & pd.to_datetime(candidate_fit["observed_at"], utc=True).lt(first.tz_convert("UTC"))
    )
    fit = candidate_fit.loc[mask].copy()
    if fit.empty:
        max_availability = None
    else:
        max_availability = _maximum_timestamp(fit, ("cutoff", "wake", "observed_at"))
        if not _timestamp(max_availability) < first:
            raise ProtocolError("Strict availability purge failed")
    return fit, {
        "candidate_fit_rows": int(len(candidate_fit)),
        "fit_rows": int(len(fit)),
        "purged_rows": int((~mask).sum()),
        "first_held_cutoff": first.isoformat(),
        "max_fit_label_availability": max_availability,
        "strictly_before_first_held_cutoff": True,
    }


def make_oof_folds(train: pd.DataFrame, cohort: str) -> tuple[list[tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]], dict[str, Any]]:
    if train.empty or not train["split"].eq("train").all():
        raise ProtocolError("OOF construction requires only outer TRAIN rows")
    if train["row_id"].duplicated().any():
        raise ProtocolError("OOF TRAIN row IDs must be unique")

    base = train.copy()
    folds: list[tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]] = []
    held_ids: list[int] = []
    if cohort == "samsung":
        if int(base["subject_id"].nunique()) != 1:
            raise ProtocolError("Samsung forward folds require exactly one participant")
        base = base.sort_values(["cutoff", "row_id"], kind="mergesort")
        splitter = TimeSeriesSplit(n_splits=3, gap=1)
        iterator = splitter.split(base)
    else:
        splitter = GroupKFold(n_splits=3)
        iterator = splitter.split(base, groups=base["subject_id"])

    for fold_number, (fit_positions, held_positions) in enumerate(iterator, start=1):
        candidate_fit = base.iloc[fit_positions].copy()
        held = base.iloc[held_positions].copy()
        if cohort == "samsung":
            held = held.sort_values(["cutoff", "row_id"], kind="mergesort")
            fit, availability = strict_availability_purge(candidate_fit, held["cutoff"].min())
            if fit.empty:
                raise ProtocolError(f"Samsung fold {fold_number}: strict purge removed every fit row")
            if not fit["cutoff"].is_monotonic_increasing or not held["cutoff"].is_monotonic_increasing:
                raise ProtocolError("Samsung fold ordering is not chronological")
            participant_overlap = int(bool(set(fit["subject_id"]) & set(held["subject_id"])))
            evaluation_claim = "single-person forward replay with held enrollment reset"
            purge_applied = True
        else:
            fit = candidate_fit
            availability = {
                "candidate_fit_rows": int(len(fit)),
                "fit_rows": int(len(fit)),
                "purged_rows": 0,
                "first_held_cutoff": _timestamp(held["cutoff"].min()).isoformat(),
                "max_fit_label_availability": _maximum_timestamp(fit, ("wake", "observed_at")),
                "strictly_before_first_held_cutoff": None,
            }
            participant_overlap = len(set(fit["subject_id"]) & set(held["subject_id"]))
            if participant_overlap:
                raise ProtocolError(f"{cohort} fold {fold_number}: participant overlap")
            evaluation_claim = "participant-disjoint generalization; not global chronological evaluation"
            purge_applied = False

        fit["split"] = "train"
        held["split"] = "validation"
        if fit.empty or held.empty:
            raise ProtocolError(f"{cohort} fold {fold_number}: empty fit or held block")
        held_ids.extend(int(value) for value in held["row_id"])
        audit = {
            "fold": fold_number,
            "fit_rows": int(len(fit)),
            "held_rows": int(len(held)),
            "fit_people": int(fit["subject_id"].nunique()),
            "held_people": int(held["subject_id"].nunique()),
            "participant_overlap_count": int(participant_overlap),
            "fit_row_ids_sha256": sha256_values(fit["row_id"].tolist()),
            "held_row_ids_sha256": sha256_values(held["row_id"].tolist()),
            "held_split_for_replay": "validation",
            "fit_split_for_replay": "train",
            "held_enrollment_reset": True,
            "strict_availability_purge_applied": purge_applied,
            "evaluation_claim": evaluation_claim,
            **availability,
        }
        folds.append((fit, held, audit))

    duplicate_held = [row_id for row_id, count in Counter(held_ids).items() if count != 1]
    if duplicate_held:
        raise ProtocolError(f"{cohort}: OOF held row IDs are not unique: {duplicate_held[:5]}")
    train_ids = set(int(value) for value in train["row_id"])
    held_set = set(held_ids)
    if cohort != "samsung" and held_set != train_ids:
        raise ProtocolError(f"{cohort}: GroupKFold did not cover every TRAIN row exactly once")
    if cohort == "samsung" and not held_set < train_ids:
        raise ProtocolError("Samsung forward OOF must leave the early prefix/gaps outside gate training")
    coverage = {
        "train_rows": int(len(train)),
        "oof_rows": int(len(held_ids)),
        "held_row_ids_unique": True,
        "all_train_rows_covered": cohort != "samsung",
        "uncovered_train_rows": int(len(train_ids - held_set)),
        "uncovered_row_ids_sha256": sha256_values(sorted(train_ids - held_set)),
    }
    return folds, coverage


def assert_replay_alignment(source: pd.DataFrame, features: pd.DataFrame, states: pd.DataFrame) -> None:
    expected = source["row_id"].tolist()
    if features["row_id"].tolist() != expected or states["row_id"].tolist() != expected:
        raise ProtocolError("Replay did not restore immutable row_id order")
    if features["row_id"].duplicated().any() or states["row_id"].duplicated().any():
        raise ProtocolError("Replay emitted duplicate row IDs")


def assert_enrollment_resets(source: pd.DataFrame, states: pd.DataFrame, cohort: str) -> None:
    groups: list[str] = ["subject_id", "split"] if cohort == "samsung" else ["subject_id"]
    first_ids = (
        source.sort_values(groups + ["cutoff", "row_id"], kind="mergesort")
        .groupby(groups, sort=True, as_index=False)
        .head(1)["row_id"]
        .tolist()
    )
    indexed = states.set_index("row_id")
    if not indexed.loc[first_ids, "valid_days"].eq(0).all():
        raise ProtocolError("A replay enrollment block did not start at zero history")


def make_base_model(family: str):
    pipeline = robust_helpers.make_model("absolute7")
    if family == "hgb":
        pipeline.set_params(model__estimator__random_state=SEED)
    elif family == "catboost":
        estimator = CatBoostRegressor(
            loss_function="MAE",
            iterations=200,
            depth=4,
            learning_rate=0.05,
            l2_leaf_reg=10,
            random_seed=SEED,
            thread_count=2,
            verbose=False,
            allow_writing_files=False,
        )
        pipeline.set_params(model=MultiOutputRegressor(estimator))
    else:
        raise ValueError(f"Unknown model family: {family}")
    return pipeline


def _feature_rows(features: pd.DataFrame, row_ids: Sequence[int]) -> pd.DataFrame:
    if features["row_id"].duplicated().any():
        raise ProtocolError("Feature rows are not unique")
    indexed = features.set_index("row_id", drop=False)
    missing = set(row_ids) - set(indexed.index)
    if missing:
        raise ProtocolError(f"Feature rows missing IDs: {sorted(missing)[:5]}")
    return indexed.loc[list(row_ids)].reset_index(drop=True)


def fit_base_model(
    family: str,
    fit: pd.DataFrame,
    features: pd.DataFrame,
    feature_names: Sequence[str],
):
    if not fit["split"].eq("train").all():
        raise ProtocolError("Base model fit received non-TRAIN rows")
    aligned = _feature_rows(features, fit["row_id"].tolist())
    model = make_base_model(family)
    weights = robust_helpers.balanced_weights(fit)
    model.fit(
        aligned[list(feature_names)].to_numpy(float),
        fit[list(TARGETS)].to_numpy(float),
        model__sample_weight=weights,
    )
    return model


def fitted_pipeline_audit(model: Any, fit_rows: int) -> dict[str, Any]:
    required = ("impute", "clip", "variance", "scale", "model")
    if tuple(model.named_steps) != required:
        raise ProtocolError("Base preprocessing pipeline changed")
    seen = np.asarray(model.named_steps["scale"].n_samples_seen_)
    if not np.all(seen == fit_rows):
        raise ProtocolError("Preprocessing sample count differs from TRAIN fit rows")
    return {
        "fit_rows": int(fit_rows),
        "imputer": "median+missing-indicators+keep-empty",
        "quantile_clip": "TRAIN 0.01/0.99",
        "variance_removal": True,
        "standard_scaling": True,
        "scale_n_samples_seen": np.asarray(seen).astype(int).tolist(),
    }


def _project_candidate(values: Any, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 2 or array.shape[1] != 2 or not np.isfinite(array).all():
        raise ProtocolError(f"{name}: prediction must be a finite (n, 2) matrix")
    projected = next_helpers.project(array)
    bed, wake = projected[:, 0], projected[:, 1]
    valid = (
        (bed >= 0.0)
        & (bed <= 960.0)
        & (wake >= bed + 30.0 - 1e-10)
        & (wake <= 1440.0 + 1e-10)
        & (wake <= bed + 960.0 + 1e-10)
    )
    if not valid.all():
        raise ProtocolError(f"{name}: projected endpoint support is invalid")
    return projected


def blend_predictions(data_pred: Any, physical_pred: Any, weights: Any) -> np.ndarray:
    data = np.asarray(data_pred, dtype=float)
    physical = np.asarray(physical_pred, dtype=float)
    if data.ndim != 2 or data.shape[1] != 2 or physical.shape != data.shape:
        raise ValueError("data_pred and physical_pred must share shape (n, 2)")
    coefficient = np.asarray(weights, dtype=float)
    if coefficient.ndim == 0:
        coefficient = np.full(len(data), float(coefficient))
    if coefficient.shape != (len(data),) or not np.isfinite(coefficient).all():
        raise ValueError("fusion weights must be finite scalar or shape (n,)")
    if np.any((coefficient < 0.0) | (coefficient > 1.0)):
        raise ValueError("fusion weights must be in [0, 1]")
    return data + coefficient[:, None] * (physical - data)


def _set_prediction_pair(frame: pd.DataFrame, prefix: str, values: np.ndarray) -> None:
    frame[f"{prefix}_bed_offset"] = values[:, 0]
    frame[f"{prefix}_wake_offset"] = values[:, 1]


def _get_prediction_pair(frame: pd.DataFrame, prefix: str) -> np.ndarray:
    return frame[[f"{prefix}_bed_offset", f"{prefix}_wake_offset"]].to_numpy(float)


def generate_train_oof(
    frame: pd.DataFrame, cohort: str
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]], dict[str, Any]]:
    train = frame.loc[frame["split"].eq("train")].copy()
    fold_specs, coverage = make_oof_folds(train, cohort)
    family_rows: dict[str, list[pd.DataFrame]] = {family: [] for family in FAMILIES}
    audits: list[dict[str, Any]] = []

    for fit, held, audit in fold_specs:
        prior = training_prior(fit)
        fit_features, fit_states = robust_helpers.replay(fit, prior)
        held_features, held_states = robust_helpers.replay(held, prior)
        assert_replay_alignment(fit, fit_features, fit_states)
        assert_replay_alignment(held, held_features, held_states)
        assert_enrollment_resets(fit, fit_states, cohort)
        assert_enrollment_resets(held, held_states, cohort)
        physical = _project_candidate(
            held_features[list(PHYSICAL_ENDPOINTS)].to_numpy(float),
            f"{cohort}/fold{audit['fold']}/physical",
        )
        model_audits: dict[str, Any] = {}
        for family in FAMILIES:
            data_model = fit_base_model(family, fit, fit_features, DATA_FEATURES)
            
            data_pred = _project_candidate(
                data_model.predict(held_features[list(DATA_FEATURES)].to_numpy(float)),
                f"{cohort}/fold{audit['fold']}/{family}_data",
            )
            
            context = make_context(held_features, data_pred, physical, list(DATA_FEATURES))
            detail = held[["row_id", "subject_id", "cutoff"]].reset_index(drop=True).copy()
            detail.insert(0, "cohort", cohort)
            detail.insert(1, "model_family", family)
            detail.insert(2, "fold", int(audit["fold"]))
            detail["oof_source"] = "fold-held prediction; never full-TRAIN/in-sample"
            detail["actual_bed_offset"] = held["bed_offset"].to_numpy(float)
            detail["actual_wake_offset"] = held["wake_offset"].to_numpy(float)
            _set_prediction_pair(detail, "data_pred", data_pred)
            
            _set_prediction_pair(detail, "physical_pred", physical)
            for column in CONTEXT_COLUMNS:
                detail[column] = context[column].to_numpy(float)
            family_rows[family].append(detail)
            model_audits[family] = {"data": fitted_pipeline_audit(data_model, len(fit)), "hybrid_oof": "not fit; hybrid is a full-TRAIN comparator only"}
        audit["prior"] = prior
        audit["fit_first_state_valid_days_zero"] = True
        audit["held_first_state_valid_days_zero"] = True
        audit["model_preprocessing"] = model_audits
        audits.append(audit)
        print(f"{cohort}: OOF fold {audit['fold']}/3 complete", flush=True)

    result: dict[str, pd.DataFrame] = {}
    for family, parts in family_rows.items():
        combined = pd.concat(parts, ignore_index=True)
        combined = combined.sort_values(["fold", "row_id"], kind="mergesort").reset_index(drop=True)
        if combined.duplicated(["cohort", "model_family", "row_id"]).any():
            raise ProtocolError(f"{cohort}/{family}: duplicate OOF model-family row")
        result[family] = combined
    return result, audits, coverage


def weight_summary(weights: Any) -> dict[str, Any]:
    values = np.asarray(weights, dtype=float)
    if values.ndim != 1 or len(values) == 0 or not np.isfinite(values).all():
        raise ProtocolError("Gate weight summary requires a nonempty finite vector")
    return {
        "n": int(len(values)),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "q05": float(np.quantile(values, 0.05)),
        "q25": float(np.quantile(values, 0.25)),
        "q75": float(np.quantile(values, 0.75)),
        "q95": float(np.quantile(values, 0.95)),
        "minimum": float(values.min()),
        "maximum": float(values.max()),
        "fraction_zero": float(np.mean(values == 0.0)),
        "fraction_one": float(np.mean(values == 1.0)),
    }


def calibrate_gates(
    oof_by_family: Mapping[str, pd.DataFrame], cohort: str
) -> tuple[dict[str, dict[int, ConditionalGate]], dict[str, float], dict[str, Any]]:
    gates: dict[str, dict[int, ConditionalGate]] = {}
    fixed_weights: dict[str, float] = {}
    descriptions: dict[str, Any] = {}
    for family in FAMILIES:
        oof = oof_by_family[family]
        data = _get_prediction_pair(oof, "data_pred")
        physical = _get_prediction_pair(oof, "physical_pred")
        labels = oof[["actual_bed_offset", "actual_wake_offset"]].to_numpy(float)
        sample_weight = robust_helpers.balanced_weights(oof)
        context = oof[list(CONTEXT_COLUMNS)].copy()
        fixed_weight = best_fixed_weight(data, physical, labels, sample_weight)
        if fixed_weight not in set(float(value) for value in GATE_GRID):
            raise ProtocolError("Fixed fusion weight is outside the locked grid")
        fixed = _project_candidate(
            blend_predictions(data, physical, fixed_weight), f"{cohort}/{family}/OOF_fixed"
        )
        if not np.allclose(fixed, blend_predictions(data, physical, fixed_weight), atol=1e-10, rtol=0):
            raise ProtocolError("Projection changed a convex fixed blend")
        _set_prediction_pair(oof, "fixed_blend_pred", fixed)
        oof["fixed_blend_weight"] = fixed_weight
        fixed_weights[family] = float(fixed_weight)
        gates[family] = {}
        descriptions[family] = {"fixed_blend_weight": float(fixed_weight), "gates": {}}
        for depth in (1, 2):
            gate = ConditionalGate(depth=depth, min_samples_leaf=0.1, seed=SEED)
            gate.fit(context, data, physical, labels, sample_weight)
            weights = gate.predict_weights(context)
            if np.any((weights < 0.0) | (weights > 1.0)):
                raise ProtocolError("Conditional gate emitted a weight outside [0, 1]")
            raw = gate.predict(context, data, physical)
            expected = blend_predictions(data, physical, weights)
            if not np.allclose(raw, expected, atol=1e-12, rtol=0):
                raise ProtocolError("Conditional gate did not use shared physical-minus-data wiring")
            prediction = _project_candidate(raw, f"{cohort}/{family}/OOF_gate_depth{depth}")
            if not np.allclose(prediction, raw, atol=1e-10, rtol=0):
                raise ProtocolError("Projection changed a convex gate blend")
            _set_prediction_pair(oof, f"gate_depth{depth}_pred", prediction)
            oof[f"gate_depth{depth}_weight"] = weights
            gates[family][depth] = gate
            descriptions[family]["gates"][f"depth{depth}"] = {
                "description": gate.description(),
                "oof_weight_summary": weight_summary(weights),
            }
        zero_identity = _project_candidate(
            blend_predictions(data, physical, 0.0), f"{cohort}/{family}/g0"
        )
        descriptions[family]["g_zero_max_abs_error_vs_data"] = float(np.max(np.abs(zero_identity - data)))
        if descriptions[family]["g_zero_max_abs_error_vs_data"] > 1e-12:
            raise ProtocolError("g=0 does not reproduce the data prediction")
    return gates, fixed_weights, descriptions


def population_median_from_train(train: pd.DataFrame) -> np.ndarray:
    if train.empty or not train["split"].eq("train").all():
        raise ProtocolError("Population median requires only TRAIN rows")
    per_person = train.groupby("subject_id")[list(TARGETS)].median()
    value = per_person.median().to_numpy(float)
    return _project_candidate(value.reshape(1, 2), "population_median")[0]


def verify_frozen_feature_rows(
    fresh: pd.DataFrame,
    frozen: pd.DataFrame,
    row_ids: Sequence[int],
    tolerance: float = 1e-8,
) -> dict[str, Any]:
    left = _feature_rows(fresh, row_ids)
    right = _feature_rows(frozen, row_ids)
    result: dict[str, Any] = {"rows": int(len(row_ids)), "tolerance": tolerance}
    for arm, names in (("data", DATA_FEATURES), ("hybrid", HYBRID_FEATURES)):
        a = left[list(names)].to_numpy(float)
        b = right[list(names)].to_numpy(float)
        if not np.array_equal(np.isnan(a), np.isnan(b)):
            raise ProtocolError(f"Frozen v2 {arm} missingness differs from fresh replay")
        finite = np.isfinite(a) & np.isfinite(b)
        maximum = float(np.max(np.abs(a[finite] - b[finite]))) if finite.any() else 0.0
        if not np.allclose(a, b, atol=tolerance, rtol=0.0, equal_nan=True):
            raise ProtocolError(f"Frozen v2 {arm} features differ from fresh replay by {maximum}")
        result[f"{arm}_max_abs_difference"] = maximum
        result[f"{arm}_matched"] = True
    return result


def predict_family_arms(
    family: str,
    features: pd.DataFrame,
    data_pred: Any,
    hybrid_pred: Any,
    physical_pred: Any,
    fixed_weight: float,
    gates: Mapping[int, Any],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, Any]]:
    data = _project_candidate(data_pred, f"{family}_data")
    hybrid = _project_candidate(hybrid_pred, f"{family}_hybrid")
    physical = _project_candidate(physical_pred, f"{family}_physical_input")
    context = make_context(features, data, physical, list(DATA_FEATURES))
    fixed_raw = blend_predictions(data, physical, fixed_weight)
    fixed = _project_candidate(fixed_raw, f"{family}_fixed_blend")
    if not np.allclose(fixed, fixed_raw, atol=1e-10, rtol=0):
        raise ProtocolError("Projection changed a fixed convex fusion prediction")
    predictions = {
        f"{family}_data": data,
        f"{family}_hybrid": hybrid,
        f"{family}_fixed_blend": fixed,
    }
    weight_vectors: dict[str, np.ndarray] = {
        f"{family}_fixed_blend": np.full(len(data), float(fixed_weight)),
    }
    diagnostics: dict[str, Any] = {
        "fixed_formula_max_abs_error": float(np.max(np.abs(fixed - fixed_raw))) if len(data) else 0.0,
    }
    for depth in (1, 2):
        gate = gates[depth]
        weights = np.asarray(gate.predict_weights(context), dtype=float)
        if weights.shape != (len(data),) or np.any((weights < 0.0) | (weights > 1.0)):
            raise ProtocolError(f"{family}/depth{depth}: invalid gate weights")
        raw = np.asarray(gate.predict(context, data, physical), dtype=float)
        expected = blend_predictions(data, physical, weights)
        wiring_error = float(np.max(np.abs(raw - expected))) if len(data) else 0.0
        if wiring_error > 1e-12:
            raise ProtocolError(f"{family}/depth{depth}: gate is not wired to physical-minus-data")
        arm = f"{family}_gate_depth{depth}"
        prediction = _project_candidate(raw, arm)
        if not np.allclose(prediction, raw, atol=1e-10, rtol=0):
            raise ProtocolError(f"{arm}: projection changed a convex gate prediction")
        predictions[arm] = prediction
        weight_vectors[arm] = weights
        diagnostics[f"gate_depth{depth}_formula_max_abs_error"] = wiring_error
    return predictions, weight_vectors, diagnostics


def baseline_predictions(features: pd.DataFrame, population_median: np.ndarray) -> dict[str, np.ndarray]:
    n = len(features)
    raw = {
        "baseline_recent7": features[["recent_bed", "recent_wake"]].to_numpy(float),
        "baseline_recent14": features[["median14_bed", "median14_wake"]].to_numpy(float),
        "baseline_ew14": features[["ew_bed", "ew_wake"]].to_numpy(float),
        "baseline_weektype": features[["weekday_bed", "weekday_wake"]].to_numpy(float),
        "baseline_population_median": np.tile(np.asarray(population_median, dtype=float), (n, 1)),
        "physical": features[list(PHYSICAL_ENDPOINTS)].to_numpy(float),
    }
    return {name: _project_candidate(values, name) for name, values in raw.items()}


def predict_all_arms(
    features: pd.DataFrame,
    models: Mapping[str, Mapping[str, Any]],
    gates: Mapping[str, Mapping[int, Any]],
    fixed_weights: Mapping[str, float],
    population_median: np.ndarray,
    frozen_models: Mapping[str, Any],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, Any]]:
    predictions = baseline_predictions(features, population_median)
    weight_vectors: dict[str, np.ndarray] = {}
    diagnostics: dict[str, Any] = {}
    physical = predictions["physical"]
    for family in FAMILIES:
        data_raw = models[family]["data"].predict(features[list(DATA_FEATURES)].to_numpy(float))
        hybrid_raw = models[family]["hybrid"].predict(features[list(HYBRID_FEATURES)].to_numpy(float))
        family_predictions, family_weights, family_diagnostics = predict_family_arms(
            family,
            features,
            data_raw,
            hybrid_raw,
            physical,
            float(fixed_weights[family]),
            gates[family],
        )
        predictions.update(family_predictions)
        weight_vectors.update(family_weights)
        diagnostics[family] = family_diagnostics
    predictions["frozen_v2_data"] = _project_candidate(
        frozen_models["data"].predict(features[list(DATA_FEATURES)].to_numpy(float)),
        "frozen_v2_data",
    )
    predictions["frozen_v2_hybrid"] = _project_candidate(
        frozen_models["hybrid"].predict(features[list(HYBRID_FEATURES)].to_numpy(float)),
        "frozen_v2_hybrid",
    )
    if set(predictions) != set(ALL_ARMS) or len(predictions) != 18:
        raise ProtocolError(f"Prediction set is not the locked 18 arms: {sorted(predictions)}")
    return predictions, weight_vectors, diagnostics


def prediction_table(
    frame: pd.DataFrame,
    cohort: str,
    split: str,
    predictions: Mapping[str, np.ndarray],
    weight_vectors: Mapping[str, np.ndarray],
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    base = frame[["row_id", "subject_id", "cutoff", "bed_offset", "wake_offset"]].reset_index(drop=True)
    for arm in ALL_ARMS:
        values = np.asarray(predictions[arm], dtype=float)
        if values.shape != (len(base), 2):
            raise ProtocolError(f"{cohort}/{split}/{arm}: prediction row count mismatch")
        part = base.copy()
        part.insert(0, "cohort", cohort)
        part.insert(1, "split", split)
        part.insert(2, "arm", arm)
        part = part.rename(columns={
            "bed_offset": "actual_bed_offset",
            "wake_offset": "actual_wake_offset",
        })
        part["predicted_bed_offset"] = values[:, 0]
        part["predicted_wake_offset"] = values[:, 1]
        if arm in weight_vectors:
            part["fusion_weight"] = np.asarray(weight_vectors[arm], dtype=float)
        else:
            part["fusion_weight"] = np.nan
        parts.append(part)
    result = pd.concat(parts, ignore_index=True)
    assert_identical_nights(result, frame, ALL_ARMS)
    return result


def assert_identical_nights(
    table: pd.DataFrame, expected: pd.DataFrame, arms: Sequence[str]
) -> dict[str, Any]:
    expected_keys = [
        (int(row.row_id), str(row.subject_id), _timestamp(row.cutoff).isoformat())
        for row in expected[["row_id", "subject_id", "cutoff"]].itertuples(index=False)
    ]
    if len(set(expected_keys)) != len(expected_keys):
        raise ProtocolError("Expected evaluation nights are not unique")
    for arm in arms:
        rows = table.loc[table["arm"].eq(arm), ["row_id", "subject_id", "cutoff"]]
        actual_keys = [
            (int(row.row_id), str(row.subject_id), _timestamp(row.cutoff).isoformat())
            for row in rows.itertuples(index=False)
        ]
        if actual_keys != expected_keys:
            raise ProtocolError(f"{arm}: candidates are not evaluated on identical nights/order")
    if table.duplicated(["cohort", "split", "arm", "row_id"]).any():
        raise ProtocolError("Prediction table contains duplicate arm/night rows")
    return {
        "arms": int(len(arms)),
        "nights_per_arm": int(len(expected_keys)),
        "row_signature_sha256": sha256_values(expected_keys),
        "identical": True,
    }


def evaluate_predictions(
    frame: pd.DataFrame, predictions: Mapping[str, np.ndarray], cohort: str
) -> dict[str, dict[str, Any]]:
    actual = frame[list(TARGETS)].to_numpy(float)
    macro = cohort != "samsung"
    subjects = frame["subject_id"].to_numpy()
    return {
        arm: next_helpers.score(actual, predictions[arm], subjects, macro=macro)
        for arm in ALL_ARMS
    }


def _metric_key(arm: str, metrics: Mapping[str, Mapping[str, Any]]) -> tuple[float, int, str]:
    value = float(metrics[arm]["joint_mae_min"])
    if not np.isfinite(value):
        raise ProtocolError(f"{arm}: Validation joint MAE is not finite")
    return value, ARM_COMPLEXITY[arm], arm


def select_validation_arm(
    validation_metrics: Mapping[str, Mapping[str, Any]], threshold_minutes: float = 5.0
) -> dict[str, Any]:
    available = set(validation_metrics)
    nonconditional = [arm for arm in NONCONDITIONAL_ARMS if arm in available]
    gates = [arm for arm in GATE_ARMS if arm in available]
    if not nonconditional or not gates:
        raise ProtocolError("Selection needs at least one nonconditional control and one gate")
    best_nonconditional = min(nonconditional, key=lambda arm: _metric_key(arm, validation_metrics))
    best_gate = min(gates, key=lambda arm: _metric_key(arm, validation_metrics))
    nonconditional_mae = float(validation_metrics[best_nonconditional]["joint_mae_min"])
    gate_mae = float(validation_metrics[best_gate]["joint_mae_min"])
    gain = nonconditional_mae - gate_mae
    gate_selected = gain >= float(threshold_minutes)
    selected = best_gate if gate_selected else best_nonconditional
    return {
        "best_nonconditional": best_nonconditional,
        "best_gate": best_gate,
        "best_nonconditional_joint_mae_min": nonconditional_mae,
        "best_gate_joint_mae_min": gate_mae,
        "gate_gain_min": float(gain),
        "gate_threshold_min": float(threshold_minutes),
        "gate_selected": bool(gate_selected),
        "selected_arm": selected,
        "tie_rule": "joint MAE, simplicity class, lexical arm ID; gate still requires threshold",
    }


def build_selection_lock(
    validation_metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    source_hashes: Mapping[str, str],
    data_hashes: Mapping[str, str],
    model_hashes: Mapping[str, str],
) -> dict[str, Any]:
    if set(validation_metrics) != set(COHORTS):
        raise ProtocolError("Global selection lock needs all three cohorts")
    cohorts: dict[str, Any] = {}
    for cohort in COHORTS:
        if set(validation_metrics[cohort]) != set(ALL_ARMS):
            raise ProtocolError(f"{cohort}: selection lock is missing an arm")
        cohorts[cohort] = {
            "validation_metrics": validation_metrics[cohort],
            "choice": select_validation_arm(validation_metrics[cohort], threshold_minutes=5.0),
        }
    return {
        "schema_version": 1,
        "protocol_version": "conditional-gate-20261026-v1",
        "status": "locked_before_any_test_prediction_or_scoring",
        "created_at_utc": utc_now(),
        "selection_input": "Validation metrics only; TEST fields are absent by construction",
        "threshold_minutes": 5.0,
        "arms": list(ALL_ARMS),
        "cohorts": cohorts,
        "source_hashes": dict(source_hashes),
        "data_hashes": dict(data_hashes),
        "model_hashes": dict(model_hashes),
        "no_refit_after_lock": True,
        "test_status": "reused exploratory, never fresh confirmation",
    }


def file_hashes_relative(root: Path, paths: Sequence[Path]) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(paths)}


def verify_selection_lock(
    output: Path, current_source_hashes: Mapping[str, str]
) -> dict[str, Any]:
    lock = load_json(output / "selection_lock.json")
    if lock.get("status") != "locked_before_any_test_prediction_or_scoring":
        raise ProtocolError("Selection lock status is invalid")
    if lock.get("source_hashes") != dict(current_source_hashes):
        raise ProtocolError("Source hashes changed before TEST phase")
    for relative, expected in lock.get("data_hashes", {}).items():
        if sha256_file(output / relative) != expected:
            raise ProtocolError(f"Locked data artifact changed: {relative}")
    for relative, expected in lock.get("model_hashes", {}).items():
        if sha256_file(output / relative) != expected:
            raise ProtocolError(f"Locked model artifact changed: {relative}")
    for cohort in COHORTS:
        locked = lock["cohorts"][cohort]
        recomputed = select_validation_arm(locked["validation_metrics"], threshold_minutes=5.0)
        if recomputed != locked["choice"]:
            raise ProtocolError(f"{cohort}: locked Validation choice is not reproducible")
    return lock


def load_frozen_models(layout: Mapping[str, Path], cohort: str) -> dict[str, Any]:
    return {
        "data": joblib.load(layout["v2"] / cohort / "data.joblib"),
        "hybrid": joblib.load(layout["v2"] / cohort / "hybrid.joblib"),
    }


def load_trained_models(output: Path, cohort: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[int, Any]], dict[str, Any]]:
    cohort_dir = output / "models" / cohort
    models: dict[str, dict[str, Any]] = {}
    gates: dict[str, dict[int, Any]] = {}
    for family in FAMILIES:
        models[family] = {
            arm: joblib.load(cohort_dir / f"{family}_{arm}.joblib")
            for arm in ("data", "hybrid")
        }
        gates[family] = {
            depth: joblib.load(cohort_dir / f"{family}_gate_depth{depth}.joblib")
            for depth in (1, 2)
        }
    calibration = load_json(cohort_dir / "calibration.json")
    return models, gates, calibration


def per_person_metrics(
    frame: pd.DataFrame, prediction: np.ndarray
) -> list[dict[str, Any]]:
    actual = frame[list(TARGETS)].to_numpy(float)
    subjects = frame["subject_id"].astype(str).to_numpy()
    result: list[dict[str, Any]] = []
    for subject in sorted(set(subjects)):
        mask = subjects == subject
        metrics = next_helpers.score(actual[mask], prediction[mask], subjects[mask], macro=False)
        result.append({"subject_id": subject, **metrics})
    return result


def paired_test_gain(
    actual: np.ndarray,
    baseline: np.ndarray,
    changed: np.ndarray,
    subjects: Sequence[Any],
    cohort: str,
) -> dict[str, Any]:
    if cohort != "samsung":
        result = next_helpers.paired_interval(
            actual, baseline, changed, subjects, temporal=False
        )
        result["status"] = "descriptive only; TEST was previously exposed"
        return result
    delta = next_helpers.errors(actual, baseline) - next_helpers.errors(actual, changed)
    names = ("onset", "wake", "interval", "joint")
    return {
        "method": "paired equal-night descriptive difference; confidence interval omitted",
        "positive_means": "lower error for changed model",
        "status": "one person and sparse nights; rows are not assumed calendar-contiguous",
        "metrics": {
            name: {"error_reduction_min": float(delta[:, index].mean())}
            for index, name in enumerate(names)
        },
    }


def history_summary(features: pd.DataFrame, states: pd.DataFrame) -> dict[str, Any]:
    return {
        "max_valid_days": int(states["valid_days"].max()),
        "max_recent_count14": float(features["recent_count14"].max()),
        "personal_weight_positive_rows": int((features["personal_weight"] > 0.0).sum()),
    }


def run_experiment(workspace: Path, output_argument: Path) -> Path:
    layout = layout_paths(workspace)
    _add_import_paths(layout["workspace"])
    validate_import_binding(layout)
    protocol_source = SCRIPT_DIR / "protocol.json"
    protocol = load_json(protocol_source)
    validate_protocol(protocol)

    output = create_output_directory(output_argument)
    with (output / "protocol.json").open("xb") as handle:
        handle.write(protocol_source.read_bytes())

    environment = environment_versions()
    validate_environment(environment, protocol)
    dump_json_new(output / "environment.json", environment)

    locked_input_hashes = validate_locked_inputs(layout, protocol)
    sources_before = source_inventory(layout)
    dump_json_new(output / "source_hashes_before.json", {
        "algorithm": "SHA-256",
        "captured_at_utc": utc_now(),
        "protocol_locked_inputs": locked_input_hashes,
        "files": sources_before,
    })

    frames: dict[str, pd.DataFrame] = {}
    dataset_audits: dict[str, Any] = {}
    frozen_feature_tables: dict[str, pd.DataFrame] = {}
    for cohort in COHORTS:
        frames[cohort], dataset_audits[cohort] = load_and_validate_cohort(layout, cohort)
        frozen_feature_tables[cohort] = pd.read_parquet(layout["v2"] / cohort / "features.parquet")

    models_root = output / "models"
    models_root.mkdir()
    all_oof: list[pd.DataFrame] = []
    fold_audits: dict[str, Any] = {}
    gate_descriptions: dict[str, Any] = {}
    phase1_features: dict[str, pd.DataFrame] = {}
    phase1_states: dict[str, pd.DataFrame] = {}
    phase1_models: dict[str, dict[str, dict[str, Any]]] = {}
    phase1_gates: dict[str, dict[str, dict[int, ConditionalGate]]] = {}
    phase1_calibrations: dict[str, dict[str, Any]] = {}
    phase1_predictions: dict[str, dict[str, np.ndarray]] = {}
    phase1_weights: dict[str, dict[str, np.ndarray]] = {}
    validation_metrics: dict[str, dict[str, dict[str, Any]]] = {}
    validation_tables: list[pd.DataFrame] = []
    frozen_checks: dict[str, Any] = {}
    model_pipeline_audits: dict[str, Any] = {}
    phase1_wiring_audits: dict[str, Any] = {}

    with threadpool_limits(limits=2):
        for cohort in COHORTS:
            frame = frames[cohort]
            oof_by_family, audits, coverage = generate_train_oof(frame, cohort)
            gates, fixed_weights, descriptions = calibrate_gates(oof_by_family, cohort)
            all_oof.extend(oof_by_family[family] for family in FAMILIES)
            fold_audits[cohort] = {"folds": audits, "coverage": coverage}
            gate_descriptions[cohort] = descriptions

            train = frame.loc[frame["split"].eq("train")].copy()
            train["split"] = "train"
            validation = frame.loc[frame["split"].eq("validation")].copy()
            phase1_frame = frame.loc[frame["split"].isin(("train", "validation"))].copy()
            prior = training_prior(train)
            features, states = robust_helpers.replay(phase1_frame, prior)
            assert_replay_alignment(phase1_frame, features, states)
            assert_enrollment_resets(phase1_frame, states, cohort)
            phase1_features[cohort], phase1_states[cohort] = features, states
            frozen_checks[cohort] = {
                "train_validation": verify_frozen_feature_rows(
                    features,
                    frozen_feature_tables[cohort],
                    phase1_frame["row_id"].tolist(),
                )
            }

            cohort_models: dict[str, dict[str, Any]] = {}
            pipeline_audit: dict[str, Any] = {}
            train_features = _feature_rows(features, train["row_id"].tolist())
            cohort_dir = models_root / cohort
            cohort_dir.mkdir()
            for family in FAMILIES:
                cohort_models[family] = {}
                pipeline_audit[family] = {}
                for arm, names in (("data", DATA_FEATURES), ("hybrid", HYBRID_FEATURES)):
                    model = fit_base_model(family, train, train_features, names)
                    cohort_models[family][arm] = model
                    pipeline_audit[family][arm] = fitted_pipeline_audit(model, len(train))
                    save_joblib_new(cohort_dir / f"{family}_{arm}.joblib", model)
                for depth in (1, 2):
                    save_joblib_new(cohort_dir / f"{family}_gate_depth{depth}.joblib", gates[family][depth])
            population_median = population_median_from_train(train)
            calibration = {
                "cohort": cohort,
                "seed": SEED,
                "prior": prior,
                "population_median": population_median.tolist(),
                "fixed_weights": fixed_weights,
                "data_features": list(DATA_FEATURES),
                "hybrid_features": list(HYBRID_FEATURES),
                "no_refit_after_validation": True,
            }
            dump_json_new(cohort_dir / "calibration.json", calibration)
            phase1_models[cohort] = cohort_models
            phase1_gates[cohort] = gates
            phase1_calibrations[cohort] = calibration
            model_pipeline_audits[cohort] = pipeline_audit

            validation_features = _feature_rows(features, validation["row_id"].tolist())
            frozen_models = load_frozen_models(layout, cohort)
            predictions, weights, wiring = predict_all_arms(
                validation_features,
                cohort_models,
                gates,
                fixed_weights,
                population_median,
                frozen_models,
            )
            phase1_predictions[cohort] = predictions
            phase1_weights[cohort] = weights
            phase1_wiring_audits[cohort] = wiring
            validation_metrics[cohort] = evaluate_predictions(validation, predictions, cohort)
            validation_tables.append(
                prediction_table(validation, cohort, "validation", predictions, weights)
            )
            print(f"{cohort}: Validation predictions complete", flush=True)

    oof_table = pd.concat(all_oof, ignore_index=True)
    if oof_table.duplicated(["cohort", "model_family", "row_id"]).any():
        raise ProtocolError("OOF rows are not unique per model family")
    oof_path = output / "oof_predictions.csv"
    write_csv_new(oof_path, oof_table)
    validation_table = pd.concat(validation_tables, ignore_index=True)
    validation_path = output / "validation_predictions.csv"
    write_csv_new(validation_path, validation_table)
    fold_audits_path = output / "fold_audits.json"
    dump_json_new(fold_audits_path, {"datasets": dataset_audits, "oof": fold_audits})
    gate_descriptions_path = output / "gate_descriptions.json"
    dump_json_new(gate_descriptions_path, gate_descriptions)

    model_files = [path for path in models_root.rglob("*") if path.is_file()]
    model_hashes = file_hashes_relative(output, model_files + [gate_descriptions_path])
    data_files = [
        output / "protocol.json",
        output / "environment.json",
        output / "source_hashes_before.json",
        fold_audits_path,
        oof_path,
        validation_path,
    ]
    data_hashes = file_hashes_relative(output, data_files)
    selection_lock = build_selection_lock(
        validation_metrics, sources_before, data_hashes, model_hashes
    )
    selection_lock_path = output / "selection_lock.json"
    dump_json_new(selection_lock_path, selection_lock)
    lock_written_at = utc_now()

    current_sources = source_inventory(layout)
    verified_lock = verify_selection_lock(output, current_sources)
    phase2_started_at = utc_now()

    test_tables: list[pd.DataFrame] = []
    test_predictions: dict[str, dict[str, np.ndarray]] = {}
    test_weights: dict[str, dict[str, np.ndarray]] = {}
    test_features: dict[str, pd.DataFrame] = {}
    test_states: dict[str, pd.DataFrame] = {}
    serialization_errors: dict[str, Any] = {}
    phase2_wiring_audits: dict[str, Any] = {}
    identical_night_checks: dict[str, Any] = {}

    with threadpool_limits(limits=2):
        for cohort in COHORTS:
            frame = frames[cohort]
            validation = frame.loc[frame["split"].eq("validation")].copy()
            test = frame.loc[frame["split"].eq("test")].copy()
            models, gates, calibration = load_trained_models(output, cohort)
            frozen_models = load_frozen_models(layout, cohort)
            fixed_weights = {key: float(value) for key, value in calibration["fixed_weights"].items()}
            population_median = np.asarray(calibration["population_median"], dtype=float)

            validation_features = _feature_rows(
                phase1_features[cohort], validation["row_id"].tolist()
            )
            reloaded_validation, _, _ = predict_all_arms(
                validation_features,
                models,
                gates,
                fixed_weights,
                population_median,
                frozen_models,
            )
            serialization_errors[cohort] = {}
            for arm in ALL_ARMS:
                error = float(np.max(np.abs(reloaded_validation[arm] - phase1_predictions[cohort][arm])))
                serialization_errors[cohort][arm] = error
                if error > 1e-9:
                    raise ProtocolError(f"{cohort}/{arm}: serialized Validation prediction mismatch")

            prior = calibration["prior"]
            if int(prior["n_nights"]) != EXPECTED_COUNTS[cohort]["rows"]["train"]:
                raise ProtocolError(f"{cohort}: saved full-TRAIN prior row count changed")
            features, states = robust_helpers.replay(test, prior)
            assert_replay_alignment(test, features, states)
            assert_enrollment_resets(test, states, cohort)
            test_features[cohort], test_states[cohort] = features, states
            frozen_checks[cohort]["test"] = verify_frozen_feature_rows(
                features, frozen_feature_tables[cohort], test["row_id"].tolist()
            )
            predictions, weights, wiring = predict_all_arms(
                features,
                models,
                gates,
                fixed_weights,
                population_median,
                frozen_models,
            )
            test_predictions[cohort] = predictions
            test_weights[cohort] = weights
            phase2_wiring_audits[cohort] = wiring
            detail = prediction_table(test, cohort, "test", predictions, weights)
            identical_night_checks[cohort] = assert_identical_nights(detail, test, ALL_ARMS)
            test_tables.append(detail)
            print(f"{cohort}: exploratory reused TEST predictions complete", flush=True)

    test_table = pd.concat(test_tables, ignore_index=True)
    predictions_path = output / "predictions.csv"
    write_csv_new(predictions_path, test_table)

    metrics_root = output / "metrics"
    metrics_root.mkdir()
    test_metrics: dict[str, dict[str, Any]] = {}
    comparison_audits: dict[str, Any] = {}
    for cohort in COHORTS:
        frame = frames[cohort]
        test = frame.loc[frame["split"].eq("test")].copy()
        actual = test[list(TARGETS)].to_numpy(float)
        subjects = test["subject_id"].to_numpy()
        predictions = test_predictions[cohort]
        test_metrics[cohort] = evaluate_predictions(test, predictions, cohort)
        best_nonconditional = verified_lock["cohorts"][cohort]["choice"]["best_nonconditional"]
        comparison_audits[cohort] = {}
        cohort_dir = metrics_root / cohort
        cohort_dir.mkdir()
        for arm in ALL_ARMS:
            comparisons: dict[str, Any] = {}
            if arm in GATE_ARMS:
                family = arm.split("_", 1)[0]
                references = {
                    "vs_own_data": f"{family}_data",
                    "vs_own_fixed_blend": f"{family}_fixed_blend",
                    "vs_best_locked_nonconditional": best_nonconditional,
                }
                for label, baseline_arm in references.items():
                    comparisons[label] = {
                        "baseline_arm": baseline_arm,
                        **paired_test_gain(
                            actual,
                            predictions[baseline_arm],
                            predictions[arm],
                            subjects,
                            cohort,
                        ),
                    }
                comparison_audits[cohort][arm] = list(references.values())
            arm_dir = cohort_dir / arm
            arm_dir.mkdir()
            payload = {
                "cohort": cohort,
                "arm": arm,
                "validation": verified_lock["cohorts"][cohort]["validation_metrics"][arm],
                "test": test_metrics[cohort][arm],
                "per_person": {"validation": per_person_metrics(frame.loc[frame["split"].eq("validation")], phase1_predictions[cohort][arm]), "test": per_person_metrics(test, predictions[arm])},
                "paired_test_gains": comparisons,
                "identical_nights": identical_night_checks[cohort],
                "selected_by_validation_lock": arm == verified_lock["cohorts"][cohort]["choice"]["selected_arm"],
                "test_status": "reused exploratory; descriptive only; never fresh confirmation",
            }
            dump_json_new(arm_dir / "metrics.json", payload)

    gate_weight_summaries: dict[str, Any] = {}
    for cohort in COHORTS:
        gate_weight_summaries[cohort] = {}
        for family in FAMILIES:
            oof = next(
                frame for frame in all_oof
                if str(frame["cohort"].iloc[0]) == cohort and str(frame["model_family"].iloc[0]) == family
            )
            gate_weight_summaries[cohort][family] = {}
            for depth in (1, 2):
                arm = f"{family}_gate_depth{depth}"
                gate_weight_summaries[cohort][family][f"depth{depth}"] = {
                    "oof": weight_summary(oof[f"gate_depth{depth}_weight"].to_numpy(float)),
                    "validation": weight_summary(phase1_weights[cohort][arm]),
                    "test": weight_summary(test_weights[cohort][arm]),
                }
    gate_weight_path = output / "gate_weight_summaries.json"
    dump_json_new(gate_weight_path, gate_weight_summaries)

    history_checks: dict[str, Any] = {}
    for cohort in COHORTS:
        features = pd.concat([phase1_features[cohort], test_features[cohort]], ignore_index=True)
        states = pd.concat([phase1_states[cohort], test_states[cohort]], ignore_index=True)
        summary = history_summary(features, states)
        expected = EXPECTED_HISTORY[cohort]
        if summary != expected:
            raise ProtocolError(f"{cohort}: replay history summary changed: {summary} != {expected}")
        history_checks[cohort] = {**summary, "matches_prespecified_audit": True}

    sources_after = source_inventory(layout)
    if sources_after != sources_before:
        changed = sorted(name for name in sources_before if sources_before[name] != sources_after.get(name))
        raise ProtocolError(f"Source/input files changed during execution: {changed}")
    dump_json_new(output / "source_hashes_after.json", {
        "algorithm": "SHA-256",
        "captured_at_utc": utc_now(),
        "files": sources_after,
        "unchanged_from_before": True,
    })

    model_hashes_after = {
        relative: sha256_file(output / relative)
        for relative in verified_lock["model_hashes"]
    }
    if model_hashes_after != verified_lock["model_hashes"]:
        raise ProtocolError("Locked model artifacts changed after Validation")

    validation_identity = {
        cohort: assert_identical_nights(
            validation_table.loc[validation_table["cohort"].eq(cohort)].reset_index(drop=True),
            frames[cohort].loc[frames[cohort]["split"].eq("validation")],
            ALL_ARMS,
        )
        for cohort in COHORTS
    }
    oof_uniqueness = {
        cohort: {
            family: {
                "rows": int(len(oof_table.loc[
                    oof_table["cohort"].eq(cohort) & oof_table["model_family"].eq(family)
                ])),
                "unique_per_model_family": not oof_table.loc[
                    oof_table["cohort"].eq(cohort) & oof_table["model_family"].eq(family),
                    "row_id",
                ].duplicated().any(),
            }
            for family in FAMILIES
        }
        for cohort in COHORTS
    }

    verification = {
        "schema_version": 1,
        "status": "passed",
        "protocol_version": protocol["version"],
        "checks": {
            "input_sha256_matches_v2_protocol": True,
            "source_hashes_unchanged": True,
            "model_hashes_unchanged_after_lock": True,
            "selection_lock_written_before_test": {
                "passed": True,
                "lock_written_at_utc": lock_written_at,
                "phase2_started_at_utc": phase2_started_at,
                "guard": "Phase 2 began only after verify_selection_lock rehashed source, data, and model artifacts.",
            },
            "split_and_row_preservation": dataset_audits,
            "oof_uniqueness": oof_uniqueness,
            "oof_fold_audits": fold_audits,
            "fold_and_full_train_only_preprocessing": {
                "folds": "recorded per fold in fold_audits.json",
                "full_train": model_pipeline_audits,
                "no_validation_eval_set_or_refit": True,
                "no_target_clipping_or_removal": True,
            },
            "frozen_v2_feature_match": frozen_checks,
            "serialized_validation_prediction_max_abs_error": serialization_errors,
            "gate_wiring_validation": phase1_wiring_audits,
            "gate_wiring_test": phase2_wiring_audits,
            "validation_identical_nights": validation_identity,
            "test_identical_nights": identical_night_checks,
            "all_18_arms_scored": all(set(values) == set(ALL_ARMS) for values in test_metrics.values()),
            "history_replay": history_checks,
            "paired_test_references": comparison_audits,
            "samsung_confidence_interval_omitted": True,
            "cmi_mesa_participant_bootstrap_descriptive_only": True,
            "service_or_product_artifacts_modified": False,
        },
        "selected_arms": {
            cohort: verified_lock["cohorts"][cohort]["choice"] for cohort in COHORTS
        },
        "limitations": [
            "Validation and TEST were previously exposed and are exploratory, not fresh confirmation.",
            "CMI/MESA OOF estimates new-participant generalization, not global chronological generalization across anonymized dates.",
            "Samsung contains one person with sparse nights; no CI is reported and rows are not treated as calendar-contiguous.",
            "MESA participants have at most 13 episodes here, so the 14-night personalization threshold never activates.",
            "The physical model uses fixed physiological constants and is not an individual physiological measurement.",
            "No external data, sensor/light/HCL features, TabPFN, deployment, upload, or service promotion was used.",
        ],
        "artifact_hashes": {
            "oof_predictions.csv": sha256_file(oof_path),
            "validation_predictions.csv": sha256_file(validation_path),
            "selection_lock.json": sha256_file(selection_lock_path),
            "predictions.csv": sha256_file(predictions_path),
            "gate_weight_summaries.json": sha256_file(gate_weight_path),
        },
    }
    dump_json_new(output / "verification.json", verification)

    public_summary = {
        cohort: {
            "selected_arm": verified_lock["cohorts"][cohort]["choice"]["selected_arm"],
            "exploratory_test_joint_mae_min": test_metrics[cohort][
                verified_lock["cohorts"][cohort]["choice"]["selected_arm"]
            ]["joint_mae_min"],
        }
        for cohort in COHORTS
    }
    print(json.dumps(public_summary, ensure_ascii=False, allow_nan=False), flush=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the prespecified research-only conditional fusion gate comparison."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_experiment(args.workspace, args.output)


if __name__ == "__main__":
    main()
