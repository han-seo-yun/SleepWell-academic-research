"""Outcome-definition and raw actigraphy quality sensitivity for frozen Test predictions.

This module never refits a model, changes the primary outcome, or removes a night
from the primary analysis. Alternative targets and quality subsets are reported as
retrospective sensitivity analyses only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

FIXED_OFFSET = timezone(timedelta(hours=9))
SEED = 20261025
ARMS = ("mean14", "physical", "data", "hybrid")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def local_wall(value) -> pd.Timestamp:
    if value is None or pd.isna(value):
        return pd.NaT
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return pd.NaT
    return pd.Timestamp(parsed.replace(tzinfo=None).replace(tzinfo=FIXED_OFFSET))


def cutoff_for(bed: pd.Timestamp) -> pd.Timestamp:
    cutoff = bed.normalize() + pd.Timedelta(hours=20)
    return cutoff - pd.Timedelta(days=1) if bed < cutoff else cutoff


def valid_target(bed: pd.Timestamp, wake: pd.Timestamp) -> bool:
    if pd.isna(bed) or pd.isna(wake) or wake <= bed:
        return False
    cutoff = cutoff_for(bed)
    onset = (bed - cutoff).total_seconds() / 60
    ending = (wake - cutoff).total_seconds() / 60
    duration = (wake - bed).total_seconds() / 60
    return 0 <= onset <= 960 and ending <= 1440 and 30 <= duration <= 960


def target_offsets(bed: pd.Timestamp, wake: pd.Timestamp) -> tuple[float, float]:
    cutoff = cutoff_for(bed)
    return (bed - cutoff).total_seconds() / 60, (wake - cutoff).total_seconds() / 60


def offsets_against(cutoff: pd.Timestamp, bed: pd.Timestamp, wake: pd.Timestamp) -> tuple[float, float]:
    """Express a sensitivity target against the archived night's fixed cutoff."""
    return (bed-cutoff).total_seconds()/60, (wake-cutoff).total_seconds()/60


def mesa_token(participant: str | int) -> str:
    normalized = str(int(str(participant).strip()))
    digest = hashlib.sha256(f"mesa_subject_v1|{normalized}".encode()).hexdigest()
    return f"mesa_{digest[:20]}"


def _candidate(row_group: pd.DataFrame, parent: pd.DataFrame) -> dict | None:
    sleep = row_group.loc[row_group.wake_value.eq(0), "stamp"]
    if sleep.empty:
        return None
    bed, wake = sleep.iloc[0], sleep.iloc[-1] + pd.Timedelta(seconds=30)
    rest_bed, rest_wake = row_group.stamp.iloc[0], row_group.stamp.iloc[-1] + pd.Timedelta(seconds=30)
    if not valid_target(bed, wake):
        return None
    onwrist_valid = parent.offwrist.eq(0) & parent.wake_value.isin([0, 1])
    elapsed = (parent.stamp.iloc[-1] - parent.stamp.iloc[0]).total_seconds() / 30 + 1
    no_cadence_break = abs(elapsed - len(parent)) < 1e-8
    sleep_minutes = float(len(sleep) * .5)
    rest_minutes = float(len(row_group) * .5)
    return {
        "bed": bed, "wake": wake, "rest_bed": rest_bed, "rest_wake": rest_wake,
        "duration_min": float((wake-bed).total_seconds()/60),
        "act_sleep_min": sleep_minutes, "first_last_sleep_span_min": float((wake-bed).total_seconds()/60),
        "eligible_rest_min": rest_minutes,
        "act_sleep_efficiency": sleep_minutes/rest_minutes if rest_minutes else None,
        "parent_rest_epochs": int(len(parent)), "parent_valid_onwrist_epochs": int(onwrist_valid.sum()),
        "parent_valid_onwrist_fraction": float(onwrist_valid.mean()) if len(parent) else None,
        "parent_no_cadence_break": bool(no_cadence_break),
    }


