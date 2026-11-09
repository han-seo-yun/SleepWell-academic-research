from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
from tabdpt.regressor import TabDPTRegressor

from probe_export import PreprocessedRegressionForward, RegressionForward, patch_exportable_attention, rss_bytes


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--opset", type=int, default=20)
    parser.add_argument("--preprocessed", action="store_true")
    parser.add_argument("--batch", type=int, default=1)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    estimator = TabDPTRegressor(
        normalizer=None,
        missing_indicators=False,
        clip_sigma=8.0,
        feature_reduction="pca",
        context_reduction="retrieval",
        device="cpu",
        use_flash=False,
        compile=False,
        model_weight_path=str(args.weights),
        verbose=False,
    )
    patch_exportable_attention(estimator.model)
    wrapper_class = PreprocessedRegressionForward if args.preprocessed else RegressionForward
    wrapper = wrapper_class(estimator.model).eval()
    generator = torch.Generator().manual_seed(20261108)
    if args.batch < 1:
        raise ValueError("batch must be positive")
    x_src = torch.randn(args.batch, args.context + 1, estimator.max_features, generator=generator)
    y_src = torch.randn(args.batch, args.context, generator=generator)
    with torch.inference_mode():
        reference = wrapper(x_src, y_src).cpu().numpy()

    started = time.perf_counter()
    torch.onnx.export(
        wrapper,
        (x_src, y_src),
        str(args.output),
        input_names=["x_src", "y_src"],
        output_names=["logits"],
        opset_version=args.opset,
        dynamo=True,
        external_data=True,
    )
    export_seconds = time.perf_counter() - started
    onnx.checker.check_model(str(args.output))

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    session_options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(args.output),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    warm_started = time.perf_counter()
    actual = session.run(
        ["logits"],
        {"x_src": x_src.numpy(), "y_src": y_src.numpy()},
    )[0]
    first_inference_seconds = time.perf_counter() - warm_started
    max_abs_error = float(np.max(np.abs(reference - actual)))
    mean_abs_error = float(np.mean(np.abs(reference - actual)))

    related_files = sorted(
        path for path in args.output.parent.glob(f"{args.output.name}*") if path.is_file()
    )
    result = {
        "torch_version": torch.__version__,
        "onnx_version": onnx.__version__,
        "onnxruntime_version": ort.__version__,
        "opset": args.opset,
        "context": args.context,
        "batch": args.batch,
        "x_src_semantics": "deterministic_mobile_preprocessed" if args.preprocessed else "raw_padded_model_preprocess",
        "inputs": {
            "x_src": [args.batch, args.context + 1, estimator.max_features],
            "y_src": [args.batch, args.context],
        },
        "output": {"logits": list(reference.shape)},
        "export_seconds": export_seconds,
        "first_inference_seconds_desktop_cpu": first_inference_seconds,
        "rss_after_inference_bytes": rss_bytes(),
        "onnx_vs_patched_pytorch_max_abs_error": max_abs_error,
        "onnx_vs_patched_pytorch_mean_abs_error": mean_abs_error,
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in related_files
        ],
    }
    manifest = args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
