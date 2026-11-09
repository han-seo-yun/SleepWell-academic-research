"""Fit one shared CMI/MESA next-night predictor in the Academic Workbench.

Training, model selection and reused-Test evaluation are separate stages.
Participant-level records and fitted artifacts stay in the selected local output.
The desktop result envelope contains aggregate results only.
"""
from __future__ import annotations

import csv
import hashlib
import json
import platform
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_selection import VarianceThreshold
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor, Ridge
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .academic_backend import AcademicRunCancelled, get_provenance, vendor_module


COHORTS = ("CMI", "MESA")
TARGETS = ["bedtime_min", "wake_min"]
STATUS = "retrospective_reused_holdout"
CANDIDATES = {
    "ridge100": {"family": "ridge", "alpha": 100.0},
    "ridge1000": {"family": "ridge", "alpha": 1000.0},
    "huber": {"family": "huber", "alpha": 1.0, "epsilon": 1.35},
    "absolute7": {"family": "hgb", "loss": "absolute_error", "leaves": 7, "min_leaf": 20},
    "absolute15": {"family": "hgb", "loss": "absolute_error", "leaves": 15, "min_leaf": 40},
    "squared7": {"family": "hgb", "loss": "squared_error", "leaves": 7, "min_leaf": 20},
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def checkpoint(progress, cancelled, stage, fraction):
    if cancelled and cancelled():
        raise AcademicRunCancelled("Pooled retraining cancelled")
    if progress:
        progress({"stage": stage, "progress": float(fraction)})


class TrainQuantileClip(TransformerMixin, BaseEstimator):
    """Learn predictor limits exclusively from the rows passed to fit."""

    def fit(self, X, y=None):
        self.low_, self.high_ = np.quantile(X, [0.01, 0.99], axis=0)
        return self

    def transform(self, X):
        return np.clip(X, self.low_, self.high_)


def make_model(candidate, seed):
    parameters = CANDIDATES[candidate]
    if parameters["family"] == "ridge":
        estimator = Ridge(alpha=parameters["alpha"])
    elif parameters["family"] == "huber":
        estimator = MultiOutputRegressor(HuberRegressor(
            alpha=parameters["alpha"], epsilon=parameters["epsilon"], max_iter=1000, tol=1e-5,
        ))
    else:
        estimator = MultiOutputRegressor(HistGradientBoostingRegressor(
            loss=parameters["loss"], max_leaf_nodes=parameters["leaves"],
            min_samples_leaf=parameters["min_leaf"], learning_rate=0.05, max_iter=120,
            l2_regularization=10.0, early_stopping=False, random_state=seed,
        ))
    return Pipeline([
        ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("clip", TrainQuantileClip()), ("variance", VarianceThreshold()),
        ("scale", StandardScaler()), ("model", estimator),
    ])


def load_database(database):
    """Require the verified, versioned CMI/MESA DB and its matching manifest."""
    database = Path(database).resolve()
    manifest = json.loads(database.with_name("manifest.json").read_text(encoding="utf-8"))
    digest = sha256(database)
    if manifest["output_sha256"].get(database.name) != digest:
        raise ValueError("Database SHA256 does not match its manifest")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database foreign-key check failed")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported database version")
        frame = pd.read_sql_query("SELECT * FROM model_data ORDER BY cohort,subject_id,cutoff", connection)
    finally:
        connection.close()
    if set(frame.cohort) != set(COHORTS):
        raise ValueError("Only CMI and MESA may be included")
    if set(frame.split) != {"train", "validation", "test"}:
        raise ValueError("Source train/validation/test splits are required")
    if frame.groupby(["cohort", "subject_id"]).split.nunique().max() != 1:
        raise ValueError("Participant crosses data splits")
    if frame.duplicated(["cohort", "subject_id", "cutoff"]).any():
        raise ValueError("Duplicate participant/cutoff")
    if not np.isfinite(frame[TARGETS].to_numpy(float)).all():
        raise ValueError("Non-finite targets")
    for cohort in COHORTS:
        actual = frame.loc[frame.cohort.eq(cohort)]
        expected = next(item for item in manifest["summary"] if item["cohort"] == cohort)
        if len(actual) != expected["episodes"] or actual.subject_id.nunique() != expected["participants"]:
            raise ValueError("Database counts disagree with manifest")
        if set(actual.split) != {"train", "validation", "test"}:
            raise ValueError("Every cohort must contain all three original splits")
    return frame, manifest, digest


