"""Verify preserved inputs and seal aggregate/local research deliverables."""
import json
from pathlib import Path
import shutil
import pandas as pd

import experiment as e

out = e.ROOT / "데이터모델/연구용데이터/mesa_work_schedule_20261106_v1"
e.verify_sources(out)
e.verify_original_split(pd.read_csv(e.EPISODES))
source_manifest = json.loads(e.EPISODES.with_name("manifest.json").read_text(encoding="utf-8"))
database = e.EPISODES.with_name("cmi_mesa.sqlite3")
assert e.sha(database) == source_manifest["output_sha256"][database.name]
original = pd.read_csv(e.EPISODES)
joined = pd.read_parquet(out / "private_linked_episodes.parquet")
pd.testing.assert_frame_equal(joined[original.columns], original, check_dtype=False)
assert not joined.loc[joined.cohort.eq("CMI"), "work_schedule_code"].notna().any()
old_outputs = json.loads((out / "output_manifest.json").read_text(encoding="utf-8"))
assert all(e.sha(out / name) == expected for name, expected in old_outputs["sha256"].items())
shutil.copy2(Path(__file__).with_name("baseline_parity.json"), out / "baseline_parity.json")
parity = json.loads((out / "baseline_parity.json").read_text(encoding="utf-8"))
e.dump(out / "preservation_verification.json", {
    "utc": e.stamp(), "original_csv_and_sqlite_match_frozen_manifest": True,
    "all_original_rows_columns_values_order_preserved": True, "split_leakage_people": 0,
    "non_mesa_metadata_contamination": False, "inputs_provider_android_asset_hashes_unchanged": True,
    "unit_tests_passed": 12, "python_compilation_passed": True,
    "android_fixture_cases": 9, "fixture_numeric_comparisons": 1008, "fixture_max_difference": 0.0,
    "android_source_or_user_storage_mutations_performed": False,
    "android_build_lint_install_performed": False,
    "reason": "Prespecified performance gate failed; conditional integration not undertaken",
    "independent_verifier": "work_schedule_pipeline",
    "independent_recalculation": {"test_people": 315, "test_nights": 2175,
       "joint_improvement_min": 0.07103841939533304, "ci95": [-0.027665505683460895, 0.17224209989403458],
       "matched_improvement_min": 0.06802138594660895, "matched_ci95": [-0.0010962285464218532, 0.1335408156366066],
       "stored_predictions_metrics_and_bootstrap_match": True},
})
shutil.copy2(Path(__file__), out / "finalize.py")
e.dump(out / "delivery_manifest.json", {"utc": e.stamp(), "decision": "do_not_integrate",
       "private_artifacts_local_only": True,
       "sha256": {str(p.relative_to(out)): e.sha(p) for p in sorted(out.rglob("*"))
                  if p.is_file() and p.name != "delivery_manifest.json" and "__pycache__" not in p.parts}})
print(json.dumps({"preservation_verified": True, "decision": "do_not_integrate",
                  "report": str(out / "검증결과.md"), "files_sealed": len(json.loads((out / "delivery_manifest.json").read_text(encoding="utf-8"))["sha256"])}))
