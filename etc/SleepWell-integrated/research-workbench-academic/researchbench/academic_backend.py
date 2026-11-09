"""Verified adapter for the bundled SleepWell academic experiment engine.

The adapter deliberately does not translate the original calculations into
scikit-learn estimators.  It verifies and imports the exact vendored source,
then returns the original result under ``raw`` together with a small table for
the native desktop UI.
"""
from __future__ import annotations

import csv
import hashlib
import importlib
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


_VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "sleepwell"
_MANIFEST_PATH = _VENDOR_ROOT / "SOURCE_MANIFEST.json"
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMPORT_LOCK = threading.RLock()
_REPORTS = {
    "continuous_constants_validation_v2": "continuous_constants_validation_v2.json",
    "full_validity_tables_v1": "full_validity_tables_v1.json",
    "personalization_validation_v1": "personalization_validation_v1.json",
    "target_validity_validation_v1": "target_validity_validation_v1.json",
}
_KST = ZoneInfo("Asia/Seoul")


class AcademicRunCancelled(RuntimeError):
    """Raised at a safe adapter boundary when cancellation was requested."""

    code = "cancelled"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest() -> dict[str, Any]:
    value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != 1 or value.get("source_policy") != "exact-byte-copy":
        raise RuntimeError("The bundled SleepWell source manifest is incompatible.")
    return value


def _manifest_entry(relative_path: str) -> dict[str, Any]:
    for item in _manifest().get("files", []):
        if item.get("bundled_path") == relative_path:
            return item
    raise RuntimeError(f"The bundled source is not declared in the manifest: {relative_path}")


def _verify(relative_path: str) -> dict[str, Any]:
    item = _manifest_entry(relative_path)
    path = _VENDOR_ROOT / Path(relative_path)
    if not path.is_file() or path.stat().st_size != item.get("size_bytes"):
        raise RuntimeError(f"The bundled SleepWell file is missing or has the wrong size: {relative_path}")
    if _sha256(path) != item.get("sha256"):
        raise RuntimeError(f"The bundled SleepWell file failed SHA-256 verification: {relative_path}")
    return item


def vendor_module(name: str):
    """Return one exact, SHA-verified top-level module from the vendor bundle."""
    if not isinstance(name, str) or not _MODULE_NAME.fullmatch(name):
        raise ValueError("Vendor module names must be simple Python identifiers.")
    relative = f"{name}.py"
    # Imported vendor modules use original absolute imports.  Verify the whole
    # Python closure before any of those imports can execute.
    for item in _manifest().get("files", []):
        bundled_path = item.get("bundled_path", "")
        if bundled_path.endswith(".py") and "/" not in bundled_path:
            _verify(bundled_path)
    with _IMPORT_LOCK:
        loaded = sys.modules.get(name)
        if loaded is not None:
            loaded_path = Path(getattr(loaded, "__file__", "")).resolve()
            if loaded_path.parent != _VENDOR_ROOT.resolve():
                raise RuntimeError(
                    f"Cannot load bundled {name!r}: a different top-level module is already loaded."
                )
            return loaded
        root_text = str(_VENDOR_ROOT)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        module = importlib.import_module(name)
        loaded_path = Path(getattr(module, "__file__", "")).resolve()
        if loaded_path != (_VENDOR_ROOT / relative).resolve():
            raise RuntimeError(f"Python resolved {name!r} outside the verified vendor bundle.")
        return module


def get_provenance() -> dict[str, Any]:
    """Return immutable source and artifact identity used by adapter results."""
    manifest = _manifest()
    source_names = (
        "research_lab.py",
        "research_lab_context.py",
        "fusion_forecast.py",
        "night_core.py",
        "robust_features.py",
        "prospective.py",
        "online_trial.py",
    )
    sources = {}
    for name in source_names:
        entry = _verify(name)
        sources[name] = {"sha256": entry["sha256"], "source_path": entry["source_path"]}
    artifacts = {}
    for name in sorted(path.name for path in (_VENDOR_ROOT / "models").glob("*.json")):
        entry = _verify(f"models/{name}")
        artifacts[name] = {"sha256": entry["sha256"], "size_bytes": entry["size_bytes"]}
    return {
        "schema_version": 1,
        "adapter": "researchbench.academic_backend",
        "canonical_source": manifest["canonical_source"],
        "source_policy": manifest["source_policy"],
        "manifest_sha256": _sha256(_MANIFEST_PATH),
        "sources": sources,
        "artifacts": artifacts,
    }


