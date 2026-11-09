"""Export selected HGB models with their frozen HYBRID39 preprocessing."""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path("C:/Users/pyomo/OneDrive/문서/ChatGPT/학술제")
OUTPUT = Path("E:/학술제/데이터모델/연구용데이터/mobile_hybrid_20261108/hgb_android")
APP_ASSETS = Path("E:/학술제/앱_소빈/통합 v8/app/src/main/assets/models")
SOURCE = ROOT / "reports/equal_budget_hybrid9_20261107/private"
CACHE = ROOT / "reports/deep_learning_comparison_20261105/private"
PROTOCOL = ROOT / "reports/equal_budget_hybrid9_20261107/protocol.json"
SELECTION = ROOT / "reports/equal_budget_hybrid9_20261107/selection_lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_estimator(estimator: HistGradientBoostingRegressor) -> dict:
    if not isinstance(estimator, HistGradientBoostingRegressor):
        raise TypeError(type(estimator).__name__)
    trees = []
    for iteration in estimator._predictors:
        if len(iteration) != 1:
            raise ValueError("multi-tree iterations are unsupported")
        predictor = iteration[0]
        if predictor.raw_left_cat_bitsets.size or predictor.binned_left_cat_bitsets.size:
            raise ValueError("categorical splits are unsupported")
        nodes = []
        for node in predictor.nodes:
            if bool(node["is_categorical"]):
                raise ValueError("categorical node is unsupported")
            if bool(node["is_leaf"]):
                nodes.append({"leaf": float(node["value"])})
            else:
                nodes.append({"feature": int(node["feature_idx"]),
                              "threshold": float(node["num_threshold"]),
                              "missing_left": bool(node["missing_go_to_left"]),
                              "left": int(node["left"]), "right": int(node["right"])})
        trees.append(nodes)
    return {"kind": "hist_gradient_boosting",
            "baseline": float(np.asarray(estimator._baseline_prediction).reshape(-1)[0]),
            "trees": trees}


def transform(raw: np.ndarray, prep: dict) -> np.ndarray:
    values = np.asarray(raw, dtype=np.float64)
    missing = np.isnan(values)
    imputed = np.where(missing, np.asarray(prep["imputer_median"]), values)
    indicators = prep["missing_indicator_features"]
    if indicators:
        imputed = np.column_stack((imputed, missing[:, indicators].astype(np.float64)))
    clipped = np.clip(imputed, prep["clip_low"], prep["clip_high"])
    kept = clipped[:, prep["variance_keep"]]
    return (kept - np.asarray(prep["scale_mean"])) / np.asarray(prep["scale"])


def tree_value(tree: list[dict], row: np.ndarray) -> float:
    index = 0
    while "leaf" not in tree[index]:
        node = tree[index]
        value = row[node["feature"]]
        left = node["missing_left"] if math.isnan(float(value)) else value <= node["threshold"]
        index = node["left"] if left else node["right"]
    return float(tree[index]["leaf"])


def portable_predict(spec: dict, raw: np.ndarray) -> np.ndarray:
    output = []
    for row in transform(raw, spec["preprocessing"]):
        output.append([estimator["baseline"] + sum(tree_value(tree, row)
                      for tree in estimator["trees"]) for estimator in spec["estimators"]])
    return np.asarray(output, dtype=np.float64)


def constrain(predictions: np.ndarray) -> np.ndarray:
    result = np.asarray(predictions, dtype=np.float64).copy()
    result[:, 0] = np.clip(result[:, 0], 0.0, 960.0)
    result[:, 1] = np.minimum(1440.0, result[:, 1])
    result[:, 1] = np.maximum(result[:, 0] + 30.0, np.minimum(result[:, 0] + 960.0, result[:, 1]))
    return result


def binary_artifact(spec: dict) -> bytes:
    """Losslessly encode every floating-point field as IEEE-754 binary64."""
    output = io.BytesIO()
    output.write(b"SWLHGB01")
    write_int = lambda value: output.write(struct.pack(">i", int(value)))
    write_double = lambda value: output.write(struct.pack(">d", float(value)))
    write_bool = lambda value: output.write(struct.pack(">?", bool(value)))
    prep = spec["preprocessing"]
    for values, writer in ((prep["imputer_median"], write_double),
                           (prep["missing_indicator_features"], write_int),
                           (prep["clip_low"], write_double), (prep["clip_high"], write_double),
                           (prep["variance_keep"], write_int),
                           (prep["scale_mean"], write_double), (prep["scale"], write_double)):
        write_int(len(values))
        for value in values:
            writer(value)
    write_int(len(spec["estimators"]))
    for estimator in spec["estimators"]:
        write_double(estimator["baseline"])
        write_int(len(estimator["trees"]))
        for tree in estimator["trees"]:
            write_int(len(tree))
            for node in tree:
                leaf = "leaf" in node
                write_bool(leaf)
                if leaf:
                    write_double(node["leaf"])
                else:
                    write_int(node["feature"]); write_double(node["threshold"])
                    write_bool(node["missing_left"]); write_int(node["left"]); write_int(node["right"])
    return output.getvalue()


