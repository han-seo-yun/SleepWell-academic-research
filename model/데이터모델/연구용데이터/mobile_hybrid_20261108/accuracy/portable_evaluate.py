"""Audit the selected deployable mobile-math variant against references."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent
ROOT = Path(r"C:/Users/pyomo/OneDrive/문서/ChatGPT/학술제")
sys.path[:0] = [str(OUT), str(ROOT / "research_workbench_academic/scripts")]

import benchmark as b


def corrected_people(records):
    grouped = defaultdict(list)
    for row in records:
        grouped[str(row["participant_id"])].append([float(row["bed_error"]), float(row["wake_error"])])
    return {
        person: {"joint_mae": float(a.mean()), "onset_mae": float(a[:, 0].mean()), "wake_mae": float(a[:, 1].mean())}
        for person, values in grouped.items() for a in [np.asarray(values, dtype=float)]
    }


def raw_people(actual, predicted, groups):
    indices = defaultdict(list)
    for index, person in enumerate(groups):
        indices[str(person)].append(index)
    errors = np.abs(actual - predicted)
    return {
        person: {"joint_mae": float(errors[ix].mean()), "onset_mae": float(errors[ix, 0].mean()), "wake_mae": float(errors[ix, 1].mean())}
        for person, ix in indices.items()
    }


def portable_prediction(selected, cohort, phase, rows, protocol_sha, method_lock_sha):
    stem = OUT / "private" / f"{selected}_portable_{cohort}_{phase}"
    receipt = b.read_json(stem.with_suffix(".json"))
    if receipt["protocol_sha256"] != protocol_sha or receipt["portable_method_lock_sha256"] != method_lock_sha:
        raise RuntimeError("portable prediction protocol mismatch")
    execution_path = OUT.parent / "mobile_export" / "portable_execution_lock.json"
    execution = b.read_json(execution_path)
    method = b.read_json(OUT / "portable_method_lock.json")
    selection_sha = b.sha256(OUT / "selection_lock.json")
    if (receipt["portable_execution_lock_sha256"] != b.sha256(execution_path)
            or execution["protocol_sha256"] != protocol_sha
            or execution["selection_lock_sha256"] != selection_sha
            or execution["portable_method_lock_sha256"] != method_lock_sha):
        raise RuntimeError("portable execution lock mismatch")
    architecture_meta = execution["architecture_parity_artifact"]
    architecture_path = Path(architecture_meta["path"])
    if not architecture_path.is_absolute():
        architecture_path = execution_path.parent / architecture_path
    if b.sha256(architecture_path) != architecture_meta["sha256"]:
        raise RuntimeError("native architecture evidence hash mismatch")
    architecture = b.read_json(architecture_path)
    if (execution["n_thinking_rows"] != 64
            or execution["target_normalization_scope"] != "full_train_before_retrieval"
            or architecture["thinking_embed_sha256"] != execution["thinking_embed_sha256"]):
        raise RuntimeError("portable architecture or target normalization contract mismatch")
    if (architecture["status"] != "pass" or architecture["n_thinking_rows"] != 64
            or architecture["context"] != execution["context_size"]
            or architecture["thinking_embed_shape"] != [64, 512]
            or architecture["output_shape"] != [1, 1, 2064]
            or architecture["native_vs_wrapper_before_attention_patch_max_abs_logit_error"] != 0
            or not 0 <= architecture["native_vs_exportable_attention_max_abs_logit_error"] <= 1e-4):
        raise RuntimeError("native architecture equivalence failed")
    bundle = execution_path.parent / execution["bundle_dir"]
    if (b.sha256(bundle / "manifest.json") != execution["bundle_manifest_sha256"]
            or b.sha256(bundle / "goldens_validation32.json") != execution["goldens_validation32_sha256"]):
        raise RuntimeError("portable bundle or golden identity mismatch")
    for name, expected_sha in execution["parity_artifacts_sha256"].items():
        parity_path = bundle / name
        if b.sha256(parity_path) != expected_sha:
            raise RuntimeError("portable parity evidence hash mismatch")
        parity = b.read_json(parity_path)
        if (parity["onnxruntime_version"] != execution["runtime_versions"]["onnxruntime_gpu"]
                or parity["active_providers"][0] != "CUDAExecutionProvider"
                or parity["provider_options"]["CUDAExecutionProvider"]["use_tf32"] != "0"):
            raise RuntimeError("portable parity runtime mismatch")
        if name == "cuda_parity_validation32.json":
            if parity["status"] != "pass" or not 0 <= parity["max_abs_error_min"] < 1 / 60:
                raise RuntimeError("portable single-query parity failed")
        elif name == "cuda_batch16_parity_validation32.json":
            if (parity["provider"] != "cuda" or set(parity["cohorts"]) != {"cmi", "mesa"}
                    or not all(0 <= item["max_abs_error_min"] < 1 / 60 for item in parity["cohorts"].values())):
                raise RuntimeError("portable batch parity failed")
        else:
            raise RuntimeError("unknown portable parity artifact")
    if set(execution["parity_artifacts_sha256"]) != {"cuda_parity_validation32.json", "cuda_batch16_parity_validation32.json"}:
        raise RuntimeError("incomplete portable parity evidence")
    expected_fields = {
        "variant": "actual_mobile_onnx_fp32", "candidate_id": selected,
        "cohort": cohort, "phase": phase, "rows": rows,
        "context_size": execution["context_size"], "n_ensembles": execution["ensembles"],
        "batch_size": execution["batch_size"], "selection_lock_sha256": selection_sha,
        "method_id": method["method_id"], "retrieval_method_id": method["retrieval_method_id"],
        "source_sha256": execution["source_sha256"], "requested_provider": "cuda",
        "onnxruntime_version": execution["runtime_versions"]["onnxruntime_gpu"],
        "numpy_version": execution["runtime_versions"]["numpy"],
    }
    if any(receipt.get(key) != value for key, value in expected_fields.items()):
        raise RuntimeError("portable runtime or input identity mismatch")
    if {entry["name"]: entry["sha256"] for entry in receipt["model_files"]} != execution["model_sha256"]:
        raise RuntimeError("portable model hash mismatch")
    provider = execution["provider_contract"]["primary"]
    if (receipt["active_providers"][0] != provider
            or receipt["provider_options"][provider]["use_tf32"] != "0"):
        raise RuntimeError("portable CUDA provider contract mismatch")
    return b.load_prediction(stem.with_suffix(".npy"), rows, receipt["prediction_sha256"]), receipt


def comparison(errors, candidate, reference, scope, metric, repeats, seed):
    rng = np.random.default_rng(seed)
    by_cohort, all_draws = [], np.zeros(repeats)
    for cohort in ("cmi", "mesa"):
        left, right = errors[reference][cohort][scope], errors[candidate][cohort][scope]
        if set(left) != set(right):
            raise RuntimeError("participant pairing changed")
        delta = np.asarray([right[p][metric] - left[p][metric] for p in sorted(left)])
        draw_index = rng.integers(0, len(delta), size=(repeats, len(delta)))
        draws = delta[draw_index].mean(axis=1)
        all_draws += draws / 2
        bounds = np.quantile(draws, [0.025, 0.975])
        by_cohort.append({
            "cohort": cohort, "participants": len(delta), "delta_min": float(delta.mean()),
            "ci_low_min": float(bounds[0]), "ci_high_min": float(bounds[1]),
            "fraction_participants_better": float(np.mean(delta < 0)),
            "median_participant_delta_min": float(np.median(delta)),
            "maximum_participant_worsening_min": float(np.max(delta)),
        })
    bounds = np.quantile(all_draws, [0.025, 0.975])
    points = [row["delta_min"] for row in by_cohort]
    return {
        "candidate": candidate, "reference": reference, "scope": scope, "metric": metric,
        "equal_cohort_delta_min": float(np.mean(points)),
        "equal_cohort_ci_low_min": float(bounds[0]), "equal_cohort_ci_high_min": float(bounds[1]),
        "cohorts": by_cohort, "interpretation": "negative favors candidate",
        "bootstrap_repeats": repeats,
    }


def main() -> int:
    p = b.protocol()
    b.verify_frozen(p)
    protocol_sha = b.sha256(OUT / "protocol.json")
    method_lock_sha = b.sha256(OUT / "portable_method_lock.json")
    lock = b.read_json(OUT / "selection_lock.json")
    if lock["protocol_sha256"] != protocol_sha:
        raise RuntimeError("selection lock protocol mismatch")
    selected = lock["selected_candidate"]
    portable = f"{selected}_actual_mobile"
    models = ("tabdpt_full_e4", "hgb", portable)
    dataset = b.load_dataset()
    errors = {model: {} for model in models}
    phases, receipts = {}, []
    for phase in ("validation", "test"):
        cells = []
        for cohort in p["cohorts"]:
            data = dataset[cohort]
            mask, rows = data["splits"] == phase, int(np.sum(data["splits"] == phase))
            portable_pred, receipt = portable_prediction(selected, cohort, phase, rows, protocol_sha, method_lock_sha)
            receipts.append(receipt)
            portable_cell, _ = b.score_prediction(cohort, phase, portable, portable_pred, data)
            cells.append({"model": portable, **portable_cell})
            if phase != "test":
                continue
            for model in models:
                if model == portable:
                    pred = portable_pred
                else:
                    source = "tabdpt_v1_3" if model == "tabdpt_full_e4" else "hgb"
                    pred = b.baseline_prediction(source, cohort, phase, rows, p)
                cell, records = b.score_prediction(cohort, phase, model, pred, data)
                if model != portable:
                    cells.append({"model": model, **cell})
                errors[model][cohort] = {
                    "corrected": corrected_people(records),
                    "raw": raw_people(data["y"][mask], pred, data["groups"][mask]),
                }
        phases[phase] = {
            "cells": cells,
            "aggregate_corrected_participant_macro_joint_mae_min": float(np.mean([
                cell["primary"]["participant_macro_joint_mae_min"] for cell in cells if cell["model"] == portable
            ])),
        }
    if len({receipt["onnxruntime_version"] for receipt in receipts}) != 1:
        raise RuntimeError("mixed ONNX Runtime versions across accuracy cells")
    if len({json.dumps(receipt["provider_options"], sort_keys=True) for receipt in receipts}) != 1:
        raise RuntimeError("mixed ONNX provider options across accuracy cells")
    private_errors = OUT / "private" / "person_errors.json"
    b.write_json(private_errors, errors)
    comparisons = []
    pairs = ((portable, "tabdpt_full_e4"), ("hgb", "tabdpt_full_e4"), (portable, "hgb"))
    offset = 0
    for candidate, reference in pairs:
        for scope in ("corrected", "raw"):
            for metric in ("joint_mae", "onset_mae", "wake_mae"):
                comparisons.append(comparison(errors, candidate, reference, scope, metric, p["bootstrap"]["repeats"], p["bootstrap"]["seed"] + offset))
                offset += 1
    result = {
        "status": "complete", "selected_resource_candidate": selected,
        "portable_model": portable,
        "runtime_scope": "Actual ONNX Runtime predictions of the frozen deployable pipeline; Android numerical parity and A52s resource measures are separate evidence",
        "portable_method_id": receipts[0]["method_id"],
        "retrieval_method_id": receipts[0]["retrieval_method_id"],
        "runtime_versions": sorted({receipt["onnxruntime_version"] for receipt in receipts}),
        "requested_providers": sorted({receipt["requested_provider"] for receipt in receipts}),
        "portable_source_sha256": receipts[0]["source_sha256"],
        "protocol_sha256": protocol_sha, "portable_method_lock_sha256": method_lock_sha,
        "portable_execution_lock_sha256": receipts[0]["portable_execution_lock_sha256"],
        "validation": phases["validation"], "test": phases["test"],
        "comparisons": comparisons, "person_errors_sha256": b.sha256(private_errors),
        "uncertainty": "Descriptive 95% paired participant percentile bootstrap intervals; equal cohort weight; no multiplicity correction and no superiority claim.",
        "historical_test_reuse": True,
        "decision_threshold": None
    }
    b.write_json(OUT / "actual_mobile_evaluation.json", result)
    print(json.dumps({
        "portable_model": portable,
        "validation_mae": result["validation"]["aggregate_corrected_participant_macro_joint_mae_min"],
        "test_mae": result["test"]["aggregate_corrected_participant_macro_joint_mae_min"],
        "comparisons": len(comparisons),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