def experiment_catalog() -> dict[str, Any]:
    """Describe supported original experiments without claiming fresh training."""
    arms = []
    labels = {
        "physical_fixed": "Fixed physical",
        "physical_personal": "Personalized physical",
        "data": "Data model",
        "hybrid": "Fusion model",
    }
    for cohort in ("cmi", "mesa"):
        for kind, label in labels.items():
            arms.append({"model_id": f"{cohort}.{kind}", "cohort": cohort, "kind": kind,
                         "label": label})
    arms.append({"model_id": "baseline.recent7", "cohort": "shared",
                 "kind": "baseline_recent7", "label": "Recent-7 median"})
    return {
        "schema_version": 1,
        "modes": [
            {"id": "retrospective", "evidence": "fresh_retrospective_replay"},
            {"id": "personalization", "evidence": "fresh_retrospective_replay_with_gradual_diagnostics"},
            {"id": "report", "evidence": "type_specific; see coverage_matrix"},
            {"id": "prospective", "evidence": "timestamped_prospective_monitoring"},
        ],
        "retrospective_arms": arms,
        "reports": [{"id": key, "file": value, "evidence": "stored_retrospective_evaluation"}
                    for key, value in _REPORTS.items()],
        "time_contract": "20:00 Asia/Seoul",
        "units": "minutes",
        "personalization_policy": "gradual_v1",
        "report_types": [
            {"id": "research_diagnostics", "call": "research_console.research_diagnostics",
             "evidence": "validated_stored_artifacts"},
            {"id": "measurement_audit", "call": "measurement_audit.audit_measurements",
             "evidence": "fresh_aggregate_input_audit"},
            {"id": "target_validity", "call": "target_validity.summarize_target_validity",
             "evidence": "fresh_aggregate_input_audit"},
            {"id": "phone_baseline", "call": "phone_baseline_evaluation.evaluate_phone_baselines",
             "evidence": "fresh_recalculation_of_supplied_frozen_phone_pairs"},
            {"id": "night_model", "call": "night_forecast.NightModel.predict",
             "evidence": "single_forecast_not_performance_evaluation"},
            {"id": "population_prior", "call": "personalization.select_population_prior",
             "evidence": "prior_selection_not_outcome_prediction"},
            *[{"id": key, "call": "stored_json", "evidence": "stored_retrospective_evaluation"}
              for key in _REPORTS],
        ],
        "coverage_matrix": [
            {"family": "robust_cmi_mesa", "models": 9, "mode": "retrospective/personalization",
             "status": "executable", "note": "Eight physical/data/fusion arms plus shared recent7."},
            {"family": "prospective_daily", "models": 9, "mode": "prospective",
             "status": "executable", "note": "Three base plus six CMI/MESA frozen fusion arms."},
            {"family": "online_residual_personalization", "models": 4, "mode": "prospective",
             "status": "executable", "note": "Data/hybrid CMI/MESA; diary-only evaluation."},
            {"family": "cmi_night_model", "models": 4, "mode": "report:night_model",
             "status": "executable_forecast", "note": "Physical/data/hybrid/hybrid_frozen; research scope."},
            {"family": "samsung_night_model", "models": 4, "mode": "report:night_model",
             "status": "executable_forecast", "note": "Requires linked personal history."},
            {"family": "population_prior", "models": 1, "mode": "report:population_prior",
             "status": "executable_selector", "note": "Observed-duration prior, not optimal sleep or accuracy."},
            {"family": "frozen_validity_tables", "models": None, "mode": "report:<stored id>",
             "status": "evidence_only", "note": "Original raw cohorts are absent; no fresh training or rerun."},
            {"family": "external_validation_plans", "models": None, "mode": None,
             "status": "not_executable", "note": "Proposed validation is not reported as completed evidence."},
        ],
    }


def model_status() -> dict[str, Any]:
    """Validate the bundled CMI and MESA artifacts using the original loader."""
    module = vendor_module("research_lab")
    result = module.model_status(_VENDOR_ROOT / "models")
    return {"status": result, "provenance": get_provenance()}


