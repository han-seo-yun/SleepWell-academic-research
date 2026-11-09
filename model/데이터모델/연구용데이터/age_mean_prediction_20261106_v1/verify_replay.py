"""Metadata, split, age-linkage, and causal replay verification for the age audit.

This deliberately does not fit models or publish participant-level output.  It
replays a deterministic two-person-per-split sample through the shipped
GRADUAL_V1 reference engine and compares only aggregate counts/hashes.
"""
from __future__ import annotations
import hashlib, json, math, sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT = Path("E:/학술제")
OUT = ROOT / "데이터모델/연구용데이터/age_mean_prediction_20261106_v1"
MESA = ROOT / "데이터셋/MESA/datasets/mesa-sleep-dataset-0.8.0.csv"
HARM = ROOT / "데이터셋/MESA/datasets/mesa-sleep-harmonized-dataset-0.8.0.csv"
EP = ROOT / "데이터모델/연구용데이터/cmi_mesa_unified_20261104_v1/cmi_mesa_episodes.csv"
WORK = ROOT / "데이터모델/연구용데이터/mesa_work_schedule_20261106_v1"
PREFIT = WORK / "prefit_initial/input_manifest.json"
MANIFEST = WORK / "input_manifest.json"
SUPPORT = WORK / "support_lock.json"
REPLAY = WORK / "private_replay.parquet"
WEB = ROOT / "기타/SleepWell-integrated/web-app"
ASSETS = ROOT / "앱_소빈/통합 v8/app/src/main/assets/models"
TOL = 1e-7

