"""Reproducible, research-only Samsung forecasting and NHANES ablations.

Input exports remain untouched. Outputs contain individual research data: keep
outside public repositories. No model is promoted to the web recommendation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

SEED = 20261010
DIARY = ["prior_bedtime_min", "prior_wake_min", "prior_interval_min",
         "previous_interval_min", "history_count", "history_age_days", "weekend"]
SENSORS = ["hr_mean", "hr_std", "hr_min", "hr_max", "hr_count", "late_hr_mean",
           "late_hr_count", "steps", "step_bins", "active_step_bins"]


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def subject_split(key):
    value = int(hashlib.sha256(f"{SEED}:{key}".encode()).hexdigest()[:8], 16) / 2**32
    return "train" if value < .70 else "validation" if value < .85 else "test"


def chronological_split(n):
    return np.array(["train"] * int(.7*n) + ["validation"] * (int(.85*n)-int(.7*n))
                    + ["test"] * (n-int(.85*n)))


def personal_features(episodes, hr, steps):
    """Use interval END availability and completed, fresh past episodes only."""
    e = episodes.loc[episodes.is_main_sleep & episodes.episode_qc_pass].copy()
    e["sleep_start"] = pd.to_datetime(e.sleep_start)
    e["sleep_end"] = pd.to_datetime(e.sleep_end)
    e = e.loc[(e.sleep_end > e.sleep_start) & e.episode_interval_min.between(30, 960)]
    e["cutoff"] = e.sleep_start.dt.normalize() + pd.Timedelta(hours=20)
    e.loc[e.sleep_start < e.cutoff, "cutoff"] -= pd.Timedelta(days=1)
    e["bedtime_min"] = (e.sleep_start-e.cutoff).dt.total_seconds()/60
    e["wake_min"] = (e.sleep_end-e.cutoff).dt.total_seconds()/60
    e = (e.loc[e.bedtime_min.between(0, 960) & e.wake_min.le(1440)]
         .sort_values(["cutoff", "episode_interval_min"], ascending=[True, False])
         .drop_duplicates("cutoff").sort_values("cutoff").reset_index(drop=True))
    hr = hr.loc[hr.hr_qc_pass].copy()
    steps = steps.loc[steps.steps_qc_pass].copy()
    hr["timestamp"] = pd.to_datetime(hr.timestamp)
    steps["timestamp"] = pd.to_datetime(steps.timestamp)
    rows = []
    for _, row in e.iterrows():
        cutoff = row.cutoff
        past = e.loc[(e.sleep_end < cutoff) & (e.sleep_end >= cutoff-pd.Timedelta(days=28))
                     & (e.cutoff < cutoff)].sort_values("sleep_end").tail(7)
        h = hr.loc[(hr.timestamp >= cutoff.normalize()) &
                   (hr.timestamp + pd.Timedelta(minutes=1) <= cutoff)]
        st = steps.loc[(steps.timestamp >= cutoff.normalize()) &
                       (steps.timestamp + pd.Timedelta(minutes=10) <= cutoff)]
        recent = h.loc[h.timestamp >= cutoff-pd.Timedelta(hours=3), "heart_rate"].dropna()
        hv = h.heart_rate.dropna()
        record = {"subject_id": "samsung_personal_001", "cutoff": cutoff,
                  "sleep_start": row.sleep_start, "sleep_end": row.sleep_end,
                  "bedtime_min": row.bedtime_min, "wake_min": row.wake_min,
                  "label_source": "consumer_device_episode_boundary",
                  "prior_bedtime_min": past.bedtime_min.median(),
                  "prior_wake_min": past.wake_min.median(),
                  "prior_interval_min": past.episode_interval_min.median(),
                  "previous_interval_min": past.episode_interval_min.iloc[-1] if len(past) else np.nan,
                  "history_count": len(past),
                  "history_age_days": (cutoff-past.sleep_end.iloc[-1]).total_seconds()/86400 if len(past) else np.nan,
                  "weekend": int(cutoff.weekday() >= 5),
                  "hr_mean": hv.mean(), "hr_std": hv.std(), "hr_min": hv.min(),
                  "hr_max": hv.max(), "hr_count": len(hv), "late_hr_mean": recent.mean(),
                  "late_hr_count": len(recent), "steps": st.steps_10min.sum(min_count=1),
                  "step_bins": len(st), "active_step_bins": int((st.steps_10min > 0).sum())}
        rows.append(record)
    result = pd.DataFrame(rows)
    result["split"] = chronological_split(len(result))
    return result


def candidate(kind, iteration=60):
    if kind == "median":
        return DummyRegressor(strategy="median")
    if kind.startswith("ridge"):
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True,
                                          keep_empty_features=True),
                             StandardScaler(), Ridge(alpha=float(kind.split("_")[1])))
    return make_pipeline(SimpleImputer(strategy="median", add_indicator=True,
                                      keep_empty_features=True),
                         HistGradientBoostingRegressor(max_iter=iteration, learning_rate=.05,
                             max_leaf_nodes=7, min_samples_leaf=15, l2_regularization=5,
                             early_stopping=False, random_state=SEED))


def metrics(y, p, weight=None):
    err = np.abs(np.asarray(y)-p)
    return {"n": len(err), "mae": float(np.mean(err)),
            "rmse": float(np.sqrt(np.mean(err**2))), "median_ae": float(np.median(err)),
            "p90_ae": float(np.quantile(err, .9)),
            "weighted_mae": float(np.average(err, weights=weight)) if weight is not None else None}


def paired_ci(y, baseline, selected, block=1):
    """Positive means lower error than baseline; sequential blocks for one user."""
    delta = np.abs(np.asarray(y)-baseline)-np.abs(np.asarray(y)-selected)
    rng = np.random.default_rng(SEED)
    n = len(delta)
    values = []
    for _ in range(2000):
        starts = rng.integers(0, n, size=int(np.ceil(n/block)))
        idx = np.concatenate([(s+np.arange(block)) % n for s in starts])[:n]
        values.append(float(delta[idx].mean()))
    return {"mean_error_reduction": float(delta.mean()),
            "ci95": np.quantile(values, [.025, .975]).tolist(),
            "bootstrap_replicates": 2000, "block_consecutive_observations": block,
            "not_complex_survey_design_ci": True}


def run_regression(frame, groups, targets, output, temporal=False, age_prior=False):
    output.mkdir(parents=True, exist_ok=False)
    if frame.empty or frame.split.nunique() != 3:
        raise ValueError("Need nonempty Train, Validation, Test")
    frame.to_parquet(output / "model_data.parquet", index=False)
    frame[[c for c in ["subject_id", "cutoff", "split"] if c in frame]].to_csv(output/"splits.csv", index=False)
    protocol = {"seed": SEED, "groups": groups, "targets": targets,
                "selection": "minimum validation MAE; test never selects",
                "models": ["median", "ridge_1", "ridge_10", "ridge_100", "hgb_20", "hgb_60", "hgb_120"],
                "fit": "Train only, no Train+Validation refit", "temporal": temporal,
                "age_prior_train_only": age_prior}
    dump(output/"protocol.json", protocol)
    results, predictions, curves, selected_summary = [], [], [], {}
    for target in targets:
        tr, va, te = [frame.loc[(frame.split == split) & frame[target].notna()]
                      for split in ["train", "validation", "test"]]
        if min(len(tr), len(va), len(te)) == 0:
            raise ValueError(f"Empty target split: {target}")
        fitted, val_scores = {}, {}
        for group, features in groups.items():
            for name in protocol["models"]:
                if name == "median" and group != next(iter(groups)):
                    continue
                key = f"{group}/{name}"
                model = candidate(name, int(name.split("_")[-1]) if name.startswith("hgb") else 60)
                model.fit(tr[features], tr[target])
                fitted[key] = (model, features)
                vp = model.predict(va[features])
                val_scores[key] = metrics(va[target], vp)
                if name == "hgb_120":
                    transformed_va = model[:-1].transform(va[features])
                    transformed_tr = model[:-1].transform(tr[features])
                    for i, (vpred, tpred) in enumerate(zip(model[-1].staged_predict(transformed_va),
                                                           model[-1].staged_predict(transformed_tr)), 1):
                        curves.append({"target": target, "group": group, "boosting_iteration": i,
                                       "validation_mae": metrics(va[target], vpred)["mae"],
                                       "train_mae": metrics(tr[target], tpred)["mae"]})
        prior_map = None
        if age_prior:
            prior_map = tr.groupby("age_decade")[target].median().to_dict()
            fallback = float(tr[target].median())
            vp = va.age_decade.map(prior_map).fillna(fallback).to_numpy()
            val_scores["train_age_decade_prior"] = metrics(va[target], vp)
        chosen = min(val_scores, key=lambda key: val_scores[key]["mae"])
        # Persist selection before reading test labels for scoring.
        dump(output/f"{target}_validation_selection.json", {"selected": chosen, "validation": val_scores})
        test_preds = {}
        for key, (model, features) in fitted.items():
            p = model.predict(te[features])
            test_preds[key] = p
        if age_prior:
            test_preds["train_age_decade_prior"] = te.age_decade.map(prior_map).fillna(fallback).to_numpy()
        weights = te["survey_weight"].to_numpy() if "survey_weight" in te else None
        for key, pred in test_preds.items():
            row = {"target": target, "candidate": key, "selected_by_validation": key == chosen,
                   "train_n": len(tr), "validation_n": len(va),
                   **{f"validation_{k}": v for k, v in val_scores[key].items()},
                   **{f"test_{k}": v for k, v in metrics(te[target], pred, weights).items()}}
            results.append(row)
            detail = te[["subject_id", target]].copy()
            if "cutoff" in te:
                detail["cutoff"] = te.cutoff
            detail["target"] = target
            detail["candidate"] = key
            detail["prediction"] = pred
            detail["absolute_error"] = abs(te[target]-pred)
            predictions.append(detail)
        baseline_key = f"{next(iter(groups))}/median"
        selected_summary[target] = {"selected": chosen,
            "test": metrics(te[target], test_preds[chosen], weights),
            "baseline": metrics(te[target], test_preds[baseline_key], weights),
            "paired_error_reduction": paired_ci(te[target], test_preds[baseline_key], test_preds[chosen], 7 if temporal else 1)}
        if chosen in fitted:
            joblib.dump({"model": fitted[chosen][0], "features": fitted[chosen][1],
                         "target": target, "research_only": True}, output/f"{target}_research_model.joblib")
        else:
            dump(output/f"{target}_research_prior.json", {"prior": prior_map, "fallback": fallback,
                                                         "research_only": True})
    pd.DataFrame(results).to_csv(output/"experiment_metrics.csv", index=False)
    pd.concat(predictions, ignore_index=True).to_parquet(output/"test_predictions.parquet", index=False)
    pd.DataFrame(curves).to_csv(output/"boosting_learning_curves.csv", index=False)
    summary = {"rows": len(frame), "subjects": int(frame.subject_id.nunique()),
               "splits": frame.split.value_counts().to_dict(), "selected": selected_summary,
               "models_saved_for_research_only": True}
    dump(output/"summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.workspace/"보조데이터셋/개인기록_과거작업/260925/한서윤데이터_전처리/output"
    paths = [source/f"{name}.parquet" for name in ["sleep_episodes", "heart_rate_1min", "steps_10min"]]
    nh_path = args.workspace/"보조데이터셋/processed/nhanes_2017_adult_activity_sleep.parquet"
    manifest = {"source_sha256": {str(p.relative_to(args.workspace)): digest(p) for p in paths+[nh_path]},
                "code_sha256": digest(__file__), "python": platform.python_version(),
                "pandas": pd.__version__, "numpy": np.__version__, "sklearn": sklearn.__version__,
                "native_thread_limit": 2, "research_only": True}
    dump(args.output/"manifest.json", manifest)
    with threadpool_limits(limits=2):
        personal = personal_features(*(pd.read_parquet(p) for p in paths))
        psummary = run_regression(personal, {"diary": DIARY, "sensors": SENSORS,
                                            "combined": DIARY+SENSORS},
                                  ["bedtime_min", "wake_min"], args.output/"personal", temporal=True)
        nh = pd.read_parquet(nh_path)
        nh["subject_id"] = nh.SEQN.map(lambda v: f"nhanes_2017_{int(v)}")
        nh["split"] = nh.subject_id.map(subject_split)
        nh["survey_weight"] = nh.WTINT2YR
        nh["age_decade"] = (nh.RIDAGEYR//10).astype(int)
        nh = nh.loc[nh.weekday_sleep_hours.notna() & (nh.survey_weight > 0)].copy()
        if nh.subject_id.duplicated().any():
            raise ValueError("Duplicate NHANES subjects")
        demo = ["RIDAGEYR", "RIAGENDR"]
        activity = ["leisure_exercise_min_per_day", "transport_walk_cycle_min_per_day",
                    "work_activity_min_per_day", "sedentary_min_per_day"]
        nsummary = run_regression(nh, {"demographics": demo, "activity_added": demo+activity},
                                  ["weekday_sleep_hours"], args.output/"nhanes", age_prior=True)
    dump(args.output/"summary.json", {"personal": psummary, "nhanes": nsummary})
    print(json.dumps({"personal": psummary, "nhanes": nsummary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
