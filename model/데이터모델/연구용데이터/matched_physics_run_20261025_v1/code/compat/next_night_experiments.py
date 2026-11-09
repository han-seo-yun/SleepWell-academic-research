"""Locked next-night ablations, subject-held-out and personal chronological.

Run once into a new output directory. Selection is persisted before Test metrics.
Online personalization may use earlier completed Test nights, never future ones.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

WEB = Path(__file__).resolve().parents[2] / "web-app"
sys.path.insert(0, str(WEB))
from night_core import CONTRACT, DATA_FEATURES, PHYSICAL_FEATURES, features, local_time, constrain

SEED = 20261020
ALPHAS = (1., 10., 100., 1000.)
TARGETS = ["bed_offset", "wake_offset"]
ARMS = {
    "data": list(DATA_FEATURES),
    "hybrid": list(DATA_FEATURES + PHYSICAL_FEATURES),
    "hybrid_frozen": list(DATA_FEATURES + PHYSICAL_FEATURES),
}


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_cohorts(frame):
    frame = frame.copy()
    frame["cohort"] = frame["cohort"].str.lower()
    if set(frame.cohort) != {"cmi", "mesa", "samsung"}:
        raise ValueError("Expected exactly CMI, MESA and Samsung cohorts")
    return frame


def age_group(age):
    if pd.isna(age) or not 18 <= float(age) <= 80:
        return None
    return "18-29" if age < 30 else "30-44" if age < 45 else "45-59" if age < 60 else "60-80"


def build_population_reference(path, output):
    """NHANES Train-only survey-weighted descriptive means, not sleep need."""
    frame = pd.read_parquet(path).copy()
    from research_tabular import subject_split
    frame["split"] = frame.SEQN.map(lambda x: subject_split(str(int(x))))
    frame = frame.loc[(frame.split == "train") & frame.weekday_sleep_hours.between(3, 12)
                      & frame.RIDAGEYR.between(18, 80) & (frame.WTINT2YR > 0)].copy()
    frame["age_group"] = frame.RIDAGEYR.map(age_group)
    frame["sex"] = frame.RIAGENDR.map({1: "male", 2: "female"})
    # Work + transport + recreation, as reported, not accelerometer exercise.
    cols = ["leisure_exercise_min_per_day", "transport_walk_cycle_min_per_day", "work_activity_min_per_day"]
    activity = frame[cols].sum(axis=1, min_count=len(cols))
    frame["activity_band"] = pd.cut(activity, [-1, 30, 90, float("inf")],
                                     labels=["low", "medium", "high"], right=False).astype(object)
    def summarize(group):
        return {"n_people": len(group), "mean_sleep_min": float(np.average(
            group.weekday_sleep_hours * 60, weights=group.WTINT2YR))}
    groups = []
    for group_cols in [["age_group", "sex", "activity_band"], ["age_group", "sex"], ["age_group"]]:
        for key, rows in frame.groupby(group_cols, observed=True):
            key = key if isinstance(key, tuple) else (key,)
            label = dict(zip(group_cols, key))
            groups.append({"age_group": label["age_group"], "sex": label.get("sex", "any"),
                           "activity_band": label.get("activity_band", "any"), **summarize(rows)})
    result = {
        "schema_version": 1, "artifact_type": "sleepwell_population_prior",
        "version": "nhanes-train-demographic-20261020-v1",
        "source": "NHANES 2017–2018 자기보고 평일 수면시간 · 학습 분할만 · 조사 가중 평균",
        "source_sha256": sha(path), "supported_age": [18, 80],
        "groups": groups, "global": summarize(frame),
        "unsupported_fields": ["occupation", "usual_bed"],
        "interpretation": "관측 수면시간 평균이며 최적 수면량·권장 효과·한국인 기준을 뜻하지 않습니다.",
        "activity_definition": "일·이동·여가 활동의 자기보고 분/일 합계",
        "minimum_group_people": 30,
    }
    dump(output, result)
    return result


def prior_from_train(frame):
    train = frame.loc[frame.split == "train"]
    # Each participant contributes equally, not in proportion to recording days.
    subject_means = train.groupby("subject_id")[TARGETS].mean()
    return {"bed_offset": float(subject_means.bed_offset.mean()),
            "wake_offset": float(subject_means.wake_offset.mean()),
            "n_people": len(subject_means), "n_nights": len(train),
            "method": "equal-participant mean from Train nights only"}


def feature_tables(frame, prior):
    rows, frozen, states = [], [], []
    groups = ["subject_id", "split"] if frame.cohort.iloc[0] == "samsung" else ["subject_id"]
    for _, group in frame.groupby(groups, sort=True):
        group = group.sort_values("cutoff")
        enrolled = str(group.cutoff.iloc[0])
        records = []
        for _, row in group.iterrows():
            cutoff = local_time(str(row.cutoff))
            values, state = features(records, cutoff, prior, enrolled_at=enrolled)
            fixed, _ = features(records, cutoff, prior, personalize=False, enrolled_at=enrolled)
            rows.append({"row_id": row.row_id, **values})
            frozen.append({"row_id": row.row_id, **fixed})
            states.append({"row_id": row.row_id, **state})
            records.append({"bed": str(row.bed), "wake": str(row.wake),
                            "observed_at": str(row.observed_at), "source": "research"})
    def align(values):
        return pd.DataFrame(values).set_index("row_id").loc[frame.row_id].reset_index()
    return align(rows), align(frozen), align(states)


def project(values):
    return np.array([constrain(b, w) for b, w in values], dtype=float)


def errors(actual, pred):
    base = np.abs(actual - pred)
    # Sleep episode interval is not TST: intra-night wake has not been removed.
    duration = np.abs((actual[:, 1] - actual[:, 0]) - (pred[:, 1] - pred[:, 0]))
    return np.column_stack([base, duration, base.mean(axis=1)])


def score(actual, pred, subjects, macro=True):
    err = errors(actual, pred)
    agg = pd.DataFrame(err, columns=["onset_mae_min", "wake_mae_min", "interval_mae_min", "joint_mae_min"])
    agg["subject"] = np.asarray(subjects)
    averages = agg.groupby("subject").mean().mean() if macro else agg.drop(columns="subject").mean()
    return {**{k: float(v) for k, v in averages.items()}, "n_nights": len(actual),
            "n_people": len(set(subjects)), "averaging": "equal participant" if macro else "equal night"}


def paired_interval(actual, baseline, changed, subjects, temporal=False):
    delta = errors(actual, baseline) - errors(actual, changed)
    rng = np.random.default_rng(SEED)
    names = ["onset", "wake", "interval", "joint"]
    if temporal:
        n = len(delta)
        values = []
        for _ in range(2000):
            starts = rng.integers(0, n, size=int(np.ceil(n / 7)))
            ix = np.concatenate([(start + np.arange(7)) % n for start in starts])[:n]
            values.append(delta[ix].mean(axis=0))
        observed = delta.mean(axis=0)
        method = "circular blocks of 7 consecutive evaluated nights; one person"
    else:
        grouped = pd.DataFrame(delta).assign(subject=np.asarray(subjects)).groupby("subject").mean().to_numpy()
        values = [grouped[rng.integers(0, len(grouped), len(grouped))].mean(axis=0) for _ in range(2000)]
        observed = grouped.mean(axis=0)
        method = "paired participant cluster bootstrap"
    ci = np.quantile(values, [.025, .975], axis=0)
    return {"method": method, "replicates": 2000, "positive_means": "lower error for changed model",
            "metrics": {name: {"error_reduction_min": float(observed[i]), "ci95": ci[:, i].tolist()}
                        for i, name in enumerate(names)}}


def portable(model, names):
    scaler, ridge = model[0], model[1]
    return {"feature_order": names, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
            "intercepts": np.asarray(ridge.intercept_).tolist(),
            "coefficients": np.asarray(ridge.coef_).tolist()}


def experiment(frame, out, cohort):
    out.mkdir(parents=True, exist_ok=False)
    frame = frame.sort_values(["subject_id", "cutoff"]).reset_index(drop=True)
    frame["row_id"] = np.arange(len(frame))
    enrollment_groups = ["subject_id", "split"] if cohort == "samsung" else ["subject_id"]
    frame["enrolled_at"] = frame.groupby(enrollment_groups).cutoff.transform("min")
    bed, wake, cutoff = [pd.to_datetime(frame[c]) for c in ["bed", "wake", "cutoff"]]
    frame["bed_offset"] = (bed - cutoff).dt.total_seconds() / 60
    frame["wake_offset"] = (wake - cutoff).dt.total_seconds() / 60
    if not all((frame.split == s).any() for s in ["train", "validation", "test"]):
        raise ValueError(f"{cohort}: missing split")
    if cohort != "samsung" and frame.groupby("subject_id").split.nunique().max() != 1:
        raise ValueError("Participant split leakage")
    if cohort == "samsung":
        for a, b in [("train", "validation"), ("validation", "test")]:
            if frame.loc[frame.split == a, "cutoff"].max() >= frame.loc[frame.split == b, "cutoff"].min():
                raise ValueError("Temporal split overlap")
    prior = prior_from_train(frame)
    normal, frozen, states = feature_tables(frame, prior)
    first_rows = frame.groupby(enrollment_groups).head(1).index
    if not (states.loc[first_rows, "valid_days"] == 0).all():
        raise ValueError("Deployment enrollment must start with zero post-enrollment history")
    all_features = frame[["row_id", "subject_id", "split", "cutoff"]].merge(normal, on="row_id")
    all_features.to_parquet(out / "features.parquet", index=False)
    states.to_parquet(out / "personal_states.parquet", index=False)
    frame.to_parquet(out / "episodes.parquet", index=False)
    masks = {key: (frame.split == key).to_numpy() for key in ["train", "validation", "test"]}
    y = frame[TARGETS].to_numpy(float)
    models, validation, predictions = {}, {}, {}
    temporal = cohort == "samsung"
    va = masks["validation"]
    for name, values in {
        "population": np.tile([prior["bed_offset"], prior["wake_offset"]], (len(frame), 1)),
        "recent_median": normal[["recent_bed", "recent_wake"]].to_numpy(),
        "physical": normal[["physical_bed", "physical_wake"]].to_numpy(),
    }.items():
        predictions[name] = project(values)
        validation[name] = score(y[va], predictions[name][va], frame.loc[va, "subject_id"], not temporal)
    selected = {}
    for arm, names in ARMS.items():
        table = frozen if arm == "hybrid_frozen" else normal
        x = table[names].to_numpy(float)
        for alpha in ALPHAS:
            key = f"{arm}/ridge_{alpha:g}"
            model = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
            model.fit(x[masks["train"]], y[masks["train"]])
            # Predictions are label-free; Test labels are scored only after selection JSON.
            predictions[key] = project(model.predict(x))
            validation[key] = score(y[va], predictions[key][va], frame.loc[va, "subject_id"], not temporal)
            models[key] = (model, names)
        selected[arm] = min((k for k in models if k.startswith(arm + "/")),
                            key=lambda k: validation[k]["joint_mae_min"])
    winner = min(["physical", selected["data"], selected["hybrid"]],
                 key=lambda k: validation[k]["joint_mae_min"])
    dump(out / "validation_selection.json", {"selected": selected, "winner": winner, "validation": validation})
    te = masks["test"]
    results = []
    for name, prediction in predictions.items():
        results.append({"candidate": name, "selected_arm": name in selected.values(),
                        **{f"validation_{k}": v for k, v in validation[name].items()},
                        **{f"test_{k}": v for k, v in score(y[te], prediction[te], frame.loc[te, "subject_id"], not temporal).items()}})
    pd.DataFrame(results).to_csv(out / "metrics.csv", index=False)
    selected_predictions = {}
    for arm, key in {"physical": "physical", **selected, "population": "population", "recent_median": "recent_median"}.items():
        p = predictions[key]
        selected_predictions[arm] = p
        detail = frame.loc[te, ["subject_id", "cutoff", *TARGETS]].copy()
        detail["predicted_bed_offset"], detail["predicted_wake_offset"] = p[te, 0], p[te, 1]
        detail["history_days"] = states.loc[te, "valid_days"].to_numpy()
        detail.to_csv(out / f"test_{arm}.csv", index=False)
    comparisons = {}
    for base in ["physical", "data", "hybrid_frozen", "recent_median"]:
        comparisons[f"hybrid_vs_{base}"] = paired_interval(
            y[te], selected_predictions[base][te], selected_predictions["hybrid"][te],
            frame.loc[te, "subject_id"], temporal)
    # Isolate adaptation in predictions from the SAME fitted hybrid model.
    hybrid_model, hybrid_names = models[selected["hybrid"]]
    frozen_same_model = project(hybrid_model.predict(frozen[hybrid_names].to_numpy(float)))
    comparisons["hybrid_adaptation_same_coefficients"] = paired_interval(
        y[te], frozen_same_model[te], selected_predictions["hybrid"][te],
        frame.loc[te, "subject_id"], temporal)
    curves = []
    for lower, upper in [(0, 7), (7, 14), (14, 28), (28, 100000)]:
        band = te & states.valid_days.between(lower, upper - 1).to_numpy()
        if not band.any():
            continue
        for arm in ["physical", "data", "hybrid", "hybrid_frozen"]:
            curves.append({"past_days": f"{lower}-{upper-1}" if upper < 100000 else "28+",
                           "arm": arm, **score(y[band], selected_predictions[arm][band],
                                              frame.loc[band, "subject_id"], not temporal)})
    pd.DataFrame(curves).to_csv(out / "personalization_curve.csv", index=False)
    summary = {
        "cohort": cohort, "n_people": int(frame.subject_id.nunique()),
        "n_nights": len(frame), "split_nights": frame.split.value_counts().to_dict(),
        "split_people": frame.groupby("split").subject_id.nunique().to_dict(),
        "selected": selected, "validation_winner": winner, "prior": prior,
        "test": {arm: score(y[te], p[te], frame.loc[te, "subject_id"], not temporal)
                 for arm, p in selected_predictions.items()},
        "comparisons": comparisons,
        "physical_fallback_nights": int(states.physical_fallback.sum()),
        "test_personalized_nights": int((te & (states.personal_weight > 0)).sum()),
        "test_previously_seen_in_other_tasks": True,
        "online_adaptation": "Earlier completed outcomes in the enrollment block may update personal state; Samsung resets at each split; population/model coefficients never update on Validation/Test.",
        "limitations": ["retrospective availability assumed at wake", "main sleep interval, not PSG total sleep time",
                       "no recommendation-effect validation", "fixed physiological tau; behavioral phase/threshold adaptation"],
    }
    dump(out / "summary.json", summary)
    artifact = {
        "schema_version": 1, "artifact_type": "sleepwell_next_night_ridge",
        "feature_contract": CONTRACT, "version": f"{cohort}-next-night-20261020-v1",
        "status": "research_evaluated", "decision_time": "20:00", "cohort": cohort,
        "prior": prior, "selected": selected, "validation_winner": winner,
        "models": {arm: portable(*models[key]) for arm, key in selected.items()},
        "test_metrics": summary["test"],
        "supports_general_population": False,
        "runtime_eligibility": "linked_personal_history" if cohort == "samsung" else "research_only",
        "requires_personal_archive_link": cohort == "samsung",
        "label_source": str(frame.label_source.iloc[0]) if "label_source" in frame else cohort + " main sleep boundaries",
        "evaluation_protocol": "Train-only coefficient fit, Validation joint MAE selection, held-out Test, causal personal replay",
    }
    dump(out / "night_model.json", artifact)
    print(json.dumps({"cohort": cohort, "summary": summary["test"], "comparisons": comparisons}, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = {
        "version": "next-night-comparison-20261020-v1", "seed": SEED,
        "target": "20:00 local -> following principal sleep onset and final wake; interval=wake-onset",
        "cohorts": "separate; CMI/MESA existing subject splits; Samsung previous chronological split",
        "alphas": ALPHAS, "arms": ARMS,
        "selection": "minimum Validation equal-participant joint onset/wake MAE; Samsung equal-night",
        "primary_comparison": "hybrid versus data and physical; positive paired error reduction with 95% CI",
        "adaptation": "all completed available personal nights; population first14days; alpha=n/(n+14) after14dates and14elapsed days",
        "enrollment_proxy": "earliest eligible 20:00 decision per participant; Samsung resets independently at each split, allowing only post-enrollment records as in deployment",
        "fixed_physiology": {"tau_wake_h": 18.2, "tau_sleep_h": 4.2, "circadian_amplitude": .15},
        "physical_observation_model": "S+C hysteresis thresholds anchored to stationary prior/personal schedule; five-minute integration",
        "feature_availability": "all history observed_at<wake_cutoff and wake<decision; no future sensors or target-night summary",
        "source_sha256": sha(args.episodes),
        "software": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__, "sklearn": sklearn.__version__},
        "no_test_tuning": True,
    }
    dump(args.output / "protocol.json", protocol)
    snapshot = args.output / "code_snapshot"
    snapshot.mkdir()
    sources = [Path(__file__), Path(__file__).with_name("next_night_dataset.py"), WEB / "night_core.py"]
    for source in sources:
        shutil.copy2(source, snapshot / source.name)
    dump(args.output / "code_sha256.json", {source.name: sha(source) for source in sources})
    data = canonical_cohorts(pd.read_parquet(args.episodes))
    all_results = {}
    with threadpool_limits(limits=2):
        for cohort, frame in data.groupby("cohort", sort=True):
            print(f"Starting {cohort}: {len(frame)} nights", flush=True)
            all_results[cohort] = experiment(frame.copy(), args.output / cohort, cohort)
    build_population_reference(args.workspace / "보조데이터셋/processed/nhanes_2017_adult_activity_sleep.parquet",
                               args.output / "population_prior.json")
    dump(args.output / "summary.json", all_results)


if __name__ == "__main__":
    main()