def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    errors = []
    # Compare the first eight frozen source entries, without exposing paths in output.
    prior = json.loads(PREFIT.read_text(encoding="utf-8"))
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    prior_items = list(prior["sha256"].items())[:8]
    canonical_first8 = [MESA, EP, ASSETS / "robust_mesa_v2.json", ASSETS / "robust_cmi_v2.json",
                        WEB / "fusion_forecast.py", WEB / "night_core.py", WEB / "robust_features.py",
                        WORK / "prefit_initial/experiment.py"]
    hash_ok = len(prior_items) == 8 and all(sha(p) == h for p, (_, h) in zip(canonical_first8, prior_items))
    if not hash_ok: errors.append("first_8_prior_source_hash_mismatch")
    support = json.loads(SUPPORT.read_text(encoding="utf-8"))
    replay_hash_ok = sha(REPLAY) == support["replay_sha256"]
    if not replay_hash_ok: errors.append("cached_replay_hash_mismatch")

    ep = pd.read_csv(EP, usecols=["cohort","subject_id","split","cutoff","bed","wake","observed_at","bedtime_min","wake_min","age","sex","label_source"])
    canonical = json.loads((EP.parent / "manifest.json").read_text(encoding="utf-8"))
    episode_hash_ok = sha(EP) == canonical["output_sha256"][EP.name]
    if not episode_hash_ok: errors.append("canonical_episode_hash_mismatch")
    mesa = ep[ep.cohort.eq("MESA")].copy()
    expected = {"train": (1525,10524), "validation": (299,2062), "test": (315,2175)}
    split_counts = {s: {"people": int(g.subject_id.nunique()), "nights": int(len(g))}
                    for s,g in mesa.groupby("split")}
    split_ok = all((v["people"], v["nights"]) == expected[k] for k,v in split_counts.items() if k in expected)
    disjoint_ok = mesa.groupby("subject_id").split.nunique().max() == 1
    if not split_ok: errors.append("split_count_mismatch")
    if not disjoint_ok: errors.append("participant_split_overlap")

    # Verify age linkage from canonical MESA sleepage5c by participant, including missingness.
    raw = pd.read_csv(MESA, usecols=["mesaid","sleepage5c"])
    harm = pd.read_csv(HARM, usecols=["nsrrid","nsrr_age"])
    sys.path.insert(0, str(WORK))
    from linkage import mesa_subject_token
    raw_age = raw.drop_duplicates("mesaid").rename(columns={"sleepage5c":"raw_age"})
    raw_age["subject_id"] = raw_age["mesaid"].map(mesa_subject_token)
    raw_age = raw_age[["subject_id","raw_age"]]
    harm_age = harm.drop_duplicates("nsrrid").rename(columns={"nsrr_age":"nsrr_age"})
    harm_age["subject_id"] = harm_age["nsrrid"].map(mesa_subject_token)
    harm_age = harm_age[["subject_id","nsrr_age"]]
    linked = mesa[["subject_id","age"]].drop_duplicates("subject_id").merge(raw_age,on="subject_id",how="left").merge(harm_age,on="subject_id",how="left")
    nsrr_equal = bool(((linked.age.isna() & linked.nsrr_age.isna()) | (linked.age.sub(linked.nsrr_age).abs() <= 1e-9)).all())
    if not nsrr_equal: errors.append("nsrr_age_linkage_mismatch")
    valid = linked.age.notna() & linked.raw_age.notna()
    diff_frame = linked.loc[valid, ["age", "raw_age"]].copy()
    differences = diff_frame.age.sub(diff_frame.raw_age)
    unsupported_topcode_only = bool(((differences.abs() == 0) | ((diff_frame.raw_age > 90) & diff_frame.age.eq(90))).all())
    age_equal = bool(((linked.age.isna() & linked.raw_age.isna()) | (linked.age.sub(linked.raw_age).abs() <= 1e-9)).all())
    if not unsupported_topcode_only: errors.append("sleepage5c_linkage_difference_within_supported_range")
    age_counts = {"people": int(len(linked)), "missing": int(linked.age.isna().sum()),
                  "raw_missing": int(linked.raw_age.isna().sum()),
                  "range": [float(linked.age.min()), float(linked.age.max())],
                  "difference_rows": int((differences.abs() > 1e-9).sum()),
                  "difference_allowed_only_raw_over_90_topcode": unsupported_topcode_only}
    train = mesa[mesa.split.eq("train")].copy()
    train["interval_min"] = train["wake_min"] - train["bedtime_min"]
    train_distribution = {}
    for col in ("bedtime_min", "wake_min", "interval_min"):
        s = pd.to_numeric(train[col], errors="coerce").dropna()
        train_distribution[col] = {"n": int(s.size), "mean": float(s.mean()), "sd": float(s.std(ddof=1)),
                                   "median": float(s.median()), "min": float(s.min()), "max": float(s.max())}
    train_age = train[["subject_id","age"]].drop_duplicates("subject_id").copy()
    train_age["age_group"] = pd.cut(train_age.age, [0,59,80,200], labels=["54-59","60-80",">80"])
    train_age_counts = {str(k): int(v) for k,v in train_age.age_group.value_counts(dropna=False).items()}

    # Fresh causal replay for two deterministic subjects per split: one 54-59 and one 60-80.
    sys.path.insert(0, str(WEB))
    from fusion_forecast import PortableCohortModel
    from night_core import GRADUAL_POLICY, local_time
    models = {c: PortableCohortModel(ASSETS / f"robust_{c}_v2.json", c) for c in ("mesa","cmi")}
    cached = pd.read_parquet(REPLAY)
    selected = []
    for split in ("train","validation","test"):
        g = mesa[mesa.split.eq(split)].merge(raw_age,on="subject_id",how="left")
        for lo,hi in ((54,59),(60,80)):
            people = sorted(g.loc[g.raw_age.between(lo,hi),"subject_id"].unique())
            if not people: raise AssertionError(f"no deterministic person for {split} {lo}-{hi}")
            selected.append((split, people[0]))
    replay_stats = {"participants": len(selected), "nights": 0, "fields_compared": 0,
                    "max_abs_difference": 0.0, "future_observation_checks": 0}
    for split, sid in selected:
        group = mesa[(mesa.split.eq(split)) & (mesa.subject_id.eq(sid))].sort_values("cutoff")
        history = []
        for _, row in group.iterrows():
            cutoff = local_time(row.cutoff)
            result = {c: m.predict({"created_at": str(group.cutoff.iloc[0])}, history, cutoff,
                                    personalization_policy=GRADUAL_POLICY) for c,m in models.items()}
            cached_row = cached[(cached.split.eq(split)) & (cached.subject_id.eq(sid)) & cached.cutoff.eq(row.cutoff)]
            if len(cached_row) != 1: errors.append("cached_selected_row_missing_or_duplicate"); continue
            cr = cached_row.iloc[0]
            checks = {"history_count": result["mesa"]["personalization"]["valid_days"]}
            checks.update({"baseline_mesa_bed": result["mesa"]["hybrid"][0], "baseline_mesa_wake": result["mesa"]["hybrid"][1],
                           "baseline_cmi_bed": result["cmi"]["hybrid"][0], "baseline_cmi_wake": result["cmi"]["hybrid"][1]})
            for k,v in result["mesa"]["features"].items(): checks["f__"+k] = v
            for k,v in checks.items():
                got = float(cr[k]); diff = abs(float(v)-got)
                replay_stats["fields_compared"] += 1; replay_stats["max_abs_difference"] = max(replay_stats["max_abs_difference"], diff)
                if diff > TOL: errors.append(f"replay_mismatch:{split}:{k}")
            expected_hist = sum(1 for h in history if h["observed_at"] <= row.cutoff)
            if int(cr.history_count) != expected_hist: errors.append("causal_history_count_mismatch")
            replay_stats["future_observation_checks"] += 1
            history.append({"source":"research","bed":row.bed,"wake":row.wake,"observed_at":row.observed_at})
            replay_stats["nights"] += 1
    report = {"checked_utc": datetime.now(timezone.utc).isoformat(), "passed": not errors,
              "scope":"metadata/linkage/split/hash/causal replay only; no fitting or performance metrics",
              "hash_checks":{"prior_first_8_sources_unchanged":hash_ok,"cached_replay_hash":replay_hash_ok,"canonical_episode_hash":episode_hash_ok,"harmonized_age_source_sha256":sha(HARM)},
              "split_counts":split_counts,"participant_splits_disjoint":bool(disjoint_ok),"age_linkage":{"equal_to_nsrr_age":nsrr_equal,"equal_to_raw_sleepage5c":bool(age_equal),**age_counts},
              "train_label_distribution": train_distribution, "train_age_group_people": train_age_counts,
              "selected_participants_per_split":2,"selected_participants_total":len(selected),"replay":replay_stats,"errors":sorted(set(errors)),
              "caveats":["No participant identifiers or individual records are written to this report.","Causal check uses observed_at/wake availability convention from the locked MESA protocol."]}
    (OUT/"verify_replay.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("passed","hash_checks","participant_splits_disjoint","age_linkage","replay","errors")},ensure_ascii=False))
    if errors: raise SystemExit(1)

if __name__ == "__main__": main()
