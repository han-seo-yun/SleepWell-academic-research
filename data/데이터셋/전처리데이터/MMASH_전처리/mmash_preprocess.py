from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

# -----------------------------
# Configuration
# -----------------------------
BASE = Path('/mnt/data/mmash_raw/DataPaper')
OUT = Path('/mnt/data/mmash_preprocessed')
OUT.mkdir(parents=True, exist_ok=True)

PRE_WINDOW_MIN = 240   # causal training: 4 h before Out Bed
POST_WINDOW_MIN = 30   # diagnostic transition window only
RR_IBI_MIN_S = 0.30    # 30-200 bpm physiologic plausibility guard
RR_IBI_MAX_S = 2.00
ACT_HR_MIN_BPM = 30
ACT_HR_MAX_BPM = 220
MAX_RR_INTERP_GAP_S = 2
EVENT_NEARBY_MAX_GAP_S = 300  # need a sensor sample within +/- 5 min for event QC
MERGE_SLEEP_GAP_MIN = 60       # merge split sleep segments within 60 min (user_1)

POSTURE_COLS = [
    'Inclinometer Off',
    'Inclinometer Standing',
    'Inclinometer Sitting',
    'Inclinometer Lying',
]


def hms_to_sec(value: str) -> int:
    parts = str(value).split(':')
    h = int(parts[0]); m = int(parts[1]); s = int(parts[2]) if len(parts) > 2 else 0
    return h * 3600 + m * 60 + s


def format_day_time(abs_sec: int) -> str:
    day = abs_sec // 86400 + 1
    sod = abs_sec % 86400
    h = sod // 3600; m = (sod % 3600) // 60; s = sod % 60
    return f'day{day} {h:02d}:{m:02d}:{s:02d}'


def correct_day_column(df: pd.DataFrame, uid: str, source: str, qc: dict) -> pd.DataFrame:
    df = df.copy()
    bad_mask = ~df['day'].isin([1, 2])
    bad_vals = sorted(df.loc[bad_mask, 'day'].dropna().unique().tolist())
    if bad_mask.any():
        # In this MMASH copy, only user_8/user_9 have -29 after midnight.
        # Sequence continuity + source specification (days should be 1 or 2)
        # support remapping -29 -> 2 rather than dropping ~47k RR rows / ~47k Actigraph rows.
        if set(bad_vals) != {-29}:
            raise ValueError(f'{uid} {source}: unexpected day values {bad_vals}')
        df.loc[bad_mask, 'day'] = 2
        qc[f'{source}_day_fixed_n'] = int(bad_mask.sum())
        qc[f'{source}_day_fixed_values'] = '-29->2'
    else:
        qc[f'{source}_day_fixed_n'] = 0
        qc[f'{source}_day_fixed_values'] = ''
    if not df['day'].isin([1, 2]).all():
        raise AssertionError(f'{uid} {source}: day correction failed')
    df['day'] = df['day'].astype(int)
    df['t_sec'] = (df['day'] - 1) * 86400 + df['time'].map(hms_to_sec)
    if (np.diff(df['t_sec'].to_numpy()) < 0).any():
        raise AssertionError(f'{uid} {source}: time not monotonic after correction')
    return df


def parse_sleep_sessions(uid: str, sleep: pd.DataFrame, qc: dict) -> list[dict]:
    sessions = []
    for idx, r in sleep.iterrows():
        in_sec = (int(r['In Bed Date']) - 1) * 86400 + hms_to_sec(r['In Bed Time'])
        out_raw = (int(r['Out Bed Date']) - 1) * 86400 + hms_to_sec(r['Out Bed Time'])
        onset_raw = (int(r['Onset Date']) - 1) * 86400 + hms_to_sec(r['Onset Time'])

        out_sec = out_raw
        out_date_corrected = False
        if out_sec < in_sec:
            out_sec += 86400
            out_date_corrected = True

        onset_sec = onset_raw
        onset_date_corrected = False
        if onset_sec < in_sec:
            onset_sec += 86400
            onset_date_corrected = True

        tib_min = int(r['Total Minutes in Bed'])
        latency_min = int(r['Latency'])
        duration_err_s = (out_sec - in_sec) - tib_min * 60
        onset_err_s = (onset_sec - in_sec) - latency_min * 60
        if abs(duration_err_s) > 60:
            raise AssertionError(f'{uid} sleep session {idx}: TIB mismatch {duration_err_s}s')
        if abs(onset_err_s) > 60:
            raise AssertionError(f'{uid} sleep session {idx}: latency mismatch {onset_err_s}s')

        sessions.append({
            'session_idx': int(idx),
            'in_sec': int(in_sec),
            'onset_sec': int(onset_sec),
            'out_sec': int(out_sec),
            'in_bed_date_raw': int(r['In Bed Date']),
            'out_bed_date_raw': int(r['Out Bed Date']),
            'onset_date_raw': int(r['Onset Date']),
            'out_date_corrected': out_date_corrected,
            'onset_date_corrected': onset_date_corrected,
            'tib_min': tib_min,
            'tst_min': float(r['Total Sleep Time (TST)']),
            'waso_min': float(r['Wake After Sleep Onset (WASO)']),
            'latency_min': latency_min,
            'efficiency': float(r['Efficiency']),
        })

    qc['sleep_date_corrected_n'] = int(sum(s['out_date_corrected'] or s['onset_date_corrected'] for s in sessions))
    return sessions


