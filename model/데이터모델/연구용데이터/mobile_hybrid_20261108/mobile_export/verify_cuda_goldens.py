"""Verify CUDAExecutionProvider against frozen CPU/Android validation goldens."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch  # noqa: F401  # Preload the matching CUDA/cuDNN runtime DLLs.
import onnxruntime as ort

from portable_runtime import PortableTabDptRuntime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--goldens", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("CUDAExecutionProvider unavailable")
    runtime = PortableTabDptRuntime(args.bundle)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    runtime.session = ort.InferenceSession(
        str(args.bundle / runtime.manifest["model_file"]),
        sess_options=options,
        providers=[
            ("CUDAExecutionProvider", {"use_tf32": "0"}),
            "CPUExecutionProvider",
        ],
    )
    if runtime.session.get_providers()[0] != "CUDAExecutionProvider":
        raise RuntimeError(f"CUDA provider fallback: {runtime.session.get_providers()}")

    payload = json.loads(args.goldens.read_text(encoding="utf-8"))
    errors: list[float] = []
    records = []
    for case in payload["cases"]:
        raw39 = np.asarray(case["raw39"], dtype=np.float64)
        case_errors = {}
        for cohort in ("cmi", "mesa"):
            actual = runtime.predict_transformed(cohort, runtime.transform_raw(cohort, raw39))
            expected = np.asarray(
                case["cohorts"][cohort]["portable_python_onnx_raw"], dtype=np.float64
            )
            absolute = np.abs(actual.astype(np.float64) - expected)
            errors.extend(absolute.tolist())
            case_errors[cohort] = absolute.tolist()
        records.append({"id": case["id"], "absolute_error_min": case_errors})
    result = {
        "status": "pass" if max(errors) < (1.0 / 60.0) else "fail",
        "criterion": "maximum absolute final prediction drift < 1 second",
        "cases": len(payload["cases"]),
        "prediction_values": len(errors),
        "max_abs_error_min": float(max(errors)),
        "mean_abs_error_min": float(np.mean(errors)),
        "onnxruntime_version": ort.__version__,
        "available_providers": ort.get_available_providers(),
        "active_providers": runtime.session.get_providers(),
        "provider_options": runtime.session.get_provider_options(),
        "graph_optimization_level": "ORT_DISABLE_ALL",
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, indent=2))
    if result["status"] != "pass":
        raise RuntimeError("CUDA provider parity failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