def extract_mesa_candidates(path: Path, participant: str | int) -> list[dict]:
    columns = ["mesaid", "line", "linetime", "date_shifted", "offwrist", "wake", "interval"]
    frame = pd.read_csv(path, usecols=columns, low_memory=False)
    identities = pd.to_numeric(frame.mesaid, errors="coerce")
    if identities.notna().any() and not identities.dropna().astype("int64").eq(int(participant)).all():
        raise ValueError(f"participant mismatch: {path.name}")
    lines = pd.to_numeric(frame.line, errors="coerce")
    stamps = pd.to_datetime(frame.date_shifted.astype("string") + " " + frame.linetime.astype("string"),
                            errors="coerce").dt.tz_localize(FIXED_OFFSET)
    wakes = pd.to_numeric(frame.wake, errors="coerce")
    offwrist = pd.to_numeric(frame.offwrist, errors="coerce")
    valid_identity_time = identities.notna() & lines.notna() & stamps.notna()
    cadence = lines.diff().eq(1) & stamps.diff().eq(pd.Timedelta(seconds=30))
    rest = valid_identity_time & frame.interval.astype("string").str.strip().eq("REST-S")
    rest_continues = rest & rest.shift(fill_value=False) & cadence
    rest_group = pd.Series(np.cumsum((~rest_continues).to_numpy(bool)), index=frame.index)
    raw = pd.DataFrame({"stamp": stamps, "wake_value": wakes, "offwrist": offwrist,
                        "valid_identity_time": valid_identity_time, "cadence": cadence,
                        "rest": rest, "rest_group": rest_group})
    output = []
    for parent_id, parent in raw.loc[rest].groupby("rest_group", sort=True):
        eligible = parent.offwrist.eq(0) & parent.wake_value.isin([0, 1])
        continues = eligible & eligible.shift(fill_value=False) & parent.stamp.diff().eq(pd.Timedelta(seconds=30))
        subgroup = pd.Series(np.cumsum((~continues).to_numpy(bool)), index=parent.index)
        eligible_frame = parent.loc[eligible].assign(subgroup=subgroup.loc[eligible])
        for _, group in eligible_frame.groupby("subgroup", sort=True):
            candidate = _candidate(group, parent)
            if candidate is not None:
                candidate["parent_group"] = int(parent_id)
                cutoff = cutoff_for(candidate["bed"])
                expected = int((candidate["wake"]-cutoff).total_seconds()/30)
                monitored = raw.loc[raw.valid_identity_time & raw.stamp.ge(cutoff)
                                    & raw.stamp.lt(candidate["wake"])]
                usable = monitored.offwrist.eq(0) & monitored.wake_value.isin([0, 1])
                candidate["monitoring_expected_epochs"] = expected
                candidate["monitoring_observed_epochs"] = int(len(monitored))
                candidate["monitoring_valid_onwrist_epochs"] = int(usable.sum())
                candidate["monitoring_valid_onwrist_fraction"] = float(min(1, usable.sum()/expected)) if expected else None
                candidate["monitoring_no_cadence_or_timestamp_gap"] = bool(
                    expected > 0 and len(monitored) == expected
                    and (len(monitored) < 2 or monitored.stamp.diff().iloc[1:].eq(pd.Timedelta(seconds=30)).all()))
                output.append(candidate)
    return output


def select_variants(candidates: list[dict]) -> dict[pd.Timestamp, dict]:
    by_cutoff: dict[pd.Timestamp, list[dict]] = {}
    for row in candidates:
        by_cutoff.setdefault(cutoff_for(row["bed"]), []).append(row)
    output = {}
    for cutoff, rows in by_cutoff.items():
        rows = sorted(rows, key=lambda item: (item["bed"], item["wake"]))
        longest = sorted(rows, key=lambda item: (-item["duration_min"], item["bed"]))[0]
        output[cutoff] = {"candidate_count": len(rows), "original_longest": longest,
                          "earliest_candidate": rows[0], "latest_candidate": rows[-1]}
    return output