def balanced_weights(frame):
    counts = frame.groupby(["cohort", "subject_id"]).subject_id.transform("size").to_numpy(float)
    people = frame[["cohort", "subject_id"]].drop_duplicates().groupby("cohort").size()
    weights = 1.0 / (counts * frame.cohort.map(people).to_numpy(float))
    return weights / weights.mean()


def prior_from_train(frame):
    if set(frame.split) != {"train"}:
        raise ValueError("Prior must use training participants only")
    means = frame.groupby(["cohort", "subject_id"])[TARGETS].mean()
    value = means.groupby(level="cohort").mean().mean()
    return {"bed_offset": float(value.bedtime_min), "wake_offset": float(value.wake_min),
            "method": "equal-cohort mean of equal-participant training means",
            "n_people": len(means), "n_nights": len(frame)}


def replay_features(frame, prior, progress=None, cancelled=None, stage="features", frozen_models=None):
    features_module = vendor_module("robust_features")
    core = vendor_module("night_core")
    names = list(features_module.HYBRID_V2)
    feature_rows, baseline_rows, history_counts = {}, {}, {}
    completed = 0
    for (cohort, _), group in frame.groupby(["cohort", "subject_id"], sort=True):
        group = group.sort_values("cutoff")
        enrollment = group.iloc[0].cutoff
        history = []
        for row in group.itertuples():
            if completed % 250 == 0:
                checkpoint(progress, cancelled, stage, completed / max(1, len(frame)))
            cutoff = core.local_time(row.cutoff)
            values, state = features_module.extended_features(
                history, cutoff, prior, enrollment, personalization_policy=core.GRADUAL_POLICY,
            )
            feature_rows[row.Index] = [values[name] for name in names]
            history_counts[row.Index] = int(state["valid_days"])
            if frozen_models is not None:
                prediction = frozen_models[cohort].predict(
                    {"created_at": enrollment}, history, cutoff,
                    personalization_policy=core.GRADUAL_POLICY,
                )
                baseline_rows[row.Index] = {
                    "existing_data": prediction["data"], "existing_hybrid": prediction["hybrid"],
                    "existing_physical": prediction["physical"],
                    "recent_median": (prediction["features"]["median14_bed"],
                                      prediction["features"]["median14_wake"]),
                }
            # Append only after predicting this night; vendor code also filters observed_at.
            history.append({"source": "research", "bed": row.bed, "wake": row.wake,
                            "observed_at": row.observed_at})
            completed += 1
    features = pd.DataFrame.from_dict(feature_rows, orient="index", columns=names).loc[frame.index]
    if not np.isfinite(features.to_numpy(float)).all():
        raise ValueError("Non-finite history features")
    baseline = {name: np.asarray([baseline_rows[i][name] for i in frame.index], float)
                for name in (next(iter(baseline_rows.values())).keys() if baseline_rows else ())}
    checkpoint(progress, cancelled, stage, 1.0)
    return features, baseline, [history_counts[i] for i in frame.index]


def constrained_predictions(model, X):
    core = vendor_module("night_core")
    prediction = np.asarray(model.predict(X), dtype=float)
    if prediction.shape != (len(X), 2) or not np.isfinite(prediction).all():
        raise ValueError("Invalid model predictions")
    return np.asarray([core.constrain(*row) for row in prediction], dtype=float)


def participant_errors(frame, predictions):
    error = np.asarray(predictions) - frame[TARGETS].to_numpy(float)
    values = frame[["cohort", "subject_id"]].copy()
    values["onset_mae_min"] = np.abs(error[:, 0])
    values["wake_mae_min"] = np.abs(error[:, 1])
    values["joint_mae_min"] = np.abs(error).mean(axis=1)
    values["interval_mae_min"] = np.abs(error[:, 1] - error[:, 0])
    values["onset_mse"] = error[:, 0] ** 2
    values["wake_mse"] = error[:, 1] ** 2
    return values.groupby(["cohort", "subject_id"], sort=True).mean()


def validation_score(frame, predictions):
    return float(participant_errors(frame, predictions).groupby(level="cohort").mean().joint_mae_min.mean())


def bootstrap_means(values, replicates, seed):
    values = np.asarray(values, float)
    rng = np.random.default_rng(seed)
    return values[rng.integers(0, len(values), size=(replicates, len(values)))].mean(axis=1)


