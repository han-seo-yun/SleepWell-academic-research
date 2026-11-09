from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch  # noqa: F401  # Preload matching CUDA/cuDNN runtime DLLs.
import onnxruntime as ort

from portable_onnx_predict import portable_onnx_predict_target
from portable_runtime import PortableTabDptRuntime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--goldens", type=Path, required=True)
    parser.add_argument("--batch-model", type=Path, required=True)
    parser.add_argument("--provider", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    runtime = PortableTabDptRuntime(args.bundle)
    golden = json.loads(args.goldens.read_text(encoding="utf-8"))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    requested = "CUDAExecutionProvider" if args.provider == "cuda" else "CPUExecutionProvider"
    providers = (
        [requested]
        if args.provider == "cpu"
        else [(requested, {"use_tf32": "0"}), "CPUExecutionProvider"]
    )
    session = ort.InferenceSession(
        str(args.batch_model), sess_options=options, providers=providers
    )
    if session.get_providers()[0] != requested:
        raise RuntimeError(f"provider fallback: {session.get_providers()}")
    result = {
        "cohorts": {},
        "provider": args.provider,
        "onnxruntime_version": ort.__version__,
        "active_providers": session.get_providers(),
        "provider_options": session.get_provider_options(),
        "graph_optimization_level": "ORT_ENABLE_ALL(default)",
    }
    for cohort in ("cmi", "mesa"):
        queries = np.stack(
            [runtime.transform_raw(cohort, np.asarray(case["raw39"])) for case in golden["cases"]]
        )
        x_train, y_train = runtime.contexts[cohort]
        predictions = []
        timings = []
        for channel in range(2):
            values, timing = portable_onnx_predict_target(
                session,
                x_train,
                y_train[:, channel],
                queries,
                runtime.manifest["cohorts"][cohort]["feature_permutations"],
                context_size=int(runtime.manifest["context_size"]),
            )
            predictions.append(values)
            timings.append(timing)
        predicted = np.column_stack(predictions)
        expected = np.asarray(
            [case["cohorts"][cohort]["portable_python_onnx_raw"] for case in golden["cases"]]
        )
        difference = np.abs(predicted - expected)
        result["cohorts"][cohort] = {
            "max_abs_error_min": float(np.max(difference)),
            "mean_abs_error_min": float(np.mean(difference)),
            "timings": timings,
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
