import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

import mesa_experiments as base
import mesa_psg_experiments as exp


ACT_HEADER = [
    "mesaid", "line", "linetime", "offwrist", "activity", "whitelight",
    "redlight", "greenlight", "bluelight", "wake", "interval",
]


def _write_act(path, participant, rows=20):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ACT_HEADER)
        for line in range(1, rows + 1):
            seconds = (line - 1) * 30
            linetime = f"00:{seconds // 60:02d}:{seconds % 60:02d}"
            writer.writerow([participant, line, linetime, 0, line, 1, 2, 3, 4,
                             line % 2, "ACT_LABEL_MUST_NOT_GATE"])


def _write_xml(path, *, malformed=False):
    if malformed:
        path.write_text("<broken>", encoding="utf-8")
        return
    path.write_text(
        "<?xml version='1.0'?><PSGAnnotation><EpochLength>30</EpochLength><ScoredEvents>"
        "<ScoredEvent><EventType/><EventConcept>Recording Start Time</EventConcept>"
        "<Start>0</Start><Duration>600</Duration><ClockTime>01.01.85 00.00.00</ClockTime>"
        "</ScoredEvent><ScoredEvent><EventType>Stages|Stages</EventType>"
        "<EventConcept>Wake|0</EventConcept><Start>0</Start><Duration>300</Duration>"
        "</ScoredEvent><ScoredEvent><EventType>Stages|Stages</EventType>"
        "<EventConcept>Stage 2 sleep|2</EventConcept><Start>300</Start><Duration>300</Duration>"
        "</ScoredEvent></ScoredEvents></PSGAnnotation>",
        encoding="utf-8",
    )


