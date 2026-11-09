"""Reproduce frozen predictions in row-ID order without fitting or selection."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--cohort', choices=('cmi', 'mesa'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.run/'source_manifest.json').read_text('utf-8'))
    for name, digest in manifest['sources'].items():
        if hashlib.sha256((args.run/name).read_bytes()).hexdigest() != digest:
            raise ValueError('Frozen source changed: '+name)
    sys.path.insert(0, str(args.run/'code'))
    import continuous_experiments as c
    c.verify_locks(args.run)
    import joblib
    import numpy as np
    import pandas as pd
    from threadpoolctl import threadpool_limits
    directory = args.run/args.cohort
    selection = json.loads((directory/'train_selection.json').read_text('utf-8'))
    frame = pd.read_parquet(directory/'test_rows_private.parquet')
    saved = np.load(directory/'test_predictions_private.npz')
    errors = {}
    with threadpool_limits(limits=1):
        for policy in ('legacy14', selection['selected_daily_policy']):
            x, states = c.replay(frame, selection['prior'], policy)
            if not np.array_equal(x.row_id, frame.row_id):
                raise ValueError('Replayed feature row IDs do not align')
            generated = {'physical': c.m.project(x[['physical_bed','physical_wake']].to_numpy(float))}
            for arm in ('data', 'hybrid'):
                artifact = joblib.load(directory/'models'/f'{policy}__{arm}.joblib')
                z = c.m.encode(frame, x, states, selection['representation'], arm)
                if list(z.columns) != artifact['feature_order']:
                    raise ValueError('Frozen model feature order mismatch')
                generated[arm] = c.m.predict([artifact['model']], z, c.m.reference(x, selection['prior'], selection['representation']))
            for arm, values in generated.items():
                key = f'{policy}__{arm}'
                np.testing.assert_allclose(values, saved[key], rtol=0, atol=1e-8)
                errors[key] = float(np.max(np.abs(values-saved[key])))
    c.verify_locks(args.run)
    args.output.write_text(json.dumps({'verified_by_frozen_model_replay':True,
        'n_rows':len(frame), 'maximum_absolute_difference_min':errors,
        'row_id_order_sha256':hashlib.sha256(frame.row_id.to_json().encode()).hexdigest(),
        'source_manifest_sha256':hashlib.sha256((args.run/'source_manifest.json').read_bytes()).hexdigest()},indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
