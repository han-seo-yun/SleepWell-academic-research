"""Reproduce the researcher lab's calculations and PNGs without a browser."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re

from research_lab import evaluate_run
from research_lab_charts import render_charts
from research_lab_server import MAX_BODY_BYTES, _object_without_duplicate_keys, _reject_non_finite_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true", help="Explicit seeded demonstration, never real-data evidence")
    source.add_argument("--input", type=Path, help="Canonical lab request JSON, not an evaluated-result JSON")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing directories are refused")
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        parser.error("Output directory already exists; choose a new research run name")
    if args.synthetic:
        payload = {"schema_version": 1, "mode": "synthetic", "cohorts": ["cmi", "mesa"],
                   "synthetic": {"n_nights": 42, "seed": 42, "start_date": "2026-01-01",
                                 "bed_hour": 23.5, "duration_hours": 8, "jitter_minutes": 45,
                                 "phase_shift_minutes": 60, "shift_after_night": 21,
                                 "missing_every": 0, "report_delay_hours": 1}}
    else:
        with args.input.open("rb") as handle:
            content = handle.read(MAX_BODY_BYTES + 1)
        if len(content) > MAX_BODY_BYTES:
            parser.error("Input exceeds the 2 MiB contract")
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys,
                             parse_constant=_reject_non_finite_json)
    result = evaluate_run(payload)
    charts = render_charts(result)
    images, descriptors = {}, []
    for chart in charts:
        identity = chart["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise ValueError("Unsafe chart identifier")
        prefix = "data:image/png;base64,"
        if not chart["data_uri"].startswith(prefix):
            raise ValueError("Expected a Matplotlib PNG")
        image = base64.b64decode(chart["data_uri"][len(prefix):], validate=True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG signature")
        filename = f"{result['mode']}-{identity}.png"
        images[filename] = image
        descriptors.append({"id": identity, "title": chart["title"], "alt": chart["alt"],
                            "file": filename, "sha256": hashlib.sha256(image).hexdigest()})
    result["charts"] = descriptors
    source_dir = Path(__file__).resolve().parent
    result["execution_source_sha256"] = {
        name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        for name in ("research_lab_cli.py", "research_lab.py", "research_lab_charts.py",
                     "fusion_forecast.py", "robust_features.py", "night_core.py")
    }
    result["export_privacy"] = "Local research output may contain sensitive participant timing; not uploaded or included in distributions."
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    destination.mkdir(parents=True, exist_ok=False)
    for filename, image in images.items():
        with (destination / filename).open("xb") as handle:
            handle.write(image)
    with (destination / "result.json").open("x", encoding="utf-8") as handle:
        handle.write(encoded)
    print(json.dumps({"mode": result["mode"], "summary": result["summary"],
                      "png_count": len(images), "output": str(destination)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
