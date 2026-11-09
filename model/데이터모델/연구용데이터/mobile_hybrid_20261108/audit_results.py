"""Independent NPZ/prediction audit; does not import the benchmark's scorer."""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = Path('C:/Users/pyomo/OneDrive/문서/ChatGPT/학술제')
CACHE = ROOT / 'reports/deep_learning_comparison_20261105/private'
BASE = ROOT / 'reports/equal_budget_hybrid9_20261107/private'

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def constrained(values):
    result = np.array(values, dtype=float, copy=True)
    result[:, 0] = np.clip(result[:, 0], 0, 960)
    result[:, 1] = np.minimum(1440, np.maximum(result[:, 0] + 30,
                               np.minimum(result[:, 0] + 960, result[:, 1])))
    return result

def per_person_metrics(actual, predicted, groups):
    errors = np.abs(actual - predicted)
    averages = np.array([errors[groups == person].mean(axis=0)
                         for person in np.unique(groups)])
    return dict(joint=float(averages.mean()), onset=float(averages[:, 0].mean()),
                wake=float(averages[:, 1].mean()))

def replay_independent(data, mask, base):
    groups = data['groups'][mask]
    actual = data['y'][mask]
    times = {key: np.array([datetime.fromisoformat(str(t)).timestamp()
                           for t in data[key][mask]])
             for key in ('cutoffs', 'wake_times', 'observed_at')}
    output = base.copy()
    residuals = np.clip(actual - base, -180, 180)
    for person in np.unique(groups):
        indices = np.flatnonzero(groups == person)
        ordered = indices[np.argsort(times['cutoffs'][indices], kind='stable')]
        for position, index in enumerate(ordered):
            earlier = ordered[:position]
            eligible = earlier[(times['observed_at'][earlier] < times['cutoffs'][index]) &
                               (times['wake_times'][earlier] <= times['observed_at'][earlier])]
            if not len(eligible):
                continue
            weights = 2 ** (-(times['cutoffs'][index] - times['wake_times'][eligible]) / (86400 * 14))
            change = (weights[:, None] * residuals[eligible]).sum(axis=0) / (14 + weights.sum())
            output[index] += np.clip(change, -60, 60)
    return constrained(output)

def main():
    report_path = HERE / 'accuracy/actual_mobile_evaluation.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    cells = list(report['test']['cells'])
    assert len(cells) == 6 and len({(c['model'], c['cohort']) for c in cells}) == 6
    protocol = json.loads((HERE / 'accuracy/protocol.json').read_text(encoding='utf-8'))
    checked = []
    independent_metrics = {}
    for cohort in ('cmi', 'mesa'):
        cache_path = CACHE / (cohort + '.npz')
        assert sha(cache_path) == protocol['cache_sha256'][cache_path.name]
        with np.load(cache_path, allow_pickle=False) as archive:
            data = {k: archive[k] for k in archive.files}
        groups = [set(data['groups'][data['splits'] == s]) for s in ('train', 'validation', 'test')]
        assert not any(groups[i] & groups[j] for i, j in ((0,1),(0,2),(1,2)))
        mask = data['splits'] == 'test'
        for cell in cells:
            if cell['cohort'] != cohort:
                continue
            model = cell['model']
            if model in ('tabdpt_full_e4', 'hgb'):
                prefix = 'tabdpt_v1_3' if model == 'tabdpt_full_e4' else 'hgb'
                prediction_path = BASE / f'{prefix}_{cohort}_2_test.npy'
            else:
                assert model == report['portable_model']
                selected = report['selected_resource_candidate']
                prediction_path = HERE / f'accuracy/private/{selected}_portable_{cohort}_test.npy'
            base = np.load(prediction_path, allow_pickle=False)
            assert base.shape == (int(mask.sum()), 2) and np.isfinite(base).all()
            assert np.max(np.abs(base - constrained(base))) < 1e-8
            corrected = replay_independent(data, mask, base)
            diffs = []
            for scope, prediction in (('raw', base), ('primary', corrected)):
                metrics = per_person_metrics(data['y'][mask], prediction, data['groups'][mask])
                independent_metrics[(model, cohort, 'corrected' if scope == 'primary' else 'raw')] = metrics
                for short, key in (('joint','participant_macro_joint_mae_min'),
                                   ('onset','onset_mae_min'), ('wake','wake_mae_min')):
                    diffs.append(abs(metrics[short] - cell[scope][key]))
            assert max(diffs) < 1e-9, (model, cohort, diffs)
            checked.append(dict(model=model, cohort=cohort, rows=int(mask.sum()),
                                participants=len(groups[2]), max_metric_difference_min=max(diffs),
                                prediction_sha256=sha(prediction_path)))
    for comparison in report['comparisons']:
        key = {'joint_mae': 'joint', 'onset_mae': 'onset', 'wake_mae': 'wake'}[comparison['metric']]
        expected_deltas = []
        for cohort in ('cmi', 'mesa'):
            left = independent_metrics[(comparison['reference'], cohort, comparison['scope'])][key]
            right = independent_metrics[(comparison['candidate'], cohort, comparison['scope'])][key]
            expected_deltas.append(right - left)
            reported = next(c for c in comparison['cohorts'] if c['cohort'] == cohort)
            assert abs(reported['delta_min'] - (right - left)) < 1e-9
        assert abs(comparison['equal_cohort_delta_min'] - np.mean(expected_deltas)) < 1e-9
    mobile_average = np.mean([independent_metrics[(report['portable_model'], c, 'corrected')]['joint']
                              for c in ('cmi', 'mesa')])
    assert abs(report['test']['aggregate_corrected_participant_macro_joint_mae_min'] - mobile_average) < 1e-9
    before = json.loads((HERE / 'production_before.json').read_text(encoding='utf-8-sig'))
    preserved = all(sha(item['path']) == item['sha256'] for item in before)
    assert preserved, 'Production source changed'
    result = dict(status='pass', independent_causal_replay=True, cells=checked,
                  verified_comparison_point_estimates=len(report['comparisons']),
                  production_source_preserved=preserved, report_sha256=sha(report_path))
    (HERE / 'audit.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