def metric_summaries(frame, predictions_by_model, replicates, seed, split):
    summaries, errors, bootstraps = [], {}, {}
    for model_name, predictions in predictions_by_model.items():
        people = participant_errors(frame, predictions)
        errors[model_name] = people
        cohort_rows = []
        bootstraps[model_name] = {}
        for number, cohort in enumerate(COHORTS):
            subset = people.xs(cohort, level="cohort")
            means = subset.mean()
            boots = bootstrap_means(subset.joint_mae_min, replicates, seed + number)
            bootstraps[model_name][cohort] = boots
            low, high = np.quantile(boots, [0.025, 0.975])
            result = {
                "model_id": f"{cohort.lower()}.{model_name}", "model": model_name, "cohort": cohort,
                "split": split, "averaging": "equal_participant", "n_participants": len(subset),
                "n_nights": int(frame.cohort.eq(cohort).sum()),
                **{key: float(means[key]) for key in ("onset_mae_min", "wake_mae_min", "joint_mae_min", "interval_mae_min")},
                "onset_rmse_min": float(np.sqrt(means.onset_mse)),
                "wake_rmse_min": float(np.sqrt(means.wake_mse)),
                "ci95_low": float(low), "ci95_high": float(high),
            }
            summaries.append(result)
            cohort_rows.append(result)
        macro_boots = np.mean([bootstraps[model_name][c] for c in COHORTS], axis=0)
        low, high = np.quantile(macro_boots, [0.025, 0.975])
        summaries.append({
            "model_id": f"macro.{model_name}", "model": model_name, "cohort": "MACRO", "split": split,
            "averaging": "equal_cohort_then_equal_participant", "n_participants": len(people),
            "n_nights": len(frame),
            **{key: float(np.mean([r[key] for r in cohort_rows]))
               for key in ("onset_mae_min", "wake_mae_min", "joint_mae_min", "interval_mae_min")},
            "ci95_low": float(low), "ci95_high": float(high),
        })
    return summaries, errors, bootstraps


def paired_comparisons(errors, bootstraps, selected):
    results = []
    for reference in ("existing_data", "existing_hybrid", "recent_median"):
        differences, samples = [], []
        for cohort in COHORTS:
            left = errors[reference].xs(cohort, level="cohort").joint_mae_min
            right = errors[selected].xs(cohort, level="cohort").joint_mae_min
            if not left.index.equals(right.index):
                raise ValueError("Paired participants do not align")
            difference = float((left - right).mean())
            # Same seed/ordered participants in metric_summaries gives paired resamples.
            boots = bootstraps[reference][cohort] - bootstraps[selected][cohort]
            low, high = np.quantile(boots, [0.025, 0.975])
            results.append({"reference": reference, "model": selected, "cohort": cohort,
                            "improvement_min": difference, "ci95_low": float(low), "ci95_high": float(high),
                            "n_participants": len(left)})
            differences.append(difference)
            samples.append(boots)
        low, high = np.quantile(np.mean(samples, axis=0), [0.025, 0.975])
        results.append({"reference": reference, "model": selected, "cohort": "MACRO",
                        "improvement_min": float(np.mean(differences)),
                        "ci95_low": float(low), "ci95_high": float(high),
                        "n_participants": len(errors[selected])})
    return results


