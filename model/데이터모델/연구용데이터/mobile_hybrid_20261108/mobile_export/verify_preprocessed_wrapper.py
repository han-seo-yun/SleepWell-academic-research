"""Verify the export wrapper against an independent transcription of TabDPT forward."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import torch
import tabdpt.model as tabdpt_model
from tabdpt.regressor import TabDPTRegressor

from probe_export import PreprocessedRegressionForward, patch_exportable_attention


def native_forward_with_identity_preprocess(model, x_src, y_src):
    """Call the installed model.forward while bypassing only its three preprocessing calls."""
    original_clip = tabdpt_model.clip_outliers
    original_normalize = tabdpt_model.normalize_data
    identity = lambda values, *args, **kwargs: values
    tabdpt_model.clip_outliers = identity
    tabdpt_model.normalize_data = identity
    try:
        return model(x_src, y_src, False)
    finally:
        tabdpt_model.clip_outliers = original_clip
        tabdpt_model.normalize_data = original_normalize


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

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
    model = estimator.model.eval()
    generator = torch.Generator().manual_seed(20261108)
    x_src = torch.randn(args.batch, args.context + 1, 128, generator=generator)
    y_src = torch.randn(args.batch, args.context, generator=generator)
    wrapper = PreprocessedRegressionForward(model).eval()
    with torch.inference_mode():
        reference = native_forward_with_identity_preprocess(model, x_src, y_src)
        wrapper_before_patch = wrapper(x_src, y_src)
    patch_exportable_attention(model)
    with torch.inference_mode():
        wrapper_after_patch = wrapper(x_src, y_src)
    wrapper_difference = torch.abs(reference - wrapper_before_patch)
    attention_difference = torch.abs(reference - wrapper_after_patch)
    thinking_bytes = model.thinking_embed.detach().cpu().contiguous().numpy().tobytes()
    result = {
        "context": args.context,
        "batch": args.batch,
        "n_thinking_rows": int(model.n_thinking_rows),
        "thinking_embed_shape": list(model.thinking_embed.shape),
        "thinking_embed_sha256": hashlib.sha256(thinking_bytes).hexdigest(),
        "output_shape": list(reference.shape),
        "native_vs_wrapper_before_attention_patch_max_abs_logit_error": float(
            wrapper_difference.max().item()
        ),
        "native_vs_exportable_attention_max_abs_logit_error": float(
            attention_difference.max().item()
        ),
        "native_vs_exportable_attention_mean_abs_logit_error": float(
            attention_difference.mean().item()
        ),
        "criterion": "unpatched wrapper exactly equal; exportable attention max error <= 1e-4",
        "status": "pass"
        if float(wrapper_difference.max().item()) == 0.0
        and float(attention_difference.max().item()) <= 1e-4
        else "fail",
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if result["status"] != "pass":
        raise RuntimeError("preprocessed wrapper is not equivalent to native post-preprocess forward")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
