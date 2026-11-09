"""Read-only checks of locally produced research artifacts (trusted joblib only)."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
from threadpoolctl import threadpool_limits
import pyarrow.parquet as pq


def verify_mesa(folder):
    from mesa_experiments import aggregate_epoch, _load_subject, _candidate_x, CANDIDATES

    manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    report = json.loads((folder/"experiment_report.json").read_text(encoding="utf-8"))
    subjects = manifest["subjects"]
    assert len({s["token"] for s in subjects}) == len(subjects)
    selected = report["selected_before_test"]
    metric = "projected_shared_native_30s_grid"
    val = report["validation"]
    validation_scores = {key: [] for key in val}
    for subject in (s for s in subjects if s["split"] == "validation"):
        with np.load(folder/"evaluation/validation"/f"{subject['token']}.npz", allow_pickle=False) as saved:
            y = saved["y_sleep"]
            assert set(saved["candidate"].tolist()) == set(val)
            for key, pred in zip(saved["candidate"].tolist(), saved["prediction"]):
                tn, fp, fn, tp = np.bincount(y.astype(np.int64)*2+pred, minlength=4)
                if tn+fp and tp+fn:
                    validation_scores[key].append(.5*(tn/(tn+fp)+tp/(tp+fn)))
    for key, scores in validation_scores.items():
        assert abs(np.mean(scores)-val[key][metric]["subject_macro_balanced_accuracy"]) < 1e-12
    assert val[selected][metric]["subject_macro_balanced_accuracy"] == max(
        row[metric]["subject_macro_balanced_accuracy"] for row in val.values())
    artifact_path = folder/"selected_model.joblib"
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == report["selected_artifact"]["sha256"]
    artifact = joblib.load(artifact_path)
    candidate = next(c for c in CANDIDATES if c["name"] == artifact["candidate"])
    assert artifact["research_only"] and artifact["fit_splits"] == ["train"]
    totals = {key: np.zeros(4, dtype=np.int64) for key in val}
    test_subjects = [s for s in subjects if s["split"] == "test"]
    for index, subject in enumerate(test_subjects):
        with np.load(folder/"evaluation/test"/f"{subject['token']}.npz", allow_pickle=False) as saved:
            keys = saved["candidate"].tolist()
            y = saved["y_sleep"]
            predictions = saved["prediction"]
            assert set(keys) == set(val) and predictions.shape == (len(val), len(y))
            assert np.isin(y, [0, 1]).all() and np.isin(predictions, [0, 1]).all()
            assert (np.diff(saved["line"]) > 0).all()
            for key, pred in zip(keys, predictions):
                totals[key] += np.bincount(y.astype(np.int64)*2+pred, minlength=4)
            if index in {0, len(test_subjects)//2, len(test_subjects)-1}:
                raw = _load_subject(folder/"subjects"/f"{subject['token']}.npz")
                positions = np.searchsorted(raw["line"], saved["line"])
                assert (positions < len(raw["line"])).all()
                assert np.array_equal(raw["line"][positions], saved["line"])
                assert np.array_equal(raw["y_sleep"][positions], y)
                epoch = aggregate_epoch(raw, artifact["epoch_seconds"])
                pred = artifact["model"].predict(_candidate_x(epoch["x"], candidate))
                width = artifact["epoch_seconds"]//30
                lines = (epoch["line_start"][:, None]+np.arange(width)).ravel()
                positions = np.searchsorted(lines, saved["line"])
                assert (positions < len(lines)).all()
                assert np.array_equal(lines[positions], saved["line"])
                projected = np.repeat(pred, width)[positions]
                assert np.array_equal(projected, predictions[keys.index(selected)])
    for key, counts in totals.items():
        stored = report["test_all_frozen_candidates_descriptive"][key][metric]
        assert counts.tolist() == [stored["confusion"][k] for k in ("tn", "fp", "fn", "tp")]
        assert int(counts.sum()) == stored["rows"]
    return "MESA: unique subjects, validation selection, all Test confusions/common support, artifact hash and three-subject prediction reproduction"


def verify_cmi(folder):
    splits = json.loads((folder/"split_manifest_private.json").read_text(encoding="utf-8"))
    report = json.loads((folder/"public_aggregate.json").read_text(encoding="utf-8"))
    files = sorted((folder/"subjects_30s").glob("*.parquet"))
    assert {p.stem for p in files} == set(splits)
    assert set(splits.values()) == {"train", "validation", "test"}
    assert len(files) == report["subject_count"]
    assert dict(Counter(splits.values())) == report["split_subject_counts"]
    assert sum(pq.ParquetFile(p).metadata.num_rows for p in files) == report["retained_30s_rows"]
    sleep_rows = wake_rows = 0
    for p in files:
        d = pd.read_parquet(p)
        assert d.subject_id.eq(p.stem).all() and d.split.eq(splits[p.stem]).all()
        assert d.step_start.is_unique and d.step_start.mod(6).eq(0).all()
        assert d.available_at_relative_sec.sub(d.relative_time_sec).eq(30).all()
        assert d.is_sleep.isin([0, 1]).all()
        assert d.label_purity.gt(.5).all() and d.label_purity.le(1).all()
        assert np.isfinite(d[["anglez_mean", "anglez_std", "enmo_mean", "enmo_std"]]).all().all()
        sleep_rows += int(d.is_sleep.sum())
        wake_rows += int(d.is_sleep.eq(0).sum())
    assert sleep_rows == report["sleep_30s_rows"] and wake_rows == report["wake_30s_rows"]
    assert sleep_rows+wake_rows == report["retained_30s_rows"]
    assert report["event_timestamp_mismatches"] == report["nonunit_source_step_pairs"] == report["nonfive_second_timestamp_pairs"] == 0
    return "CMI: split roster, all Parquet rows/features/labels/times, aggregate class counts, reported zero cadence/event errors"


def verify(root):
    checks = []
    base = root/"tabular_run_20261010"
    for lane, targets in {"personal": ["bedtime_min", "wake_min"],
                          "nhanes": ["weekday_sleep_hours"]}.items():
        folder = base/lane
        d = pd.read_parquet(folder/"model_data.parquet")
        te = d.loc[d.split.eq("test")]
        report = json.loads((folder/"summary.json").read_text(encoding="utf-8"))
        p = pd.read_parquet(folder/"test_predictions.parquet")
        scores = pd.read_csv(folder/"experiment_metrics.csv")
        if lane == "personal":
            assert d.cutoff.is_monotonic_increasing
            assert d.loc[d.split.eq("train"), "cutoff"].max() < d.loc[d.split.eq("validation"), "cutoff"].min()
            assert d.loc[d.split.eq("validation"), "cutoff"].max() < te.cutoff.min()
        else:
            assert d.groupby("subject_id").split.nunique().max() == 1
        for target in targets:
            chosen = report["selected"][target]["selected"]
            a = joblib.load(folder/f"{target}_research_model.joblib")
            pred = a["model"].predict(te[a["features"]])
            saved = p.loc[p.target.eq(target) & p.candidate.eq(chosen), "prediction"].to_numpy()
            assert np.allclose(pred, saved)
            assert abs(mean_absolute_error(te[target], pred)-report["selected"][target]["test"]["mae"]) < 1e-9
            target_scores = scores.loc[scores.target.eq(target)]
            assert chosen == target_scores.loc[target_scores.validation_mae.idxmin(), "candidate"]
            assert target not in a["features"]
        checks.append(f"{lane}: split, validation selection, artifact/predictions/metrics agreement")
    w = root/"walch_run_20261010_v2"
    split = json.loads((w/"split_manifest_private.json").read_text(encoding="utf-8"))
    assert not (set(split["train"]) & set(split["test"]) or
                set(split["train"]) & set(split["validation"]) or
                set(split["test"]) & set(split["validation"]))
    d = pd.read_csv(w/"test_predictions_private.csv.gz")
    assert d.groupby(["epoch_seconds", "model"]).size().nunique() == 1
    support = [set(group[["subject_id", "epoch_idx"]].itertuples(index=False, name=None))
               for _, group in d.groupby(["epoch_seconds", "model"])]
    assert all(s == support[0] for s in support)
    a = joblib.load(w/"selected_model_research_only.joblib")
    x = pd.read_csv(w/f"model_ready_{a['epoch_seconds']}s_private.csv.gz", dtype={"subject_id": str})
    x = x.loc[x.subject_id.isin(split["test"])].copy()
    x["probability"] = a["estimator"].predict_proba(x[a["feature_names"]])[:, 1]
    selected = d.loc[(d.epoch_seconds == a["epoch_seconds"]) & (d.model == a["model"])].copy()
    selected["subject_id"] = selected.subject_id.astype(str)
    match = selected.merge(x[["subject_id", "bin_idx", "probability"]], on=["subject_id", "bin_idx"], validate="many_to_one")
    assert len(match) == len(selected)
    assert np.allclose(match.probability, match.probability_sleep)
    checks.append("Walch: subject separation, exact common time support, selected artifact agrees with test probabilities")
    mm = pd.read_parquet(root/"support_run_20261010_v3/mmash_conditional/model_data.parquet")
    assert set(mm.split) == {"train", "validation", "test"}
    assert mm.groupby("subject_id").split.nunique().max() == 1
    assert (mm.minutes_to_out_bed >= 0).all()
    checks.append("MMASH: actual three-way subject separation, pre-event only; no performance claim")
    checks.append(verify_mesa(root/"mesa_run_20261010"))
    checks.append(verify_cmi(root/"cmi_run_20261010_v2"))
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    args = parser.parse_args()
    with threadpool_limits(limits=2):
        print(json.dumps({"passed": verify(args.research_root)}, ensure_ascii=False, indent=2))