def run_pooled_retraining(config, progress=None, cancelled=None):
    database = Path(config["database_path"]).resolve()
    output = Path(config["output_dir"]).resolve()
    if output.exists():
        raise FileExistsError("Choose a new output directory; completed evidence is immutable")
    seed = int(config.get("seed", 20261104))
    replicates = int(config.get("bootstrap_replicates", 5000))
    if not 100 <= replicates <= 20000:
        raise ValueError("bootstrap_replicates must be between 100 and 20000")
    checkpoint(progress, cancelled, "verify_database", 0)
    frame, source_manifest, database_hash = load_database(database)
    source_provenance = get_provenance()
    core, features_module = vendor_module("night_core"), vendor_module("robust_features")
    arms = {"data": list(features_module.DATA_V2), "hybrid": list(features_module.HYBRID_V2)}
    split_counts = [{"cohort": cohort, "split": split, "n_nights": len(group),
                     "n_participants": group.subject_id.nunique()}
                    for (cohort, split), group in frame.groupby(["cohort", "split"], sort=True)]
    protocol = {
        "schema": "sleepwell.pooled_training.v1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": STATUS, "cohorts": list(COHORTS), "seed": seed,
        "database_path": str(database), "database_sha256": database_hash,
        "code_sha256": sha256(__file__), "split_counts": split_counts,
        "target": "next main-sleep onset/final wake minutes after local 20:00",
        "fit_split": "train only; no train+validation refit", "selection_split": "validation",
        "selection": "lowest equal-cohort/equal-participant joint MAE per arm, then across arms; fixed candidate order breaks exact ties",
        "fit_weighting": "each cohort has half the total weight; each participant equal within cohort; weights normalized to mean 1",
        "features": arms, "personalization_policy": core.GRADUAL_POLICY,
        "cohort_feature": False, "demographic_features": False, "candidates": CANDIDATES,
        "preprocessing": "train-only median imputation, predictor 1/99-percentile clipping, constant removal, standardization; targets unchanged",
        "hgb": {"early_stopping": False, "learning_rate": 0.05, "max_iter": 120, "l2_regularization": 10.0},
        "bootstrap": {"unit": "participant", "replicates": replicates, "interval": "95% percentile",
                      "macro": "independent within-cohort resampling, equal cohort weights",
                      "scope": "conditional on fitted/selected models; exploratory reused holdout; no multiplicity adjustment"},
        "time_contract": source_manifest["time_contract"],
        "service_promotion": False,
        "environment": {"python": platform.python_version(), "numpy": np.__version__,
                        "pandas": pd.__version__, "scikit_learn": sklearn.__version__},
        "references": ["https://scikit-learn.org/1.8/common_pitfalls.html",
                       "https://scikit-learn.org/1.8/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html"],
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "protocol.json", protocol)
    train = frame.loc[frame.split.eq("train")].copy()
    validation = frame.loc[frame.split.eq("validation")].copy()
    prior = prior_from_train(train)
    write_json(output / "prior.json", prior)
    x_train, _, _ = replay_features(train, prior, progress, cancelled, "train_features")
    x_val, _, _ = replay_features(validation, prior, progress, cancelled, "validation_features")
    weights = balanced_weights(train)
    y_train = train[TARGETS].to_numpy(float)
    models, val_predictions, candidates = {}, {}, []
    with threadpool_limits(limits=2):
        for arm, names in arms.items():
            best = None
            for candidate in CANDIDATES:
                checkpoint(progress, cancelled, f"fit_{arm}_{candidate}", len(candidates) / 12)
                model = make_model(candidate, seed)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ConvergenceWarning)
                    model.fit(x_train[names].to_numpy(float), y_train, model__sample_weight=weights)
                converged = not any(issubclass(item.category, ConvergenceWarning) for item in caught)
                prediction = constrained_predictions(model, x_val[names].to_numpy(float))
                score = validation_score(validation, prediction)
                per_cohort = participant_errors(validation, prediction).groupby(level="cohort").mean()
                candidates.append({"arm": arm, "candidate": candidate, "converged": converged,
                                   "macro_joint_mae_min": score,
                                   "cmi_joint_mae_min": float(per_cohort.loc["CMI", "joint_mae_min"]),
                                   "mesa_joint_mae_min": float(per_cohort.loc["MESA", "joint_mae_min"])})
                if converged and (best is None or score < best[0]):
                    best = score, candidate, model, prediction
            if best is None:
                raise ValueError(f"No converged candidate for {arm}")
            score, candidate, model, prediction = best
            name = f"pooled_{arm}"
            models[name] = {"pipeline": model, "features": names, "prior": prior,
                            "candidate": candidate, "validation_macro_joint_mae_min": score,
                            "personalization_policy": core.GRADUAL_POLICY,
                            "database_sha256": database_hash, "feature_contract": features_module.FEATURE_CONTRACT}
            val_predictions[name] = prediction
            joblib.dump(models[name], output / f"{name}.joblib")
    selected = min(models, key=lambda name: models[name]["validation_macro_joint_mae_min"])
    pd.DataFrame(candidates).to_csv(output / "validation_candidates.csv", index=False)
    lock = {"locked_utc": datetime.now(timezone.utc).isoformat(), "selected_model": selected,
            "selected_by_arm": {name: {"candidate": m["candidate"],
                                       "validation_macro_joint_mae_min": m["validation_macro_joint_mae_min"]}
                                for name, m in models.items()},
            "protocol_sha256": sha256(output / "protocol.json"),
            "models_sha256": {name: sha256(output / f"{name}.joblib") for name in models},
            "test_used_for_selection": False}
    write_json(output / "selection_lock.json", lock)
    checkpoint(progress, cancelled, "selection_locked_before_test", 1)

    # Only after the lock is written do we construct Test features or score any Test prediction.
    test = frame.loc[frame.split.eq("test")].copy()
    fusion = vendor_module("fusion_forecast")
    vendor_root = Path(fusion.__file__).parent
    frozen_models = {cohort: fusion.PortableCohortModel(
        vendor_root / "models" / f"robust_{cohort.lower()}_v2.json", cohort.lower(),
    ) for cohort in COHORTS}
    x_test, predictions, history_counts = replay_features(
        test, prior, progress, cancelled, "test_features_and_frozen_baselines", frozen_models,
    )
    reload_errors = {}
    with threadpool_limits(limits=2):
        for name, artifact in models.items():
            X = x_test[artifact["features"]].to_numpy(float)
            predictions[name] = constrained_predictions(artifact["pipeline"], X)
            if sha256(output / f"{name}.joblib") != lock["models_sha256"][name]:
                raise ValueError("Model artifact changed after selection lock")
            reloaded = joblib.load(output / f"{name}.joblib")
            difference = float(np.abs(constrained_predictions(reloaded["pipeline"], X) - predictions[name]).max())
            if difference > 1e-10:
                raise ValueError("Reloaded model predictions changed")
            reload_errors[name] = difference
    checkpoint(progress, cancelled, "participant_bootstrap", 0)
    summaries, errors, bootstraps = metric_summaries(test, predictions, replicates, seed, "test")
    validation_summaries, _, _ = metric_summaries(validation, val_predictions, replicates, seed, "validation")
    comparisons = paired_comparisons(errors, bootstraps, selected)
    private_rows = []
    for name, prediction in predictions.items():
        for number, row in enumerate(test.itertuples()):
            private_rows.append({"cohort": row.cohort, "subject_id": row.subject_id, "split": "test",
                                 "cutoff": row.cutoff, "model": name, "history_count": history_counts[number],
                                 "actual_onset_min": row.bedtime_min, "actual_wake_min": row.wake_min,
                                 "predicted_onset_min": float(prediction[number, 0]),
                                 "predicted_wake_min": float(prediction[number, 1])})
    pd.DataFrame(private_rows).to_csv(output / "private_test_predictions.csv", index=False)
    pd.DataFrame(summaries).to_csv(output / "test_metrics.csv", index=False)
    pd.DataFrame(comparisons).to_csv(output / "paired_comparisons.csv", index=False)
    if sha256(database) != database_hash:
        raise ValueError("Input database changed during training")
    evaluation = {
        "schema": "sleepwell.pooled_evaluation.v1", "status": STATUS,
        "completed_utc": datetime.now(timezone.utc).isoformat(), "selected_model": selected,
        "selection": lock, "prior": prior, "split_counts": split_counts,
        "test_metrics": summaries, "validation_metrics": validation_summaries,
        "paired_comparisons": comparisons,
        "verification": {"database_unchanged": True, "samsung_rows": 0,
                         "test_used_for_selection": False, "reload_max_abs_error_min": reload_errors,
                         "finite_features": True, "failed_predictions": 0,
                         "test_participants": len(errors[selected]), "test_nights": len(test)},
        "limitations": [
            "Reused historical Test; not independent external or prospective validation.",
            "CMI event-pair labels and MESA ACT-derived weak labels differ; neither is PSG staging.",
            "Pooled retraining also changes training weighting and fits gradual_v1 features; differences do not isolate pooling alone.",
            "Confidence intervals are conditional on fitted models and exclude selection/training uncertainty; comparisons are exploratory.",
            "One shared predictor and prior; no cohort ID or unavailable demographic variables used as predictors.",
            "No automatic replacement of installed frozen models; no evidence of improved sleep or clinical benefit.",
        ],
        "artifacts": {"output_dir": str(output), "models": {name: str(output / f"{name}.joblib") for name in models},
                      "private_test_predictions": str(output / "private_test_predictions.csv")},
    }
    write_json(output / "evaluation.json", evaluation)
    hashes = {path.name: sha256(path) for path in output.iterdir() if path.is_file()}
    write_json(output / "manifest.json", {"artifacts_sha256": hashes,
                                         "database_sha256": database_hash, "source_provenance": source_provenance})
    checkpoint(progress, cancelled, "completed", 1)
    return {"mode": "pooled_retraining", "raw": evaluation, "summary": summaries,
            "provenance": {"evidence": "new_pooled_model_training_reused_holdout", "input_synthetic": False,
                           "input_sha256": database_hash, "input_source": database.name,
                           "database_path": str(database), "protocol_sha256": lock["protocol_sha256"],
                           "evaluation_path": str(output / "evaluation.json"),
                           "source_provenance": source_provenance},
            "warnings": evaluation["limitations"]}
