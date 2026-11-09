"""Data loading, reproducible fingerprints, and bounded quality profiling."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}


def load_data(path: str | Path, sheet_name: str | int = 0) -> pd.DataFrame:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported_data_format:{suffix}")
    if suffix == ".csv":
        frame = pd.read_csv(source, low_memory=False)
    else:
        frame = pd.read_excel(source, sheet_name=sheet_name)
        if isinstance(frame, dict):
            raise ValueError("sheet_name_must_select_one_sheet")
    if frame.columns.duplicated().any():
        duplicates = frame.columns[frame.columns.duplicated()].astype(str).tolist()
        raise ValueError(f"duplicate_column_names:{duplicates}")
    frame.columns = [str(column) for column in frame.columns]
    return frame.reset_index(drop=True)


def fingerprint(df: pd.DataFrame) -> str:
    """Return a stable content hash including column order, names and dtypes."""
    frame = df.reset_index(drop=True)
    digest = hashlib.sha256()
    digest.update(str(frame.shape).encode("ascii"))
    # Normalize storage-level dtype differences (for example datetime us vs ns and
    # pandas string vs object) while keeping semantic type families in the hash.
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_datetime64_any_dtype(series):
            family, normalized = "datetime", pd.to_datetime(series).astype("datetime64[ns]")
        elif pd.api.types.is_bool_dtype(series):
            family, normalized = "boolean", series.astype("boolean")
        elif pd.api.types.is_integer_dtype(series):
            family, normalized = "integer", series.astype("Int64")
        elif pd.api.types.is_float_dtype(series):
            family, normalized = "float", series.astype("Float64")
        elif pd.api.types.is_numeric_dtype(series):
            family, normalized = "numeric", series
        elif isinstance(series.dtype, pd.CategoricalDtype):
            family, normalized = "category", series.astype("string")
        else:
            family = "string_or_object"
            normalized = series.map(lambda value: repr(value) if isinstance(value, (list, dict, set)) else value).astype("string")
        digest.update(f"{name}\x1e{family}".encode("utf-8"))
        hashes = pd.util.hash_pandas_object(normalized, index=False, categorize=True)
        digest.update(hashes.to_numpy(dtype="uint64", copy=False).tobytes())
    return digest.hexdigest()


def _native(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def data_profile(df: pd.DataFrame) -> dict[str, Any]:
    frame = df.reset_index(drop=True)
    rows = len(frame)
    features: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for column in frame.columns:
        series = frame[column]
        missing = int(series.isna().sum())
        unique = int(series.nunique(dropna=True))
        constant = unique <= 1
        features.append(
            {
                "name": str(column),
                "dtype": str(series.dtype),
                "missing": missing,
                "missing_pct": float(missing / rows * 100.0) if rows else 0.0,
                "unique": unique,
                "constant": constant,
            }
        )
        if constant:
            warnings.append({"code": "constant_feature", "column": str(column)})
        if pd.api.types.is_numeric_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
            nonfinite = int(np.isinf(numeric).sum())
            if nonfinite:
                warnings.append({"code": "nonfinite_values", "column": str(column), "count": nonfinite})
        if rows and missing / rows >= 0.4:
            warnings.append(
                {"code": "high_missingness", "column": str(column), "missing_pct": float(missing / rows * 100.0)}
            )
        non_missing = series.dropna()
        if not non_missing.empty and (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            converted = pd.to_numeric(non_missing, errors="coerce")
            numeric_count = int(converted.notna().sum())
            if 0 < numeric_count < len(non_missing):
                warnings.append(
                    {
                        "code": "mixed_numeric_text",
                        "column": str(column),
                        "conversion_failures": int(len(non_missing) - numeric_count),
                    }
                )
        normalized_name = str(column).lower().replace(" ", "_")
        if unique == rows and rows > 1 and (normalized_name == "id" or normalized_name.endswith("_id")):
            warnings.append({"code": "possible_identifier", "column": str(column)})
        if rows and unique / rows > 0.95 and not pd.api.types.is_numeric_dtype(series):
            warnings.append({"code": "high_cardinality_categorical", "column": str(column), "unique": unique})

    duplicates = int(frame.duplicated().sum())
    if duplicates:
        warnings.append({"code": "duplicate_rows", "count": duplicates})
    return {
        "rows": int(rows),
        "columns": int(frame.shape[1]),
        "features": features,
        "duplicates": duplicates,
        "warnings": warnings,
    }

