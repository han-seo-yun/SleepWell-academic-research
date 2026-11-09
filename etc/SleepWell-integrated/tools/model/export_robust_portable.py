"""Export the locked robust-v2 sklearn pipelines to strict portable JSON.

Run this with the research environment that can load the original joblib and
Parquet artifacts.  Runtime inference uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import HuberRegressor


SCHEMA = 2
TYPE = "sleepwell_robust_next_night_portable"
CONTRACT = "sleepwell_next_night_robust_v2"
COHORTS = ("cmi", "mesa")
ARMS = ("data", "hybrid")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite_list(values):
    result = np.asarray(values, dtype=float).reshape(-1).tolist()
    if not all(math.isfinite(value) for value in result):
        raise ValueError("Non-finite fitted parameter")
    return result


def export_estimator(estimator):
    if isinstance(estimator, HuberRegressor):
        return {
            "kind": "linear",
            "intercept": float(estimator.intercept_),
            "coefficients": finite_list(estimator.coef_),
        }
    if not isinstance(estimator, HistGradientBoostingRegressor):
        raise TypeError(f"Unsupported estimator: {type(estimator).__name__}")
    trees = []
    for iteration in estimator._predictors:
        if len(iteration) != 1:
            raise ValueError("Only one tree per iteration is supported")
        predictor = iteration[0]
        if predictor.raw_left_cat_bitsets.size or predictor.binned_left_cat_bitsets.size:
            raise ValueError("Categorical tree splits are not supported")
        nodes = []
        for node in predictor.nodes:
            if bool(node["is_categorical"]):
                raise ValueError("Categorical tree node is not supported")
            if bool(node["is_leaf"]):
                nodes.append({"leaf": float(node["value"])})
            else:
                nodes.append({
                    "feature": int(node["feature_idx"]),
                    "threshold": float(node["num_threshold"]),
                    "missing_left": bool(node["missing_go_to_left"]),
                    "left": int(node["left"]),
                    "right": int(node["right"]),
                })
        trees.append(nodes)
    return {
        "kind": "hist_gradient_boosting",
        "baseline": float(np.asarray(estimator._baseline_prediction).reshape(-1)[0]),
        "trees": trees,
    }


def export_pipeline(pipeline, feature_order):
    imputer = pipeline.named_steps["impute"]
    clip = pipeline.named_steps["clip"]
    variance = pipeline.named_steps["variance"]
    scaler = pipeline.named_steps["scale"]
    model = pipeline.named_steps["model"]
    indicator_features = [int(value) for value in imputer.indicator_.features_.tolist()]
    variances = np.asarray(variance.variances_, dtype=float)
    keep = [index for index, value in enumerate(variances) if value > variance.threshold]
    estimators = [export_estimator(estimator) for estimator in model.estimators_]
    if len(estimators) != 2:
        raise ValueError("Exactly onset and wake estimators are required")
    result = {
        "feature_order": list(feature_order),
        "preprocessing": {
            "imputer_median": finite_list(imputer.statistics_),
            "missing_indicator_features": indicator_features,
            "clip_low": finite_list(clip.low_),
            "clip_high": finite_list(clip.high_),
            "variance_keep": keep,
            "scale_mean": finite_list(scaler.mean_),
            "scale": finite_list(scaler.scale_),
        },
        "estimators": estimators,
    }
    expected = len(feature_order) + len(indicator_features)
    if not (len(result["preprocessing"]["clip_low"]) == expected
            and len(result["preprocessing"]["variance_keep"]) == len(scaler.mean_)):
        raise ValueError("Unexpected fitted preprocessing dimensions")
    return result


def tree_predict(tree, row):
    index = 0
    while "leaf" not in tree[index]:
        node = tree[index]
        value = row[node["feature"]]
        index = node["left"] if (
            math.isnan(value) and node["missing_left"]
            or not math.isnan(value) and value <= node["threshold"]
        ) else node["right"]
    return tree[index]["leaf"]


def portable_predict(spec, matrix):
    prep = spec["preprocessing"]
    transformed = []
    for source in np.asarray(matrix, dtype=float):
        missing = [math.isnan(float(value)) for value in source]
        row = [prep["imputer_median"][i] if missing[i] else float(value)
               for i, value in enumerate(source)]
        row.extend(1.0 if missing[i] else 0.0 for i in prep["missing_indicator_features"])
        row = [min(prep["clip_high"][i], max(prep["clip_low"][i], value))
               for i, value in enumerate(row)]
        row = [row[i] for i in prep["variance_keep"]]
        transformed.append([(value - prep["scale_mean"][i]) / prep["scale"][i]
                            for i, value in enumerate(row)])
    output = []
    for row in transformed:
        predicted = []
        for estimator in spec["estimators"]:
            if estimator["kind"] == "linear":
                value = estimator["intercept"] + sum(
                    coefficient * feature
                    for coefficient, feature in zip(estimator["coefficients"], row)
                )
            else:
                value = estimator["baseline"] + sum(
                    tree_predict(tree, row) for tree in estimator["trees"]
                )
            predicted.append(value)
        output.append(predicted)
    return np.asarray(output)


def export_cohort(analysis, output, cohort, summary, protocol):
    source = analysis / cohort
    artifact = json.loads((source / "artifact.json").read_text(encoding="utf-8"))
    features = pd.read_parquet(source / "features.parquet")
    test_rows = set(pd.read_csv(source / "test_data.csv")["row_id"].astype(int).tolist())
    models = {}
    parity = {}
    source_hashes = {}
    for arm in ARMS:
        joblib_path = source / f"{arm}.joblib"
        actual_hash = sha256(joblib_path)
        if actual_hash != artifact["model_sha256"][arm]:
            raise ValueError(f"{cohort}/{arm} source model hash mismatch")
        pipeline = joblib.load(joblib_path)
        names = artifact["feature_names"][arm]
        spec = export_pipeline(pipeline, names)
        source_matrix = features[names].to_numpy(float)
        expected = pipeline.predict(source_matrix)
        reproduced = portable_predict(spec, source_matrix)
        max_error = float(np.max(np.abs(expected - reproduced)))
        if max_error > 1e-9:
            raise ValueError(f"{cohort}/{arm} portable parity failed: {max_error}")
        test_mask = features["row_id"].astype(int).isin(test_rows).to_numpy()
        parity[arm] = {
            "all_rows": int(len(features)),
            "test_rows": int(test_mask.sum()),
            "max_abs_error_all_rows": max_error,
            "max_abs_error_test_rows": float(
                np.max(np.abs(expected[test_mask] - reproduced[test_mask]))
            ),
        }
        source_hashes[arm] = actual_hash
        models[arm] = spec
    payload = {
        "schema_version": SCHEMA,
        "artifact_type": TYPE,
        "feature_contract": CONTRACT,
        "model_version": artifact["version"],
        "status": "research_comparison_only",
        "cohort": cohort,
        "decision_time": "20:00 Asia/Seoul",
        "target": "next main-sleep onset and final wake offsets in minutes after the local 20:00 cutoff",
        "physical_provider": "two_process_fixed_v1",
        "prior": artifact["prior"],
        "models": models,
        "source_model_sha256": source_hashes,
        "research_metrics": {
            "test": {arm: summary[cohort]["test"][arm] for arm in ("physical", "data", "hybrid")},
            "test_status": protocol["test_status"],
            "selection": protocol["selection"],
            "processing": protocol["processing"],
            "parity": parity,
        },
        "provenance": {
            "study_cohort": "CMI" if cohort == "cmi" else "MESA Sleep",
            "fitted_run": protocol["version"],
            "seed": protocol["seed"],
            "training_rows": artifact["prior"]["n_nights"],
            "training_people": artifact["prior"]["n_people"],
            "source_artifact_sha256": sha256(source / "artifact.json"),
            "source_features_sha256": sha256(source / "features.parquet"),
        },
        "limitations": [
            "Research comparison output, not a treatment recommendation or demonstrated sleep improvement.",
            "Validation and Test were previously exposed and reused; this is not fresh external confirmation.",
            "The fixed Two-Process constants are an engineering model, not measured personal physiology.",
            "Wearable-derived timing can be affected by wear, device, and scoring behavior.",
            "CMI and MESA are separate reference cohorts; their predictions are not a validated combined ensemble.",
            "Population and device generalization require prospective validation after SleepWell enrollment.",
        ],
    }
    destination = output / f"robust_{cohort}_v2.json"
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = json.loads((args.analysis / "summary.json").read_text(encoding="utf-8"))
    protocol = json.loads((args.analysis / "protocol.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    for cohort in COHORTS:
        path = export_cohort(args.analysis, args.output, cohort, summary, protocol)
        print(f"{cohort}: {path.name} sha256={sha256(path)}")


if __name__ == "__main__":
    main()
