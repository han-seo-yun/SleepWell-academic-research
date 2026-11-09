"""Separate, local MESA ACT-to-PSG benchmark. Never modifies source data."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

import mesa_experiments as base
import mesa_psg_alignment as alignment
import joblib
import numpy as np
from threadpoolctl import threadpool_limits

LABEL = "PSG sleep-stage annotation aligned by official 30-second overlap anchor"
MAX_EXCLUDED_FRACTION = 0.10


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(root):
    """Require one exact file per participant and modality; no arbitrary priority."""
    root = root.resolve(strict=True)
    def index(folder, pattern, regex):
        found = {}
        for p in sorted(folder.glob(pattern)):
            if p.is_symlink() or not p.is_file() or root not in p.resolve().parents:
                raise ValueError("unsafe source path")
            match = re.fullmatch(regex, p.name)
            if match is None:
                continue
            participant = base.normalize_participant_id(match.group(1))
            if participant in found:
                raise ValueError("duplicate participant source file")
            found[participant] = p
        return found
    act = index(root / "actigraphy", "*.csv", r"mesa-sleep-(\d+)\.csv")
    xml = index(root / "polysomnography" / "annotations-events-nsrr", "*.xml",
                r"mesa-sleep-(\d+)-nsrr\.xml")
    return act, xml


def validate_baseline(baseline, expected_hash=None):
    path = baseline / "selected_model.joblib"
    report = json.loads((baseline / "experiment_report.json").read_text(encoding="utf-8"))
    actual_hash = digest(path)
    if actual_hash != report["selected_artifact"]["sha256"] or (expected_hash is not None and actual_hash != expected_hash):
        raise ValueError("existing trusted ACT model hash mismatch")
    artifact = joblib.load(path)  # Local project artifact with recorded provenance.
    if (artifact["epoch_seconds"] != 30 or artifact["feature_names"] != list(base.FEATURE_NAMES)
            or artifact.get("fit_splits") != ["train"] or artifact.get("research_only") is not True):
        raise ValueError("baseline ACT feature contract mismatch")
    if digest(path) != actual_hash:
        raise RuntimeError("baseline model changed while reading")
    return artifact, actual_hash


def freeze_protocol(root, output, baseline):
    if output.exists():
        raise FileExistsError("use a new output directory; previous results are preserved")
    if root == output or root in output.parents:
        raise ValueError("output must be outside raw data")
    _, old_hash = validate_baseline(baseline)
    old_manifest_hash = digest(baseline / "manifest.json")
    output.mkdir(parents=True)
    protocol = {
        "schema_version": 1, "frozen_at_utc": base._utc_now(),
        "purpose": "retrospective wrist-sensor classification against independently scored PSG",
        "label": LABEL, "epochs_seconds": list(base.EPOCH_SECONDS),
        "candidates": list(base.CANDIDATES), "hgb_iterations": 60,
        "split_seed": base.SPLIT_SEED,
        "selection": "validation subject-macro balanced accuracy on common native 30-second rows",
        "test_policy": "freeze selection before PSG Test; all prespecified candidates descriptive",
        "prior_test_exposure": "same-cohort ACT weak-label Test results previously seen; not an external cohort",
        "features": list(base.FEATURE_NAMES),
        "excluded_features": ["wake", "interval", "PSG stage", "future sensor windows"],
        "alignment": "official overlap line is PSG epoch zero; rounded clock consistency only; no residual sub-epoch shift; full PSG coverage",
        "common_support": "complete 20-row line-aligned ten-minute blocks; report coverage loss",
        "coarse_training_labels": "majority of included native PSG epochs; ties assigned sleep as in existing aggregate_epoch; primary evaluation always original 30-second truth",
        "training_cap": {"per_subject": base.MAX_PER_SUBJECT, "total": base.MAX_TRAIN_ROWS},
        "baseline_ACT_model_sha256": old_hash,
        "baseline_ACT_manifest_sha256": old_manifest_hash,
        "baseline_report_sha256": digest(baseline / "experiment_report.json"),
        "exclusions": {"allowed_reasons": ["empty_psg_xml", "no_complete_eligible_ten_minute_blocks"],
                       "maximum_subject_fraction": MAX_EXCLUDED_FRACTION,
                       "schema_alignment_or_IO_failure": "abort before fitting"},
        "data_preflight": {
            "policy": "validate every matched nonempty PSG XML before creating subject archives",
            "empty_file_policy": "stable zero-byte PSG XML is an explicit expected exclusion with provenance",
            "restart_policy": "a pre-fit interruption may restart in a new output directory after source preflight",
        },
        "environment": {"python": platform.python_version(),
                        **{p: version(p) for p in ["numpy", "pandas", "scipy", "scikit-learn", "joblib"]}},
        "source_code_sha256": {p.name: digest(p) for p in
            [Path(__file__), Path(base.__file__), Path(alignment.__file__)]},
        "research_only": True, "web_policy_promotion": False,
    }
    base._write_json(output / "protocol.json", protocol)
    return protocol


def build_dataset(root, output, baseline, protocol=None):
    overlap_path = root / "overlap" / "mesa-actigraphy-psg-overlap.csv"
    overlap_hash = digest(overlap_path)
    overlap = alignment.read_overlap(overlap_path)
    act, xml = discover(root)
    participants = sorted(set(act) & set(xml) & set(overlap), key=int)
    snapshot = {p: base._signature(p) for s in participants for p in (act[s], xml[s])}
    if protocol is not None and digest(baseline / "manifest.json") != protocol["baseline_ACT_manifest_sha256"]:
        raise RuntimeError("previous ACT manifest changed after protocol freeze")
    previous = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
    old_split = {s["token"]: s["split"] for s in previous["subjects"]}
    empty_xml = {}
    for participant in participants:
        path = xml[participant]
        source_snapshot = snapshot[path]
        if source_snapshot[0] == 0:
            source_hash = digest(path)
            if base._signature(path) != source_snapshot:
                raise RuntimeError("source_changed_during_psg_preflight")
            empty_xml[participant] = {
                "token": base.subject_token(participant),
                "reason": "empty_psg_xml",
                "source_sha256": {"xml_sha256": source_hash},
                "source_snapshot": {"xml_size": source_snapshot[0], "xml_mtime_ns": source_snapshot[1]},
            }
        else:
            alignment.parse_stages(path)
            if base._signature(path) != source_snapshot:
                raise RuntimeError("source_changed_during_psg_preflight")
    if any(base._signature(path) != stat for path, stat in snapshot.items()):
        raise RuntimeError("source_changed_during_psg_preflight")

    subjects, excluded = [], []
    total_qc = Counter()
    subject_dir = output / "subjects"
    subject_dir.mkdir()
    for i, participant in enumerate(participants, 1):
        token = base.subject_token(participant)
        if any(base._signature(p) != snapshot[p] for p in (act[participant], xml[participant])):
            raise RuntimeError("source_changed_since_discovery")
        if participant in empty_xml:
            excluded.append(empty_xml[participant])
        else:
            arrays, qc, hashes = alignment.align_subject(
                act[participant], xml[participant], overlap[participant], participant
            )
            total_qc.update(qc)
            if any(base._signature(p) != snapshot[p] for p in (act[participant], xml[participant])):
                raise RuntimeError("source_changed_since_discovery")
            if not len(arrays["line"]):
                excluded.append({"token": token, "reason": "no_complete_eligible_ten_minute_blocks", "qc": qc})
            else:
                split = base.subject_split(participant)
                if token in old_split and split != old_split[token]:
                    raise RuntimeError("previous participant split changed")
                path = subject_dir / f"{token}.npz"
                np.savez_compressed(path, **arrays)
                subjects.append({"token": token, "split": split, "eligible_native_rows": len(arrays["line"]),
                                 "source_sha256": hashes, "output_sha256": digest(path), "qc": qc,
                                 "previous_ACT_snapshot_member": token in old_split})
        if i % 100 == 0 or i == len(participants):
            print(json.dumps({"phase": "PSG_alignment", "processed": i, "matched": len(participants),
                              "included": len(subjects), "excluded": len(excluded)}), flush=True)
    if digest(overlap_path) != overlap_hash:
        raise RuntimeError("overlap table changed during dataset build")
    if any(base._signature(p) != stat for p, stat in snapshot.items()):
        raise RuntimeError("snapshot changed during dataset build; do not train")
    subjects.sort(key=lambda s: s["token"])
    manifest = {
        "schema_version": 1, "created_at_utc": base._utc_now(), "label": LABEL,
        "inventory": {"ACT_files": len(act), "PSG_XML_files": len(xml), "overlap_participants": len(overlap),
                      "matched_candidates": len(participants), "included_subjects": len(subjects),
                      "new_vs_previous_ACT_snapshot": sum(not s["previous_ACT_snapshot_member"] for s in subjects)},
        "overlap_sha256": overlap_hash, "split": dict(Counter(s["split"] for s in subjects)),
        "exclusion_reasons": dict(Counter(s["reason"] for s in excluded)),
        "excluded_fraction": len(excluded) / len(participants) if participants else 1.0,
        "rows_by_split": {split: sum(s["eligible_native_rows"] for s in subjects if s["split"] == split)
                          for split in ("train", "validation", "test")},
        "aggregate_qc": dict(total_qc), "excluded_private": excluded, "subjects": subjects,
    }
    base._write_json(output / "manifest.json", manifest)
    if manifest["excluded_fraction"] > MAX_EXCLUDED_FRACTION:
        raise ValueError("prespecified exclusion threshold exceeded; inspect manifest before any fitting")
    if set(manifest["split"]) != {"train", "validation", "test"}:
        raise ValueError("all three participant splits need eligible data")
    return manifest


def evaluate_baseline(artifact, output, subjects):
    metric = base.MetricAccumulator()
    scores, predictions = {}, []
    for s in subjects:
        arr = base._load_subject(output / "subjects" / f"{s['token']}.npz")
        pred = artifact["model"].predict(arr["x"]).astype(np.uint8)
        metric.add(arr["y_sleep"], pred)
        tn, fp, fn, tp = metric.confusion_for(arr["y_sleep"], pred)
        if tn + fp and tp + fn:
            scores[s["token"]] = .5 * (tn / (tn + fp) + tp / (tp + fn))
        predictions.append({"token": s["token"], "confusion": [tn, fp, fn, tp]})
    base._write_json(output / "baseline_ACT_test_confusions_private.json", predictions)
    return metric.report(), scores


def fit_and_evaluate(output, baseline, protocol, manifest):
    frozen_baseline, _ = validate_baseline(baseline, protocol["baseline_ACT_model_sha256"])
    subjects = manifest["subjects"]
    groups = {split: [s for s in subjects if s["split"] == split] for split in ("train", "validation", "test")}
    print(json.dumps({"phase": "fit_PSG_candidates", "subjects": manifest["split"]}), flush=True)
    models, sampling = base._fit_candidates(groups["train"], output / "subjects")
    validation, _ = base.evaluate_models(models, groups["validation"], output / "subjects", output / "evaluation" / "validation")
    selected = base._select(validation)
    base._write_json(output / "selected.json", {"selected_at_utc": base._utc_now(), "selected_before_test": selected,
                                               "validation_metrics": validation[selected]})
    print(json.dumps({"phase": "PSG_test", "selected_before_test": selected}), flush=True)
    test, scores = base.evaluate_models(models, groups["test"], output / "subjects", output / "evaluation" / "test")
    seconds, name = selected.split(":")
    epoch = int(seconds.removesuffix("s"))
    candidate = next(c for c in base.CANDIDATES if c["name"] == name)
    artifact = {"model": models[(epoch, name)], "epoch_seconds": epoch, "candidate": name,
                "feature_names": list(candidate["features"]), "label": LABEL, "fit_splits": ["train"],
                "research_only": True, "feature_contract": "mesa_ACT_native_sensor_PSG_v1"}
    joblib.dump(artifact, output / "selected_model.joblib", compress=3)
    old_metric, old_scores = evaluate_baseline(frozen_baseline, output, groups["test"])
    selected_scores = {}
    for s in groups["test"]:
        with np.load(output / "evaluation" / "test" / f"{s['token']}.npz", allow_pickle=False) as saved:
            pred = saved["prediction"][saved["candidate"].tolist().index(selected)]
            tn, fp, fn, tp = base.MetricAccumulator.confusion_for(saved["y_sleep"], pred)
            if tn + fp and tp + fn:
                selected_scores[s["token"]] = .5 * (tn / (tn + fp) + tp / (tp + fn))
    common = sorted(set(selected_scores) & set(old_scores))
    report = {
        "schema_version": 1, "completed_at_utc": base._utc_now(), "purpose": protocol["purpose"],
        "label": LABEL, "protocol_sha256": digest(output / "protocol.json"),
        "protocol_frozen_before_test": True, "candidate_protocol": {"training_sampling": sampling},
        "validation": validation, "selected_before_test": selected,
        "test_all_frozen_candidates_descriptive": test,
        "test_selected_vs_dummy": base._paired_bootstrap(scores[selected], scores[f"{epoch}s:dummy_prior"]),
        "frozen_ACT_model_on_same_PSG_test": old_metric,
        "PSG_selected_minus_frozen_ACT_paired_subject_bootstrap": base._paired_bootstrap(
            [selected_scores[s] for s in common], [old_scores[s] for s in common]),
        "selected_artifact": {"file": "selected_model.joblib", "sha256": digest(output / "selected_model.joblib"),
                              "fit_splits": ["train"], "research_only": True},
        "limitations": ["Partial available PSG cohort, no external validation", "Complete-block conditional coverage",
                        "Retrospective classification; no next-night prediction or recommendation efficacy",
                        "ACT weak-label test previously examined in same cohort", "EDF signals unavailable; scorer annotations not re-scored"],
    }
    base._write_json(output / "experiment_report.json", report)
    return report


def run(root, output, baseline):
    root, output, baseline = root.resolve(strict=True), output.resolve(), baseline.resolve(strict=True)
    protocol = freeze_protocol(root, output, baseline)
    with threadpool_limits(limits=2):
        manifest = build_dataset(root, output, baseline, protocol)
        report = fit_and_evaluate(output, baseline, protocol, manifest)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.root, args.output, args.baseline)
    print(json.dumps({"completed": True, "selected": result["selected_before_test"]}), flush=True)