def merge_sleep_sessions(sessions: list[dict]) -> dict | None:
    if not sessions:
        return None
    sessions = sorted(sessions, key=lambda x: x['in_sec'])
    groups: list[list[dict]] = []
    current = [sessions[0]]
    for s in sessions[1:]:
        gap = s['in_sec'] - current[-1]['out_sec']
        if 0 <= gap <= MERGE_SLEEP_GAP_MIN * 60:
            current.append(s)
        else:
            groups.append(current)
            current = [s]
    groups.append(current)

    # Project target is final wake time. Pick the overnight group ending latest.
    g = max(groups, key=lambda x: x[-1]['out_sec'])
    gap_awake_min = 0.0
    if len(g) > 1:
        gap_awake_min = sum(max(0, g[i]['in_sec'] - g[i-1]['out_sec']) for i in range(1, len(g))) / 60

    return {
        'in_sec': g[0]['in_sec'],
        'onset_sec': g[0]['onset_sec'],
        'out_sec': g[-1]['out_sec'],
        'n_segments_merged': len(g),
        'span_in_bed_min': (g[-1]['out_sec'] - g[0]['in_sec']) / 60,
        'tst_sum_min': sum(s['tst_min'] for s in g),
        'waso_sum_min': sum(s['waso_min'] for s in g),
        'latency_first_min': g[0]['latency_min'],
        'gap_awake_between_segments_min': gap_awake_min,
        'efficiency_weighted': np.average([s['efficiency'] for s in g], weights=[max(s['tib_min'], 1) for s in g]),
        'source_session_indices': ','.join(str(s['session_idx']) for s in g),
    }


def interpolate_short_gaps(series: pd.Series, max_gap: int = 2) -> tuple[pd.Series, pd.Series]:
    s = series.copy().astype(float)
    orig_na = s.isna()
    if not orig_na.any():
        return s, pd.Series(False, index=s.index)
    groups = (orig_na != orig_na.shift(fill_value=False)).cumsum()
    out = s.copy()
    imputed = pd.Series(False, index=s.index)
    linear = s.interpolate(method='linear', limit_area='inside')
    for _, idx in orig_na.groupby(groups).groups.items():
        idx = pd.Index(idx)
        if bool(orig_na.loc[idx[0]]) and len(idx) <= max_gap:
            vals = linear.loc[idx]
            if vals.notna().all():
                out.loc[idx] = vals
                imputed.loc[idx] = True
    return out, imputed


def hrv_metrics(ibi_seconds: np.ndarray) -> dict:
    x = np.asarray(ibi_seconds, dtype=float)
    x = x[np.isfinite(x)]
    out = {
        'n_beats': int(len(x)),
        'mean_nn_ms': np.nan,
        'sdnn_ms': np.nan,
        'rmssd_ms': np.nan,
        'pnn50_pct': np.nan,
    }
    if len(x) >= 2:
        out['mean_nn_ms'] = float(np.mean(x) * 1000)
        if len(x) >= 3:
            out['sdnn_ms'] = float(np.std(x, ddof=1) * 1000)
            diffs = np.diff(x)
            out['rmssd_ms'] = float(np.sqrt(np.mean(diffs ** 2)) * 1000)
            out['pnn50_pct'] = float(np.mean(np.abs(diffs) > 0.05) * 100)
    return out


