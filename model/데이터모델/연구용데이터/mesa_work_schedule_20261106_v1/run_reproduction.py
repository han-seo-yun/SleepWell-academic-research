"""Repeat the final locked procedure in a NEW directory, without tuning."""
import argparse
from pathlib import Path
import shutil

import experiment as e

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
out = parser.parse_args().output
if out.exists():
    raise RuntimeError("Reproduction requires a new, nonexistent output directory")
e.prepare(out)
e.dump(out / "prefit_amendment.json", {"utc": e.stamp(), "amendment": None,
       "note": "Exact final reviewed procedure reproduced; no new search, tuning or protocol change"})
for name in ("PROTOCOL.md", "experiment.py", "linkage.py", "run_reproduction.py"):
    shutil.copy2(Path(__file__).with_name(name), out / name)
e.dump(out / "prefit_seal.json", {"utc": e.stamp(), "sha256": {name: e.sha(out / name)
       for name in ("input_manifest.json", "support_lock.json", "prefit_amendment.json", "private_replay.parquet")}})
e.fit(out)
e.evaluate(out)
