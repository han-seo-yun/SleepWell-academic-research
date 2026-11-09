"""One-time prefit review amendment; requires no model/results yet exist."""
import argparse
import json
from pathlib import Path
import shutil
import pandas as pd

import experiment as e

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
out = parser.parse_args().output
for name in ("fit_started.json", "selection_lock.json", "research_models.joblib", "evaluation.json", "prefit_seal.json"):
    if (out / name).exists():
        raise RuntimeError(f"Cannot amend after fit or sealing: {name}")
original = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
initial = Path(__file__).with_name("prefit_initial")
for path, expected in original["sha256"].items():
    p = Path(path)
    if p.name in ("PROTOCOL.md", "experiment.py") and p.parent == Path(__file__).parent:
        p = initial / p.name
    if e.sha(p) != expected:
        raise RuntimeError(f"Original input/snapshot mismatch: {path}")
e.verify_original_split(pd.read_csv(e.EPISODES))
replay = pd.read_parquet(out / "private_replay.parquet")
old_support = json.loads((out / "support_lock.json").read_text(encoding="utf-8"))
if e.sha(out / "private_replay.parquet") != old_support["replay_sha256"]:
    raise RuntimeError("Replay changed")
snapshot = out / "prefit_initial"
snapshot.mkdir()
for name in ("PROTOCOL.md", "experiment.py"):
    shutil.copy2(initial / name, snapshot / name)
shutil.copy2(out / "input_manifest.json", snapshot / "input_manifest.json")
shutil.copy2(out / "support_lock.json", snapshot / "support_lock.json")
people = replay.drop_duplicates("subject_id")
counts = people.loc[people.split.eq("train") & people.age.between(60, 80)].work_schedule_code.value_counts()
supported = sorted(int(k) for k, n in counts.items() if n >= 50)
mask = e.eligible(replay, supported)
support = {**old_support, "locked_utc": e.stamp(), "supported_codes": supported,
           "support_count_age_scope": [60, 80],
           "train_category_people": {str(k): int(v) for k, v in counts.items()},
           "eligible_rows": int(mask.sum()), "eligible_people": int(replay.loc[mask].subject_id.nunique())}
e.dump(out / "support_lock.json", support)
e.dump(out / "input_manifest.json", {**original, "amended_before_fit_utc": e.stamp(),
       "initial_manifest_sha256": e.sha(snapshot / "input_manifest.json"),
       "sha256": {str(p): e.sha(p) for p in e.source_paths()}})
e.dump(out / "prefit_amendment.json", {"utc": e.stamp(), "no_models_or_scores_existed": True,
       "reason": "independent prefit review: evaluate harm/support within actual eligible age group; strengthen split/phase integrity",
       "replay_reused_unchanged": True, "originals_preserved_at": str(snapshot),
       "support_codes": supported})
for name in ("PROTOCOL.md", "experiment.py", "linkage.py", "seal_prefit.py", "test_experiment.py", "test_linkage.py"):
    shutil.copy2(Path(__file__).with_name(name), out / name)
e.dump(out / "prefit_seal.json", {"utc": e.stamp(), "sha256": {name: e.sha(out / name)
       for name in ("input_manifest.json", "support_lock.json", "prefit_amendment.json", "private_replay.parquet")}})
e.verify_sources(out)
print(json.dumps({"prefit_sealed": True, "supported_codes": supported, "eligible_people": support["eligible_people"]}))
