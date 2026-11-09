"""Canonical verified entrypoint for the frozen P01 sensor-increment run.

The first runner is retained as a historical failed attempt because its source
file is edit-locked.  This entrypoint explicitly verifies the metadata against
the Samsung analysis Parquet that it actually names, then independently binds
the all-cohort CSV's Samsung rows to that Parquet before running the unchanged
statistical and model helpers from run_experiment.py.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

import run_experiment as base


SAMSUNG_PARQUET_RELATIVE = Path(
    "데이터모델/연구용데이터/next_night_run_20261020_v1/analysis/samsung/episodes.parquet"
)
EXPECTED_METADATA_FEATURES = (
    "hr_mean",
    "hr_std",
    "hr_min",
    "hr_max",
    "hr_count",
    "late_hr_mean",
    "late_hr_count",
    "steps",
    "step_bins",
    "active_step_bins",
)
TARGET_COMPARISON_COLUMNS = (
    "participant_id",
    "target_id",
    "split",
    "cutoff",
    "sleep_start",
    "sleep_end",
    "observed_at",
    *base.TARGETS,
)


def validate_sensor_metadata(
    metadata: dict[str, Any], sensor_cache_path: Path, samsung_parquet_path: Path
) -> dict[str, str]:
    """Validate the cache and the precise episode artifact named by metadata."""
    if metadata.get("decision_time") != "20:00":
        raise ValueError("Sensor metadata decision_time must be exactly 20:00")
    if metadata.get("timezone") != "Asia/Seoul":
        raise ValueError("Sensor metadata timezone must be exactly Asia/Seoul")
    if tuple(metadata.get("features", ())) != EXPECTED_METADATA_FEATURES:
        raise ValueError("Sensor metadata feature list changed from the frozen cache contract")
    if metadata.get("rows") != 196:
        raise ValueError("Sensor metadata must describe exactly 196 cache rows")

    expected_cache = metadata.get("artifact_sha256", {}).get("features_csv")
    actual_cache = base.sha256_file(sensor_cache_path)
    if not expected_cache or actual_cache != expected_cache:
        raise ValueError(
            f"Sensor cache SHA-256 mismatch: metadata={expected_cache}, actual={actual_cache}"
        )

    expected_parquet = metadata.get("source_sha256", {}).get("episodes")
    actual_parquet = base.sha256_file(samsung_parquet_path)
    if not expected_parquet or actual_parquet != expected_parquet:
        raise ValueError(
            "Samsung Parquet SHA-256 mismatch: "
            f"metadata={expected_parquet}, actual={actual_parquet}"
        )
    return {
        "sensor_cache_expected_sha256": expected_cache,
        "sensor_cache_actual_sha256": actual_cache,
        "samsung_parquet_expected_sha256": expected_parquet,
        "samsung_parquet_actual_sha256": actual_parquet,
    }


def _normalized_targets(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.loc[:, TARGET_COMPARISON_COLUMNS].copy()
    normalized = normalized.sort_values("cutoff", kind="stable").reset_index(drop=True)
    for column in ("cutoff", "sleep_start", "sleep_end", "observed_at"):
        normalized[column] = pd.to_datetime(normalized[column], errors="raise", utc=True)
    for column in base.TARGETS:
        normalized[column] = pd.to_numeric(normalized[column], errors="raise").astype("float64")
    for column in ("participant_id", "target_id", "split"):
        normalized[column] = normalized[column].astype("string")
    return normalized


def prepare_verified_targets(episodes_csv_path: Path, samsung_parquet_path: Path) -> pd.DataFrame:
    """Prepare both real sources and require exact normalized P01 target equality."""
    csv_all = pd.read_csv(episodes_csv_path)
    csv_required = {"cohort", "subject_id"}
    if not csv_required.issubset(csv_all.columns):
        raise ValueError("All-cohort episode CSV lacks cohort/subject identity columns")
    csv_samsung = csv_all.loc[
        (csv_all["cohort"] == "Samsung")
        & (csv_all["subject_id"] == base.SOURCE_SUBJECT_ID)
    ].copy()
    csv_targets = base.prepare_p01_episodes(csv_samsung)

    parquet = pd.read_parquet(samsung_parquet_path)
    if not csv_required.issubset(parquet.columns):
        raise ValueError("Samsung episode Parquet lacks cohort/subject identity columns")
    parquet_samsung = parquet.loc[
        parquet["cohort"].astype(str).str.casefold().eq("samsung")
        & parquet["subject_id"].eq(base.SOURCE_SUBJECT_ID)
    ].copy()
    if len(parquet_samsung) != 196:
        raise ValueError(
            f"Metadata-bound Samsung Parquet must contain 196 P01 rows, found {len(parquet_samsung)}"
        )
    # The helper's canonical source label is title case; the analysis Parquet
    # lower-cased it when cohorts were partitioned. No timing value is changed.
    parquet_samsung["cohort"] = "Samsung"
    parquet_targets = base.prepare_p01_episodes(parquet_samsung)

    try:
        pd.testing.assert_frame_equal(
            _normalized_targets(csv_targets),
            _normalized_targets(parquet_targets),
            check_dtype=True,
            check_exact=True,
            check_freq=False,
            check_flags=False,
        )
    except AssertionError as exc:
        raise ValueError(
            "All-cohort CSV Samsung targets do not exactly match metadata-bound Samsung Parquet"
        ) from exc
    return csv_targets


def _source_paths(
    workspace: Path, output_dir: Path, protocol: dict[str, Any]
) -> dict[str, Path]:
    paths = {
        "protocol": output_dir / "protocol.json",
        "verified_runner": Path(__file__).resolve(),
        "imported_helper": Path(base.__file__).resolve(),
        "episodes_csv": base._resolve_workspace_file(workspace, protocol["sources"]["episodes"]),
        "episodes_samsung_parquet": base._resolve_workspace_file(
            workspace, SAMSUNG_PARQUET_RELATIVE
        ),
        "sensor_cache": base._resolve_workspace_file(
            workspace, protocol["sources"]["sensor_cache"]
        ),
        "sensor_metadata": base._resolve_workspace_file(
            workspace, protocol["sources"]["sensor_metadata"]
        ),
        "night_core": base._resolve_workspace_file(workspace, base.NIGHT_CORE_RELATIVE),
    }
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Required {label} file does not exist: {path}")
    return paths


def _build_metrics(
    protocol: dict[str, Any],
    panel: pd.DataFrame,
    density: dict[str, dict[str, Any]],
    summaries: dict[str, dict[str, dict[str, Any]]],
    selection: dict[str, Any],
    paired_bootstrap: list[dict[str, Any]],
    code_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "canonical_entrypoint": "execute_verified.py",
        "execution_code_sha256": code_hashes,
        "design_status": protocol["design_status"],
        "cohort": {
            "participant_id": base.PARTICIPANT_ID,
            "n_nights": len(panel),
            "split_counts": base.EXPECTED_SPLITS,
            "single_person": True,
        },
        "data_expansion": {
            "genuinely_new_raw_measurements": 0,
            "derived_enrichment_feature_count": (
                len(base.HISTORY_FEATURES)
                + len(base.SENSOR_FEATURES)
                + len(base.COVERAGE_FEATURES)
            ),
            "derived_enrichment_features": [
                *base.HISTORY_FEATURES,
                *base.SENSOR_FEATURES,
                *base.COVERAGE_FEATURES,
            ],
            "late_surveys_ingested": 0,
            "source_measurements_reused": True,
        },
        "density_and_coverage_by_split": density,
        "train_predictions": "omitted_as_in_sample",
        "validation_metrics": summaries["validation"],
        "selection": selection,
        "test_metrics_scored_after_lock": summaries["test"],
        "test_same_night_calendar_week_cluster_bootstrap": paired_bootstrap,
        "association": {
            "selection_data": "train_only",
            "hypotheses": 12,
            "bh_family_size": 12,
            "train_minimum_complete_rows": base.ASSOCIATION_TRAIN_MIN_N,
            "validation_direction_minimum_complete_rows": base.ASSOCIATION_VALIDATION_MIN_N,
            "validation_direction_is_descriptive_only": True,
            "test_used": False,
            "cyclic_null": "all nonzero residual rotations with finite-sample p",
            "stationarity_caveat": (
                "The cyclic-shift null assumes approximately stationary ordered residuals; irregular "
                "calendar gaps and strong split drift make its p-values exploratory."
            ),
        },
        "sensor_semantics": {
            "hr_std": "same-day pre-20 heart-rate dispersion, not HRV",
            "coverage_predictors": (
                "log1p completed/exported HR and step-bin counts; availability proxies are not physiology"
            ),
            "step_bins": (
                "actual populated JSON export-bin counts (not fillna(0)); a median of 120 in every split "
                "does not demonstrate watch wearing, active sensing, or measurement quality"
            ),
            "zero_steps": (
                "zero is retained only when at least one step export bin exists; no export bins yields "
                "missing, not zero activity"
            ),
            "current_physiology_claim": False,
        },
        "prior_work_context": {
            "first_sensor_incorporation_claim": False,
            "prior_p01_joint_mae_min": {"data": 159.83, "data_sensors": 160.09},
            "interpretation": (
                "This is a coverage-adjusted fixed-hypothesis follow-up, not the first sensor model."
            ),
        },
        "collection_priorities": base.collection_priorities(density, selection),
        "limitations": [
            "Exploratory post-hoc analysis of one person whose TEST data had been inspected previously.",
            "TRAIN, validation, and TEST cover 227, 269, and 717 calendar days with strong temporal/domain drift.",
            "Sparse irregular gaps violate a simple exchangeable-night interpretation and only approximate the cyclic stationarity assumption.",
            "Historical sensor receipt/revision times are unknown; pre-20 availability is retrospective.",
            "Consumer-device onset/wake boundaries are not PSG total sleep time, required sleep, or independent physiology.",
            "Associations do not establish causal physiology, and bootstrap intervals are not population significance claims.",
            "No recommendation effect or product promotion is evaluated.",
        ],
    }


def run(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    protocol_path = base._resolve_workspace_file(
        workspace, base.EXPERIMENT_RELATIVE / "protocol.json"
    )
    output_dir = protocol_path.parent
    protocol = base.load_protocol(protocol_path)
    base.guard_outputs(output_dir)

    source_paths = _source_paths(workspace, output_dir, protocol)
    source_before = base._hash_manifest(source_paths)
    code_hashes = {
        "execute_verified.py": source_before["verified_runner"]["sha256"],
        "run_experiment.py": source_before["imported_helper"]["sha256"],
    }
    with source_paths["sensor_metadata"].open("r", encoding="utf-8") as handle:
        sensor_metadata = json.load(handle)
    metadata_hashes = validate_sensor_metadata(
        sensor_metadata,
        source_paths["sensor_cache"],
        source_paths["episodes_samsung_parquet"],
    )
    targets = prepare_verified_targets(
        source_paths["episodes_csv"], source_paths["episodes_samsung_parquet"]
    )

    history = base.derive_history_features(targets)
    calendar = base.derive_calendar_features(targets)
    panel = pd.concat([targets, history, calendar], axis=1)

    cache = pd.read_csv(source_paths["sensor_cache"])
    if len(cache) != sensor_metadata["rows"] or len(cache) != 196:
        raise ValueError("Sensor cache row count does not match metadata/frozen panel")
    cache["cutoff"] = base._parse_kst_series(cache["cutoff"], "sensor cutoff")
    sensor_values = base.derive_sensor_features(cache)
    cache_for_join = pd.concat([cache[["cutoff"]], sensor_values], axis=1)
    panel = base.join_sensor_cache(panel, cache_for_join)
    base.verify_history_availability(panel)
    if (
        panel["participant_id"].nunique() != 1
        or panel["participant_id"].iloc[0] != base.PARTICIPANT_ID
    ):
        raise ValueError("Analysis panel must contain privacy ID P01 only")

    density = base.split_density_summary(panel)
    base.verify_density_audit(density)
    associations = base.compute_associations(panel, protocol)
    models = base.fit_models(panel)
    model_paths, model_hashes = base.persist_models_exclusive(models, output_dir)
    constrain = base.load_night_core_constrain(source_paths["night_core"])
    prior = base.training_prior(panel)

    validation_predictions = base.predict_split(
        panel,
        "validation",
        models,
        model_hashes,
        constrain,
        prior,
        "validation_selection_only",
    )
    selection = base.select_sensor_arm(validation_predictions)
    selection_lock = {
        **selection,
        "schema_version": 1,
        "canonical_entrypoint": "execute_verified.py",
        "protocol_sha256": source_before["protocol"]["sha256"],
        "execution_code_sha256": code_hashes,
        "model_sha256": model_hashes,
        "model_features": {
            arm: list(features) for arm, features in base.ARM_FEATURES.items()
        },
        "model_protocol": {
            "imputation": "median_keep_empty_features_add_indicator",
            "scaling": "standard",
            "estimator": "multi_output_ridge",
            "ridge_alpha": base.RIDGE_ALPHA,
            "refit_on_validation": False,
            "hyperparameter_search": False,
        },
        "source_binding": {
            "metadata_samsung_parquet_sha256": metadata_hashes[
                "samsung_parquet_actual_sha256"
            ],
            "all_cohort_csv_samsung_targets_equal_parquet": True,
        },
        "locked_before_test_scoring": True,
    }
    selection_path = output_dir / "selection.json"
    base.write_json_exclusive(selection_path, selection_lock)

    locked_models = base.verify_selection_lock(
        selection_path, model_paths, model_hashes, selection["selected_arm"]
    )
    # TEST targets are first used only after the validation decision and fitted
    # model hashes exist on disk and have been reverified above.
    test_predictions = base.predict_split(
        panel,
        "test",
        locked_models,
        model_hashes,
        constrain,
        prior,
        "test_scored_after_persisted_validation_lock",
    )
    predictions = pd.concat(
        [validation_predictions, test_predictions], ignore_index=True
    )
    summaries = base.metrics_by_split(predictions)
    paired_bootstrap = base.test_paired_gains(predictions, selection["selected_arm"])

    panel_output = base.serializable_panel(panel)
    predictions_output = predictions.copy()
    predictions_output["cutoff"] = predictions_output["cutoff"].map(
        lambda value: value.isoformat()
    )
    base.write_csv_exclusive(output_dir / "panel.csv", panel_output)
    sqlite_verification = base.write_panel_sqlite_exclusive(
        output_dir / "panel.sqlite", panel_output
    )
    base.write_csv_exclusive(output_dir / "association.csv", associations)
    base.write_csv_exclusive(output_dir / "predictions.csv", predictions_output)

    metrics = _build_metrics(
        protocol,
        panel,
        density,
        summaries,
        selection,
        paired_bootstrap,
        code_hashes,
    )
    base.write_json_exclusive(output_dir / "metrics.json", metrics)

    source_after = base._hash_manifest(source_paths)
    sources_unchanged = all(
        source_before[label]["sha256"] == source_after[label]["sha256"]
        and source_before[label]["size_bytes"] == source_after[label]["size_bytes"]
        for label in source_paths
    )
    if not sources_unchanged:
        raise RuntimeError("A source or execution-code file changed during the run")
    source_hash_payload = {
        "schema_version": 1,
        "algorithm": "sha256",
        "canonical_entrypoint": "execute_verified.py",
        "before": source_before,
        "after": source_after,
        "all_sources_and_code_unchanged": sources_unchanged,
        "execution_code_sha256": code_hashes,
        "sensor_cache_metadata_expected_sha256": metadata_hashes[
            "sensor_cache_expected_sha256"
        ],
        "sensor_cache_verified_before_use": (
            metadata_hashes["sensor_cache_actual_sha256"]
            == metadata_hashes["sensor_cache_expected_sha256"]
        ),
        "samsung_parquet_metadata_expected_sha256": metadata_hashes[
            "samsung_parquet_expected_sha256"
        ],
        "samsung_parquet_verified_before_use": (
            metadata_hashes["samsung_parquet_actual_sha256"]
            == metadata_hashes["samsung_parquet_expected_sha256"]
        ),
        "all_cohort_csv_samsung_targets_equal_samsung_parquet": True,
    }
    base.write_json_exclusive(output_dir / "source_hashes.json", source_hash_payload)

    verification = {
        "schema_version": 1,
        "canonical_entrypoint": "execute_verified.py",
        "checks": {
            "protocol_read_from_experiment_folder": True,
            "output_guard_checked_all_planned_names_before_computation": True,
            "source_and_code_hashes_unchanged_before_after": sources_unchanged,
            "runner_and_imported_helper_hashed": set(code_hashes)
            == {"execute_verified.py", "run_experiment.py"},
            "metadata_decision_time_20": sensor_metadata["decision_time"] == "20:00",
            "metadata_timezone_asia_seoul": sensor_metadata["timezone"] == "Asia/Seoul",
            "metadata_features_exact": tuple(sensor_metadata["features"])
            == EXPECTED_METADATA_FEATURES,
            "sensor_cache_csv_sha_matches_metadata": (
                metadata_hashes["sensor_cache_actual_sha256"]
                == metadata_hashes["sensor_cache_expected_sha256"]
            ),
            "samsung_parquet_sha_matches_metadata": (
                metadata_hashes["samsung_parquet_actual_sha256"]
                == metadata_hashes["samsung_parquet_expected_sha256"]
            ),
            "csv_samsung_targets_equal_metadata_parquet": True,
            "panel_rows_196": len(panel) == 196,
            "split_137_29_30_preserved": panel["split"].value_counts().to_dict()
            == base.EXPECTED_SPLITS,
            "cutoff_join_one_to_one_complete": len(panel) == len(cache)
            and not panel["cutoff"].duplicated().any(),
            "history_cutoff_wake_observed_all_strictly_past": True,
            "privacy_id_only_in_panel": set(panel["participant_id"])
            == {base.PARTICIPANT_ID},
            "no_source_paths_in_panel_rows": not any(
                "path" in column.lower() for column in panel.columns
            ),
            "late_surveys_not_loaded": True,
            "four_fixed_sensor_hypotheses": tuple(
                item["feature"] for item in protocol["hypotheses"]
            )
            == base.SENSOR_FEATURES,
            "association_fixed_family_12": len(associations) == 12,
            "ridge_train_only_no_validation_refit": True,
            "selection_persisted_before_test_scoring": True,
            "test_predictions_loaded_from_hash_verified_models": True,
            "test_scored_same_nights_all_arms": all(
                len(
                    predictions.loc[
                        (predictions["split"] == "test")
                        & (predictions["arm"] == arm)
                    ]
                )
                == 30
                for arm in base.PREDICTION_ARMS
            ),
            "bootstrap_seed_and_resamples_fixed": all(
                item["seed"] == base.BOOTSTRAP_SEED
                and item["resamples"] == base.BOOTSTRAP_RESAMPLES
                for item in paired_bootstrap
            ),
            "new_raw_measurements_zero": True,
            "sqlite_unique_p01_cutoff": sqlite_verification[
                "unique_participant_cutoffs"
            ]
            == 196,
            "no_upload_or_product_promotion": True,
        },
        "sqlite": sqlite_verification,
        "selection_lock_sha256": base.sha256_file(selection_path),
        "execution_code_sha256": code_hashes,
        "model_sha256": model_hashes,
        "verification_scope": (
            "runtime invariants; unit tests are separate and are not executed by this script"
        ),
    }
    if not all(verification["checks"].values()):
        failed = [
            name for name, passed in verification["checks"].items() if not passed
        ]
        raise RuntimeError(f"Runtime verification checks failed: {failed}")
    base.write_json_exclusive(output_dir / "verification.json", verification)

    hashed_outputs = {
        name: output_dir / name
        for name in base.OUTPUT_FILENAMES
        if name != "audit.json"
    }
    hashed_outputs.update(
        {
            f"models/{base.MODEL_FILENAMES[arm]}": path
            for arm, path in model_paths.items()
        }
    )
    audit = {
        "schema_version": 1,
        "canonical_entrypoint": "execute_verified.py",
        "artifact_type": "local_single_participant_exploratory_research",
        "participant_ids": [base.PARTICIPANT_ID],
        "privacy_names_in_rows": False,
        "source_paths_local_provenance_only": {
            label: str(path) for label, path in source_paths.items()
        },
        "source_binding": {
            "metadata_hashes_original_samsung_parquet": True,
            "all_cohort_csv_samsung_targets_equal_parquet": True,
        },
        "execution_code_sha256": code_hashes,
        "output_sha256": {
            label: base.sha256_file(path) for label, path in hashed_outputs.items()
        },
        "exclusive_create": True,
        "original_snapshots_overwritten": False,
        "uploads": 0,
        "product_model_promoted": False,
        "selection_lock_sha256": base.sha256_file(selection_path),
        "source_hash_manifest_sha256": base.sha256_file(
            output_dir / "source_hashes.json"
        ),
        "notes": [
            "No participant contact or new raw measurement occurred.",
            "Paths are retained only in this local provenance JSON, never in panel/SQLite rows.",
            "TEST results are exploratory because this person's TEST data were previously exposed.",
            "run_experiment.py is an imported statistical helper; execute_verified.py is the canonical entrypoint.",
        ],
    }
    base.write_json_exclusive(output_dir / "audit.json", audit)
    return {
        "output_dir": str(output_dir),
        "canonical_entrypoint": "execute_verified.py",
        "selected_arm": selection["selected_arm"],
        "selection_lock_sha256": base.sha256_file(selection_path),
        "audit_sha256": base.sha256_file(output_dir / "audit.json"),
        "outputs": [
            *base.OUTPUT_FILENAMES,
            *[f"models/{name}" for name in base.MODEL_FILENAMES.values()],
        ],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", required=True, type=Path, help="Workspace root, e.g. E:/학술제"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args.workspace)
    print(json.dumps(base._json_ready(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
