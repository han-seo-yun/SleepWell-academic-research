"""Read-only replay of shipped Android golden fixtures through the research baseline.

Only the requested aggregate verification report is written. This does not run or
install Android code and does not regenerate the fixture used as the reference.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(r"E:\학술제")
APP = ROOT / "앱_소빈/통합 v8"
WEB = ROOT / "기타/SleepWell-integrated/web-app"
ASSETS = APP / "app/src/main/assets/models"
FIXTURE = APP / "app/src/androidTest/assets/portable_fusion_gradual_v1_golden.json"
TOLERANCE = 1e-7


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compare(expected, actual, stats, path="root"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise AssertionError(f"Object keys differ at {path}")
        for key, value in expected.items():
            compare(value, actual[key], stats, f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise AssertionError(f"List differs at {path}")
        for index, value in enumerate(expected):
            compare(value, actual[index], stats, f"{path}[{index}]")
    elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
        if not isinstance(actual, (float, int)) or not math.isfinite(actual):
            raise AssertionError(f"Non-finite/nonnumeric value at {path}")
        difference = abs(float(expected) - float(actual))
        stats["numeric_comparisons"] += 1
        stats["maximum_absolute_difference"] = max(stats["maximum_absolute_difference"], difference)
        if difference > TOLERANCE:
            raise AssertionError(f"Numeric difference {difference} exceeds tolerance at {path}")
    elif expected != actual:
        raise AssertionError(f"Value differs at {path}")


def record_to_python(row):
    # Match the Android fixture's named time/availability fields explicitly.
    return {"id": row["id"], "day": row["day"], "bed": row["bedtime"],
            "wake": row["wake_time"], "observed_at": row["modified_at"],
            "source": row["source"], "latency_min": 0}


def run():
    sys.path.insert(0, str(WEB))
    from fusion_forecast import PortableCohortModel
    from night_core import GRADUAL_POLICY, local_time

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if fixture["personalization_policy"] != GRADUAL_POLICY or len(fixture["cases"]) != 9:
        raise AssertionError("Unexpected shipped fixture policy or case count")
    models = {c: PortableCohortModel(ASSETS / f"robust_{c}_v2.json", c) for c in ("cmi", "mesa")}
    totals = {"numeric_comparisons": 0, "maximum_absolute_difference": 0.0}
    cases = []
    for case in fixture["cases"]:
        expected = case["expected"]
        cutoff = local_time(case["cutoff"])
        records = [record_to_python(r) for r in case["records"]]
        stats = {"numeric_comparisons": 0, "maximum_absolute_difference": 0.0}
        actual = {"snapshots": {}, "personalization": {}, "predictions": {}}
        research_source_difference = 0.0
        for cohort, model in models.items():
            prediction = model.predict(case["profile"], records, cutoff,
                                       personalization_policy=GRADUAL_POLICY)
            actual["snapshots"][cohort] = prediction["features"]
            actual["personalization"][cohort] = prediction["personalization"]
            for arm in ("physical", "data", "hybrid"):
                actual["predictions"][f"{arm}_{cohort}"] = {
                    "onset_offset_min": prediction[arm][0], "wake_offset_min": prediction[arm][1]}
            # The research replay uses source=research for accepted observational
            # rows. Check this canonical normalization alias on the fixture while
            # retaining archive exclusions; this is not wearable import parity.
            research_rows = [dict(r, source="research") if r["source"] == "diary" else r for r in records]
            research = model.predict(case["profile"], research_rows, cutoff,
                                     personalization_policy=GRADUAL_POLICY)
            alias_stats = {"numeric_comparisons": 0, "maximum_absolute_difference": 0.0}
            compare(prediction, research, alias_stats, f"{case['name']}.{cohort}.research_alias")
            research_source_difference = max(research_source_difference, alias_stats["maximum_absolute_difference"])
        compare(expected, actual, stats, case["name"])
        for endpoint in ("onset_offset_min", "wake_offset_min"):
            expected_ensemble = sum(expected["predictions"][f"hybrid_{c}"][endpoint] for c in models) / 2
            actual_ensemble = sum(actual["predictions"][f"hybrid_{c}"][endpoint] for c in models) / 2
            compare(expected_ensemble, actual_ensemble, stats, f"{case['name']}.ensemble.{endpoint}")
        cases.append({"name": case["name"], "passed": True, **stats,
                      "research_source_alias_max_difference": research_source_difference})
        totals["numeric_comparisons"] += stats["numeric_comparisons"]
        totals["maximum_absolute_difference"] = max(totals["maximum_absolute_difference"], stats["maximum_absolute_difference"])
    files = [FIXTURE, ASSETS / "robust_cmi_v2.json", ASSETS / "robust_mesa_v2.json",
             WEB / "fusion_forecast.py", WEB / "night_core.py", WEB / "robust_features.py",
             APP / "app/src/main/java/app/sleepwell/user/PortableFusionModel.kt",
             APP / "app/src/androidTest/java/app/sleepwell/user/PortableFusionGradualParityTest.kt",
             Path(__file__)]
    return {"checked_utc": datetime.now(timezone.utc).isoformat(), "passed": True,
            "scope": "canonical Python baseline vs existing Android fixture; no fresh Android execution",
            "cases_passed": len(cases), "tolerance": TOLERANCE, **totals,
            "model_version": fixture["model_version"], "personalization_policy": GRADUAL_POLICY,
            "comparisons": ["all feature snapshots", "all personalization state", "physical/data/hybrid endpoints",
                            "50/50 hybrid ensemble", "research source alias"],
            "cases": cases, "sha256": {str(p): sha(p) for p in files},
            "caveats": [
                "This proves reference fixture parity, not newly executed Kotlin bytecode or installation identity.",
                "MESA replay assumes observed_at=wake and baseline work schedule known at enrollment; real Health Connect availability depends on import timestamps.",
                "Canonical research-source acceptance is explicitly checked against diary-source fixture rows. Android wearable source/provenance policy is a separate contract.",
                "Existing source wall-clock offsets and selection constraints are preserved; +09:00 serialization does not identify measurement location.",
                "Forecast parity does not establish sleep recommendation benefit or a clinical effect."
            ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("passed", "cases_passed", "numeric_comparisons", "maximum_absolute_difference")}, ensure_ascii=False))
