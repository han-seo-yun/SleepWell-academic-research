"""Outcome-blind protocol amendment: current Train-fit DATA33 group ablation."""
from pathlib import Path
import os, sys, json, time, gc
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
from tmp.run_tabdpt_group_ablation import ROOT, SRC, OUT as PARENT, GROUPS, WEIGHTS, SEED, sha, write
import numpy as np
import pandas as pd
import torch
from robust_features import DATA_V2
from tabular_foundation_models import _make_model, _seed_runtime
from tabdpt import distribution_median
from deep_tabular_models import constrain_predictions

OUT = PARENT / 'train_refit_primary'

def log(event, **kwargs):
    row = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'event': event, **kwargs}
    line = json.dumps(row, ensure_ascii=False)
    print(line, flush=True)
    with (OUT / 'progress.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')

def fit_preprocess(train, query, names):
    """All operations columnwise, fitted on current Train predictors only."""
    assert np.isfinite(train).all() and np.isfinite(query).all()
    med = np.median(train, axis=0)
    lo, hi = np.quantile(train, [.01, .99], axis=0)
    clipped = np.clip(train, lo, hi)
    keep = np.flatnonzero(np.ptp(clipped, axis=0) > 1e-10)
    retained = clipped[:, keep]
    mean, scale = retained.mean(axis=0), retained.std(axis=0)
    assert (scale > 0).all()
    x = (retained - mean) / scale
    q = (np.clip(query, lo, hi)[:, keep] - mean) / scale
    pp = {'raw_features': names, 'imputer_median': med.tolist(), 'clip_low': lo.tolist(),
          'clip_high': hi.tolist(), 'variance_keep': keep.tolist(),
          'scale_mean': mean.tolist(), 'scale': scale.tolist(),
          'effective_features': [names[i] for i in keep],
          'constant_removed': [n for i, n in enumerate(names) if i not in keep]}
    return x, q, pp