def build_mesa_private(manifest_path: Path, raw_dir: Path, archived: pd.DataFrame) -> pd.DataFrame:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test = {item["token"]: item for item in manifest["subjects"] if item["split"] == "test"}
    files = {}
    for path in raw_dir.glob("mesa-sleep-*.csv"):
        suffix = path.stem.rsplit("-", 1)[-1]
        if suffix.isdigit():
            token = mesa_token(suffix)
            if token in test:
                files[token] = (path, suffix)
    if set(files) != set(test):
        raise ValueError(f"MESA Test raw files mismatch: missing={len(set(test)-set(files))}")
    archived_by_subject = {key: group for key, group in archived.groupby("subject_id")}
    rows = []
    for token in sorted(test):
        path, participant = files[token]
        if sha256(path) != test[token]["source_sha256"]:
            raise ValueError(f"MESA raw hash mismatch: {path.name}")
        variants = select_variants(extract_mesa_candidates(path, participant))
        for saved in archived_by_subject[token].itertuples(index=False):
            cutoff = pd.Timestamp(saved.cutoff)
            selected = variants.get(cutoff)
            if selected is None:
                raise ValueError(f"No reconstructed target for archived MESA row {saved.row_id}")
            record = {"row_id": int(saved.row_id), "subject_id": token, "cutoff": cutoff,
                      "candidate_count": selected["candidate_count"]}
            for name in ("original_longest", "earliest_candidate", "latest_candidate"):
                item = selected[name]
                onset, wake = target_offsets(item["bed"], item["wake"])
                record[f"{name}_onset"] = onset; record[f"{name}_wake"] = wake
            original = selected["original_longest"]
            rest_onset, rest_wake = offsets_against(cutoff, original["rest_bed"], original["rest_wake"])
            record.update({"selected_rest_boundary_onset": rest_onset,
                           "selected_rest_boundary_wake": rest_wake,
                           "act_sleep_min": original["act_sleep_min"],
                           "first_last_sleep_span_min": original["first_last_sleep_span_min"],
                           "eligible_rest_min": original["eligible_rest_min"],
                           "act_sleep_efficiency": original["act_sleep_efficiency"],
                           "parent_rest_epochs": original["parent_rest_epochs"],
                           "parent_valid_onwrist_epochs": original["parent_valid_onwrist_epochs"],
                           "parent_valid_onwrist_fraction": original["parent_valid_onwrist_fraction"],
                           "parent_no_cadence_break": original["parent_no_cadence_break"],
                           "monitoring_expected_epochs": original["monitoring_expected_epochs"],
                           "monitoring_observed_epochs": original["monitoring_observed_epochs"],
                           "monitoring_valid_onwrist_epochs": original["monitoring_valid_onwrist_epochs"],
                           "monitoring_valid_onwrist_fraction": original["monitoring_valid_onwrist_fraction"],
                           "monitoring_no_cadence_or_timestamp_gap": original["monitoring_no_cadence_or_timestamp_gap"]})
            record["quality_fixed"] = bool(record["monitoring_valid_onwrist_fraction"] >= .90
                                             and record["monitoring_no_cadence_or_timestamp_gap"]
                                             and record["candidate_count"] == 1)
            for arm in ARMS:
                record[f"{arm}_onset"] = float(getattr(saved, f"{arm}_onset"))
                record[f"{arm}_wake"] = float(getattr(saved, f"{arm}_wake"))
            rows.append(record)
    return pd.DataFrame(rows).sort_values("row_id").reset_index(drop=True)


def build_cmi_private(events_path: Path, archived: pd.DataFrame) -> pd.DataFrame:
    subjects = set(archived.subject_id.astype(str))
    events = pd.read_csv(events_path, usecols=["series_id", "night", "event", "timestamp"])
    events = events.loc[events.series_id.astype(str).isin(subjects)].copy()
    candidates = []
    for (subject, _), group in events.groupby(["series_id", "night"], sort=True):
        counts = group.event.value_counts().to_dict()
        if len(group) != 2 or counts != {"onset": 1, "wakeup": 1}:
            continue
        bed = local_wall(group.loc[group.event.eq("onset"), "timestamp"].iloc[0])
        wake = local_wall(group.loc[group.event.eq("wakeup"), "timestamp"].iloc[0])
        if valid_target(bed, wake):
            candidates.append({"subject_id": str(subject), "bed": bed, "wake": wake,
                               "duration_min": (wake-bed).total_seconds()/60})
    by_subject = {}
    for subject, group in pd.DataFrame(candidates).groupby("subject_id"):
        by_subject[subject] = select_variants(group.to_dict("records"))
    rows = []
    for saved in archived.itertuples(index=False):
        selected = by_subject[str(saved.subject_id)].get(pd.Timestamp(saved.cutoff))
        if selected is None:
            raise ValueError(f"No reconstructed target for archived CMI row {saved.row_id}")
        record = {"row_id": int(saved.row_id), "subject_id": str(saved.subject_id),
                  "cutoff": pd.Timestamp(saved.cutoff), "candidate_count": selected["candidate_count"]}
        for name in ("original_longest", "earliest_candidate", "latest_candidate"):
            onset, wake = target_offsets(selected[name]["bed"], selected[name]["wake"])
            record[f"{name}_onset"] = onset; record[f"{name}_wake"] = wake
        record["quality_fixed"] = selected["candidate_count"] == 1
        for arm in ARMS:
            record[f"{arm}_onset"] = float(getattr(saved, f"{arm}_onset"))
            record[f"{arm}_wake"] = float(getattr(saved, f"{arm}_wake"))
        rows.append(record)
    return pd.DataFrame(rows).sort_values("row_id").reset_index(drop=True)