def _participant_fixture(root, count=11, incomplete_index=0, empty_xml_index=None):
    act_dir = root / "actigraphy"
    xml_dir = root / "polysomnography" / "annotations-events-nsrr"
    overlap_dir = root / "overlap"
    act_dir.mkdir(parents=True)
    xml_dir.mkdir(parents=True)
    overlap_dir.mkdir(parents=True)
    participants = []
    seen_splits = set()
    candidate = 1
    while len(participants) < count or seen_splits != {"train", "validation", "test"}:
        participant = str(candidate)
        participants.append(participant)
        seen_splits.add(base.subject_split(participant))
        candidate += 1
    with (overlap_dir / "mesa-actigraphy-psg-overlap.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["mesaid", "line", "linetime", "starttime_psg"])
        for index, participant in enumerate(participants):
            writer.writerow([participant, 1, "00:00:00", "00:00:00"])
            _write_act(
                act_dir / f"mesa-sleep-{int(participant):04d}.csv",
                participant,
                rows=19 if index == incomplete_index else 20,
            )
            xml_path = xml_dir / f"mesa-sleep-{int(participant):04d}-nsrr.xml"
            if index == empty_xml_index:
                xml_path.write_bytes(b"")
            else:
                _write_xml(xml_path)
    return participants


class PSGExperimentTest(unittest.TestCase):
    def baseline(self, folder):
        folder.mkdir()
        x = np.tile(np.arange(40, dtype=np.float32)[:, None], (1, 5))
        y = (x[:, 0] < 20).astype(np.uint8)
        candidate = next(c for c in base.CANDIDATES if c['name'] == 'activity_light_hgb')
        model = base._make_model(candidate).fit(x, y)
        joblib.dump({'model': model, 'epoch_seconds': 30, 'feature_names': list(base.FEATURE_NAMES), 'fit_splits': ['train'], 'research_only': True}, folder/'selected_model.joblib')
        base._write_json(folder/'experiment_report.json', {'selected_artifact': {'sha256': exp.digest(folder/'selected_model.joblib')}})
        base._write_json(folder/'manifest.json', {'subjects': []})

    def test_freeze_rejects_overwrite_raw_output_and_bad_model_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'raw'; root.mkdir()
            baseline = Path(temp)/'baseline'
            with threadpool_limits(limits=2): self.baseline(baseline)
            with self.assertRaises(ValueError): exp.freeze_protocol(root, root/'new', baseline)
            out = Path(temp)/'result'
            protocol = exp.freeze_protocol(root, out, baseline)
            self.assertEqual(
                protocol["exclusions"]["allowed_reasons"],
                ["empty_psg_xml", "no_complete_eligible_ten_minute_blocks"],
            )
            self.assertIn("restart", protocol["data_preflight"]["restart_policy"])
            with self.assertRaises(FileExistsError): exp.freeze_protocol(root, out, baseline)
            (baseline/'selected_model.joblib').write_bytes(b'changed')
            with self.assertRaises(ValueError): exp.freeze_protocol(root, Path(temp)/'other', baseline)
            self.assertFalse((Path(temp)/'other').exists())

    def test_discover_duplicate_numeric_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'actigraphy').mkdir()
            for name in ['mesa-sleep-0001.csv', 'mesa-sleep-1.csv']:
                (root/'actigraphy'/name).touch()
            with self.assertRaises(ValueError): exp.discover(root)

    def test_synthetic_fit_selection_prediction_and_full_verification(self):
        with tempfile.TemporaryDirectory() as temp, threadpool_limits(limits=2):
            root = Path(temp)/'raw'; root.mkdir()
            baseline = Path(temp)/'baseline'; self.baseline(baseline)
            out = Path(temp)/'result'
            protocol = exp.freeze_protocol(root, out, baseline)
            (out/'subjects').mkdir()
            subjects = []
            rng = np.random.default_rng(7)
            for i in range(18):
                token = f'synthetic_{i}'
                split = 'train' if i < 8 else 'validation' if i < 13 else 'test'
                lines = np.arange(1, 401, dtype=np.int32)
                y = ((lines // 80) % 2).astype(np.uint8)
                x = (rng.uniform(1, 20, (400, 5)) + (1-y[:, None])*20).astype(np.float32)
                np.savez_compressed(out/'subjects'/f'{token}.npz', line=lines, t_rel_sec=(lines-1)*30, x=x, y_sleep=y)
                subjects.append({'token': token, 'split': split})
            manifest = {'subjects': subjects, 'split': {'train': 8, 'validation': 5, 'test': 5}}
            base._write_json(out/'manifest.json', manifest)
            original = base.evaluate_models
            calls = []
            def observed(*args, **kwargs):
                split = args[1][0]['split']; calls.append(split)
                if split == 'test': self.assertTrue((out/'selected.json').exists())
                return original(*args, **kwargs)
            with patch.object(base, 'evaluate_models', side_effect=observed):
                report = exp.fit_and_evaluate(out, baseline, protocol, manifest)
            self.assertEqual(calls, ['validation', 'test'])
            self.assertEqual(report['selected_before_test'], base._select(report['validation']))
            self.assertEqual(len(report['test_all_frozen_candidates_descriptive']), 16)
            self.assertTrue(report['selected_artifact']['research_only'])
            from verify_research_run import verify_mesa
            self.assertIn('MESA:', verify_mesa(out))

    def test_build_dataset_preserves_prior_splits_hashes_archives_and_expected_empty_subject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            participants = _participant_fixture(root)
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            prior_subjects = [
                {"token": base.subject_token(p), "split": base.subject_split(p)}
                for p in participants
            ]
            base._write_json(baseline / "manifest.json", {"subjects": prior_subjects})
            output = Path(temp) / "output"
            output.mkdir()

            manifest = exp.build_dataset(root, output, baseline)

            self.assertEqual(manifest["inventory"]["matched_candidates"], len(participants))
            self.assertEqual(manifest["inventory"]["included_subjects"], len(participants) - 1)
            self.assertEqual(len(manifest["excluded_private"]), 1)
            self.assertIn("no_complete_eligible_ten_minute_blocks", manifest["excluded_private"][0]["reason"])
            expected_split = {base.subject_token(p): base.subject_split(p) for p in participants}
            for subject in manifest["subjects"]:
                self.assertEqual(subject["split"], expected_split[subject["token"]])
                self.assertTrue(subject["previous_ACT_snapshot_member"])
                archive = output / "subjects" / f"{subject['token']}.npz"
                self.assertEqual(subject["output_sha256"], exp.digest(archive))
                with np.load(archive, allow_pickle=False) as loaded:
                    self.assertEqual(set(loaded.files), {"line", "t_rel_sec", "x", "y_sleep", "y_actigraphy"})
                    self.assertEqual(loaded["line"].tolist(), list(range(1, 21)))

    def test_build_dataset_rejects_source_mutation_after_alignment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            _participant_fixture(root, count=1, incomplete_index=-1)
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "output"
            output.mkdir()
            original = exp.alignment.align_subject

            def mutate_after_read(act_path, *args, **kwargs):
                result = original(act_path, *args, **kwargs)
                with Path(act_path).open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                return result

            with patch.object(exp.alignment, "align_subject", side_effect=mutate_after_read):
                with self.assertRaisesRegex(RuntimeError, "source_changed_since_discovery"):
                    exp.build_dataset(root, output, baseline)

    def test_zero_byte_xml_is_provenance_recorded_expected_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            participants = _participant_fixture(
                root, count=11, incomplete_index=-1, empty_xml_index=0
            )
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "output"
            output.mkdir()

            manifest = exp.build_dataset(root, output, baseline)

            self.assertEqual(manifest["inventory"]["matched_candidates"], len(participants))
            self.assertEqual(manifest["inventory"]["included_subjects"], len(participants) - 1)
            self.assertEqual(manifest["exclusion_reasons"], {"empty_psg_xml": 1})
            excluded = manifest["excluded_private"][0]
            self.assertEqual(excluded["reason"], "empty_psg_xml")
            self.assertEqual(excluded["source_snapshot"]["xml_size"], 0)
            self.assertEqual(excluded["source_sha256"]["xml_sha256"], exp.digest(
                root / "polysomnography" / "annotations-events-nsrr" / "mesa-sleep-0001-nsrr.xml"
            ))
            self.assertEqual(manifest["aggregate_qc"]["source_rows"], (len(participants) - 1) * 20)

    def test_psg_preflight_detects_mutation_before_subject_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            participants = _participant_fixture(root, count=1, incomplete_index=-1)
            first_xml = (
                root / "polysomnography" / "annotations-events-nsrr"
                / f"mesa-sleep-{int(participants[0]):04d}-nsrr.xml"
            )
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "output"
            output.mkdir()
            original = exp.alignment.parse_stages

            def mutate_after_parse(path):
                result = original(path)
                if Path(path) == first_xml:
                    with Path(path).open("a", encoding="utf-8") as handle:
                        handle.write("\n")
                return result

            with patch.object(exp.alignment, "parse_stages", side_effect=mutate_after_parse):
                with self.assertRaisesRegex(RuntimeError, "source_changed_during_psg_preflight"):
                    exp.build_dataset(root, output, baseline)
            self.assertFalse((output / "subjects").exists())

    def test_exclusion_threshold_aborts_after_preserving_inventory_and_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            participants = _participant_fixture(root, count=3)
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "output"
            output.mkdir()

            with patch.object(exp, "MAX_EXCLUDED_FRACTION", 0.05):
                with self.assertRaisesRegex(ValueError, "exclusion threshold exceeded"):
                    exp.build_dataset(root, output, baseline)

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["inventory"]["matched_candidates"], len(participants))
            self.assertEqual(manifest["exclusion_reasons"], {"no_complete_eligible_ten_minute_blocks": 1})
            self.assertGreater(manifest["excluded_fraction"], 0.05)
            self.assertFalse((output / "selected.json").exists())
            self.assertFalse((output / "evaluation").exists())

    def test_build_dataset_propagates_schema_and_io_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            participants = _participant_fixture(root, count=1, incomplete_index=-1)
            participant = participants[0]
            xml = root / "polysomnography" / "annotations-events-nsrr" / f"mesa-sleep-{int(participant):04d}-nsrr.xml"
            _write_xml(xml, malformed=True)
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "schema-output"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "invalid NSRR XML"):
                exp.build_dataset(root, output, baseline)
            self.assertFalse((output / "subjects").exists())

            _write_xml(xml)
            io_output = Path(temp) / "io-output"
            io_output.mkdir()
            with patch.object(exp.alignment, "align_subject", side_effect=OSError("synthetic read failure")):
                with self.assertRaisesRegex(OSError, "synthetic read failure"):
                    exp.build_dataset(root, io_output, baseline)

    def test_incompatible_baseline_fails_before_output_or_evaluation_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"
            root.mkdir()
            baseline = Path(temp) / "baseline"
            baseline.mkdir()
            artifact = {
                "model": object(), "epoch_seconds": 60,
                "feature_names": ["wrong"], "fit_splits": ["validation"],
                "research_only": False,
            }
            joblib.dump(artifact, baseline / "selected_model.joblib")
            base._write_json(
                baseline / "experiment_report.json",
                {"selected_artifact": {"sha256": exp.digest(baseline / "selected_model.joblib")}},
            )
            base._write_json(baseline / "manifest.json", {"subjects": []})
            output = Path(temp) / "result"

            with self.assertRaisesRegex(ValueError, "baseline ACT feature contract mismatch"):
                exp.run(root, output, baseline)

            self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
