from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
from tabdpt.regressor import TabDPTRegressor
from tabdpt.utils import clip_outliers, normalize_data

from mobile_retrieval import mobile_l2_indices
from portable_runtime import PortableTabDptRuntime, normalize_context_y
from probe_export import patch_exportable_attention


class DiagnosticForward(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, x_src: torch.Tensor, y_src: torch.Tensor):
        x_src = x_src.transpose(0, 1)
        y_src = y_src.transpose(0, 1)
        eval_pos = y_src.shape[0]
        clipped_once = clip_outliers(x_src, eval_pos, n_sigma=self.model.clip_sigma)
        normalized = normalize_data(clipped_once, eval_pos)
        clipped_twice = clip_outliers(normalized, eval_pos, n_sigma=self.model.clip_sigma)
        x_src = clipped_twice
        x_src = torch.nan_to_num(x_src, nan=0.0, posinf=0.0, neginf=0.0)
        src = self.model.enc_norm(self.model.encoder(x_src))
        states = [src]
        for index, layer in enumerate(self.model.transformer_encoder):
            y_emb = self.model.reg_y_encoders[index](y_src.unsqueeze(-1))
            src = src + layer(src, y_emb, eval_pos)
            states.append(src)
        logits = self.model.head(src[eval_pos:].float())
        return logits, torch.stack(states), clipped_once, normalized, clipped_twice


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()

    runtime = PortableTabDptRuntime(args.bundle)
    cohort = "mesa"
    with np.load(args.cache_dir / f"{cohort}.npz", allow_pickle=False) as data:
        query = data["X_hybrid"][data["splits"] == "test"][0].astype(np.float32)
    x_train, y_train = runtime.contexts[cohort]
    k = int(runtime.manifest["context_size"])
    indices = mobile_l2_indices(x_train, query[None], k)[0]
    permutation = np.asarray(
        runtime.manifest["cohorts"][cohort]["feature_permutations"][0], dtype=np.int64
    )
    normalized_full_y, _, _ = normalize_context_y(y_train[:, 0])
    normalized_y = normalized_full_y[indices]
    x_src = np.zeros((1, k + 1, 128), dtype=np.float32)
    x_src[0, :k, : len(permutation)] = x_train[indices][:, permutation]
    x_src[0, k, : len(permutation)] = query[permutation]
    y_src = normalized_y[None, :]

    estimator = TabDPTRegressor(
        normalizer=None,
        device="cpu",
        use_flash=False,
        compile=False,
        model_weight_path=str(args.weights),
        verbose=False,
    )
    patch_exportable_attention(estimator.model)
    wrapper = DiagnosticForward(estimator.model).eval()
    tensors = (torch.from_numpy(x_src), torch.from_numpy(y_src))
    with torch.inference_mode():
        reference_outputs = wrapper(*tensors)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if not args.reuse:
        torch.onnx.export(
            wrapper,
            tensors,
            str(args.output),
            input_names=["x_src", "y_src"],
            output_names=["logits", "states", "clipped_once", "normalized", "clipped_twice"],
            opset_version=20,
            dynamo=True,
            external_data=True,
        )
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = ort.InferenceSession(str(args.output), sess_options=options, providers=["CPUExecutionProvider"])
    actual_outputs = session.run(None, {"x_src": x_src, "y_src": y_src})
    reference_logits, reference_states = reference_outputs[:2]
    actual_logits, actual_states = actual_outputs[:2]
    reference_states_np = reference_states.numpy()
    state_metrics = []
    for index in range(len(reference_states_np)):
        difference = np.abs(reference_states_np[index] - actual_states[index])
        state_metrics.append(
            {
                "state": index,
                "max_abs_error": float(np.max(difference)),
                "mean_abs_error": float(np.mean(difference)),
                "reference_max_abs": float(np.max(np.abs(reference_states_np[index]))),
            }
        )
    result = {
        "export_seconds": time.perf_counter() - started,
        "logits_max_abs_error": float(np.max(np.abs(reference_logits.numpy() - actual_logits))),
        "states": state_metrics,
        "preprocessing": {
            name: {
                "max_abs_error": float(np.max(np.abs(reference.detach().numpy() - actual))),
                "mean_abs_error": float(np.mean(np.abs(reference.detach().numpy() - actual))),
            }
            for name, reference, actual in zip(
                ("clipped_once", "normalized", "clipped_twice"),
                reference_outputs[2:],
                actual_outputs[2:],
            )
        },
    }
    ref_norm = reference_outputs[3].detach().numpy()
    ort_norm = actual_outputs[3]
    max_coordinate = np.unravel_index(np.argmax(np.abs(ref_norm - ort_norm)), ref_norm.shape)
    feature = int(max_coordinate[-1])
    source_feature = reference_outputs[2].detach().numpy()[:k, 0, feature]
    result["normalization_max_error_detail"] = {
        "coordinate": [int(value) for value in max_coordinate],
        "reference": float(ref_norm[max_coordinate]),
        "onnx": float(ort_norm[max_coordinate]),
        "context_feature_mean_float32": float(np.mean(source_feature, dtype=np.float32)),
        "context_feature_std_sample_float32": float(np.std(source_feature, ddof=1, dtype=np.float32)),
        "context_feature_min": float(np.min(source_feature)),
        "context_feature_max": float(np.max(source_feature)),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
