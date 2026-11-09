"""Explicit column mappings; creates derived inputs without editing source data."""
from __future__ import annotations
import copy
import pandas as pd
import numpy as np

SLEEP_ROLES = ('participant_id', 'cutoff', 'sleep_start', 'sleep_end', 'observed_at')

def default_mapping(frame):
    return {str(c): {'role': c if c in SLEEP_ROLES else 'feature',
        'type': 'original', 'unit': '', 'timezone': 'Asia/Seoul', 'allowed': '',
        'missing_rule': 'retain'} for c in frame.columns}

def validate_mapping(frame, mapping, *, sleepwell=False):
    output = frame.copy(deep=True)
    rules = copy.deepcopy(mapping)
    renamed = {}
    assigned = []
    for col, rule in rules.items():
        if col not in frame: raise ValueError(f'unknown_column: {col}')
        series = frame[col]
        role = rule.get('role', 'feature')
        if role in SLEEP_ROLES:
            if role in assigned: raise ValueError(f'duplicate_role: {role}')
            assigned.append(role); renamed[col] = role
        allowed = [v.strip() for v in rule.get('allowed', '').split(',') if v.strip()]
        if allowed and not series.dropna().astype(str).isin(allowed).all():
            raise ValueError(f'value_outside_allowed: {col}')
        if rule.get('missing_rule') == 'reject' and series.isna().any():
            raise ValueError(f'missing_value: {col}')
        kind = rule.get('type', 'original')
        if kind == 'numeric':
            converted = pd.to_numeric(series, errors='coerce')
            if (series.notna() & converted.isna()).any(): raise ValueError(f'numeric_conversion_failed: {col}')
            if not np.isfinite(converted.dropna()).all(): raise ValueError(f'nonfinite_value: {col}')
            output[col] = converted
        if kind == 'datetime' or sleepwell and role in SLEEP_ROLES[1:]:
            def convert(value):
                if pd.isna(value): return value
                try:
                    parsed = pd.Timestamp(value)
                    if pd.isna(parsed): raise ValueError('invalid_timestamp')
                    if parsed.tzinfo is None:
                        if not rule.get('timezone'): raise ValueError('timezone_required')
                        parsed = parsed.tz_localize(rule['timezone'], ambiguous='raise', nonexistent='raise')
                    return parsed.isoformat()
                except Exception as error: raise ValueError(f'datetime_conversion_failed: {col}: {error}') from error
            output[col] = series.map(convert)
        if role == 'exclude': output = output.drop(columns=col)
    if sleepwell and not set(SLEEP_ROLES).issubset(assigned):
        raise ValueError('required_roles: ' + ', '.join(sorted(set(SLEEP_ROLES)-set(assigned))))
    output = output.rename(columns=renamed)
    if output.columns.duplicated().any(): raise ValueError('duplicate_output_columns')
    return output, rules