def _cancel_check(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise AcademicRunCancelled("The academic experiment was cancelled.")


def _progress(callback: Callable[[dict[str, Any]], None] | None, stage: str,
              fraction: float, message: str) -> None:
    if callback is not None:
        callback({"stage": stage, "fraction": fraction, "message": message})


def _research_payload(config: dict[str, Any]) -> dict[str, Any]:
    cohorts = config.get("cohorts", ["cmi", "mesa"])
    payload: dict[str, Any] = {"schema_version": 1, "cohorts": cohorts}
    if "chart_participant_id" in config:
        payload["chart_participant_id"] = config["chart_participant_id"]

    requested = config.get("input_mode")
    if requested is None:
        requested = "synthetic" if "synthetic" in config else "real"
    if requested == "synthetic":
        payload.update({"mode": "synthetic", "synthetic": config.get("synthetic", {})})
        return payload
    if requested != "real":
        raise ValueError("input_mode must be 'real' or 'synthetic'.")
    payload["mode"] = "real"
    choices = [name for name in ("records", "csv_text", "csv_file") if name in config]
    if len(choices) != 1:
        raise ValueError("Real retrospective mode requires exactly one of records, csv_text, or csv_file.")
    choice = choices[0]
    if choice == "csv_file":
        payload["csv_text"] = Path(config[choice]).read_text(encoding="utf-8-sig")
    else:
        payload[choice] = config[choice]
    if "context" in config:
        payload["context"] = config["context"]
    return payload


def _metric_summary(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for metric in raw.get("metrics", []):
        row = dict(metric)
        row["n_nights"] = row.pop("n", None)
        row["comparison_scope"] = "common_nights"
        rows.append(row)
    return rows


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _generic_tables(value: Any, path: str = "root") -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value and all(_scalar(item) or (isinstance(item, list) and all(_scalar(v) for v in item))
                         for item in value.values()):
            tables.append({"table_id": path, "columns": list(value),
                           "rows": [[json.dumps(item, ensure_ascii=False) if isinstance(item, list) else item
                                     for item in value.values()]]})
        else:
            for key, item in value.items():
                tables.extend(_generic_tables(item, f"{path}.{key}"))
    elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        columns = []
        for item in value:
            for key in item:
                if key not in columns:
                    columns.append(key)
        rows = []
        for item in value:
            row = []
            for key in columns:
                cell = item.get(key)
                row.append(cell if _scalar(cell) else json.dumps(cell, ensure_ascii=False, sort_keys=True))
            rows.append(row)
        tables.append({"table_id": path, "columns": columns, "rows": rows})
    return tables


def _artifact_tables(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw.get("panels"), list):
        return [{"table_id": panel["id"], "title": panel.get("title"),
                 "columns": panel["columns"], "rows": panel["rows"],
                 "notes": panel.get("notes", [])} for panel in raw["panels"]]
    return _generic_tables(raw)


def _artifact_summary(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for cohort, details in (raw.get("cohorts") or {}).items():
        for model in details.get("models", []) if isinstance(details, dict) else []:
            if isinstance(model, dict) and "joint_mae_min" in model:
                rows.append({"model_id": f"{cohort}.{model.get('name')}", "cohort": cohort,
                             "onset_mae_min": model.get("onset_mae_min"),
                             "wake_mae_min": model.get("wake_mae_min"),
                             "joint_mae_min": model.get("joint_mae_min"),
                             "n_nights": details.get("n_nights"),
                             "n_participants": details.get("n_people"),
                             "evidence": "stored_retrospective_evaluation"})
    return rows


def _run_artifact_report(config: dict[str, Any]) -> dict[str, Any]:
    report_id = config.get("report_id", config.get("report_type"))
    if report_id not in _REPORTS:
        raise ValueError(f"report_id must be one of: {', '.join(sorted(_REPORTS))}")
    filename = _REPORTS[report_id]
    _verify(f"models/{filename}")
    raw = json.loads((_VENDOR_ROOT / "models" / filename).read_text(encoding="utf-8"))
    warning = (
        "This is a stored retrospective evaluation artifact. It was not recomputed, retrained, "
        "or prospectively validated by this run."
    )
    return {"mode": "report", "report_type": report_id, "raw": raw, "summary": _artifact_summary(raw),
            "normalized_tables": _artifact_tables(raw), "provenance": get_provenance(),
            "warnings": [warning] + list(raw.get("limitations", []))}


def _report_envelope(report_type: str, raw: Any, summary: list[dict[str, Any]] | None = None,
                     warnings: list[str] | None = None) -> dict[str, Any]:
    normalized_source = raw if isinstance(raw, (dict, list)) else {"value": raw}
    return {"mode": "report", "report_type": report_type, "raw": raw,
            "summary": summary or [], "normalized_tables": _artifact_tables(normalized_source)
            if isinstance(normalized_source, dict) else _generic_tables(normalized_source),
            "provenance": get_provenance(), "warnings": warnings or []}


def _report_csv_records(config: dict[str, Any], *, night_history: bool = False
                        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map one-participant canonical CSV rows without changing timestamp values."""
    path = Path(config["csv_file"])
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"participant_id", "sleep_start", "sleep_end", "observed_at"}
    if not rows or any(not required.issubset(row) for row in rows):
        raise ValueError("report CSV requires participant_id, sleep_start, sleep_end, and observed_at")
    participants = {str(row["participant_id"]).strip() for row in rows}
    if "" in participants or len(participants) != 1:
        raise ValueError("report CSV conversion requires exactly one participant")
    declared = config.get("record_source")
    if declared is not None and (not isinstance(declared, str) or not declared.strip()):
        raise ValueError("record_source must be a non-empty visible source label")
    output, sources = [], set()
    for index, row in enumerate(rows, 1):
        source = str(row.get("source") or declared or "").strip().lower()
        if not source:
            raise ValueError("CSV has no source column; choose record_source explicitly")
        onset = datetime.fromisoformat(row["sleep_start"])
        wake = datetime.fromisoformat(row["sleep_end"])
        observed = datetime.fromisoformat(row["observed_at"])
        if any(value.utcoffset() is None for value in (onset, wake, observed)):
            raise ValueError("report CSV timestamps must be timezone-aware")
        if not onset < wake <= observed:
            raise ValueError("report CSV requires sleep_start < sleep_end <= observed_at")
        sources.add(source)
        if night_history:
            output.append({"source": source, "sleep_start": row["sleep_start"],
                           "wake": row["sleep_end"], "observed_at": row["observed_at"]})
        else:
            participant = str(row["participant_id"]).strip()
            output.append({
                "id": f"{participant}:{source}:{index:04d}",
                "day": wake.astimezone(_KST).date().isoformat(),
                "bedtime": row["sleep_start"], "wake_time": row["sleep_end"],
                "duration_minutes": int(round((wake - onset).total_seconds() / 60)),
                "source": source, "modified_at": row["observed_at"],
            })
    fields = ({"sleep_start": "sleep_start", "sleep_end": "wake",
               "observed_at": "observed_at", "source": "source"}
              if night_history else
              {"participant_id+source+row": "id", "KST date(sleep_end)": "day",
               "sleep_start": "bedtime", "sleep_end": "wake_time",
               "rounded_minutes(sleep_end-sleep_start)": "duration_minutes",
               "source": "source", "observed_at": "modified_at"})
    return output, {
        "type": "canonical_csv_field_mapping", "source_file": str(path.resolve()),
        "source_declared": declared, "source_values": sorted(sources),
        "participant_count": 1, "record_count": len(output), "fields": fields,
        "timestamp_values_changed": False,
    }


def _mapped(envelope: dict[str, Any], mapping: dict[str, Any] | None) -> dict[str, Any]:
    if mapping:
        envelope.setdefault("provenance", {})["field_mapping"] = mapping
    return envelope


def _run_executable_report(config: dict[str, Any]) -> dict[str, Any]:
    report_type = config.get("report_type")
    mapping = None
    if report_type in {"measurement_audit", "target_validity"} and "records" not in config and config.get("csv_file"):
        records, mapping = _report_csv_records(config)
        config = {**config, "records": records}
    elif report_type == "night_model" and "records" not in config and config.get("csv_file"):
        records, mapping = _report_csv_records(config, night_history=True)
        config = {**config, "records": records}
    if report_type in _REPORTS:
        return _run_artifact_report(config)
    if report_type == "research_diagnostics":
        raw = vendor_module("research_console").research_diagnostics(_VENDOR_ROOT / "models")
        return _report_envelope(report_type, raw, warnings=[
            "This validates and displays stored artifacts; it does not retrain or repeat the original cohorts."
        ])
    if report_type == "measurement_audit":
        raw = vendor_module("measurement_audit").audit_measurements(
            config.get("records", []), config["as_of"], profile=config.get("profile"),
            snapshots=config.get("snapshots"), context=config.get("context"))
        return _mapped(_report_envelope(report_type, raw, warnings=list(raw.get("limitations", []))), mapping)
    if report_type == "target_validity":
        raw = vendor_module("target_validity").summarize_target_validity(
            config.get("records", []), config["as_of"])
        return _mapped(_report_envelope(report_type, raw, warnings=list(raw.get("limitations", []))), mapping)
    if report_type == "phone_baseline":
        result, lag = vendor_module("phone_baseline_evaluation").evaluate_phone_baselines(
            config.get("outcomes", {}), config["as_of"], _VENDOR_ROOT / "models")
        raw = {"evaluation": result, "reporting_lag": lag}
        summary = [{"model_id": row.get("name"), "onset_mae_min": row.get("onset_mae_min"),
                    "wake_mae_min": row.get("wake_mae_min"), "joint_mae_min": row.get("joint_mae_min"),
                    "n_nights": row.get("n_nights"), "n_participants": None,
                    "comparison_scope": "supplied_eligible_frozen_phone_pairs"}
                   for row in result.get("models", [])]
        return _report_envelope(report_type, raw, summary, [result.get("interpretation", "")])
    if report_type == "night_model":
        cohort = config.get("cohort")
        files = {"cmi": "cmi_night_model.json", "samsung": "samsung_night_model.json"}
        if cohort not in files:
            raise ValueError("night_model cohort must be 'cmi' or 'samsung'.")
        night_forecast = vendor_module("night_forecast")
        cutoff = vendor_module("night_core").local_time(config["cutoff"])
        model = night_forecast.NightModel(_VENDOR_ROOT / "models" / files[cohort])
        raw = model.predict(config.get("profile", {}), config.get("records", []), cutoff,
                            research_context=bool(config.get("research_context", cohort == "cmi")),
                            personalization_policy=config.get("personalization_policy", "gradual_v1"))
        return _mapped(_report_envelope(report_type, raw, warnings=[
            "This is a single research forecast, not a new accuracy evaluation or treatment recommendation."
        ]), mapping)
    if report_type == "population_prior":
        personalization = vendor_module("personalization")
        loaded = personalization.load_population_prior(_VENDOR_ROOT / "models" / "population_prior.json")
        raw = personalization.select_population_prior(config.get("profile", {}), loaded)
        return _report_envelope(report_type, raw, warnings=[
            "This selects an observed sleep-duration initialization prior; it is not an optimal-need estimate."
        ])
    supported = sorted([*_REPORTS, "measurement_audit", "night_model", "phone_baseline",
                        "population_prior", "research_diagnostics", "target_validity"])
    raise ValueError(f"report_type must be one of: {', '.join(supported)}")


def run_research(config: dict[str, Any], progress: Callable[[dict[str, Any]], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run an original SleepWell retrospective/personalization experiment.

    Progress callbacks receive ``{stage, fraction, message}``.  The inherited
    engine is atomic, so cancellation is checked immediately before and after
    that call; the native queue may terminate its worker process for hard stop.
    """
    if not isinstance(config, dict):
        raise TypeError("config must be a dictionary.")
    mode = config.get("mode", "retrospective")
    if mode in ("report", "artifact_report"):
        _cancel_check(cancelled)
        result = _run_executable_report(config) if mode == "report" else _run_artifact_report(config)
        _cancel_check(cancelled)
        return result
    if mode not in ("retrospective", "personalization"):
        raise ValueError("mode must be retrospective, personalization, or report.")

    _cancel_check(cancelled)
    _progress(progress, "prepare", 0.05, "Validating academic experiment input")
    payload = _research_payload(config)
    module = vendor_module("research_lab")
    _cancel_check(cancelled)
    _progress(progress, "evaluate", 0.15, "Running the original SleepWell replay")
    raw = module.evaluate_run(payload, models_dir=_VENDOR_ROOT / "models")
    _cancel_check(cancelled)
    _progress(progress, "summarize", 0.95, "Preparing common-night comparison table")

    summary = _metric_summary(raw)
    if len(summary) != (4 * len(payload["cohorts"]) + 1):
        raise RuntimeError("The original engine did not return every requested comparison arm.")
    provenance = get_provenance()
    provenance.update({"runtime_model_version": raw.get("runtime_model_version"),
                       "personalization_policy": raw.get("personalization_policy"),
                       "run_id": raw.get("run_id")})
    result = {"mode": mode, "raw": raw, "summary": summary,
              "provenance": provenance, "warnings": list(raw.get("warnings", []))}
    _progress(progress, "complete", 1.0, "Academic experiment complete")
    return result


__all__ = [
    "AcademicRunCancelled",
    "experiment_catalog",
    "get_provenance",
    "model_status",
    "run_research",
    "vendor_module",
]