def main() -> None:
    artifact_dir, fixture_dir = OUTPUT / "artifacts", OUTPUT / "fixtures"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    summary = {"schema_version": 1, "cohorts": {}}
    for cohort in ("cmi", "mesa"):
        source_model = SOURCE / f"hgb_{cohort}_2.joblib"
        model = joblib.load(source_model)
        prior_path = APP_ASSETS / f"robust_{cohort}_v2.json"
        prior_hybrid = json.loads(prior_path.read_text(encoding="utf-8"))["models"]["hybrid"]
        spec = {"schema_version": 1, "artifact_type": "sleepwell_mobile_hgb_hybrid39",
                "model_version": "hgb-hybrid39-equal-budget-20261107-trial2",
                "feature_contract": "sleepwell_next_night_hybrid39_v1", "cohort": cohort,
                "feature_order": prior_hybrid["feature_order"],
                "preprocessing": prior_hybrid["preprocessing"],
                "estimators": [export_estimator(x) for x in model.estimators_],
                "source": {"model_sha256": sha256(source_model),
                           "preprocessing_artifact_sha256": sha256(prior_path),
                           "protocol_sha256": sha256(PROTOCOL),
                           "selection_lock_sha256": sha256(SELECTION),
                           "fit_scope": "frozen participant-disjoint Train only",
                           "selection": "shared trial 2 selected on Validation only"}}
        cache_path = CACHE / f"{cohort}.npz"
        with np.load(cache_path, allow_pickle=False) as cached:
            test = cached["splits"] == "test"
            raw, transformed = cached["X_hybrid_raw"][test], cached["X_hybrid"][test]
            expected, reproduced = model.predict(transformed), portable_predict(spec, raw)
            saved_path = SOURCE / f"hgb_{cohort}_2_test.npy"
            saved = np.load(saved_path, allow_pickle=False)
            prep_error = float(np.max(np.abs(transform(raw, spec["preprocessing"]) - transformed)))
            pred_error = float(np.max(np.abs(reproduced - expected)))
            saved_raw_error = float(np.max(np.abs(expected - saved)))
            constrained_error = float(np.max(np.abs(constrain(reproduced) - constrain(saved))))
            if prep_error > 1e-10 or pred_error > 1e-9 or saved_raw_error > 1e-12 or constrained_error > 1e-9:
                raise ValueError(f"{cohort} parity failed: preprocessing={prep_error}, prediction={pred_error}")
            np.save(fixture_dir / f"{cohort}_test_expected.npy", expected, allow_pickle=False)
            np.save(fixture_dir / f"{cohort}_test_reproduced.npy", reproduced, allow_pickle=False)
            indices = np.unique(np.linspace(0, len(raw) - 1, num=min(12, len(raw)), dtype=int))
            fixtures = [{"row_index_in_test": int(i),
                         "features": [None if math.isnan(float(v)) else float(v) for v in raw[i]],
                         "expected": expected[i].tolist(),
                         "expected_constrained": constrain(expected[i:i + 1])[0].tolist()} for i in indices]
        artifact_path = artifact_dir / f"mobile_hgb_{cohort}.json"
        artifact_path.write_text(json.dumps(spec, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False), encoding="utf-8")
        binary_path = artifact_dir / f"mobile_hgb_{cohort}.bin"
        binary_path.write_bytes(binary_artifact(spec))
        fixture_path = fixture_dir / f"mobile_hgb_{cohort}_golden.json"
        fixture_path.write_text(json.dumps(fixtures, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False), encoding="utf-8")
        summary["cohorts"][cohort] = {"test_rows": int(len(raw)), "raw_features": 39,
            "transformed_features": int(transformed.shape[1]), "preprocessing_max_abs_error": prep_error,
            "prediction_max_abs_error_min": pred_error, "saved_raw_max_abs_error_min": saved_raw_error,
            "saved_constrained_max_abs_error_min": constrained_error,
            "saved_test_prediction_sha256": sha256(saved_path), "artifact_bytes": artifact_path.stat().st_size,
            "artifact_sha256": sha256(artifact_path), "golden_rows": len(fixtures),
            "golden_sha256": sha256(fixture_path), "binary_bytes": binary_path.stat().st_size,
            "binary_sha256": sha256(binary_path)}
    (OUTPUT / "export_parity.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