def participant_errors(frame: pd.DataFrame, variant: str, arm: str) -> pd.DataFrame:
    errors = pd.DataFrame({
        "subject_id": frame.subject_id,
        "onset": (frame[f"{arm}_onset"]-frame[f"{variant}_onset"]).abs(),
        "wake": (frame[f"{arm}_wake"]-frame[f"{variant}_wake"]).abs(),
    })
    errors["joint"] = (errors.onset+errors.wake)/2
    return errors.groupby("subject_id")[["onset", "wake", "joint"]].mean()


def model_summary(frame: pd.DataFrame, variant: str, arm: str) -> dict:
    value = participant_errors(frame, variant, arm).mean()
    return {f"{key}_mae_min": float(value[key]) for key in ("onset", "wake", "joint")}


def paired_target_delta(frame: pd.DataFrame, variant: str, arm: str) -> dict:
    original = participant_errors(frame, "original_longest", arm).joint
    alternative = participant_errors(frame, variant, arm).joint
    delta = alternative-original
    values = delta.to_numpy()
    rng = np.random.default_rng(SEED)
    # Bound peak memory on research PCs while preserving the exact participant
    # cluster-bootstrap estimand and deterministic RNG stream.
    boot = np.empty(10000, dtype=float)
    for start in range(0, len(boot), 250):
        stop = min(start+250, len(boot))
        boot[start:stop] = values[rng.integers(0, len(values), (stop-start, len(values)))].mean(axis=1)
    return {"alternative_minus_original_mae_min": float(values.mean()),
            "ci95_min": np.quantile(boot, [.025, .975]).tolist(),
            "participants_lower_error_under_alternative_fraction": float((values < 0).mean())}