def build_rr_1s(rr: pd.DataFrame, grid: pd.Index) -> pd.DataFrame:
    rr = rr.copy()
    rr['ibi_phys_valid'] = rr['ibi_s'].between(RR_IBI_MIN_S, RR_IBI_MAX_S)
    total_per_s = rr.groupby('t_sec').size().rename('rr_beats_total_s')
    valid = rr[rr['ibi_phys_valid']]
    agg = valid.groupby('t_sec')['ibi_s'].agg(['median', 'mean', 'std', 'count'])
    agg = agg.rename(columns={
        'median': 'rr_ibi_median_raw_s',
        'mean': 'rr_ibi_mean_raw_s',
        'std': 'rr_ibi_std_raw_s',
        'count': 'rr_beats_valid_s',
    })
    agg['rr_hr_bpm_raw_s'] = 60.0 / agg['rr_ibi_median_raw_s']
    out = pd.DataFrame(index=grid)
    out = out.join(total_per_s, how='left').join(agg, how='left')
    out['rr_beats_total_s'] = out['rr_beats_total_s'].fillna(0).astype(int)
    out['rr_beats_valid_s'] = out['rr_beats_valid_s'].fillna(0).astype(int)
    out['rr_event_observed'] = out['rr_beats_total_s'] > 0
    out['rr_valid_event_observed'] = out['rr_beats_valid_s'] > 0
    out['rr_valid_fraction_s'] = np.where(
        out['rr_beats_total_s'] > 0,
        out['rr_beats_valid_s'] / out['rr_beats_total_s'],
        np.nan,
    )
    ibi_interp, imp = interpolate_short_gaps(out['rr_ibi_median_raw_s'], MAX_RR_INTERP_GAP_S)
    out['rr_ibi_s_1hz'] = ibi_interp
    out['rr_hr_bpm_1hz'] = 60.0 / out['rr_ibi_s_1hz']
    out['rr_1hz_imputed'] = imp
    out['rr_1hz_available'] = out['rr_ibi_s_1hz'].notna()
    return out


