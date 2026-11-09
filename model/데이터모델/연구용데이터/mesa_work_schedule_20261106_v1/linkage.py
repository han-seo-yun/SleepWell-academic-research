"""Privacy-preserving linkage of MESA work-schedule metadata to sleep episodes.

The public functions never retain the raw ``mesaid``.  They reproduce the
participant token used by the existing next-night dataset builder and expose
only aggregate linkage diagnostics.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd


WORK_SCHEDULE_LABELS = {
    1: "day_shift",
    2: "afternoon_shift",
    3: "night_shift",
    4: "split_shift",
    5: "irregular_or_on_call",
    6: "rotating_shifts",
    7: "does_not_work",
}

LINKED_COLUMNS = (
    "work_schedule_code",
    "work_schedule_category",
    "work_schedule_status",
    "extra_hours_days_per_month",
    "extra_hours_status",
)


def mesa_subject_token(mesaid: object) -> str:
    """Return the canonical subject token used by ``next_night_dataset.py``."""

    normalized = _normalize_mesaid(mesaid)
    digest = hashlib.sha256(f"mesa_subject_v1|{normalized}".encode("utf-8")).hexdigest()
    return f"mesa_{digest[:20]}"


def _normalize_mesaid(value: object) -> int:
    if pd.isna(value):
        raise ValueError("mesaid must not be missing")
    text = str(value).strip()
    if not text:
        raise ValueError("mesaid must not be blank")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError("mesaid must be an integer-like value") from exc
    if not number.is_integer() or number < 0:
        raise ValueError("mesaid must be a non-negative integer-like value")
    return int(number)


def _classified_integer(
    values: pd.Series,
    *,
    minimum: int,
    maximum: int,
) -> tuple[pd.Series, pd.Series]:
    stripped = values.astype("string").str.strip()
    missing = values.isna() | stripped.eq("")
    numeric = pd.to_numeric(stripped.mask(missing), errors="coerce")
    integer = numeric.notna() & numeric.mod(1).eq(0)
    valid = integer & numeric.between(minimum, maximum, inclusive="both")
    status = pd.Series("invalid", index=values.index, dtype="string")
    status.loc[missing] = "missing"
    status.loc[valid] = "observed"
    cleaned = numeric.where(valid).astype("Int64")
    return cleaned, status


def load_schedule(path: str | Path) -> pd.DataFrame:
    """Load and validate MESA schedule metadata without retaining raw IDs.

    Invalid schedule and extra-hours values remain distinguishable from true
    missing values.  They are never imputed or converted to zero.
    """

    source = Path(path)
    required = ["mesaid", "wrksched5", "extrahrs5"]
    frame = pd.read_csv(source, usecols=required, dtype="string", encoding="utf-8-sig")
    missing_columns = set(required).difference(frame.columns)
    if missing_columns:
        raise ValueError(f"schedule source is missing columns: {sorted(missing_columns)}")

    try:
        subject_ids = frame["mesaid"].map(mesa_subject_token)
    except ValueError as exc:
        raise ValueError(f"invalid mesaid in schedule source: {exc}") from exc
    if subject_ids.duplicated().any():
        raise ValueError("schedule source contains duplicate participants after ID normalization")

    schedule_code, schedule_status = _classified_integer(
        frame["wrksched5"], minimum=1, maximum=7
    )
    schedule_category = schedule_code.map(WORK_SCHEDULE_LABELS).astype("string")
    schedule_category.loc[schedule_status.eq("missing")] = "missing"
    schedule_category.loc[schedule_status.eq("invalid")] = "unknown_invalid"

    extra_hours, extra_status = _classified_integer(
        frame["extrahrs5"], minimum=0, maximum=31
    )

    return pd.DataFrame(
        {
            "subject_id": subject_ids.astype("string"),
            "work_schedule_code": schedule_code,
            "work_schedule_category": schedule_category,
            "work_schedule_status": schedule_status,
            "extra_hours_days_per_month": extra_hours,
            "extra_hours_status": extra_status,
        }
    )


def _age_band(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    bands = pd.cut(
        numeric,
        bins=[float("-inf"), 44, 54, 64, 74, float("inf")],
        labels=["under_45", "45_54", "55_64", "65_74", "75_plus"],
    ).astype("string")
    return bands.fillna("missing")


def _nested_counts(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    grouped = frame.groupby(columns, dropna=False, observed=True).size().reset_index(name="rows")
    return grouped.to_dict(orient="records")


def merge_mesa_schedule(
    episodes: pd.DataFrame,
    metadata: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Many-to-one merge into MESA episodes and return aggregate diagnostics.

    Original row order, index, columns, values, and split assignments are
    preserved.  A cohort key prevents an identically named CMI participant from
    receiving MESA metadata.
    """

    episode_required = {"cohort", "subject_id", "split"}
    metadata_required = {"subject_id", *LINKED_COLUMNS}
    absent_episode = episode_required.difference(episodes.columns)
    absent_metadata = metadata_required.difference(metadata.columns)
    if absent_episode:
        raise ValueError(f"episodes are missing columns: {sorted(absent_episode)}")
    if absent_metadata:
        raise ValueError(f"metadata are missing columns: {sorted(absent_metadata)}")
    collisions = set(LINKED_COLUMNS).intersection(episodes.columns)
    if collisions:
        raise ValueError(f"episodes already contain linked columns: {sorted(collisions)}")
    if metadata["subject_id"].duplicated().any():
        raise ValueError("metadata subject_id must be unique for a many-to-one merge")

    original_index = episodes.index.copy()
    original_columns = list(episodes.columns)
    left = episodes.copy()
    left["__linkage_row_order"] = range(len(left))
    right = metadata[["subject_id", *LINKED_COLUMNS]].copy()
    right.insert(0, "cohort", "MESA")

    merged = left.merge(
        right,
        how="left",
        on=["cohort", "subject_id"],
        sort=False,
        validate="many_to_one",
    ).sort_values("__linkage_row_order", kind="stable")
    linked_values = merged.loc[:, list(LINKED_COLUMNS)].reset_index(drop=True)
    joined = pd.concat(
        [episodes.reset_index(drop=True), linked_values],
        axis=1,
    )
    joined.index = original_index

    if list(joined.columns[: len(original_columns)]) != original_columns:
        raise AssertionError("merge changed original column order")
    if not joined[original_columns].equals(episodes[original_columns]):
        raise AssertionError("merge changed original episode values or split assignments")

    mesa = joined.loc[joined["cohort"].eq("MESA")].copy()
    non_mesa = joined.loc[~joined["cohort"].eq("MESA")]
    if non_mesa[list(LINKED_COLUMNS)].notna().any(axis=None):
        raise AssertionError("non-MESA rows received MESA schedule metadata")

    mesa["age_band"] = _age_band(mesa["age"]) if "age" in mesa else "unavailable"
    matched = mesa["work_schedule_status"].notna()
    participants = mesa.drop_duplicates("subject_id", keep="first")
    participant_matched = participants["work_schedule_status"].notna()
    audit: dict[str, Any] = {
        "episode_rows": int(len(joined)),
        "mesa_episode_rows": int(len(mesa)),
        "non_mesa_episode_rows": int(len(non_mesa)),
        "mesa_matched_rows": int(matched.sum()),
        "mesa_unmatched_rows": int((~matched).sum()),
        "mesa_unique_participants": int(mesa["subject_id"].nunique()),
        "mesa_matched_participants": int(participant_matched.sum()),
        "mesa_unmatched_participants": int((~participant_matched).sum()),
        "schedule_status_rows": _nested_counts(mesa, ["split", "work_schedule_status"]),
        "schedule_category_rows": _nested_counts(mesa, ["split", "work_schedule_category"]),
        "extra_hours_status_rows": _nested_counts(mesa, ["split", "extra_hours_status"]),
        "age_band_rows": _nested_counts(mesa, ["split", "age_band"]),
        "schedule_status_participants": _nested_counts(
            participants, ["split", "work_schedule_status"]
        ),
        "schedule_category_participants": _nested_counts(
            participants, ["split", "work_schedule_category"]
        ),
        "extra_hours_status_participants": _nested_counts(
            participants, ["split", "extra_hours_status"]
        ),
        "age_band_participants": _nested_counts(participants, ["split", "age_band"]),
        "row_order_preserved": True,
        "original_values_preserved": True,
        "many_to_one_verified": True,
    }
    return joined, audit

