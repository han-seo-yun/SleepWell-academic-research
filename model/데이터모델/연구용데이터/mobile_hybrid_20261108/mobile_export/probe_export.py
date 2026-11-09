from __future__ import annotations

import argparse
import json
import math
import os
import time
import types
from pathlib import Path

import torch
import torch.nn.functional as F
from tabdpt.model import TransformerEncoderLayer
from tabdpt.regressor import TabDPTRegressor


class RegressionForward(torch.nn.Module):
    """Freeze TabDPT's task flag for a two-tensor mobile forward contract."""

    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, x_src: torch.Tensor, y_src: torch.Tensor) -> torch.Tensor:
        return self.model(x_src, y_src, False)


class PreprocessedRegressionForward(torch.nn.Module):
    """TabDPT forward after its numerically unstable row normalization."""

    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, x_src: torch.Tensor, y_src: torch.Tensor) -> torch.Tensor:
        x_src = x_src.transpose(0, 1)
        y_src = y_src.transpose(0, 1)
        eval_pos = y_src.shape[0]
        n_think = self.model.n_thinking_rows
        src = self.model.enc_norm(self.model.encoder(x_src))
        if n_think > 0:
            batch = src.shape[1]
            thinking = self.model.thinking_embed.unsqueeze(1).expand(n_think, batch, -1)
            src = torch.cat([thinking, src], dim=0)
        for index, layer in enumerate(self.model.transformer_encoder):
            y_emb = self.model.reg_y_encoders[index](y_src.unsqueeze(-1))
            if n_think > 0:
                batch = y_emb.shape[1]
                zeros = y_emb.new_zeros(n_think, batch, y_emb.shape[-1])
                y_emb = torch.cat([zeros, y_emb], dim=0)
            src = src + layer(src, y_emb, eval_pos + n_think)
        return self.model.head(src[eval_pos + n_think :].float())


def portable_attention_forward(
    self: TransformerEncoderLayer,
    x: torch.Tensor,
    y: torch.Tensor,
    eval_pos: int,
) -> torch.Tensor:
    """Equivalent attention with tensor scaling applied to Q before SDPA.

    ``torch.export`` cannot guard TabDPT's tensor-valued ``scale=`` argument.
    Applying beta to Q and using SDPA's default 1/sqrt(head_dim) scale is
    algebraically identical and export friendly.
    """

    x = x.transpose(0, 1)
    y = y.transpose(0, 1)
    batch, length, _ = x.size()
    h = self.attn_norm(x)
    q = self.q_proj(h)
    gate = torch.sigmoid(self.q_gate(q))
    k = self.k_proj(h[:, :eval_pos])
    h_ctx = h[:, :eval_pos]
    v = self.v_proj(torch.cat([h_ctx, y], dim=-1))
    q = q.view(batch, length, self.num_heads, self.head_dim).transpose(1, 2)
    k = k.view(batch, eval_pos, self.num_heads, self.head_dim).transpose(1, 2)
    v = v.view(batch, eval_pos, self.num_heads, self.head_dim).transpose(1, 2)
    q, k = self.q_norm(q), self.k_norm(k)
    beta = self.get_scale_param(eval_pos, device=q.device, dtype=q.dtype)
    attn = F.scaled_dot_product_attention(q * beta, k, v).transpose(1, 2)
    attn = attn * gate.unsqueeze(-1)
    attn = self.out_proj(attn.reshape(batch, length, self.num_heads * self.head_dim))
    residual = attn + self.ff(self.ff_norm(x + attn))
    return residual.transpose(0, 1)


def patch_exportable_attention(model: torch.nn.Module) -> None:
    for module in model.modules():
        if isinstance(module, TransformerEncoderLayer):
            module.forward = types.MethodType(portable_attention_forward, module)


def rss_bytes() -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    return int(psutil.Process(os.getpid()).memory_info().rss)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--context", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    started_rss = rss_bytes()
    started = time.perf_counter()
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
    load_seconds = time.perf_counter() - started
    loaded_rss = rss_bytes()

    wrapper = RegressionForward(estimator.model).eval()
    generator = torch.Generator().manual_seed(20261108)
    x_src = torch.randn(1, args.context + 1, estimator.max_features, generator=generator)
    y_src = torch.randn(1, args.context, generator=generator)

    with torch.inference_mode():
        eager = wrapper(x_src, y_src)

    patch_exportable_attention(estimator.model)
    with torch.inference_mode():
        portable = wrapper(x_src, y_src)
    portable_max_abs_error = float(torch.max(torch.abs(eager - portable)).item())

    export_started = time.perf_counter()
    exported = torch.export.export(wrapper, (x_src, y_src), strict=False)
    export_seconds = time.perf_counter() - export_started
    with torch.inference_mode():
        exported_result = exported.module()(x_src, y_src)

    max_abs_error = float(torch.max(torch.abs(eager - exported_result)).item())
    result = {
        "torch_version": torch.__version__,
        "context": args.context,
        "load_seconds": load_seconds,
        "export_seconds": export_seconds,
        "rss_before_bytes": started_rss,
        "rss_loaded_bytes": loaded_rss,
        "rss_after_export_bytes": rss_bytes(),
        "output_shape": list(eager.shape),
        "portable_eager_max_abs_error": portable_max_abs_error,
        "export_eager_max_abs_error": max_abs_error,
        "graph_nodes": sum(1 for _ in exported.graph_module.graph.nodes),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        torch.export.save(exported, args.output)
        result["output"] = str(args.output)
        result["output_bytes"] = args.output.stat().st_size
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
