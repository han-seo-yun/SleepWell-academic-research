"""Frozen general-model artifacts, external evaluation, and paired comparisons.

This module is deliberately additive.  It reuses the validated general engine's
component factories, but never changes a saved experiment or reads final-test
rows while fitting a frozen artifact.  ``skops`` loading is fail-closed: the
static trusted-type allow-list is empty, so any type skops itself reports as
untrusted is rejected rather than accepted from artifact metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import sklearn
import skops
import skops.io as sio
from sklearn.utils.class_weight import compute_sample_weight

from . import engine
from .data import fingerprint, load_data
from .metrics import classification_metrics, regression_metrics
from .storage import FORMAT_VERSION, Project


ARTIFACT_SCHEMA_VERSION = 1
EXTERNAL_RESULT_SCHEMA_VERSION = 1
EXTERNAL_INPUT_DESIGNATIONS = frozenset({"unknown", "declared_synthetic", "declared_observed"})
# skops 0.16 reports numpy.dtype for fitted sklearn pipelines.  It is the only
# non-sklearn constructor admitted here; artifact metadata cannot extend this
# source-controlled list.
STATIC_TRUSTED_TYPES: tuple[str, ...] = ("numpy.dtype",)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=_json_default).encode("utf-8")


def _immutable_copy(value: Any) -> Any:
    """Return a detached JSON-safe copy for immutable result provenance."""
    return json.loads(_canonical(value).decode("utf-8"))


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(f"not_json_serializable:{type(value).__name__}")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hash(path: Path) -> str:
    return _sha_file(path.resolve())


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return _sha_bytes(_canonical(payload))


def _atomic_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False, default=_json_default)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _schema(frame: pd.DataFrame, features: Sequence[str]) -> list[dict[str, Any]]:
    result = []
    for column in features:
        series = frame[column]
        numeric = bool(pd.api.types.is_numeric_dtype(series) and
                       not pd.api.types.is_bool_dtype(series))
        result.append({"name": column, "kind": "numeric" if numeric else "categorical",
                       "pandas_dtype": str(series.dtype)})
    return result


def _inverse_target(values: np.ndarray, transform: Mapping[str, Any]) -> np.ndarray:
    method = transform["method"]
    if method == "standard":
        return values * float(transform["scale"]) + float(transform["mean"])
    if method == "log1p":
        return np.expm1(values)
    if method != "none":
        raise ValueError("unsupported frozen target transform")
    return values


def _fit_components(df: pd.DataFrame, config: dict[str, Any], plan: dict[str, Any]) -> tuple[Any, Any, dict[str, Any], list[int], np.ndarray]:
    resolved = engine.resolve_config(df, config, plan)
    train_rows, y_train = engine._prepare_target(df, list(plan["train_rows"]), resolved)
    if not train_rows:
        raise ValueError("development data has no eligible target rows")
    _, _, classes = engine._development_target_and_classes(df, resolved, plan)
    x_train = engine._feature_frame(df, train_rows, list(resolved["features"]))
    preprocess = engine._preprocessor(x_train, resolved)
    transformed = preprocess.fit_transform(x_train)
    estimator = engine._model(resolved)
    target = engine._TargetTransform(resolved.get("target_transform", "none"))
    if resolved["task"] == "regression":
        target.fit(np.asarray(y_train, dtype=float))
        fit_y = target.transform(np.asarray(y_train, dtype=float))
    else:
        fit_y = y_train
    if resolved.get("model") == "mlp":
        epochs = int((resolved.get("params") or {}).get("epochs", 30))
        params = resolved.get("params") or {}
        if "epochs" in params:
            copied = dict(resolved)
            copied["params"] = {key: value for key, value in params.items() if key != "epochs"}
            estimator = engine._model(copied)
        weights = None
        if resolved["task"] == "classification" and resolved.get("class_weight") == "balanced":
            weights = compute_sample_weight("balanced", fit_y)
        for epoch in range(epochs):
            if resolved["task"] == "classification":
                kwargs: dict[str, Any] = {"classes": classes} if epoch == 0 else {}
                if weights is not None:
                    kwargs["sample_weight"] = weights
                estimator.partial_fit(transformed, fit_y, **kwargs)
            else:
                estimator.partial_fit(transformed, fit_y)
    else:
        estimator.fit(transformed, fit_y)
    transform = {"method": target.method, "mean": float(target.mean), "scale": float(target.scale)}
    return preprocess, estimator, transform, train_rows, classes


def _open_project_readonly(project_path: str | Path) -> Project:
    root = Path(project_path).expanduser().resolve()
    state_path, data_path = root / "project.json", root / "data.json"
    if not state_path.is_file() or not data_path.is_file():
        raise FileNotFoundError(f"invalid_project:{root}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported_project_version:{state.get('format_version')}")
    frame = pd.read_json(data_path, orient="table", precise_float=True).reset_index(drop=True)
    if fingerprint(frame) != state.get("data_hash"):
        raise ValueError("project_snapshot_hash_mismatch")
    return Project(root, frame, state)


def freeze_general(project_path: str | Path, run_id: str, *, artifact_name: str | None = None,
                   allow_validation_config: bool = False) -> dict[str, Any]:
    """Fit and freeze a terminal locked configuration on development rows only.

    Final-test runs are accepted by default.  A completed validation result can
    only be used when ``allow_validation_config=True``; this creates a new
    training artifact from that locked configuration and is never described as
    reuse of the validation model.
    """
    project = _open_project_readonly(project_path)
    try:
        matches = [record for record in project.experiments() if str(record.get("id")) == str(run_id)]
        if len(matches) != 1:
            raise ValueError("run_id was not found or is ambiguous")
        record = matches[0]
        if record.get("status") != "completed":
            raise ValueError("only a completed run can be frozen")
        phase = record.get("phase")
        if phase != "final_test" and not (phase == "validation" and allow_validation_config):
            raise ValueError("freeze requires a chosen final_test run; validation requires explicit opt-in")
        if record.get("data_hash") != project.state.get("data_hash"):
            raise ValueError("run data hash does not match the project snapshot")
        split_id = str(record.get("split_id", ""))
        plan = project.state.get("splits", {}).get(split_id)
        if not isinstance(plan, dict):
            raise ValueError("saved split plan was not found")
        config = dict(record.get("config") or {})
        preprocess, estimator, transform, training_rows, classes = _fit_components(
            project.df, config, plan
        )
        artifact_identity = {
            "project_data_hash": project.state["data_hash"], "run_id": str(run_id),
            "split_id": split_id, "config": config, "training_rows": training_rows,
        }
        artifact_id = "frozen_" + _sha_bytes(_canonical(artifact_identity))[:24]
        directory = project.path / "research" / "frozen" / artifact_id
        directory.mkdir(parents=True, exist_ok=True)
        model_path = directory / "model.skops"
        manifest_path = directory / "manifest.json"
        if model_path.exists() or manifest_path.exists():
            if not (model_path.is_file() and manifest_path.is_file()):
                raise ValueError("incomplete frozen artifact already exists")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            _validate_manifest(existing, manifest_path, model_path, check_runtime=True)
            return existing
        sio.dump({"preprocessor": preprocess, "estimator": estimator}, model_path)
        untrusted = sio.get_untrusted_types(file=model_path)
        unknown = sorted(set(untrusted) - set(STATIC_TRUSTED_TYPES))
        if unknown:
            model_path.unlink(missing_ok=True)
            raise ValueError(f"frozen model contains non-whitelisted types: {unknown}")
        manifest: dict[str, Any] = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "artifact_name": artifact_name or str(record.get("name") or run_id),
            "created_at": datetime.now().astimezone().isoformat(),
            "project_path": str(project.path),
            "source_run_id": str(run_id),
            "source_phase": phase,
            "training_semantics": ("chosen_final_test_configuration_refit_on_development"
                                   if phase == "final_test" else
                                   "locked_validation_configuration_new_training_artifact"),
            "task": config["task"], "config": config, "split_id": split_id,
            "project_data_hash": project.state["data_hash"],
            "training_row_ids": training_rows,
            "training_row_ids_sha256": _sha_bytes(_canonical(training_rows)),
            "excluded_development_target_missing": len(plan["train_rows"]) - len(training_rows),
            "final_test_row_ids_never_read_for_fit": list(plan["test_rows"]),
            "feature_schema": _schema(project.df, list(config["features"])),
            "target": config["target"], "target_unit": config.get("target_unit"),
            "target_transform": transform,
            "classes": [engine._native(value) for value in classes],
            "positive_class": config.get("positive_class"),
            "unseen_category_policy": ("ignore_with_all_zero_onehot_columns"
                                       if config.get("encoding", "onehot") == "onehot"
                                       else "ordinal_value_minus_one"),
            "model_file": model_path.name,
            "model_sha256": _sha_file(model_path),
            "static_trusted_types": list(STATIC_TRUSTED_TYPES),
            "versions": {"python": platform.python_version(), "numpy": np.__version__,
                         "pandas": pd.__version__, "scikit_learn": sklearn.__version__,
                         "skops": skops.__version__},
            "code_hashes": {"advanced_evaluation": _source_hash(Path(__file__)),
                            "engine": _source_hash(Path(engine.__file__))},
        }
        manifest["feature_schema_sha256"] = _sha_bytes(_canonical(manifest["feature_schema"]))
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        _atomic_json(manifest_path, manifest)
        return manifest
    finally:
        # Project.close() updates project.json. Freezing is intentionally
        # additive and must not rewrite the source project's saved contract.
        project._closed = True


def _validate_manifest(manifest: Mapping[str, Any], manifest_path: Path, model_path: Path,
                       *, check_runtime: bool) -> None:
    if manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported frozen artifact schema")
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise ValueError("frozen artifact manifest hash mismatch")
    if manifest.get("feature_schema_sha256") != _sha_bytes(_canonical(manifest.get("feature_schema"))):
        raise ValueError("frozen artifact feature schema hash mismatch")
    if manifest.get("training_row_ids_sha256") != _sha_bytes(_canonical(manifest.get("training_row_ids"))):
        raise ValueError("frozen artifact training row hash mismatch")
    if not model_path.is_file() or manifest.get("model_sha256") != _sha_file(model_path):
        raise ValueError("frozen model hash mismatch")
    if check_runtime:
        versions = manifest.get("versions") or {}
        if versions.get("scikit_learn") != sklearn.__version__ or versions.get("skops") != skops.__version__:
            raise ValueError("frozen artifact library version mismatch")
        hashes = manifest.get("code_hashes") or {}
        if hashes.get("advanced_evaluation") != _source_hash(Path(__file__)):
            raise ValueError("frozen artifact evaluator code hash mismatch")
        if hashes.get("engine") != _source_hash(Path(engine.__file__)):
            raise ValueError("frozen artifact engine code hash mismatch")
    untrusted = sio.get_untrusted_types(file=model_path)
    unknown = sorted(set(untrusted) - set(STATIC_TRUSTED_TYPES))
    if unknown:
        raise ValueError(f"frozen model contains non-whitelisted types: {unknown}")


def _load_artifact(artifact_path: str | Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    supplied = Path(artifact_path).expanduser().resolve()
    manifest_path = supplied / "manifest.json" if supplied.is_dir() else supplied
    if not manifest_path.is_file():
        raise FileNotFoundError(f"frozen artifact manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_path = manifest_path.parent / str(manifest.get("model_file", "model.skops"))
    _validate_manifest(manifest, manifest_path, model_path, check_runtime=True)
    loaded = sio.load(model_path, trusted=list(STATIC_TRUSTED_TYPES))
    if not isinstance(loaded, dict) or set(loaded) != {"preprocessor", "estimator"}:
        raise ValueError("frozen model payload is malformed")
    return manifest, loaded, manifest_path


def _resolved_artifact_project(manifest: Mapping[str, Any], manifest_path: Path) -> Path:
    """Resolve write authority from the artifact location, never its metadata path.

    A project may be moved as one directory tree.  In that case the obsolete
    recorded absolute path no longer exists and the validated project/data hash
    at the artifact's actual parent is authoritative.  A mismatch pointing at
    another existing path is rejected as an ambiguous redirect.
    """
    artifact_dir = manifest_path.parent.resolve()
    if (artifact_dir.name != str(manifest.get("artifact_id")) or
            artifact_dir.parent.name != "frozen" or
            artifact_dir.parent.parent.name != "research"):
        raise ValueError("frozen artifact is outside project/research/frozen/artifact_id")
    project_root = artifact_dir.parent.parent.parent.resolve()
    recorded_value = manifest.get("project_path")
    if not isinstance(recorded_value, str) or not recorded_value.strip():
        raise ValueError("frozen artifact project path is missing")
    recorded_root = Path(recorded_value).expanduser().resolve()
    if recorded_root != project_root and recorded_root.exists():
        raise ValueError("frozen artifact project path redirects to another existing location")
    project = _open_project_readonly(project_root)
    try:
        if project.state.get("data_hash") != manifest.get("project_data_hash"):
            raise ValueError("frozen artifact does not match the containing project data hash")
    finally:
        project._closed = True
    return project_root


def _strict_external_frame(frame: pd.DataFrame, manifest: Mapping[str, Any]) -> pd.DataFrame:
    required = [item["name"] for item in manifest["feature_schema"]] + [manifest["target"]]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"external data is missing required columns: {missing}")
    checked = frame.copy()
    for item in manifest["feature_schema"]:
        column = item["name"]
        if item["kind"] == "numeric":
            converted = pd.to_numeric(checked[column], errors="coerce")
            failed = checked[column].notna() & converted.isna()
            if failed.any():
                raise ValueError(f"external numeric feature has invalid values: {column}")
            checked[column] = converted
        else:
            checked[column] = checked[column].astype(object).where(checked[column].notna(), np.nan)
    return checked


def _calibration(y_true: np.ndarray, probabilities: np.ndarray, classes: np.ndarray,
                 positive_class: Any, bins: int = 10) -> dict[str, Any]:
    if len(classes) != 2:
        return {"available": False, "reason": "binary_classification_only"}
    matches = np.flatnonzero(classes == positive_class)
    if not len(matches):
        raise ValueError("positive class is absent from frozen classes")
    scores = probabilities[:, int(matches[0])]
    truth = np.asarray([1.0 if value == positive_class else 0.0 for value in y_true])
    edges = np.linspace(0.0, 1.0, bins + 1)
    indices = np.minimum(np.digitize(scores, edges[1:-1], right=False), bins - 1)
    rows = []
    for number in range(bins):
        mask = indices == number
        rows.append({"bin": number + 1, "lower": float(edges[number]),
                     "upper": float(edges[number + 1]), "n": int(mask.sum()),
                     "mean_probability": float(scores[mask].mean()) if mask.any() else None,
                     "observed_rate": float(truth[mask].mean()) if mask.any() else None})
    return {"available": True, "method": "equal_width_10_bins",
            "brier_score": float(np.mean((scores - truth) ** 2)), "bins": rows,
            "n": int(len(truth)), "positive_class": engine._native(positive_class)}


def _residual_summary(y_true: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    residual = np.asarray(y_true, dtype=float) - np.asarray(predicted, dtype=float)
    return {"definition": "observed_minus_predicted_original_unit", "n": int(len(residual)),
            "mean": float(np.mean(residual)), "std": float(np.std(residual, ddof=1)) if len(residual) > 1 else None,
            "median": float(np.median(residual)), "q05": float(np.quantile(residual, 0.05)),
            "q95": float(np.quantile(residual, 0.95))}


def _metric_units(task: str, target_unit: Any) -> dict[str, str]:
    if task == "classification":
        return {name: "dimensionless" for name in (
            "roc_auc", "average_precision", "accuracy", "balanced_accuracy",
            "recall", "specificity", "precision", "f1", "brier_score",
        )}
    unit = str(target_unit).strip() if target_unit not in (None, "") else "target_unit"
    return {"mae": unit, "mse": f"({unit})²", "rmse": unit, "r2": "dimensionless"}


def evaluate_external(artifact_path: str | Path, external_path: str | Path, study_label: str,
                      *, subgroup: str | None = None,
                      selection_after_external: bool = False,
                      input_designation: str = "unknown") -> dict[str, Any]:
    """Evaluate a frozen artifact on a separate CSV/XLSX file without refitting."""
    if not str(study_label).strip():
        raise ValueError("study_label is required")
    input_designation = str(input_designation).strip()
    if input_designation not in EXTERNAL_INPUT_DESIGNATIONS:
        raise ValueError(
            "input_designation must be unknown, declared_synthetic, or declared_observed"
        )
    manifest, objects, manifest_path = _load_artifact(artifact_path)
    project_root = _resolved_artifact_project(manifest, manifest_path)
    source = Path(external_path).expanduser().resolve()
    frame = _strict_external_frame(load_data(source), manifest)
    if subgroup and subgroup not in frame.columns:
        raise ValueError("subgroup column was not found")
    config = dict(manifest["config"])
    if config["task"] == "regression" or config.get("bin_edges"):
        original_target = frame[config["target"]]
        numeric_target = pd.to_numeric(original_target, errors="coerce")
        invalid = original_target.notna() & (
            numeric_target.isna() | ~np.isfinite(numeric_target.to_numpy(dtype=float, na_value=np.nan))
        )
        if invalid.any():
            raise ValueError("external numerical target has invalid or non-finite values")
        frame[config["target"]] = numeric_target
    rows, y_true = engine._prepare_target(frame, list(range(len(frame))), config)
    if not rows:
        raise ValueError("external data has no eligible target rows")
    if config["task"] == "classification" and not config.get("bin_edges"):
        unknown = [value for value in pd.unique(y_true).tolist()
                   if not any(value == known for known in manifest["classes"])]
        if unknown:
            raise ValueError(f"external target contains unseen classes: {unknown}")
    x = engine._feature_frame(frame, rows, list(config["features"]))
    transformed = objects["preprocessor"].transform(x)
    estimator = objects["estimator"]
    probabilities: np.ndarray | None = None
    if config["task"] == "classification":
        classes = np.asarray(manifest["classes"])
        probabilities = engine._aligned_probabilities(estimator, transformed, classes)
        metrics, reasons, predicted = classification_metrics(
            y_true, probabilities, classes, positive_class=config.get("positive_class"),
            threshold=float(config.get("threshold", 0.5)), average=config.get("average", "macro"),
        )
        calibration = (_calibration(y_true, probabilities, classes, config.get("positive_class"))
                       if probabilities is not None else
                       {"available": False, "reason": "probability_unavailable"})
        residual = None
    else:
        raw = np.asarray(estimator.predict(transformed), dtype=float)
        predicted = _inverse_target(raw, manifest["target_transform"])
        metrics, reasons = regression_metrics(y_true, predicted)
        calibration = None
        residual = _residual_summary(np.asarray(y_true, dtype=float), predicted)
    predictions = []
    for index, row_id in enumerate(rows):
        item = {"row_id": int(row_id), "y_true": engine._native(y_true[index]),
                "y_pred": engine._native(predicted[index]),
                "probabilities": ([float(value) for value in probabilities[index]]
                                  if probabilities is not None else None)}
        if subgroup:
            item["subgroup"] = engine._native(frame.iloc[row_id][subgroup])
        predictions.append(item)
    subgroup_rows = []
    if subgroup:
        values = frame.iloc[rows][subgroup]
        for value in pd.unique(values).tolist():
            mask = values.isna().to_numpy() if pd.isna(value) else (values == value).to_numpy()
            indices = np.flatnonzero(mask)
            if config["task"] == "classification":
                group_metrics, group_reasons, _ = classification_metrics(
                    np.asarray(y_true)[indices], probabilities[indices] if probabilities is not None else None,
                    np.asarray(manifest["classes"]), positive_class=config.get("positive_class"),
                    threshold=float(config.get("threshold", 0.5)), average=config.get("average", "macro"))
            else:
                group_metrics, group_reasons = regression_metrics(
                    np.asarray(y_true)[indices], np.asarray(predicted)[indices])
            subgroup_rows.append({"value": None if pd.isna(value) else engine._native(value),
                                  "n": int(len(indices)), "small_group": len(indices) < 20,
                                  "small_group_threshold": 20,
                                  "metrics": group_metrics, "reasons": group_reasons})
    external_hash = _sha_file(source)
    identity = {"artifact": manifest["manifest_sha256"], "external": external_hash,
                "study_label": study_label, "subgroup": subgroup,
                "selection_after_external": bool(selection_after_external),
                "input_designation": input_designation}
    result_id = "external_" + _sha_bytes(_canonical(identity))[:24]
    frozen_config = _immutable_copy(config)
    evaluation_scope = ("external_after_model_selection"
                        if selection_after_external else "external_frozen_no_selection")
    result = {
        "schema_version": EXTERNAL_RESULT_SCHEMA_VERSION, "id": result_id,
        "status": "completed", "kind": "frozen_external_evaluation",
        "created_at": datetime.now().astimezone().isoformat(), "study_label": study_label,
        "name": study_label, "phase": "external_evaluation",
        "artifact_id": manifest["artifact_id"], "artifact_manifest_sha256": manifest["manifest_sha256"],
        "source_run_id": manifest["source_run_id"], "task": config["task"],
        "config": frozen_config, "data_hash": manifest["project_data_hash"],
        "split_id": manifest["split_id"], "versions": _immutable_copy(manifest["versions"]),
        "external_path": str(source), "external_sha256": external_hash,
        "input_designation": input_designation,
        "selection_after_external": bool(selection_after_external),
        "selection_note": ("External data influenced selection; recorded as a new evaluation version and not an untouched external validation."
                           if selection_after_external else
                           "Frozen configuration evaluated without refit or external-data model selection."),
        "n_source_rows": int(len(frame)), "n_evaluated": int(len(rows)),
        "excluded_target_missing_or_invalid": int(len(frame) - len(rows)),
        "metrics": metrics, "reasons": reasons, "classes": manifest["classes"],
        "target_unit": manifest.get("target_unit"),
        "metric_units": _metric_units(config["task"], manifest.get("target_unit")),
        "calibration": calibration,
        "residual_summary": residual, "subgroup_column": subgroup, "subgroups": subgroup_rows,
        "subgroup_uncertainty": {
            "available": False,
            "reason": "independent_resampling_unit_not_provided",
            "note": ("Subgroup rows are descriptive point estimates only. Row-level IID intervals are not "
                     "created because repeated observations may belong to the same participant."),
        } if subgroup else None,
        "predictions": predictions,
        "target_metadata": {
            "column": manifest["target"],
            "unit": manifest.get("target_unit"),
            "transform": _immutable_copy(manifest["target_transform"]),
            "positive_class": engine._native(manifest.get("positive_class")),
            "classes": _immutable_copy(manifest["classes"]),
        },
        "model_metadata": {
            "model": config.get("model"),
            "params": _immutable_copy(config.get("params") or {}),
            "artifact_id": manifest["artifact_id"],
            "artifact_manifest_sha256": manifest["manifest_sha256"],
            "model_sha256": manifest["model_sha256"],
        },
        "evaluation_metadata": {
            "scope": evaluation_scope,
            "input_designation": input_designation,
            "selection_after_external": bool(selection_after_external),
            "refit": False,
            "n_source_rows": int(len(frame)),
            "n_evaluated": int(len(rows)),
            "excluded_target_missing_or_invalid": int(len(frame) - len(rows)),
            "subgroup_column": subgroup,
        },
        "provenance": {
            "source_run": {
                "id": manifest["source_run_id"],
                "phase": manifest["source_phase"],
                "split_id": manifest["split_id"],
                "project_data_hash": manifest["project_data_hash"],
                "training_semantics": manifest["training_semantics"],
                "training_row_ids_sha256": manifest["training_row_ids_sha256"],
                "config": frozen_config,
            },
            "frozen_artifact": {
                "id": manifest["artifact_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "model_sha256": manifest["model_sha256"],
                "feature_schema_sha256": manifest["feature_schema_sha256"],
                "versions": _immutable_copy(manifest["versions"]),
                "code_hashes": _immutable_copy(manifest["code_hashes"]),
            },
            "external_input": {
                "sha256": external_hash,
                "designation": input_designation,
                "designation_source": "user_explicit" if input_designation != "unknown" else "default_unknown",
            },
        },
        "methods": {"refit": False, "preprocessing": "frozen", "estimator": "frozen",
                    "target_transform_inverse": manifest["target_transform"],
                    "unseen_category_policy": manifest["unseen_category_policy"]},
    }
    result["result_sha256"] = _sha_bytes(_canonical(result))
    output = project_root / "research" / "external" / f"{result_id}.json"
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        invariant = ("id", "artifact_manifest_sha256", "external_sha256", "study_label",
                     "selection_after_external", "subgroup_column", "input_designation")
        if any(existing.get(key) != result.get(key) for key in invariant):
            raise ValueError("immutable external result ID collision")
        return existing
    _atomic_json(output, result)
    return result


def _result(value: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return json.loads(Path(value).expanduser().resolve().read_text(encoding="utf-8"))


def _metric_value(rows: Sequence[Mapping[str, Any]], metric: str) -> float:
    truth = np.asarray([row["y_true"] for row in rows])
    predicted = np.asarray([row["y_pred"] for row in rows])
    if metric == "accuracy":
        return float(np.mean(truth == predicted))
    y = truth.astype(float)
    pred = predicted.astype(float)
    if metric == "mae":
        return float(np.mean(np.abs(y - pred)))
    if metric == "mse":
        return float(np.mean((y - pred) ** 2))
    if metric == "rmse":
        return float(np.sqrt(np.mean((y - pred) ** 2)))
    raise ValueError("paired metric must be one of accuracy, mae, mse, rmse")


def _validate_paired_rows(rows: Sequence[Mapping[str, Any]], metric: str, unit_id: str,
                          side: str) -> None:
    for index, row in enumerate(rows):
        if unit_id not in row or row.get(unit_id) is None:
            raise ValueError(f"{side} paired unit IDs must be present and unique")
        if "y_true" not in row or "y_pred" not in row:
            raise ValueError(f"{side} paired prediction {index} is missing y_true or y_pred")
        values_to_check = (row.get("y_true"), row.get("y_pred"))
        if any(value is None or (np.isscalar(value) and bool(pd.isna(value)))
               for value in values_to_check):
            raise ValueError(f"{side} paired predictions contain missing labels or predictions")
        if metric != "accuracy":
            try:
                values = np.asarray([row["y_true"], row["y_pred"]], dtype=float)
            except (TypeError, ValueError):
                raise ValueError(f"{side} paired regression predictions must be numeric") from None
            if not np.isfinite(values).all():
                raise ValueError(f"{side} paired regression predictions must be finite")


def paired_compare(left_result: str | Path | Mapping[str, Any],
                   right_result: str | Path | Mapping[str, Any], *, metric: str,
                   unit_id: str = "row_id", participant_ids: Mapping[Any, Any] | None = None,
                   ordered: bool = False, bootstrap_repeats: int = 1000, seed: int = 42,
                   block_length: int | None = None) -> dict[str, Any]:
    """Paired comparison with participant-cluster or moving-block bootstrap.

    The two prediction sets must contain exactly the same unique unit IDs.
    Stored reports without raw predictions are rejected; no interval is inferred
    from aggregate metrics or fold standard deviations.
    """
    left, right = _result(left_result), _result(right_result)
    left_rows, right_rows = left.get("predictions"), right.get("predictions")
    if not left_rows or not right_rows:
        raise ValueError("paired comparison requires raw predictions")
    _validate_paired_rows(left_rows, metric, unit_id, "left")
    _validate_paired_rows(right_rows, metric, unit_id, "right")
    left_map = {row.get(unit_id): row for row in left_rows}
    right_map = {row.get(unit_id): row for row in right_rows}
    if None in left_map or None in right_map or len(left_map) != len(left_rows) or len(right_map) != len(right_rows):
        raise ValueError("paired unit IDs must be present and unique")
    if set(left_map) != set(right_map):
        raise ValueError("paired comparison requires identical evaluation unit IDs")
    ids = [row[unit_id] for row in left_rows]
    paired_left = [left_map[value] for value in ids]
    paired_right = [right_map[value] for value in ids]
    if any(a["y_true"] != b["y_true"] for a, b in zip(paired_left, paired_right)):
        raise ValueError("paired predictions disagree on observed outcomes")
    if bootstrap_repeats < 100:
        raise ValueError("bootstrap_repeats must be at least 100")
    left_task, right_task = left.get("task"), right.get("task")
    if left_task and right_task and left_task != right_task:
        raise ValueError("paired comparison requires the same task")
    if metric == "accuracy" and any(task and task != "classification" for task in (left_task, right_task)):
        raise ValueError("accuracy paired comparison requires classification results")
    if metric != "accuracy" and any(task and task != "regression" for task in (left_task, right_task)):
        raise ValueError("error-metric paired comparison requires regression results")
    left_unit, right_unit = left.get("target_unit"), right.get("target_unit")
    if left_unit not in (None, "") and right_unit not in (None, "") and left_unit != right_unit:
        raise ValueError("paired comparison requires the same target unit")
    rng = np.random.default_rng(int(seed))

    def delta(indices: Sequence[int]) -> float:
        a = _metric_value([paired_left[index] for index in indices], metric)
        b = _metric_value([paired_right[index] for index in indices], metric)
        return b - a

    samples: list[float] = []
    if participant_ids is not None:
        missing_ids = [value for value in ids if value not in participant_ids]
        if missing_ids:
            raise ValueError("participant mapping is missing paired unit IDs")
        labels = [participant_ids[value] for value in ids]
        if any(value is None or str(value).strip() == "" for value in labels):
            raise ValueError("participant mapping contains missing participant IDs")
        participants = list(dict.fromkeys(labels))
        by_participant = {participant: [index for index, label in enumerate(labels) if label == participant]
                          for participant in participants}
        independent_units = len(participants)
        if independent_units < 2:
            method = "paired_point_estimate_no_interval"
            method_notes = ("Only one independent participant was available; a paired point estimate is "
                            "reported without a confidence interval.")
        else:
            for _ in range(bootstrap_repeats):
                chosen = rng.choice(participants, size=len(participants), replace=True)
                indices = [index for participant in chosen for index in by_participant[participant]]
                samples.append(delta(indices))
            method = "paired_participant_cluster_bootstrap"
            method_notes = "Participants are the independent resampling units; rows remain clustered."
    elif ordered:
        if block_length is None or isinstance(block_length, bool) or int(block_length) < 2:
            raise ValueError("ordered series requires an explicit block_length of at least 2")
        length = int(block_length)
        if len(ids) < 2 * length:
            raise ValueError("ordered series needs at least two moving blocks")
        starts = np.arange(0, len(ids) - length + 1)
        for _ in range(bootstrap_repeats):
            indices: list[int] = []
            while len(indices) < len(ids):
                start = int(rng.choice(starts))
                indices.extend(range(start, start + length))
            samples.append(delta(indices[:len(ids)]))
        method = "paired_moving_block_bootstrap"
        independent_units = len(starts)
        method_notes = "A single ordered series was resampled in contiguous moving blocks; block length was user specified."
    else:
        raise ValueError("specify participant_ids or ordered=True; row independence is not assumed")
    left_value = _metric_value(paired_left, metric)
    right_value = _metric_value(paired_right, metric)
    observed = right_value - left_value
    interval = ([float(value) for value in np.quantile(np.asarray(samples), [0.025, 0.975])]
                if samples else None)
    higher_better = metric == "accuracy"
    target_unit = left_unit if left_unit not in (None, "") else right_unit
    metric_unit = ("dimensionless" if metric == "accuracy" else
                   _metric_units("regression", target_unit)[metric])
    return {"metric": metric, "left": left_value, "right": right_value,
            "delta_right_minus_left": observed,
            "improvement": observed if higher_better else -observed,
            "improvement_direction": "higher_is_better" if higher_better else "lower_is_better",
            "n_paired_rows": len(ids), "n_independent_units": independent_units,
            "unit_id": unit_id, "method": method, "bootstrap_repeats": bootstrap_repeats,
            "seed": int(seed), "block_length": int(block_length) if block_length is not None else None,
            "metric_unit": metric_unit,
            "delta_ci_95_percentile": interval,
            "uncertainty_available": interval is not None,
            "uncertainty_reason": None if interval is not None else "fewer_than_two_independent_participants",
            "method_notes": method_notes,
            "paired_ids_sha256": _sha_bytes(_canonical(ids))}