def summarize(cohort: str, frame: pd.DataFrame, archived: pd.DataFrame) -> dict:
    if not np.array_equal(frame.row_id.to_numpy(), archived.row_id.to_numpy()):
        raise ValueError(f"{cohort} private sensitivity rows do not align with archived row IDs")
    maxdiff = float(np.max(np.abs(frame[["original_longest_onset", "original_longest_wake"]].to_numpy()
                                  - archived[["bed_offset", "wake_offset"]].to_numpy())))
    if maxdiff > 1e-8:
        raise ValueError(f"{cohort} primary target reconstruction differs: {maxdiff}")
    variants = ["original_longest", "earliest_candidate", "latest_candidate"]
    if cohort == "mesa":
        variants.append("selected_rest_boundary")
    variant_output = {}
    for variant in variants:
        changed = ((frame[f"{variant}_onset"]-frame.original_longest_onset).abs() > 1e-8) | ((frame[f"{variant}_wake"]-frame.original_longest_wake).abs() > 1e-8)
        variant_output[variant] = {"matched_nights": int(len(frame)), "changed_nights": int(changed.sum()),
            "models": {arm: {**model_summary(frame, variant, arm),
                              **paired_target_delta(frame, variant, arm)} for arm in ARMS}}
    quality = frame.loc[frame.quality_fixed].copy()
    quality_components = {"below_90pct_valid_onwrist_cutoff_to_wake": int(frame.monitoring_valid_onwrist_fraction.lt(.90).sum()),
        "cadence_or_timestamp_gap_cutoff_to_wake": int((~frame.monitoring_no_cadence_or_timestamp_gap).sum()),
        "multiple_eligible_candidates": int(frame.candidate_count.gt(1).sum())} if cohort == "mesa" else {
        "not_exactly_one_candidate": int(frame.candidate_count.ne(1).sum())}
    result = {"n_people": int(frame.subject_id.nunique()), "n_nights": int(len(frame)),
              "primary_reconstruction_maxabs_min": maxdiff,
              "candidate_ambiguity": {"multiple_candidate_nights": int(frame.candidate_count.gt(1).sum()),
                                      "maximum_candidates_per_night": int(frame.candidate_count.max())},
              "target_variants": variant_output,
              "fixed_quality_rule": {"definition": ("MESA: 20:00 cutoff-to-selected-wake monitoring window >=90% valid on-wrist binary-wake epochs, "
                    "complete 30-second cadence/timestamps, and one eligible sleep candidate per cutoff; CMI: one exact event pair candidate"),
                    "selected_nights": int(len(quality)), "selected_people": int(quality.subject_id.nunique()),
                    "excluded_nights": int(len(frame)-len(quality)),
                    "component_failure_counts_nonexclusive": quality_components,
                    "all_models": {arm:model_summary(frame,"original_longest",arm) for arm in ARMS},
                    "subset_models": {arm:model_summary(quality,"original_longest",arm) for arm in ARMS} if len(quality) else {},
                    "interpretation":"Outcome-blind descriptive subset. All-versus-subset differences mix measurement quality and sample composition; primary results retain all nights."}}
    if cohort == "mesa":
        metrics = ("act_sleep_min", "first_last_sleep_span_min", "eligible_rest_min",
                   "act_sleep_efficiency", "parent_valid_onwrist_fraction", "monitoring_valid_onwrist_fraction")
        result["actigraphy_construct"] = {name:{"median":float(frame[name].median()),
            "q25":float(frame[name].quantile(.25)), "q75":float(frame[name].quantile(.75))} for name in metrics}
        result["actigraphy_construct"]["definitions"] = {
            "act_sleep_min":"count of ACT wake=0 epochs × 0.5 min within selected eligible REST-S run",
            "first_last_sleep_span_min":"first to last ACT wake=0 epoch boundary; includes intervening wake and is not TST",
            "eligible_rest_min":"duration of contiguous on-wrist REST-S run with binary ACT wake values",
            "act_sleep_efficiency":"ACT sleep minutes / eligible REST-S minutes; weak ACT construct, not PSG sleep efficiency",
            "parent_valid_onwrist_fraction":"valid on-wrist binary-wake epochs / all observed epochs in enclosing REST-S label block; REST-S itself may encode eligibility",
            "monitoring_valid_onwrist_fraction":"valid on-wrist binary-wake epochs / expected 30-second epochs from fixed 20:00 cutoff to selected wake; bounded at 1"}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--prior-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = {"schema_version":1, "version":"2026-10-25-v1", "written_before_scoring":True,
        "primary":"retain all archived Test nights and original longest eligible candidate target",
        "sensitivity_targets":["earliest eligible candidate", "latest eligible candidate", "MESA selected eligible REST-S run boundary"],
        "quality_threshold":"fixed before scoring: >=90% valid on-wrist binary-wake epochs and complete cadence/timestamps from 20:00 cutoff to selected wake, exactly one eligible candidate",
        "uncertainty":"paired participant bootstrap, 10000 draws, seed 20261025; descriptive unadjusted intervals",
        "status":"retrospective reused Test; no refit, selection, relabeling, exclusion from primary, or service promotion",
        "interpretation":"MESA ACT-derived weak labels are not PSG ground truth. CMI events are not PSG stages."}
    dump(args.output/"protocol.json", protocol)
    outputs = {}
    for cohort in ("cmi", "mesa"):
        archived = pd.read_parquet(args.prior_run/f"{cohort}_predictions_private.parquet").sort_values("row_id").reset_index(drop=True)
        if cohort == "mesa":
            private = build_mesa_private(args.project/"데이터모델/연구용데이터/mesa_run_20261010/manifest.json",
                                         args.project/"데이터셋/MESA/actigraphy", archived)
        else:
            private = build_cmi_private(args.project/"데이터셋/CMI Detect Sleep States/train_events.csv", archived)
        private.to_parquet(args.output/f"{cohort}_label_sensitivity_private.parquet", index=False)
        outputs[cohort] = summarize(cohort, private, archived)
    artifact = {"schema_version":1, "artifact_type":"sleepwell_label_sensitivity", "version":"2026-10-25-v1",
        "study_status":"retrospective_reused_test", "no_service_promotion":True, "cohorts":outputs,
        "limitations":["Alternative target definitions are sensitivity analyses, not competing clinical truth.",
            "MESA ACT wake labels and REST-S intervals are actigraphy-derived weak constructs, not PSG.",
            "The fixed quality subset is not a randomized comparison and can differ in participant/night composition.",
            "No model was refit; a target change can alter apparent error without improving prediction."],
        "public_artifact_contains_identifiers":False,
        "provenance":{"protocol_sha256":sha256(args.output/"protocol.json"), "code_sha256":sha256(Path(__file__))}}
    dump(args.output/"label_sensitivity_v1.json", artifact)
    dump(args.output/"verification.json", {"archived_primary_targets_reproduced":True,
        "raw_mesa_test_files_hash_verified":True, "all_primary_test_nights_retained":True,
        "model_refit":False, "service_promotion":False, "private_outputs_separated":True})
    print(json.dumps({c:{"nights":v["n_nights"],"ambiguous":v["candidate_ambiguity"],
        "quality_nights":v["fixed_quality_rule"]["selected_nights"]} for c,v in outputs.items()},indent=2))


if __name__ == "__main__":
    main()