def build_features_30s(joined: pd.DataFrame, rr_clean: pd.DataFrame, out_sec: int, uid: str) -> pd.DataFrame:
    df = joined.copy()
    df['epoch_start_sec'] = (df['t_sec'] // 30) * 30

    def slope30(x: pd.Series) -> float:
        y = x.dropna().to_numpy(dtype=float)
        if len(y) < 3:
            return np.nan
        xx = np.arange(len(y), dtype=float)
        return float(np.polyfit(xx, y, 1)[0])

    # Basic causal epoch features. No sleep.csv summary fields enter as predictors.
    rows = []
    rr_t = rr_clean['t_sec'].to_numpy(dtype=int)
    rr_ibi = rr_clean['ibi_s'].to_numpy(dtype=float)

    for ep, g in df.groupby('epoch_start_sec', sort=True):
        ep_end = int(ep + 30)
        row = {
            'subject_id': uid,
            'epoch_start_sec': int(ep),
            'epoch_end_sec': ep_end,
            'clock_sec': int(ep % 86400),
            'clock_sin': math.sin(2 * math.pi * ((ep % 86400) / 86400.0)),
            'clock_cos': math.cos(2 * math.pi * ((ep % 86400) / 86400.0)),
            'act_coverage_30s': float(g['act_observed'].mean()),
            'rr_coverage_30s': float(g['rr_1hz_available'].mean()),
            'hr_act_valid_coverage_30s': float(g['hr_act_clean'].notna().mean()),
            'hr_act_mean_30s': float(g['hr_act_clean'].mean()),
            'hr_act_std_30s': float(g['hr_act_clean'].std()),
            'hr_act_median_30s': float(g['hr_act_clean'].median()),
            'hr_act_slope_30s': slope30(g['hr_act_clean']),
            'rr_hr_mean_30s': float(g['rr_hr_bpm_1hz'].mean()),
            'rr_hr_std_30s': float(g['rr_hr_bpm_1hz'].std()),
            'steps_sum_30s': float(g['Steps'].sum(min_count=1)),
            'vm_mean_30s': float(g['Vector Magnitude'].mean()),
            'vm_std_30s': float(g['Vector Magnitude'].std()),
            'vm_max_30s': float(g['Vector Magnitude'].max()),
            'vm_active_frac_30s': float((g['Vector Magnitude'].fillna(0) > 0).mean()) if g['Vector Magnitude'].notna().any() else np.nan,
            'axis1_std_30s': float(g['Axis1'].std()),
            'axis2_std_30s': float(g['Axis2'].std()),
            'axis3_std_30s': float(g['Axis3'].std()),
            'posture_off_frac_30s': float(g['Inclinometer Off'].mean()),
            'posture_standing_frac_30s': float(g['Inclinometer Standing'].mean()),
            'posture_sitting_frac_30s': float(g['Inclinometer Sitting'].mean()),
            'posture_lying_frac_30s': float(g['Inclinometer Lying'].mean()),
            'rr_imputed_frac_30s': float(g['rr_1hz_imputed'].mean()),
        }

        # Beat-level HRV: 30 s and trailing 5 min, using cleaned raw beat intervals.
        for label, start in [('30s', ep_end - 30), ('5m', ep_end - 300)]:
            left = np.searchsorted(rr_t, start, side='left')
            right = np.searchsorted(rr_t, ep_end, side='left')
            m = hrv_metrics(rr_ibi[left:right])
            row[f'rr_n_beats_{label}'] = m['n_beats']
            row[f'mean_nn_ms_{label}'] = m['mean_nn_ms']
            row[f'sdnn_ms_{label}'] = m['sdnn_ms']
            row[f'rmssd_ms_{label}'] = m['rmssd_ms']
            row[f'pnn50_pct_{label}'] = m['pnn50_pct']

        seconds_to_out = out_sec - ep_end
        row['seconds_to_out_bed'] = int(seconds_to_out)
        row['minutes_to_out_bed'] = float(seconds_to_out / 60.0)
        row['post_out_bed'] = int(seconds_to_out < 0)
        row['wake_within_5m'] = int(0 <= seconds_to_out <= 300)
        row['wake_within_15m'] = int(0 <= seconds_to_out <= 900)
        row['wake_within_30m'] = int(0 <= seconds_to_out <= 1800)
        rows.append(row)

    feat = pd.DataFrame(rows).sort_values('epoch_start_sec').reset_index(drop=True)

    # Trailing 5-min summaries from prior/current epochs only (causal).
    for src in [
        'hr_act_mean_30s', 'rr_hr_mean_30s', 'steps_sum_30s', 'vm_mean_30s',
        'vm_active_frac_30s', 'posture_off_frac_30s', 'posture_lying_frac_30s',
        'act_coverage_30s', 'rr_coverage_30s'
    ]:
        feat[src.replace('_30s', '_5m')] = feat[src].rolling(window=10, min_periods=5).mean()

    feat['hr_act_delta_vs_5m'] = feat['hr_act_mean_30s'] - feat['hr_act_mean_5m']
    feat['rr_hr_delta_vs_5m'] = feat['rr_hr_mean_30s'] - feat['rr_hr_mean_5m']
    feat['vm_delta_vs_5m'] = feat['vm_mean_30s'] - feat['vm_mean_5m']
    feat['off_delta_vs_5m'] = feat['posture_off_frac_30s'] - feat['posture_off_frac_5m']
    feat['lying_delta_vs_5m'] = feat['posture_lying_frac_30s'] - feat['posture_lying_frac_5m']
    return feat


def main():
    user_dirs = sorted(BASE.glob('user_*'), key=lambda p: int(p.name.split('_')[-1]))
    qc_rows = []
    labels = []
    all_joined = []
    all_features = []

    for udir in user_dirs:
        uid = udir.name
        qc = {'subject_id': uid}

        act = pd.read_csv(udir / 'Actigraph.csv').drop(columns=['Unnamed: 0'], errors='ignore')
        rr = pd.read_csv(udir / 'RR.csv').drop(columns=['Unnamed: 0'], errors='ignore')
        sleep = pd.read_csv(udir / 'sleep.csv').drop(columns=['Unnamed: 0'], errors='ignore')

        # Raw schema/quality checks
        qc['act_rows_raw'] = len(act)
        qc['rr_rows_raw'] = len(rr)
        qc['sleep_rows_raw'] = len(sleep)
        qc['act_missing_values_raw'] = int(act.isna().sum().sum())
        qc['rr_missing_values_raw'] = int(rr.isna().sum().sum())
        posture_sum = act[POSTURE_COLS].sum(axis=1)
        qc['posture_onehot_bad_n'] = int((posture_sum != 1).sum())
        vm_calc = np.sqrt(act['Axis1'] ** 2 + act['Axis2'] ** 2 + act['Axis3'] ** 2)
        qc['vector_magnitude_max_abs_error'] = float(np.max(np.abs(vm_calc - act['Vector Magnitude'])))
        qc['act_hr_out_of_range_n'] = int((~act['HR'].between(ACT_HR_MIN_BPM, ACT_HR_MAX_BPM)).sum())
        qc['rr_ibi_out_of_range_n'] = int((~rr['ibi_s'].between(RR_IBI_MIN_S, RR_IBI_MAX_S)).sum())
        qc['rr_ibi_out_of_range_frac'] = float(qc['rr_ibi_out_of_range_n'] / len(rr)) if len(rr) else np.nan

        # Repair known day-field corruption and build monotonic time.
        act = correct_day_column(act, uid, 'act', qc)
        rr = correct_day_column(rr, uid, 'rr', qc)
        if act['t_sec'].duplicated().any():
            raise AssertionError(f'{uid}: duplicate Actigraph seconds')
        qc['act_start_sec'] = int(act['t_sec'].iloc[0])
        qc['act_end_sec'] = int(act['t_sec'].iloc[-1])
        qc['rr_start_sec'] = int(rr['t_sec'].iloc[0])
        qc['rr_end_sec'] = int(rr['t_sec'].iloc[-1])

        sessions = parse_sleep_sessions(uid, sleep, qc)
        episode = merge_sleep_sessions(sessions)
        if episode is None:
            qc['include_model'] = 0
            qc['exclude_reason'] = 'no_sleep_label'
            qc_rows.append(qc)
            continue

        out_sec = int(episode['out_sec'])
        labels.append({
            'subject_id': uid,
            **episode,
            'in_time_corrected': format_day_time(int(episode['in_sec'])),
            'onset_time_corrected': format_day_time(int(episode['onset_sec'])),
            'out_time_corrected': format_day_time(out_sec),
            'label_source': 'ActiGraph sleep.csv (device-derived weak label)',
        })

        # Event coverage QC: target is unusable if transition is not observed nearby.
        act_before = act.loc[act['t_sec'] <= out_sec, 't_sec']
        act_after = act.loc[act['t_sec'] >= out_sec, 't_sec']
        rr_valid = rr[rr['ibi_s'].between(RR_IBI_MIN_S, RR_IBI_MAX_S)].copy()
        rr_before = rr_valid.loc[rr_valid['t_sec'] <= out_sec, 't_sec']
        rr_after = rr_valid.loc[rr_valid['t_sec'] >= out_sec, 't_sec']
        qc['act_gap_before_out_s'] = int(out_sec - act_before.max()) if len(act_before) else np.nan
        qc['act_gap_after_out_s'] = int(act_after.min() - out_sec) if len(act_after) else np.nan
        qc['rr_gap_before_out_s'] = int(out_sec - rr_before.max()) if len(rr_before) else np.nan
        qc['rr_gap_after_out_s'] = int(rr_after.min() - out_sec) if len(rr_after) else np.nan

        gaps = [qc['act_gap_before_out_s'], qc['act_gap_after_out_s'], qc['rr_gap_before_out_s'], qc['rr_gap_after_out_s']]
        if any(pd.isna(x) or x > EVENT_NEARBY_MAX_GAP_S for x in gaps):
            qc['include_model'] = 0
            qc['exclude_reason'] = 'wake_transition_not_observed_within_5min'
            qc_rows.append(qc)
            continue

        qc['include_model'] = 1
        qc['exclude_reason'] = ''

        # Full 1-second wake-centered grid.
        win_lo = out_sec - PRE_WINDOW_MIN * 60
        win_hi = out_sec + POST_WINDOW_MIN * 60
        grid = pd.Index(np.arange(win_lo, win_hi + 1, dtype=int), name='t_sec')

        act_w = act[act['t_sec'].between(win_lo, win_hi)].copy().set_index('t_sec')
        act_w['act_observed'] = True
        act_w['hr_act_clean'] = act_w['HR'].where(act_w['HR'].between(ACT_HR_MIN_BPM, ACT_HR_MAX_BPM))
        act_cols = [
            'day', 'time', 'Axis1', 'Axis2', 'Axis3', 'Steps', 'HR', 'hr_act_clean',
            *POSTURE_COLS, 'Vector Magnitude', 'act_observed'
        ]
        joined = pd.DataFrame(index=grid).join(act_w[act_cols], how='left')
        joined['act_observed'] = joined['act_observed'].fillna(False).astype(bool)

        rr_w = rr[rr['t_sec'].between(win_lo - 300, win_hi)].copy()  # 5-min lookback for first epoch HRV
        rr1 = build_rr_1s(rr_w, grid)
        joined = joined.join(rr1, how='left')
        joined['subject_id'] = uid
        joined['out_bed_sec'] = out_sec
        joined['relative_sec_to_out_bed'] = joined.index.to_numpy() - out_sec
        joined['relative_min_to_out_bed'] = joined['relative_sec_to_out_bed'] / 60.0
        joined['hr_pair_absdiff'] = np.abs(joined['hr_act_clean'] - joined['rr_hr_bpm_1hz'])
        joined['hr_disagreement_gt20'] = (joined['hr_pair_absdiff'] > 20).where(
            joined['hr_act_clean'].notna() & joined['rr_hr_bpm_1hz'].notna()
        )
        joined = joined.reset_index()

        # Feature table uses raw cleaned beat-to-beat intervals.
        rr_clean = rr_w[rr_w['ibi_s'].between(RR_IBI_MIN_S, RR_IBI_MAX_S)].sort_values('t_sec').copy()
        feat = build_features_30s(joined, rr_clean, out_sec, uid)

        # Subject-level feature QC before adding.
        qc['window_act_coverage'] = float(joined['act_observed'].mean())
        qc['window_rr_1hz_coverage'] = float(joined['rr_1hz_available'].mean())
        qc['window_hr_act_valid_coverage'] = float(joined['hr_act_clean'].notna().mean())
        qc['window_hr_pair_median_absdiff'] = float(joined['hr_pair_absdiff'].median())
        qc['window_hr_pair_corr'] = float(joined[['hr_act_clean', 'rr_hr_bpm_1hz']].corr().iloc[0, 1])

        all_joined.append(joined)
        all_features.append(feat)
        qc_rows.append(qc)

    qc_df = pd.DataFrame(qc_rows).sort_values('subject_id', key=lambda s: s.str.extract(r'(\d+)')[0].astype(int))
    labels_df = pd.DataFrame(labels).sort_values('subject_id', key=lambda s: s.str.extract(r'(\d+)')[0].astype(int))
    joined_df = pd.concat(all_joined, ignore_index=True) if all_joined else pd.DataFrame()
    feat_df = pd.concat(all_features, ignore_index=True) if all_features else pd.DataFrame()

    # Diagnostic transition dataset includes +30 min after wake; model dataset is strictly causal/pre-wake.
    transition_30s = feat_df.copy()
    model_30s = feat_df[(feat_df['seconds_to_out_bed'] >= 0) & (feat_df['seconds_to_out_bed'] <= PRE_WINDOW_MIN * 60)].copy()

    # Row quality flag: don't drop automatically; preserve reproducibility and allow sensitivity analysis.
    model_30s['row_quality_ok'] = (
        (model_30s['act_coverage_30s'] >= 0.50)
        & (model_30s['rr_coverage_30s'] >= 0.50)
        & (model_30s['hr_act_valid_coverage_30s'] >= 0.50)
        & (model_30s['rr_n_beats_5m'] >= 100)
        & model_30s['hr_act_mean_5m'].notna()
        & model_30s['rr_hr_mean_5m'].notna()
        & model_30s['vm_mean_5m'].notna()
        & model_30s['posture_off_frac_5m'].notna()
    ).astype(int)

    # Model-clean view for immediate use: key feature columns are complete.
    model_clean = model_30s[model_30s['row_quality_ok'] == 1].copy()

    # Strict leakage guard: label-derived timing fields remain targets/metadata and are excluded from feature manifest.
    targets = [
        'seconds_to_out_bed', 'minutes_to_out_bed',
        'wake_within_5m', 'wake_within_15m', 'wake_within_30m', 'post_out_bed'
    ]
    metadata = ['subject_id', 'epoch_start_sec', 'epoch_end_sec', 'row_quality_ok']
    quality = [c for c in model_clean.columns if 'coverage' in c or c.startswith('rr_n_beats_') or c.startswith('rr_imputed_')]
    candidate_features = [c for c in model_clean.columns if c not in set(targets + metadata + quality)]

    feature_groups = {
        'hr': [c for c in candidate_features if c.startswith('hr_act_') or c.startswith('rr_hr_')],
        'hrv': [c for c in candidate_features if c.startswith(('mean_nn_', 'sdnn_', 'rmssd_', 'pnn50_'))],
        'acc_motion': [c for c in candidate_features if c.startswith(('vm_', 'axis1_', 'axis2_', 'axis3_', 'steps_'))],
        'posture': [c for c in candidate_features if c.startswith('posture_') or c.startswith(('off_delta_', 'lying_delta_'))],
        'clock_optional': [c for c in candidate_features if c.startswith('clock_')],
        'quality': quality,
        'targets': targets,
        'metadata': metadata,
        'fit3_likely_transferable_now': [c for c in candidate_features if c.startswith(('hr_act_', 'steps_', 'clock_'))],
        'requires_device_access_confirmation_for_fit3': [c for c in candidate_features if c.startswith(('rr_', 'mean_nn_', 'sdnn_', 'rmssd_', 'pnn50_', 'vm_', 'axis', 'posture_', 'off_delta_', 'lying_delta_'))],
    }

    # Save
    qc_df.to_csv(OUT / 'mmash_qc_report.csv', index=False)
    labels_df.to_csv(OUT / 'mmash_sleep_labels_corrected.csv', index=False)
    model_30s.to_csv(OUT / 'mmash_model_preout_30s_allrows.csv', index=False)
    model_clean.to_csv(OUT / 'mmash_model_preout_30s_clean.csv', index=False)
    transition_30s.to_csv(OUT / 'mmash_transition_30s.csv', index=False)
    with open(OUT / 'feature_manifest.json', 'w', encoding='utf-8') as f:
        json.dump(feature_groups, f, ensure_ascii=False, indent=2)

    # Compressed 1-second joined table (portable; no optional parquet dependency required).
    joined_df.to_csv(OUT / 'mmash_wake_window_1s.csv.gz', index=False, compression='gzip')
    model_clean.to_csv(OUT / 'mmash_model_preout_30s_clean.csv.gz', index=False, compression='gzip')
    transition_30s.to_csv(OUT / 'mmash_transition_30s.csv.gz', index=False, compression='gzip')
    parquet_ok = False

    # Report
    included = qc_df[qc_df['include_model'] == 1]['subject_id'].tolist()
    excluded = qc_df[qc_df['include_model'] != 1][['subject_id', 'exclude_reason']]
    date_fixed_users = qc_df.loc[qc_df['sleep_date_corrected_n'] > 0, 'subject_id'].tolist()
    day_fixed_users = qc_df.loc[(qc_df['act_day_fixed_n'] > 0) | (qc_df['rr_day_fixed_n'] > 0), 'subject_id'].tolist()

    report = f'''# MMASH preprocessing report\n\n## 목적\n학술제 연구의 물리 기반 생체리듬 baseline + 잔차학습 구조에 보조적으로 사용할 MMASH wake-transition 학습 데이터를 생성했다. 최종 예측 표적은 `sleep.csv`의 Out Bed 시각이며, 센서 특징은 모두 해당 시점 이전의 과거값만 사용하도록 구성했다.\n\n## 핵심 결과\n- 원본 피험자: {len(user_dirs)}명\n- sleep label 보유: {len(labels_df)}명\n- wake transition 주변 센서 품질 통과: {len(included)}명 ({', '.join(included)})\n- 제외: {len(excluded)}명\n{excluded.to_string(index=False)}\n- 30초 causal 학습행(품질필터 전): {len(model_30s):,}\n- 30초 causal 학습행(품질필터 후): {len(model_clean):,}\n- 1초 wake-centered join 행: {len(joined_df):,}\n- 1초 join 압축 CSV 생성: True\n\n## 원본 오류/보정\n1. day=-29 오류\n- 대상: {', '.join(day_fixed_users)}\n- Actigraph와 RR 모두에서 확인됨. 원본 설명상 day는 1/2만 허용되고, -29 구간은 day1 23:59:59 직후 00:00:00부터 연속되므로 day2로 복구했다.\n\n2. sleep.csv 자정 rollover 오류\n- Out Bed Date 보정 대상: {', '.join(date_fixed_users)}\n- `Out Bed - In Bed == Total Minutes in Bed` 관계를 이용해 날짜만 +1일 보정했다. 수면 시각 자체는 변경하지 않았다.\n\n3. user_1 split sleep\n- 00:46-03:31, 03:57-07:30 두 구간 사이 gap 26분. 최종 기상시각 예측 목적상 60분 이내 분할수면으로 보고 하나의 episode로 병합했다. 최종 label은 07:30이다.\n\n4. RR artifact guard\n- ibi_s가 {RR_IBI_MIN_S:.2f}-{RR_IBI_MAX_S:.2f}s 밖인 값은 HRV 계산에서 제외하고 원본은 보존했다. 전체/피험자별 제거율은 qc_report에 기록했다.\n- RR은 beat-event 자료이므로 1Hz 변환 시 초별 median IBI를 사용하고, 이벤트가 없는 짧은 구간은 최대 {MAX_RR_INTERP_GAP_S}초만 선형 보간했다. 더 긴 gap은 NaN으로 유지했다.\n\n5. Actigraph gap\n- Actigraph 원본이 실제로 매초 완전하지 않으므로 ACC/Steps/Posture는 임의 보간하지 않았다. 각 30초 epoch에 coverage를 남기고 quality filter를 적용했다.\n\n## 학습 데이터 설계\n- wake-centered window: Out Bed -{PRE_WINDOW_MIN}분 ~ +{POST_WINDOW_MIN}분\n- 학습용 dataset: Out Bed 이전 {PRE_WINDOW_MIN}분만 사용 (post-wake 데이터 제외)\n- epoch: 30초\n- HR: Actigraph HR 통계 + RR-derived HR 통계\n- HRV: clean beat-to-beat IBI 기반 SDNN/RMSSD/pNN50/meanNN (30초 및 trailing 5분)\n- ACC: Vector Magnitude + axis variability + Steps\n- Posture: Off/Standing/Sitting/Lying fraction. `Lying`만 단독 sleep proxy로 가정하지 않음. 실제 검수상 수면 중 `Inclinometer Off` 비중이 높은 피험자가 많아 4개 상태를 모두 보존했다.\n- target: minutes_to_out_bed + wake_within_5m/15m/30m\n- 모든 rolling feature는 trailing window만 사용해 미래정보 누출을 막았다.\n\n## 중요한 한계\n- Out Bed는 PSG 독립 정답이 아니라 ActiGraph가 산출한 sleep report의 device-derived label이다. 따라서 MMASH는 weak supervision/보조학습 및 population calibration용으로 사용하고, 최종 성능 주장에는 독립 PSG/paired 데이터가 필요하다.\n- 22명 모두 젊은 남성이며 반복-night가 거의 없어 개인화 일반화 검증에는 제한이 있다.\n- row random split 금지. 반드시 subject 단위 GroupKFold/LOSO로 검증한다.\n\n## 파일\n- `mmash_qc_report.csv`: 피험자별 오류/보정/포함여부/coverage\n- `mmash_sleep_labels_corrected.csv`: 보정/병합된 sleep episode label\n- `mmash_wake_window_1s.csv.gz`: RR 1Hz + Actigraph join\n- `mmash_transition_30s.csv|csv.gz`: -4h~+30m diagnostic table\n- `mmash_model_preout_30s_allrows.csv`: post-wake 제외, 품질 flag 포함\n- `mmash_model_preout_30s_clean.csv|csv.gz`: 즉시 학습 가능한 품질 통과 rows\n- `feature_manifest.json`: feature/target 그룹 및 Galaxy Fit3 전이 가능성 구분\n'''
    (OUT / 'MMASH_PREPROCESSING_REPORT.md').write_text(report, encoding='utf-8')

    # Assertions / leakage checks
    assert set(model_clean['subject_id'].unique()) == set(included)
    assert (model_clean['seconds_to_out_bed'] >= 0).all()
    assert not any(c in feature_groups['hr'] + feature_groups['hrv'] + feature_groups['acc_motion'] + feature_groups['posture'] for c in targets)
    assert (qc_df['posture_onehot_bad_n'] == 0).all()
    assert (qc_df['vector_magnitude_max_abs_error'] <= 0.011).all()

    print('=== MMASH PREPROCESS DONE ===')
    print('included subjects:', len(included), included)
    print('excluded:')
    print(excluded.to_string(index=False))
    print('model rows all/clean:', len(model_30s), len(model_clean))
    print('1s joined rows:', len(joined_df))
    print('output:', OUT)


if __name__ == '__main__':
    main()
