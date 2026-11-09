"""Read-only checks of collected files and derived reference tables."""
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import unicodedata
import numpy as np
import pandas as pd
import pyarrow

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'processed'
manifest = json.loads((ROOT / 'manifest.json').read_text())
assert not manifest['errors']
entries = manifest['files'] + [json.loads((ROOT / 'korea_manifest.json').read_text())]
paths = {unicodedata.normalize('NFC', str(p.relative_to(ROOT))): p
         for p in (ROOT / 'raw').rglob('*') if p.is_file()}
for entry in entries:
    path = paths[unicodedata.normalize('NFC', entry['local_path'])]
    assert path.stat().st_size == entry['bytes']
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    assert digest.hexdigest() == entry['sha256']
print('Original checksums passed:', len(entries), 'files,', sum(e['bytes'] for e in entries), 'bytes')
with sqlite3.connect(f'file:{OUT / "supplementary_reference.sqlite3"}?mode=ro', uri=True) as connection:
    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    for file in sorted(OUT.glob('age_*_reference.csv')):
        frame = pd.read_csv(file)
        keys = ['age_group', 'sex', 'metric'] + (['clock_hour'] if 'clock_hour' in frame else [])
        assert not frame.duplicated(keys).any()
        assert not frame.isna().any().any()
        numeric = frame.select_dtypes(include='number')
        assert np.isfinite(numeric.to_numpy()).all()
        assert (numeric >= 0).all().all()
        assert (frame.weighted_p25 <= frame.weighted_median).all()
        assert (frame.weighted_median <= frame.weighted_p75).all()
        assert (frame.kish_effective_n <= frame.n_people + 1e-8).all()
        table = file.stem.removesuffix('_reference')
        assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == len(frame)
        print(table, len(frame), 'summary rows, no missing/nonfinite/duplicate keys')
payload = json.loads((OUT / 'candidate_age_priors.json').read_text(),
                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
assert len(payload['entries']) == 7
assert all(e['auto_apply'] is False and e['korea_validated'] is False for e in payload['entries'])
report = json.loads((OUT / 'processing_report.json').read_text())
assert report['steps']['raw_minutes'] == 74874095
print('Candidate safety flags and source row count passed')
print('Versions:', platform.python_version(), 'pandas', pd.__version__, 'numpy', np.__version__, 'pyarrow', pyarrow.__version__)
