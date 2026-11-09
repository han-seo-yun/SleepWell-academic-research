from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import onnxruntime as ort

from mobile_retrieval import mobile_l2_indices


CONTEXT_MAGIC = b"SWCTX01\0"


def read_context(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with path.open("rb") as stream:
        if stream.read(8) != CONTEXT_MAGIC:
            raise ValueError("invalid context magic")
        rows, features, targets = struct.unpack("<iii", stream.read(12))
        x_count = rows * features
        x_train = np.frombuffer(stream.read(x_count * 4), dtype="<f4").reshape(rows, features).copy()
        y_train = np.frombuffer(stream.read(rows * targets * 4), dtype="<f4").reshape(rows, targets).copy()
        if stream.read(1):
            raise ValueError("context file has trailing bytes")
    return x_train, y_train


def distribution_median(logits: np.ndarray, borders: np.ndarray) -> float:
    shifted = np.asarray(logits, dtype=np.float32) - np.max(logits)
    probabilities = np.exp(shifted, dtype=np.float32)
    probabilities /= np.sum(probabilities, dtype=np.float32)
    cumulative = np.cumsum(probabilities, dtype=np.float32)
    index = int(np.searchsorted(cumulative, np.float32(0.5), side="left"))
    index = min(index, len(probabilities) - 1)
    previous = np.float32(0.0) if index == 0 else cumulative[index - 1]
    rest = (np.float32(0.5) - previous) / probabilities[index]
    return float(borders[index] + (borders[index + 1] - borders[index]) * rest)


def normalize_context_y(values: np.ndarray) -> tuple[np.ndarray, np.float32, np.float32]:
    values = np.asarray(values, dtype=np.float32)
    mean = np.mean(values, dtype=np.float32)
    centered = values - mean
    variance = np.sum(centered * centered, dtype=np.float32) / np.float32(len(values) - 1)
    std = np.sqrt(variance, dtype=np.float32) + np.float32(1e-6)
    return (values - mean) / std, mean, std


def _mean_std64(values: np.ndarray) -> tuple[float, float]:
    count = len(values)
    if count == 0:
        return 0.0, 0.0
    total = 0.0
    for value in values:
        total += float(value)
    mean = total / count
    if count <= 1:
        return mean, 0.0
    squared = 0.0
    for value in values:
        delta = float(value) - mean
        squared += delta * delta
    return mean, (squared / (count - 1)) ** 0.5


def deterministic_clip(values: np.ndarray, context_size: int, sigma: float = 8.0) -> np.ndarray:
    output = np.asarray(values, dtype=np.float64).copy()
    for feature in range(output.shape[1]):
        context = output[:context_size, feature]
        mean, std = _mean_std64(context)
        first_cutoff = sigma * std
        inliers = context[np.abs(context - mean) <= first_cutoff]
        _, inlier_std = _mean_std64(inliers)
        cutoff = sigma * inlier_std
        output[:, feature] = np.clip(output[:, feature], mean - cutoff, mean + cutoff)
    return output


def deterministic_model_preprocess(x_src: np.ndarray, context_size: int) -> np.ndarray:
    values = deterministic_clip(np.asarray(x_src, dtype=np.float64), context_size)
    for feature in range(values.shape[1]):
        mean, std = _mean_std64(values[:context_size, feature])
        values[:, feature] = (values[:, feature] - mean) / (std + 1e-6)
    values = deterministic_clip(values, context_size)
    return values.astype(np.float32)


class PortableTabDptRuntime:
    """Reference implementation of the Android tensor and postprocessing contract."""

    def __init__(self, bundle_dir: Path, threads: int = 4) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.manifest = json.loads((self.bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        self.session = ort.InferenceSession(
            str(self.bundle_dir / self.manifest["model_file"]),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.contexts: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.preprocessors: dict[str, dict[str, object]] = {}
        for cohort, metadata in self.manifest["cohorts"].items():
            self.contexts[cohort] = read_context(self.bundle_dir / metadata["context_file"])
            self.preprocessors[cohort] = json.loads(
                (self.bundle_dir / metadata["preprocessor_file"]).read_text(encoding="utf-8")
            )

    def transform_raw(self, cohort: str, raw_features: np.ndarray) -> np.ndarray:
        pipeline = self.preprocessors[cohort]
        values = np.asarray(raw_features, dtype=np.float64)
        if values.shape != (len(pipeline["feature_order"]),):
            raise ValueError("raw feature count does not match cohort feature order")
        preprocessing = pipeline["preprocessing"]
        missing = np.isnan(values)
        if np.isinf(values).any():
            raise ValueError("raw features contain infinity")
        imputed = np.where(missing, np.asarray(preprocessing["imputer_median"]), values)
        indicators = preprocessing["missing_indicator_features"]
        if indicators:
            imputed = np.concatenate((imputed, missing[indicators].astype(np.float64)))
        clipped = np.clip(
            imputed,
            np.asarray(preprocessing["clip_low"]),
            np.asarray(preprocessing["clip_high"]),
        )
        kept = clipped[np.asarray(preprocessing["variance_keep"], dtype=np.int64)]
        transformed = (
            kept - np.asarray(preprocessing["scale_mean"])
        ) / np.asarray(preprocessing["scale"])
        if not np.isfinite(transformed).all():
            raise ValueError("preprocessing produced non-finite values")
        return transformed.astype(np.float32)

    def predict_all(self, cmi_raw39: np.ndarray, mesa_raw39: np.ndarray) -> np.ndarray:
        return np.concatenate(
            [
                self.predict_transformed("cmi", self.transform_raw("cmi", cmi_raw39)),
                self.predict_transformed("mesa", self.transform_raw("mesa", mesa_raw39)),
            ]
        )

    def predict_transformed(self, cohort: str, query: np.ndarray) -> np.ndarray:
        query = np.asarray(query, dtype=np.float32)
        if query.ndim != 1:
            raise ValueError("query must be a 1D transformed feature vector")
        metadata = self.manifest["cohorts"][cohort]
        x_train, y_train = self.contexts[cohort]
        if len(query) != x_train.shape[1]:
            raise ValueError("query feature count does not match cohort")
        k = int(self.manifest["context_size"])
        indices = mobile_l2_indices(x_train, query[None, :], k)[0]
        permutations = metadata["feature_permutations"]
        outputs = np.empty(2, dtype=np.float32)
        for channel in range(2):
            normalized_y, mean_y, std_y = normalize_context_y(y_train[indices, channel])
            member_logits = []
            for permutation in permutations:
                perm = np.asarray(permutation, dtype=np.int64)
                x_src = np.zeros((1, k + 1, 128), dtype=np.float32)
                x_src[0, :k, : len(perm)] = x_train[indices][:, perm]
                x_src[0, k, : len(perm)] = query[perm]
                x_src[0] = deterministic_model_preprocess(x_src[0], k)
                logits = self.session.run(
                    ["logits"],
                    {"x_src": x_src, "y_src": normalized_y[None, :]},
                )[0][0, 0, 16:]
                member_logits.append(logits)
            averaged_logits = np.mean(np.stack(member_logits), axis=0, dtype=np.float32)
            borders = (
                np.linspace(-10.0, 10.0, 2049, dtype=np.float32) * std_y + mean_y
            )
            outputs[channel] = distribution_median(averaged_logits, borders)
        return outputs

