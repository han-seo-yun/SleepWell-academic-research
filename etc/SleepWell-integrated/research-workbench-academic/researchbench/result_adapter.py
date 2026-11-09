"""Read-only normalized views over both original experiment storage contracts."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path

SCHEMA_VERSION = 1
LOWER = {"mae", "mse", "rmse", "brier_score", "onset_mae_min", "wake_mae_min", "joint_mae_min", "interval_mae_min", "onset_rmse_min", "wake_rmse_min"}
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#6A3D9A", "#777777"]

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def resolve_field(record, path):
    value = record
    for part in path:
        value = value[part]
    return value

def model_color(model_id):
    return PALETTE[int(hashlib.sha256(str(model_id).encode()).hexdigest()[:8], 16) % len(PALETTE)]

def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def _scope_ids(predictions, academic=False):
    if not academic:
        return sorted({str(p["row_id"]) for p in predictions if "row_id" in p})
    by_model = {}
    for p in predictions:
        if not all(_finite(p.get(k)) for k in ("onset_offset_min", "wake_offset_min", "actual_onset_offset_min", "actual_wake_offset_min")): continue
        by_model.setdefault(p.get("model_id"), set()).add((str(p.get("participant_id")), str(p.get("cutoff"))))
    return sorted(set.intersection(*by_model.values())) if by_model else []

def _adapt_external(record, project_path):
    """View additive frozen-pipeline evaluations without altering their source."""
    payload = {k: v for k, v in record.items() if k != 'result_sha256'}
    if digest(payload) != record.get('result_sha256'):
        raise ValueError('external_result_hash_mismatch')
    config = copy.deepcopy(record.get('frozen_config') or record.get('config') or {})
    predictions = copy.deepcopy(record.get('predictions') or [])
    ids = _scope_ids(predictions)
    designation = record.get('input_designation', 'unknown')
    synthetic = {'declared_synthetic': True, 'declared_observed': False}.get(designation)
    phase = 'external_selected' if record.get('selection_after_external') else 'external'
    evaluation = {
        'task': record.get('task'), 'target': config.get('target'),
        'target_rule': config.get('target_rule'),
        'split_id': 'external:' + str(record.get('external_sha256')),
        'evaluation_type': phase, 'synthetic': synthetic,
        'input_designation': designation, 'calculation': 'computed',
        'population': 'sample' if synthetic else 'user_declared_external_dataset',
        'scope_ids': ids, 'scope_hash': digest(ids) if ids else None,
        'positive_class': config.get('positive_class'), 'threshold': config.get('threshold'),
        'average': config.get('average'), 'averaging': 'pooled_external_rows',
        'selection_after_external': bool(record.get('selection_after_external')),
    }
    metrics = []
    for key, value in record.get('metrics', {}).items():
        metrics.append({
            'id': digest([record['id'], key, ['metrics', key]])[:24],
            'model_id': config.get('model', 'unknown'), 'metric': key,
            'value': value if _finite(value) else None,
            'unit': record.get('metric_units', {}).get(key, 'dimensionless'),
            'direction': 'lower' if key in LOWER else 'higher',
            'n_rows': record.get('n_evaluated'), 'n_participants': None,
            'variation': None, 'reason': record.get('reasons', {}).get(key) if _finite(value)
                else record.get('reasons', {}).get(key, 'not_available_in_original_result'),
            'field_path': ['metrics', key],
        })
    warnings = []
    if not config: warnings.append('legacy_external_config_unavailable')
    if synthetic is None: warnings.append('external_data_origin_not_declared')
    if synthetic: warnings.append('synthetic_software_validation_only')
    if record.get('selection_after_external'): warnings.append('external_data_used_for_selection')
    warnings.append('external_dataset_independence_requires_user_verification')
    root = Path(project_path).resolve() if project_path else None
    return {'schema': SCHEMA_VERSION, 'engine': 'general', 'run_id': record['id'],
        'name': record.get('study_label') or record['id'], 'config': config,
        'data_hash': record.get('external_sha256'), 'result_hash': digest(record),
        'source_file': str(root / 'research' / 'external' / (record['id'] + '.json')) if root else None,
        'project_path': str(root) if root else None, 'evaluation': evaluation,
        'provenance': copy.deepcopy(record.get('provenance') or {
            'artifact_id': record.get('artifact_id'), 'source_run_id': record.get('source_run_id'),
            'artifact_manifest_sha256': record.get('artifact_manifest_sha256')}),
        'metrics': metrics, 'predictions': predictions, 'duration': None, 'warnings': warnings}

def adapt_run(record, engine, project_path=""):
    if record.get("status") != "completed": raise ValueError("completed_run_required")
    if record.get('kind') == 'frozen_external_evaluation':
        if engine != 'general': raise ValueError('external_evaluation_engine_mismatch')
        return _adapt_external(record, project_path)
    result = record.get("result") or record
    config = record.get("config") or result.get("config") or {}
    academic = engine == "sleepwell"
    raw = result.get("raw", {}) if academic else result
    original_predictions=raw.get("predictions")
    predictions = copy.deepcopy(original_predictions) if isinstance(original_predictions,list) and all(isinstance(p,dict) for p in original_predictions) else []
    phase = result.get("mode", config.get("mode")) if academic else result.get("phase", "validation")
    controlled_study = academic and phase in {
        "pooled_retraining", "controlled_comparison", "cross_cohort_evaluation",
        "participant_revalidation", "personalization_comparison",
    }
    participant_revalidation = academic and phase in {
        "participant_revalidation", "personalization_comparison",
    }
    historical = bool(result.get("provenance", {}).get("stored_artifact")) or str(result.get("provenance", {}).get("execution", "")).startswith("stored")
    if controlled_study:
        historical = True
    if academic and phase == "report":
        historical = historical or config.get('report_type')=='population_prior'
        historical = historical or not result.get("provenance", {}).get("executed", True) or result.get("provenance", {}).get("report_source") == "stored_artifact"
        historical = historical or any("stored" in str(w).lower() or "historical" in str(w).lower() for w in result.get("warnings", []))
    synthetic = config.get("input_synthetic", config.get("synthetic"))
    if phase == "prospective_demo" or config.get("input_mode") == "synthetic": synthetic = True
    ids = _scope_ids(predictions, academic)
    data_hash = config.get("data_hash") or result.get("data_hash") or (result.get("provenance") or {}).get("input_sha256")
    aggregate_scope = None
    verification = raw.get("verification", {})
    verified_scope = verification.get("database_unchanged") is True or (
        participant_revalidation
        and verification.get("participant_overlap") == 0
        and verification.get("oof_once_per_repeat") is True
        and verification.get("same_nights_all_models") is True
    )
    if controlled_study and verified_scope and data_hash:
        summary_scope = sorted(
            (str(row.get("model_id")), str(row.get("cohort")), str(row.get("split")),
             row.get("n_nights"), row.get("n_participants"))
            for row in (result.get("summary") or []) if isinstance(row, dict)
        )
        expected_split = "out_of_fold" if participant_revalidation else "test"
        if summary_scope and all(item[2] == expected_split for item in summary_scope):
            aggregate_scope = digest({
                "kind": ("verified_grouped_out_of_fold_scope" if participant_revalidation else
                         "verified_same_run_aggregate_test_scope"),
                "run_id": record.get("id"), "data_hash": data_hash,
                "status": raw.get("status"), "summary_scope": summary_scope,
            })
    aggregate_rows = next(
        (row for row in (result.get("summary") or [])
         if isinstance(row, dict) and str(row.get("cohort")).upper() == "MACRO"),
        {},
    ) if controlled_study else {}
    target = "sleep_onset_and_wake" if academic else config.get("target")
    evaluation = {"task": "sleep_timing" if academic else result.get("task", config.get("task")),
        "target": target, "target_rule": result.get("target_rule"),
        "split_id": result.get("split_id") or ("out_of_fold" if participant_revalidation else
                                                 "test" if controlled_study else None),
        "evaluation_type": phase, "synthetic": synthetic,
        "calculation": ("retrospective_grouped_cv" if participant_revalidation else
                        "historical_reuse" if historical else "computed"),
        "population": ("cmi_mesa_all_participant_grouped_cv" if participant_revalidation else
                       "cmi_mesa_reused_test" if controlled_study else
                       "sample" if synthetic else "unspecified"),
        "scope_ids": ids, "scope_hash": aggregate_scope or (digest(ids) if ids else None),
        "scope_basis": ("verified_grouped_out_of_fold_scope" if aggregate_scope and participant_revalidation else
                       "verified_same_run_aggregate_test_scope" if aggregate_scope else
                       "participant_cutoff_ids" if ids else None),
        "scope_identifiers_exported": bool(ids),
        "aggregate_n_nights": aggregate_rows.get("n_nights"),
        "aggregate_n_participants": aggregate_rows.get("n_participants"),
        "positive_class": config.get("positive_class"), "threshold": config.get("threshold"), "average": config.get("average"),
        "averaging": ("equal_participant_with_equal_cohort_macro" if controlled_study else
                      "participant_equal" if academic else "fold_mean")}
    metrics = []
    def add(model, key, value, path, n, people, variation=None, reason=None, unit=None):
        if unit is None:
            unit = "min" if key.endswith("_min") else "dimensionless"
            if evaluation["task"] == "regression" and key != "r2":
                base_unit=config.get("target_unit") or "unspecified"
                unit=f"({base_unit})²" if key=="mse" else base_unit
        metrics.append({"id": digest([record["id"], model, key, path])[:24], "model_id": model, "metric": key,
            "value": value if _finite(value) else None, "unit": unit,
            "direction": "lower" if key in LOWER else "higher", "n_rows": n, "n_participants": people,
            "variation": variation, "reason": reason if _finite(value) else (reason or "not_available_in_original_result"), "field_path": path})
    prefix = ["result"] if "result" in record else []
    if academic:
        for index, row in enumerate(result.get("summary") or []):
            for key, value in row.items():
                if (key.endswith("_min") and "mae" in key or key.endswith("rmse_min")):
                    variation = None
                    if controlled_study and key == "joint_mae_min" and _finite(row.get("ci95_low")) and _finite(row.get("ci95_high")):
                        variation = {
                            "kind": "participant_cluster_bootstrap_ci",
                            "ci95_low": row["ci95_low"], "ci95_high": row["ci95_high"],
                            "n": row.get("n_participants"), "is_confidence_interval": True,
                            "conditional_on_fitted_model": not participant_revalidation,
                            "conditional_on_fixed_procedure": participant_revalidation,
                            "includes_training_selection_uncertainty": False,
                        }
                    add(row.get("model_id", "unknown"), key, value, prefix+["summary", index, key], row.get("n_nights"), row.get("n_participants"), variation=variation, unit="min")
    else:
        reasons = {k: v for fold in result.get("folds", []) for k, v in fold.get("reasons", {}).items()}
        for key, value in (result.get("summary") or {}).items():
            add(config.get("model", "unknown"), key, value.get("mean"), prefix+["summary", key, "mean"], len(ids), None,
                {"kind":"fold_sd", "sd":value.get("std"), "n":value.get("n"), "is_confidence_interval":False}, reasons.get(key))
    return {"schema": SCHEMA_VERSION, "engine": engine, "run_id": record["id"], "name": record.get("name", record["id"]),
        "config": copy.deepcopy(config), "data_hash": data_hash,
        "result_hash": digest(record), "source_file": str(Path(project_path).resolve() / ("runs" if academic else "experiments") / (record["id"]+".json")) if project_path else None,
        "project_path": str(Path(project_path).resolve()) if project_path else None, "evaluation": evaluation,
        "provenance": copy.deepcopy(result.get("provenance") or {"versions":result.get("versions"), "device":result.get("device")}),
        "metrics": metrics, "predictions": predictions, "duration": record.get("duration", result.get("duration")), "warnings":result.get("warnings", [])}

def load_runs(project_path):
    root = Path(project_path).resolve()
    state = json.loads((root / "project.json").read_text(encoding="utf-8"))
    academic = state.get("schema") == 1 and "run_ids" in state
    if not academic and state.get("format_version") != 1: raise ValueError("unsupported_project")
    if academic:
        from .academic_store import AcademicProject
        records = AcademicProject(root, state).runs()
        for record in records:
            config = record.get("config", {})
            if config.get("input_snapshot"):
                path = AcademicProject(root, state).snapshot_path(config["input_snapshot"])
                if config.get("data_hash") and hashlib.sha256(path.read_bytes()).hexdigest() != config["data_hash"]: raise ValueError("input_hash_mismatch")
    else:
        import pandas as pd
        from .data import fingerprint
        if fingerprint(pd.read_json(root/"data.json", orient="table", precise_float=True)) != state["data_hash"]: raise ValueError("input_hash_mismatch")
        records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root/"experiments").glob("*.json"))]
    views = [adapt_run(record, "sleepwell" if academic else "general", root) for record in records if record.get("status") == "completed"]
    if not academic:
        for path in sorted((root / 'research' / 'external').glob('*.json')):
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('kind') != 'frozen_external_evaluation' or path.stem != record.get('id'):
                raise ValueError('external_result_identity_mismatch')
            views.append(adapt_run(record, 'general', root))
    return views

def verify_run(view):
    path = Path(view["source_file"])
    root = Path(view["project_path"]).resolve()
    if not path.resolve().is_relative_to(root): raise ValueError("run_outside_project")
    record = json.loads(path.read_text(encoding="utf-8"))
    if digest(record) != view["result_hash"]: raise ValueError("result_hash_mismatch")
    if "metrics" in view and "engine" in view:
        canonical=adapt_run(record,view['engine'],root)
        for field in ('run_id','metrics','evaluation','config','data_hash','predictions','provenance','warnings'):
            if view.get(field)!=canonical.get(field): raise ValueError('normalized_view_mismatch:'+field)
    return record

def compare(left, right, metric, model_id=None):
    a = next((m for m in left["metrics"] if m["metric"] == metric and (model_id is None or m["model_id"] == model_id)), None)
    b = next((m for m in right["metrics"] if m["metric"] == metric and (model_id is None or m["model_id"] == model_id)), None)
    reasons=[]
    for key in ("task","target","target_rule","evaluation_type","synthetic","positive_class","threshold","average","averaging"):
        if left["evaluation"].get(key) != right["evaluation"].get(key): reasons.append(key)
    if left["engine"] != right["engine"]: reasons.append("engine")
    if not a or not b: reasons.append("missing_metric")
    if a and b and a["unit"] != b["unit"]: reasons.append("unit")
    state = "incomparable" if reasons else "comparable"
    for key in ("data_hash",):
        if not left.get(key) or left.get(key) != right.get(key): reasons.append(key)
    for key in ("scope_hash", "split_id"):
        if (key == "scope_hash" and not left["evaluation"].get(key)) or left["evaluation"].get(key) != right["evaluation"].get(key): reasons.append(key)
    if not left['evaluation'].get('target') or not right['evaluation'].get('target'):
        reasons.append('target_unknown')
    if left["evaluation"]["calculation"] == "historical_reuse" or right["evaluation"]["calculation"] == "historical_reuse": reasons.append("historical_reuse")
    if state != "incomparable" and reasons: state="reference_only"
    delta = b["value"] - a["value"] if state == "comparable" and a["value"] is not None and b["value"] is not None else None
    diffs={k:{"baseline":left["config"].get(k),"candidate":right["config"].get(k)} for k in left["config"].keys()|right["config"].keys() if left["config"].get(k)!=right["config"].get(k)}
    return {"status":state,"reasons":reasons,"baseline":a,"candidate":b,"delta":delta,"improvement":(-delta if a["direction"]=="lower" else delta) if delta is not None else None,"config_diff":diffs,
        "delta_unit":"percentage_points" if a and a["unit"]=="dimensionless" and metric not in {"r2"} else (a or {}).get("unit"),
        "percentage_points":delta*100 if delta is not None and metric in {"accuracy","balanced_accuracy","recall","specificity","precision","f1","roc_auc","average_precision"} else None}