def main():
    OUT.mkdir(exist_ok=True); (OUT / 'private').mkdir(exist_ok=True)
    previous = json.loads((PARENT / 'protocol.json').read_text(encoding='utf-8'))
    provenance = json.loads((SRC / 'provenance.json').read_text(encoding='utf-8'))
    assert sha(WEIGHTS) == previous['checkpoint_sha256']
    for key in ['input_csv', 'frozen_predictions']:
        assert sha(provenance[key]['path']) == provenance[key]['sha256']
    source = pd.read_csv(provenance['input_csv']['path'], dtype={'participant_id': str, 'row_id': str})
    datasets, configs = {}, []
    variants = [('all33', '전체 33개', [])] + [(f'drop_{i+1}', g['group'], g['features']) for i, g in enumerate(GROUPS)]
    for cohort in ['cmi', 'mesa']:
        path = SRC / f'{cohort}.npz'
        assert sha(path) == provenance['private_artifacts'][path.name]
        ds = {}
        with np.load(path, allow_pickle=False) as z:
            assert tuple(provenance['cohorts'][cohort]['feature_orders']['data']) == DATA_V2
            for split in ['train', 'validation']:
                mask = z['splits'] == split
                raw, target = z['X_data_raw'][mask].copy(), z['y'][mask].copy()
                ids, rows = z['groups'][mask].copy(), z['row_ids'][mask].copy()
                assert np.isfinite(raw).all() and np.isfinite(target).all()
                check = source[source.cohort.eq(cohort)].set_index('row_id').loc[rows]
                assert check.split.eq(split).all() and list(check.participant_id) == list(ids)
                np.testing.assert_allclose(target, check[['bed_offset','wake_offset']].to_numpy(), atol=1e-9)
                ds[split] = {'X': raw, 'y': target, 'ids': ids, 'rows': rows}
        assert not (set(ds['train']['ids']) & set(ds['validation']['ids']))
        datasets[cohort] = ds
        x, q, pp = fit_preprocess(ds['train']['X'], ds['validation']['X'], list(DATA_V2))
        # Independently refitting each reduced arm must reproduce retained columns.
        for _, _, removed in variants[1:]:
            k = [i for i, n in enumerate(DATA_V2) if n not in removed]
            ax, aq, ap = fit_preprocess(ds['train']['X'][:, k], ds['validation']['X'][:, k], [DATA_V2[i] for i in k])
            positions = [pp['effective_features'].index(n) for n in ap['effective_features']]
            np.testing.assert_allclose(ax, x[:, positions], atol=1e-12)
            np.testing.assert_allclose(aq, q[:, positions], atol=1e-12)
        configs.append({'cohort': cohort, 'train_nights': len(ds['train']['y']), 'train_people': len(set(ds['train']['ids'])),
                        'validation_nights': len(ds['validation']['y']), 'validation_people': len(set(ds['validation']['ids'])),
                        'effective_features': pp['effective_features'], 'constant_removed': pp['constant_removed'],
                        'cache_sha256': sha(path)})
    protocol = {**previous, 'schema': 'tabdpt.dropgroup.train_refit.v2',
        'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'datasets': configs,
        'amendment': 'Before inspecting any ablation MAE, code audit found frozen MESA preprocessing excludes currently variable habit_bed/habit_wake/personal_weight. Refit current Train preprocessing for primary nominal DATA33 analysis; retain frozen run as separate sensitivity. No outcome-based group, seed or hyperparameter change.',
        'preprocessing': 'Per cohort and arm: fit median, 1/99 percentile clipping, constant filter (range<=1e-10), mean/std on current Train only; all raw entries finite; verify columnwise reduced-fit equivalence to baseline retained columns.',
        'scope': 'exploratory reused validation, fixed pretrained checkpoint and seed; current Train-fit preprocessing',
        'script_sha256': sha(__file__), 'parent_script_sha256': previous['script_sha256']}
    lock = OUT / 'protocol.json'
    if lock.exists():
        assert json.loads(lock.read_text(encoding='utf-8'))['script_sha256'] == sha(__file__)
    else:
        write(lock, protocol)
    log('verified_protocol_locked', datasets=configs)
    for cohort, ds in datasets.items():
        train, val = ds['train'], ds['validation']
        for variant, title, removed in variants:
            dest = OUT / 'private' / f'{cohort}_{variant}.npz'
            meta_path = OUT / f'{cohort}_{variant}_metadata.json'
            if dest.exists() and meta_path.exists():
                assert sha(dest) == json.loads(meta_path.read_text(encoding='utf-8'))['sha256']
                log('resume_existing', cohort=cohort, variant=variant); continue
            k = [i for i, n in enumerate(DATA_V2) if n not in removed]
            x, q, pp = fit_preprocess(train['X'][:, k], val['X'][:, k], [DATA_V2[i] for i in k])
            write(OUT / f'{cohort}_{variant}_preprocessing.json', pp)
            channels, metadata = [], []
            log('start', cohort=cohort, variant=variant, effective_features=x.shape[1])
            for channel in range(2):
                _seed_runtime(SEED); torch.cuda.empty_cache()
                model = _make_model('tabdpt_v1_3', WEIGHTS, SEED, 4, None)
                model.fit(x, train['y'][:, channel]); start = time.perf_counter()
                for batch in [4096, 1024, 256, 64]:
                    try:
                        full = model.predict(q, n_ensembles=4, context_size=None, batch_size=batch, seed=SEED, output_type='full')
                        pred = distribution_median(full)
                        if hasattr(pred, 'detach'): pred = pred.detach().cpu().numpy()
                        pred = np.asarray(pred).reshape(-1)
                        assert len(pred) == len(q) and np.isfinite(pred).all()
                        break
                    except torch.cuda.OutOfMemoryError:
                        log('oom_retry', cohort=cohort, variant=variant, batch=batch); torch.cuda.empty_cache()
                else: raise RuntimeError('OOM fallback exhausted')
                channels.append(pred); metadata.append({'channel': channel, 'seconds': time.perf_counter()-start, 'batch': batch})
                if variant == 'all33':
                    fullsmall = model.predict(q[:8], n_ensembles=4, context_size=None, batch_size=8, seed=SEED, output_type='full')
                    small = distribution_median(fullsmall)
                    if hasattr(small, 'detach'): small = small.detach().cpu().numpy()
                    gap = float(np.max(abs(pred[:8] - np.asarray(small).reshape(-1))))
                    assert gap < .01, ('batch_invariance_failed', gap)
                    metadata[-1]['batch_check_max_min'] = gap
                    del fullsmall, small
                log('channel_complete', cohort=cohort, variant=variant, **metadata[-1])
                del model, full; gc.collect(); torch.cuda.empty_cache()
            prediction = constrain_predictions(np.column_stack(channels))
            np.savez_compressed(dest, prediction=prediction, actual=val['y'], participant_id=val['ids'], row_id=val['rows'])
            write(meta_path, {'cohort': cohort, 'variant': variant, 'title': title, 'dropped': removed,
                             'remaining_raw': len(k), 'effective': x.shape[1], 'channels': metadata, 'sha256': sha(dest)})
            log('variant_complete', cohort=cohort, variant=variant)
    log('all_inference_complete')

if __name__ == '__main__':
    main()
