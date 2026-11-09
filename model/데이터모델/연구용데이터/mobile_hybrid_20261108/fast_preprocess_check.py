"""Compare exact sequential float64 accumulation in Python vs NumPy cumsum.

Diagnostic only: never changes the frozen implementation or predictions.
"""
from pathlib import Path
import json
import hashlib
import importlib.util
import sys
import time
import numpy as np

HERE = Path(__file__).resolve().parent
EXPORT = HERE / 'mobile_export'
sys.path[:0] = [str(EXPORT / 'python_deps'), str(EXPORT)]
import portable_runtime as r
from mobile_retrieval import mobile_l2_indices

def sequential_numpy(values):
    values = np.asarray(values, dtype=np.float64)
    count = len(values)
    if count == 0:
        return 0.0, 0.0
    mean = float(np.cumsum(values, dtype=np.float64)[-1]) / count
    if count <= 1:
        return mean, 0.0
    delta = values - mean
    squared = float(np.cumsum(delta * delta, dtype=np.float64)[-1])
    return mean, (squared / (count - 1)) ** 0.5

def main():
    reference_path = EXPORT / 'archive_pre_optimization/portable_runtime.py'
    reference_sha = hashlib.sha256(reference_path.read_bytes()).hexdigest()
    assert reference_sha == 'b1d1a1712af9e50e0238ec5fb1fd238db7757311aab33f141822690ceb6c4dd1'
    spec = importlib.util.spec_from_file_location('preoptimization_runtime', reference_path)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    original = reference._mean_std64
    rng = np.random.default_rng(20261108)
    arrays = [np.array([]), np.zeros(1024), np.ones(1024), np.array([-0.0])]
    for count in (1, 2, 15, 16, 1024, 1025):
        for scale in (1e-12, 1.0, 1e12):
            arrays.extend([rng.normal(size=count) * scale, 1.0 + rng.normal(size=count) * scale])
    for values in arrays:
        assert original(values) == sequential_numpy(values)
    bundle = EXPORT / 'bundle_tabdpt_c1024_e2_selected'
    runtime = r.PortableTabDptRuntime(bundle)
    goldens = json.loads((bundle / 'goldens_validation32.json').read_text(encoding='utf-8'))
    elapsed_old = elapsed_new = 0.0
    tensors = 0
    for cohort in ('cmi', 'mesa'):
        train, _ = runtime.contexts[cohort]
        queries = np.stack([runtime.transform_raw(cohort, np.array(case['raw39'])) for case in goldens['cases']])
        neighbors = mobile_l2_indices(train, queries, 1024)
        for row, query in enumerate(queries):
            for permutation in runtime.manifest['cohorts'][cohort]['feature_permutations']:
                x = np.zeros((1025,128), dtype=np.float32)
                x[:1024,:len(permutation)] = train[neighbors[row]][:,permutation]
                x[1024,:len(permutation)] = query[permutation]
                r._mean_std64 = original
                start = time.perf_counter()
                expected = r.deterministic_model_preprocess(x,1024)
                elapsed_old += time.perf_counter() - start
                r._mean_std64 = sequential_numpy
                start = time.perf_counter()
                actual = r.deterministic_model_preprocess(x,1024)
                elapsed_new += time.perf_counter() - start
                assert expected.tobytes() == actual.tobytes(), (cohort,row)
                tensors += 1
    r._mean_std64 = original
    result = dict(status='pass', exact_tensors=tensors, scalar_test_arrays=len(arrays),
                  original_seconds=elapsed_old, optimized_seconds=elapsed_new,
                  speedup=elapsed_old/elapsed_new, numpy_version=np.__version__,
                  reference_runtime_sha256=reference_sha,
                  check_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  method='float64 sequential cumsum, not pairwise sum; identical subtraction/multiplication and Python sqrt')
    (HERE / 'fast_preprocess_check.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__ == '__main__':
    main()
