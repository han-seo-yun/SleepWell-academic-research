"""Prespecified local MESA work-context comparison; never edits the app or inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.multioutput import MultiOutputRegressor
from threadpoolctl import threadpool_limits

from linkage import load_schedule, merge_mesa_schedule

ROOT = Path(r"E:\학술제")
WEB = ROOT / "기타/SleepWell-integrated/web-app"
ASSETS = ROOT / "앱_소빈/통합 v8/app/src/main/assets/models"
EPISODES = ROOT / "데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa_episodes.csv"
SOURCE = ROOT / "데이터셋/MESA/datasets/mesa-sleep-dataset-0.8.0.csv"
sys.path.insert(0, str(WEB))
from fusion_forecast import PortableCohortModel, _pipeline_predict
from night_core import GRADUAL_POLICY, constrain, local_time
from robust_features import HYBRID_V2

SEED = 20261106
ARMS = ("control", "schedule", "schedule_extra")
TARGETS = ["bedtime_min", "wake_min"]
FEATURES = list(HYBRID_V2)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, data):
    def convert(value):
        if isinstance(value, np.generic):
            return value.item()
        if pd.isna(value):
            return None
        raise TypeError(type(value).__name__)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                    default=convert, allow_nan=False), encoding="utf-8")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def source_paths():
    return [SOURCE, EPISODES, ASSETS / "robust_mesa_v2.json", ASSETS / "robust_cmi_v2.json",
            WEB / "fusion_forecast.py", WEB / "night_core.py", WEB / "robust_features.py",
            Path(__file__), Path(__file__).with_name("linkage.py"),
            Path(__file__).with_name("PROTOCOL.md")]


def verify_sources(out):
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    for path, expected in manifest["sha256"].items():
        if sha(path) != expected:
            raise ValueError(f"Input or procedure changed after lock: {path}")


def project(prediction):
    return np.asarray([constrain(*x) for x in prediction], float)


def fit_transform_spec(x, frame):
    median = np.nanmedian(x, axis=0)
    if not np.isfinite(median).all():
        raise ValueError("Unexpected empty original feature")
    imputed = np.where(np.isnan(x), median, x)
    extra = pd.to_numeric(frame.extra_hours_days_per_month, errors="coerce").to_numpy(float, na_value=np.nan)
    observed = extra[np.isfinite(extra)]
    if not len(observed):
        raise ValueError("No Train extra-work responses")
    return {"median": median, "low": np.quantile(imputed, .01, axis=0),
            "high": np.quantile(imputed, .99, axis=0), "extra_median": float(np.median(observed))}


def matrix(x, frame, spec, arm):
    base = np.clip(np.where(np.isnan(x), spec["median"], x), spec["low"], spec["high"])
    if arm == "control":
        return base
    codes = pd.to_numeric(frame.work_schedule_code, errors="coerce").to_numpy(float, na_value=np.nan)
    known = np.isin(codes, np.arange(1, 8))
    context = [np.column_stack([codes == c for c in range(1, 8)]).astype(float), (~known)[:, None].astype(float)]
    if arm == "schedule_extra":
        days = pd.to_numeric(frame.extra_hours_days_per_month, errors="coerce").to_numpy(float, na_value=np.nan)
        missing = ~np.isfinite(days)
        context.extend([np.where(missing, spec["extra_median"], days)[:, None] / 31., missing[:, None].astype(float)])
    return np.column_stack([base, *context])


def eligible(frame, supported):
    return (frame.work_schedule_code.isin(supported) & frame.age.between(60, 80)).to_numpy(bool)


def subject_losses(frame, pred):
    errors = pred - frame[TARGETS].to_numpy(float)
    data = pd.DataFrame({"subject_id": frame.subject_id.to_numpy(),
                         "onset": np.abs(errors[:, 0]), "wake": np.abs(errors[:, 1]),
                         "joint": np.abs(errors).mean(axis=1),
                         "interval": np.abs(errors[:, 1] - errors[:, 0])})
    return data.groupby("subject_id", sort=True).mean()


def summary(frame, pred):
    loss = subject_losses(frame, pred)
    return {"people": len(loss), "nights": len(frame), **loss.mean().to_dict()}


def paired(frame, comparator, candidate):
    diff = subject_losses(frame, comparator) - subject_losses(frame, candidate)
    values = diff.joint.to_numpy()
    rng = np.random.default_rng(SEED)
    means = values[rng.integers(0, len(values), size=(5000, len(values)))].mean(axis=1)
    return {"people": len(values), "improvement_min": float(values.mean()),
            "ci95": np.quantile(means, [.025, .975]).tolist(),
            "method": "paired participant bootstrap; positive=comparator-candidate;5000 replicates"}


def prepare(out):
    if (out / "input_manifest.json").exists():
        raise ValueError("Preparation already exists; use a new run folder")
    out.mkdir(parents=True, exist_ok=True)
    dump(out / "input_manifest.json", {"started_utc": stamp(), "seed": SEED,
         "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
         "sklearn": sklearn.__version__, "sha256": {str(p): sha(p) for p in source_paths()}})
    frame = pd.read_csv(EPISODES)
    if frame.groupby(["cohort", "subject_id"]).split.nunique().max() != 1:
        raise ValueError("Participant overlap across splits")
    if frame.duplicated(["cohort", "subject_id", "cutoff"]).any():
        raise ValueError("Duplicate participant cutoff")
    joined, audit = merge_mesa_schedule(frame, load_schedule(SOURCE))
    joined.to_parquet(out / "private_linked_episodes.parquet", index=False)
    dump(out / "linkage_audit.json", audit)
    mesa = joined.loc[joined.cohort.eq("MESA")].reset_index(drop=True)
    models = {c: PortableCohortModel(ASSETS / f"robust_{c}_v2.json", c) for c in ("mesa", "cmi")}
    rows = {}
    n = 0
    for _, group in mesa.groupby("subject_id", sort=True):
        group = group.sort_values("cutoff")
        enrollment = str(group.cutoff.iloc[0])
        history = []
        for row in group.itertuples():
            cutoff = local_time(row.cutoff)
            result = {c: m.predict({"created_at": enrollment}, history, cutoff,
                                  personalization_policy=GRADUAL_POLICY) for c, m in models.items()}
            record = {"f__" + k: v for k, v in result["mesa"]["features"].items()}
            record["history_count"] = result["mesa"]["personalization"]["valid_days"]
            for c in models:
                record[f"baseline_{c}_bed"], record[f"baseline_{c}_wake"] = result[c]["hybrid"]
            rows[row.Index] = record
            # No target or future record enters this night's feature vector.
            history.append({"source": "research", "bed": row.bed, "wake": row.wake, "observed_at": row.observed_at})
            n += 1
            if n % 1000 == 0:
                print(f"Causal replay {n}/{len(mesa)}", flush=True)
    features = pd.DataFrame.from_dict(rows, orient="index").loc[mesa.index]
    replay = pd.concat([mesa, features], axis=1)
    if not np.isfinite(replay[["f__" + k for k in FEATURES]].to_numpy(float)).all():
        raise ValueError("Non-finite replay")
    replay.to_parquet(out / "private_replay.parquet", index=False)
    people = mesa.drop_duplicates("subject_id")
    train_counts = people.loc[people.split.eq("train")].work_schedule_code.value_counts()
    supported = sorted(int(k) for k, v in train_counts.items() if v >= 50)
    eligibility = eligible(replay, supported)
    dump(out / "support_lock.json", {"locked_utc": stamp(), "supported_codes": supported,
         "minimum_train_people": 50, "age_scope": [60, 80], "source_age_range": [float(people.age.min()), float(people.age.max())],
         "train_category_people": {str(k): int(v) for k, v in train_counts.items()},
         "eligible_rows": int(eligibility.sum()),
         "eligible_people": int(replay.loc[eligibility].subject_id.nunique()),
         "feature_order": FEATURES, "replay_sha256": sha(out / "private_replay.parquet")})
    verify_sources(out)
    print("Replay and label-free support lock complete", flush=True)


def baseline(frame, cohort):
    return frame[[f"baseline_{cohort}_bed", f"baseline_{cohort}_wake"]].to_numpy(float)


def predictions(frame, models, spec, supported, guarded=True):
    old_mesa, cmi = baseline(frame, "mesa"), baseline(frame, "cmi")
    old = (old_mesa + cmi) / 2
    mask = eligible(frame, supported) if guarded else frame.work_schedule_code.notna().to_numpy()
    x = frame[["f__" + k for k in FEATURES]].to_numpy(float)
    result = {"current_v8": old}
    for arm, model in models.items():
        new_mesa = project(model.predict(matrix(x, frame, spec, arm)))
        new_mesa[~mask] = old_mesa[~mask]
        result[arm] = (new_mesa + cmi) / 2
        if not np.array_equal(result[arm][~mask], old[~mask]):
            raise AssertionError("Unsupported/missing fallback changed")
    return result


def fit(out):
    verify_sources(out)
    if (out / "selection_lock.json").exists():
        raise ValueError("Selection already locked")
    support = json.loads((out / "support_lock.json").read_text(encoding="utf-8"))
    if sha(out / "private_replay.parquet") != support["replay_sha256"]:
        raise ValueError("Replay changed")
    replay = pd.read_parquet(out / "private_replay.parquet")
    train = replay.loc[replay.split.eq("train")].copy()
    val = replay.loc[replay.split.eq("validation")].copy()
    x = train[["f__" + k for k in FEATURES]].to_numpy(float)
    spec = fit_transform_spec(x, train)
    counts = train.groupby("subject_id").subject_id.transform("size").to_numpy(float)
    weights = 1 / counts
    weights /= weights.mean()
    y = train[TARGETS].to_numpy(float)
    models = {}
    dump(out / "fit_started.json", {"started_utc": stamp(), "protocol_sha256": sha(Path(__file__).with_name("PROTOCOL.md")),
         "train_people": int(train.subject_id.nunique()), "train_nights": len(train)})
    with threadpool_limits(limits=2):
        for arm in ARMS:
            model = MultiOutputRegressor(HistGradientBoostingRegressor(loss="absolute_error", max_leaf_nodes=7,
                    min_samples_leaf=20, learning_rate=.05, max_iter=120, l2_regularization=10.,
                    early_stopping=False, random_state=SEED))
            model.fit(matrix(x, train, spec, arm), y, sample_weight=weights)
            models[arm] = model
            print(f"Fitted {arm} on Train only", flush=True)
        validation = predictions(val, models, spec, support["supported_codes"])
    metrics = {arm: summary(val, pred) for arm, pred in validation.items()}
    selected = min(("schedule", "schedule_extra"), key=lambda a: (metrics[a]["joint"], a != "schedule"))
    joblib.dump({"models": models, "spec": spec}, out / "research_models.joblib")
    dump(out / "selection_lock.json", {"locked_before_test_metrics_utc": stamp(), "selected": selected,
         "validation_metrics": metrics, "selection_rule": "lowest Validation joint MAE; tie=schedule",
         "models_sha256": sha(out / "research_models.joblib"), "automatic_promotion": False})
    verify_sources(out)
    print(json.dumps({"selected_on_validation": selected, "validation_metrics": metrics}), flush=True)


def evaluate(out):
    verify_sources(out)
    if (out / "evaluation.json").exists():
        raise ValueError("Evaluation already exists; do not overwrite")
    support = json.loads((out / "support_lock.json").read_text(encoding="utf-8"))
    lock = json.loads((out / "selection_lock.json").read_text(encoding="utf-8"))
    if sha(out / "research_models.joblib") != lock["models_sha256"]:
        raise ValueError("Model lock mismatch")
    if sha(out / "private_replay.parquet") != support["replay_sha256"]:
        raise ValueError("Replay lock mismatch")
    bundle = joblib.load(out / "research_models.joblib")
    replay = pd.read_parquet(out / "private_replay.parquet")
    selected = lock["selected"]
    result = {"started_utc": stamp(), "selected": selected, "splits": {}, "comparisons": {}, "strata": [],
              "limitations": ["historical Test reused", "retrospective questionnaire availability assumption",
                 "ACT-derived main-sleep endpoints", "no independent young-adult/shift-worker validation",
                 "predictive errors do not measure recommendation or health effects"]}
    all_predictions = []
    for split in ("validation", "test"):
        frame = replay.loc[replay.split.eq(split)].copy()
        with threadpool_limits(limits=2):
            pred = predictions(frame, bundle["models"], bundle["spec"], support["supported_codes"])
            raw = predictions(frame, bundle["models"], bundle["spec"], support["supported_codes"], guarded=False)
        result["splits"][split] = {a: summary(frame, p) for a, p in pred.items()}
        result["comparisons"][split] = {a: {b: paired(frame, pred[b], pred[a]) for b in ("current_v8", "control")}
                                          for a in ("schedule", "schedule_extra")}
        result.setdefault("unguarded_descriptive", {})[split] = {a: summary(frame, p) for a, p in raw.items()}
        strata = {"eligible": eligible(frame, support["supported_codes"]),
                  "fallback": ~eligible(frame, support["supported_codes"]),
                  "history_0": frame.history_count.eq(0).to_numpy(),
                  "history_1_2": frame.history_count.between(1, 2).to_numpy(),
                  "history_3_plus": frame.history_count.ge(3).to_numpy()}
        for code in range(1, 8):
            strata[f"schedule_{code}"] = frame.work_schedule_code.eq(code).fillna(False).to_numpy(bool)
        for label, mask in strata.items():
            if not mask.any():
                continue
            result["strata"].append({"split": split, "stratum": label,
                "metrics": {a: summary(frame.loc[mask], p[mask]) for a, p in pred.items()},
                "selected_vs_current": paired(frame.loc[mask], pred["current_v8"][mask], pred[selected][mask])})
        saved = frame[["subject_id", "split", "cutoff", "work_schedule_code", "age", "history_count", *TARGETS]].copy()
        saved["eligible"] = eligible(frame, support["supported_codes"])
        for arm, p in pred.items():
            saved[f"{arm}_bed"], saved[f"{arm}_wake"] = p[:, 0], p[:, 1]
        all_predictions.append(saved)
    checks = {}
    for split in ("validation", "test"):
        scores = result["splits"][split]
        checks[f"{split}_improvement_at_least_1min"] = scores["current_v8"]["joint"] - scores[selected]["joint"] >= 1.
        for endpoint in ("onset", "wake"):
            checks[f"{split}_{endpoint}_harm_at_most_1min"] = scores[selected][endpoint] - scores["current_v8"][endpoint] <= 1.
        first = next(s for s in result["strata"] if s["split"] == split and s["stratum"] == "history_0")
        checks[f"{split}_first_night_harm_at_most_1min"] = first["metrics"][selected]["joint"] - first["metrics"]["current_v8"]["joint"] <= 1.
        for s in result["strata"]:
            if s["split"] == split and s["stratum"].startswith("schedule_"):
                code = int(s["stratum"].split("_")[-1])
                if code in support["supported_codes"] and s["metrics"]["current_v8"]["people"] >= 20:
                    checks[f"{split}_{s['stratum']}_harm_at_most_1min"] = s["metrics"][selected]["joint"] - s["metrics"]["current_v8"]["joint"] <= 1.
    checks["validation_not_worse_than_matched"] = result["splits"]["validation"][selected]["joint"] <= result["splits"]["validation"]["control"]["joint"]
    for comparator in ("current_v8", "control"):
        checks[f"test_ci_above_zero_vs_{comparator}"] = result["comparisons"]["test"][selected][comparator]["ci95"][0] > 0
    result["gate"] = {"checks": checks, "performance_pass": all(checks.values()),
                      "decision": "eligible_for_integration_checks" if all(checks.values()) else "do_not_integrate",
                      "failed": [k for k, v in checks.items() if not v]}
    pd.concat(all_predictions).to_parquet(out / "private_predictions.parquet", index=False)
    verify_sources(out)
    result["input_integrity_verified"] = True
    dump(out / "evaluation.json", result)
    dump(out / "output_manifest.json", {"created_utc": stamp(), "sha256": {p.name: sha(p) for p in out.iterdir() if p.is_file() and p.name != "output_manifest.json"}})
    print(json.dumps({"gate": result["gate"], "splits": result["splits"], "selected_comparisons":
                     {s: c[selected] for s, c in result["comparisons"].items()}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "fit", "evaluate"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    globals()[args.phase](args.output)
