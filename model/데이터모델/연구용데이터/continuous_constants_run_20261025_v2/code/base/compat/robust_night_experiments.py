"""Locked research improvement: nested Train selection, unchanged reused Test."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import warnings
import platform
import importlib.metadata
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge, HuberRegressor
from sklearn.model_selection import GroupKFold, TimeSeriesSplit
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_selection import VarianceThreshold
from sklearn.impute import SimpleImputer
from threadpoolctl import threadpool_limits

from next_night_experiments import prior_from_train, score, paired_interval, project, dump, sha, TARGETS, SEED
from night_features_v2 import extended_features, DATA_V2, HYBRID_V2
from night_core import local_time, DATA_FEATURES, PHYSICAL_FEATURES
from night_estimators import TrainQuantileClip

CANDIDATES = {
    "ridge100": {"family": "ridge", "alpha": 100.},
    "ridge1000": {"family": "ridge", "alpha": 1000.},
    "huber": {"family": "huber", "alpha": 1., "epsilon": 1.35},
    "absolute7": {"family": "hgb", "loss": "absolute_error", "leaves": 7, "min_leaf": 20},
    "absolute15": {"family": "hgb", "loss": "absolute_error", "leaves": 15, "min_leaf": 40},
    "squared7": {"family": "hgb", "loss": "squared_error", "leaves": 7, "min_leaf": 20},
}
ARMS = {"data": list(DATA_V2), "hybrid": list(HYBRID_V2)}
SENSORS = ["hr_mean", "hr_std", "hr_min", "hr_max", "hr_count", "late_hr_mean",
           "late_hr_count", "steps", "step_bins", "active_step_bins"]


def arm_features(frame):
    result = dict(ARMS)
    if frame.cohort.iloc[0] == "samsung" and all(name in frame for name in SENSORS):
        result["data_sensors"] = list(DATA_V2) + SENSORS
        result["hybrid_sensors"] = list(HYBRID_V2) + SENSORS
    return result


def make_model(name):
    c = CANDIDATES[name]
    if c["family"] == "ridge":
        estimator = Ridge(alpha=c["alpha"])
    elif c["family"] == "huber":
        estimator = MultiOutputRegressor(HuberRegressor(alpha=c["alpha"], epsilon=c["epsilon"],
                                                       max_iter=1000, tol=1e-5))
    else:
        estimator = MultiOutputRegressor(HistGradientBoostingRegressor(
            loss=c["loss"], max_leaf_nodes=c["leaves"], min_samples_leaf=c["min_leaf"],
            learning_rate=.05, max_iter=120, l2_regularization=10.,
            early_stopping=False, random_state=SEED))
    return Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                     ("clip", TrainQuantileClip()), ("variance", VarianceThreshold()),
                     ("scale", StandardScaler()), ("model", estimator)])


def balanced_weights(frame):
    counts = frame.groupby("subject_id").subject_id.transform("size").to_numpy(float)
    weights = 1 / counts
    return weights / weights.mean()


def replay(frame, prior):
    output, states = [], []
    grouping = ["subject_id", "split"] if frame.cohort.iloc[0] == "samsung" else ["subject_id"]
    for _, group in frame.groupby(grouping, sort=True):
        group = group.sort_values("cutoff")
        records = []
        enrollment = str(group.cutoff.iloc[0])
        for row in group.itertuples():
            values, state = extended_features(records, local_time(str(row.cutoff)), prior, enrollment)
            if all(name in frame for name in SENSORS):
                values.update({name: float(getattr(row, name)) for name in SENSORS})
            output.append({"row_id": row.row_id, **values})
            states.append({"row_id": row.row_id, **state})
            records.append({"source": "research", "bed": str(row.bed), "wake": str(row.wake),
                            "observed_at": str(row.observed_at)})
    align = lambda rows: pd.DataFrame(rows).set_index("row_id").loc[frame.row_id].reset_index()
    return align(output), align(states)


def fit_model(name, frame, features, names):
    model = make_model(name)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        model.fit(features[names].to_numpy(float), frame[TARGETS].to_numpy(float),
                  model__sample_weight=balanced_weights(frame))
    converged = not any(issubclass(w.category, ConvergenceWarning) for w in caught)
    return model, converged


def inner_select(train, output, cohort):
    arms = arm_features(train)
    folds = TimeSeriesSplit(n_splits=3, gap=1) if cohort == "samsung" else GroupKFold(n_splits=3)
    results = []
    failures = set()
    for number, (fit_ids, val_ids) in enumerate(folds.split(train, groups=train.subject_id)):
        fit, held = train.iloc[fit_ids].copy(), train.iloc[val_ids].copy()
        fit["split"], held["split"] = "train", "validation"
        if cohort == "samsung" and not (fit.cutoff.max() < held.cutoff.min()
                                       and fit.observed_at.max() < held.cutoff.min()):
            raise ValueError("Inner training outcomes unavailable before validation decision")
        if cohort != "samsung" and set(fit.subject_id) & set(held.subject_id):
            raise ValueError("Inner subject overlap")
        prior = prior_from_train(fit)
        xf, _ = replay(fit, prior)
        xv, _ = replay(held, prior)
        baseline_predictions = {
            "physical": xv[["physical_bed", "physical_wake"]].to_numpy(float),
            "population_mean": np.tile([prior["bed_offset"], prior["wake_offset"]], (len(held), 1)),
            "population_median": np.tile(fit.groupby("subject_id")[TARGETS].median().median().to_numpy(), (len(held), 1)),
            "recent_median": xv[["median14_bed", "median14_wake"]].to_numpy(float),
        }
        for arm, pred in baseline_predictions.items():
            metrics = score(held[TARGETS].to_numpy(float), project(pred), held.subject_id, cohort != "samsung")
            results.append({"fold": number, "arm": arm, "candidate": "fixed",
                            "converged": True, "fit_rows": len(fit), **metrics})
        for arm, names in arms.items():
            for candidate in CANDIDATES:
                model, converged = fit_model(candidate, fit, xf, names)
                pred = project(model.predict(xv[names].to_numpy(float)))
                metrics = score(held[TARGETS].to_numpy(float), pred, held.subject_id, cohort != "samsung")
                key = (arm, candidate)
                if not converged:
                    failures.add(key)
                results.append({"fold": number, "arm": arm, "candidate": candidate,
                                "converged": converged, "fit_rows": len(fit), **metrics})
        print(f"{cohort}: Train-only fold {number+1}/3 complete", flush=True)
    result = pd.DataFrame(results)
    result.to_csv(output / "inner_cv.csv", index=False)
    selected = {}
    for arm in arms:
        scores = result.loc[result.arm.eq(arm)].groupby("candidate").joint_mae_min.mean()
        eligible = {key: float(value) for key, value in scores.items() if (arm, key) not in failures}
        if not eligible:
            raise ValueError("No converged candidate")
        selected[arm] = min(eligible, key=eligible.get)
    choices = []
    complexity = {"population_mean": 0, "population_median": 0, "recent_median": 1,
                  "physical": 2, "data": 3, "hybrid": 4}
    if "data_sensors" in arms:
        complexity.update(data_sensors=4, hybrid_sensors=5)
    for arm, candidate in {**{name: "fixed" for name in complexity if name not in arms}, **selected}.items():
        scores = result.loc[result.arm.eq(arm) & result.candidate.eq(candidate)].joint_mae_min
        choices.append({"arm": arm, "mean": float(scores.mean()),
                        "se": float(scores.std(ddof=1) / np.sqrt(len(scores))), "complexity": complexity[arm]})
    best = min(choices, key=lambda x: x["mean"])
    threshold = best["mean"] + best["se"]
    # A prespecified one-SE simplicity rule avoids treating tiny physics gains as reliable.
    winner = min([c for c in choices if c["mean"] <= threshold],
                 key=lambda c: (c["complexity"], c["mean"], c["arm"]))["arm"]
    dump(output / "train_selection.json", {"selected": selected, "winner": winner,
         "choices": choices, "one_se_threshold": threshold, "rejected_nonconverged": sorted(list(failures))})
    return selected, winner


def target_flagger(train, frame):
    y = train[TARGETS].to_numpy(float)
    center = np.median(y, axis=0)
    scale = np.maximum(30., 1.4826 * np.median(np.abs(y-center), axis=0))
    scores = np.max(np.abs(frame[TARGETS].to_numpy(float)-center)/scale, axis=1)
    return scores > 3.5, {"center": center.tolist(), "scale": scale.tolist(), "threshold": 3.5,
                          "meaning": "statistical rarity, not a proven sensor error; no target removed"}


def diagnose_v1(frame, old_dir, xf, output):
    train = frame.split.eq("train").to_numpy()
    v1 = pd.read_parquet(old_dir / "features.parquet")
    x = v1.loc[train, list(DATA_FEATURES)].to_numpy(float)
    z = v1.loc[train, list(PHYSICAL_FEATURES)].to_numpy(float)
    scaled = StandardScaler().fit_transform(x)
    pred = Ridge(alpha=1.).fit(scaled, z).predict(scaled)
    denom = ((z-z.mean(axis=0))**2).sum(axis=0)
    r2 = np.divide(((z-pred)**2).sum(axis=0), denom, out=np.zeros_like(denom), where=denom>1e-12)
    diagnostics = {"physics_train_std": dict(zip(PHYSICAL_FEATURES, z.std(axis=0).tolist())),
                   "physics_reconstructible_from_data_train_r2": {
                       name: (float(1-r2[i]) if denom[i] > 1e-12 else None) for i, name in enumerate(PHYSICAL_FEATURES)},
                   "note": "Descriptive in-sample dependency check; not generalization R2 or causal proof."}
    dump(output / "v1_redundancy.json", diagnostics)


def run_cohort(old_dir, output, cohort, sensors=None):
    output.mkdir()
    frame = pd.read_parquet(old_dir / "episodes.parquet")
    frame["cohort"] = cohort
    if cohort == "samsung" and sensors is not None:
        sensor_frame = pd.read_parquet(sensors)
        frame["cutoff"] = pd.to_datetime(frame.cutoff, utc=True)
        sensor_frame["cutoff"] = pd.to_datetime(sensor_frame.cutoff, utc=True)
        frame = frame.merge(sensor_frame[["cutoff", *SENSORS]], on="cutoff", how="left",
                            validate="one_to_one", indicator=True)
        if not frame["_merge"].eq("both").all():
            raise ValueError("Missing sensor cutoff coverage")
        frame = frame.drop(columns="_merge")
        frame["cutoff"] = frame.cutoff.dt.tz_convert("Asia/Seoul")
    arms = arm_features(frame)
    if not (frame.row_id.tolist() == list(range(len(frame)))
            and not frame.duplicated(["subject_id", "cutoff"]).any()
            and frame.bed_offset.between(0, 960).all() and (frame.wake_offset <= 1440).all()
            and (frame.wake_offset-frame.bed_offset).between(30, 960).all()):
        raise ValueError("Episode identity or target bounds invalid")
    train = frame.loc[frame.split.eq("train")].copy()
    selected, winner = inner_select(train, output, cohort)
    prior = prior_from_train(train)
    x, states = replay(frame, prior)
    x.to_parquet(output / "features.parquet", index=False)
    states.to_parquet(output / "states.parquet", index=False)
    diagnose_v1(frame, old_dir, x, output)
    masks = {s: frame.split.eq(s).to_numpy() for s in ("train", "validation", "test")}
    predictions, models = {}, {}
    for arm, candidate in selected.items():
        model, converged = fit_model(candidate, train, x.loc[masks["train"]], arms[arm])
        if not converged:
            raise ValueError("Final selected estimator did not converge; refuse Test evaluation")
        predictions[arm] = project(model.predict(x[arms[arm]].to_numpy(float)))
        models[arm] = model
        joblib.dump(model, output / f"{arm}.joblib")
    train_medians = train.groupby("subject_id")[TARGETS].median().median().to_numpy()
    predictions.update({
        "physical": x[["physical_bed", "physical_wake"]].to_numpy(float),
        "population_mean": np.tile([prior["bed_offset"], prior["wake_offset"]], (len(frame), 1)),
        "population_median": project(np.tile(train_medians, (len(frame), 1))),
        "recent_median": project(x[["median14_bed", "median14_wake"]].to_numpy(float)),
    })
    y = frame[TARGETS].to_numpy(float)
    va, te = masks["validation"], masks["test"]
    validation = {k: score(y[va], p[va], frame.loc[va].subject_id, cohort != "samsung") for k, p in predictions.items()}
    # Freeze all choices before reading prior Test predictions or scoring Test labels.
    dump(output / "validation_selection.json", {"selected": selected, "winner": winner, "validation": validation,
         "winner_source": "Train-only CV one-SE rule; reused Validation does not select",
         "test_status": "previously exposed reused Validation/Test; not fresh confirmation"})
    result = {k: score(y[te], p[te], frame.loc[te].subject_id, cohort != "samsung") for k, p in predictions.items()}
    comparisons = {}
    for base in ("physical", "data"):
        comparisons[f"v2_hybrid_vs_v2_{base}"] = paired_interval(y[te], predictions[base][te], predictions["hybrid"][te],
                                                               frame.loc[te].subject_id, cohort == "samsung")
    if "hybrid_sensors" in predictions:
        for base in ("data_sensors", "hybrid"):
            comparisons[f"v2_hybrid_sensors_vs_v2_{base}"] = paired_interval(
                y[te], predictions[base][te], predictions["hybrid_sensors"][te], frame.loc[te].subject_id, True)
    for arm in ("data", "hybrid"):
        old = pd.read_csv(old_dir / f"test_{arm}.csv")
        if not (old.subject_id.astype(str).tolist() == frame.loc[te].subject_id.astype(str).tolist()
                and np.array_equal(pd.to_datetime(old.cutoff, utc=True).to_numpy(),
                                   pd.to_datetime(frame.loc[te].cutoff, utc=True).to_numpy())
                and np.allclose(old[TARGETS].to_numpy(), y[te])):
            raise ValueError("Prior Test rows/cutoffs/targets do not align")
        oldp = old[["predicted_bed_offset", "predicted_wake_offset"]].to_numpy(float)
        comparisons[f"v2_{arm}_vs_v1_{arm}"] = paired_interval(y[te], oldp, predictions[arm][te],
                                                              frame.loc[te].subject_id, cohort == "samsung")
        comparisons[f"v2_selected_vs_v1_{arm}"] = paired_interval(y[te], oldp, predictions[winner][te],
                                                                  frame.loc[te].subject_id, cohort == "samsung")
    flags, flag_definition = target_flagger(train, frame)
    qc = {"definition": flag_definition, "removed_rows": 0,
          "split_flagged_counts": {s: int((mask & flags).sum()) for s, mask in masks.items()},
          "split_counts": {s: int(mask.sum()) for s, mask in masks.items()}}
    subgroups = {}
    for label, mask in {"statistically_rare": te & flags, "other": te & ~flags}.items():
        if mask.any():
            subgroups[label] = {k: score(y[mask], p[mask], frame.loc[mask].subject_id, cohort != "samsung") for k, p in predictions.items()}
    for arm, pred in predictions.items():
        detail = frame.loc[te, ["row_id", "subject_id", "cutoff", *TARGETS]].copy()
        detail["predicted_bed_offset"], detail["predicted_wake_offset"] = pred[te, 0], pred[te, 1]
        detail["rare_by_train_mad"] = flags[te]
        detail.to_csv(output / f"test_{arm}.csv", index=False)
    # Reload fitted artifacts and compare every prediction; no app promotion.
    reload_errors = {}
    for arm in arms:
        loaded = joblib.load(output / f"{arm}.joblib")
        reproduced = project(loaded.predict(x[arms[arm]].to_numpy(float)))
        reload_errors[arm] = float(np.max(np.abs(reproduced-predictions[arm])))
        if reload_errors[arm] > 1e-9:
            raise ValueError("Serialized model prediction mismatch")
    artifact = {"version": "robust-next-night-research-v2", "status": "research_only", "feature_names": arms,
                "candidates": selected, "train_cv_winner": winner, "prior": prior,
                "limitations": ["reused Test", "fixed physiological tau", "no new information from physics alone",
                                "no service promotion", "wake-time availability assumption"],
                "model_sha256": {arm: sha(output / f"{arm}.joblib") for arm in arms}}
    dump(output / "artifact.json", artifact)
    summary = {"cohort": cohort, "inner_selected": selected, "train_cv_winner": winner, "validation": validation,
               "test": result, "comparisons": comparisons, "qc": qc, "test_subgroups": subgroups,
               "reload_max_error": reload_errors}
    dump(output / "summary.json", summary)
    print(json.dumps({"cohort": cohort, "winner": winner, "test_joint_mae": {k: v["joint_mae_min"] for k, v in result.items()}}, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--personal-sensors", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = {
        "version": "robust-next-night-v2-20261020", "seed": SEED, "candidate_grid": CANDIDATES,
        "features": {**ARMS, **({"data_sensors": list(DATA_V2) + SENSORS,
                                 "hybrid_sensors": list(HYBRID_V2) + SENSORS} if args.personal_sensors else {})},
        "target": "unchanged local20 onset/finalwake; same v1 rows/splits; all Test rows retained",
        "inner": "3-fold participant-disjoint GroupKFold; Samsung 3 forward folds gap1, each validation block enrollment reset",
        "fold_prior": "recomputed from fold training labels only",
        "processing": "Train-only predictor 1/99 percentile clipping + constant removal + standard scaling in every fold; no target clipping/removal; equal participant fit weights",
        "selection": "Train inner-CV selects candidate per data/hybrid arm, then one-SE simplicity rule selects final arm including baselines; Validation does not select",
        "test_status": "adaptive analysis of reused Validation and Test; never claim fresh external confirmation; no Validation/Test tuning in this run",
        "outlier_policy": "Train median/MAD threshold3.5 descriptive rarity flag only; real extreme behavior is not automatically data error",
        "date_availability": "causal replay with observed_at and completed wake before20; Samsung separate enrollment per split",
        "service_promotion": False,
        "sensor_ablation": "Samsung only: same ten pre20 HR/steps features for paired data_sensors/hybrid_sensors; no movement available only in Test",
        "sensor_sha256": sha(args.personal_sensors) if args.personal_sensors else None,
        "software": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
                     "sklearn": sklearn.__version__, "joblib": joblib.__version__},
        "input_hashes": {f"{c}/{name}": sha(args.previous / c / name) for c in ("cmi", "mesa", "samsung")
                         for name in ("episodes.parquet", "features.parquet", "test_data.csv", "test_hybrid.csv")},
    }
    dump(args.output / "protocol.json", protocol)
    packages = ("numpy", "scipy", "pandas", "scikit-learn", "pyarrow", "joblib", "threadpoolctl")
    environment = "\n".join(f"{name}=={importlib.metadata.version(name)}" for name in packages) + "\n"
    (args.output / "environment.txt").write_text(environment, encoding="utf-8")
    dump(args.output / "environment_sha256.json", {"environment.txt": sha(args.output / "environment.txt"),
                                                 "python": platform.python_version()})
    snapshot = args.output / "code_snapshot"
    snapshot.mkdir()
    for source in [Path(__file__), Path(__file__).with_name("night_features_v2.py"),
                   Path(__file__).with_name("night_estimators.py"),
                   Path(__file__).with_name("next_night_experiments.py"),
                   Path(__file__).resolve().parents[2] / "web-app/night_core.py"]:
        shutil.copy2(source, snapshot / source.name)
    dump(args.output / "code_sha256.json", {p.name: sha(p) for p in snapshot.iterdir()})
    summary = {}
    with threadpool_limits(limits=2):
        for cohort in ("cmi", "mesa", "samsung"):
            summary[cohort] = run_cohort(args.previous / cohort, args.output / cohort, cohort, args.personal_sensors)
    # Confirm inputs remained frozen during the complete run.
    for relative, expected in protocol["input_hashes"].items():
        if sha(args.previous / relative) != expected:
            raise ValueError("Previous experiment input changed during run")
    if args.personal_sensors and sha(args.personal_sensors) != protocol["sensor_sha256"]:
        raise ValueError("Sensor cache changed during run")
    dump(args.output / "summary.json", summary)


if __name__ == "__main__":
    main()
