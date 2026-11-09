import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

import mesa_psg_alignment as alignment


ACT_HEADER = [
    "mesaid", "line", "linetime", "offwrist", "activity", "whitelight",
    "redlight", "greenlight", "bluelight", "wake", "interval",
]


def clock(seconds):
    seconds %= 86400
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def write_act(path, participant="1", count=40, start_clock=23 * 3600 + 55 * 60,
              changes=None):
    changes = changes or {}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ACT_HEADER)
        for line in range(1, count + 1):
            row = {
                "mesaid": participant,
                "line": line,
                "linetime": clock(start_clock + (line - 1) * 30),
                "offwrist": "0",
                "activity": str(line),
                "whitelight": "1",
                "redlight": "2",
                "greenlight": "3",
                "bluelight": "4",
                "wake": "0" if line % 2 else "1",
                "interval": "IGNORED",
            }
            row.update(changes.get(line, {}))
            writer.writerow([row[name] for name in ACT_HEADER])


def write_xml(path, events, epoch=30, recording_clock="00:00:00", recording_duration=None,
              include_recording=True):
    if recording_duration is None:
        recording_duration = max((start + duration for _, start, duration in events), default=30)
    recording = ""
    if include_recording:
        recording = (
            "<ScoredEvent><EventType/><EventConcept>Recording Start Time</EventConcept>"
            f"<Start>0</Start><Duration>{recording_duration}</Duration>"
            f"<ClockTime>01.01.85 {recording_clock.replace(':', '.')}</ClockTime></ScoredEvent>"
        )
    body = "".join(
        "<ScoredEvent><EventType>Stages|Stages</EventType>"
        f"<EventConcept>{concept}</EventConcept><Start>{start}</Start>"
        f"<Duration>{duration}</Duration></ScoredEvent>"
        for concept, start, duration in events
    )
    path.write_text(
        f"<?xml version='1.0'?><PSGAnnotation><EpochLength>{epoch}</EpochLength>"
        f"<ScoredEvents>{recording}{body}</ScoredEvents></PSGAnnotation>",
        encoding="utf-8",
    )


class MesaPsgAlignmentTest(unittest.TestCase):
    def test_read_overlap_normalizes_and_rejects_malformed_or_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "overlap.csv"
            path.write_text(
                "mesaid,line,linetime,starttime_psg\n0001,3,23:56:00,23:55:59\n",
                encoding="utf-8",
            )
            self.assertEqual(alignment.read_overlap(path)["1"]["line"], 3)
            path.write_text(
                "mesaid,line,linetime,starttime_psg\n1,3,23:56:00,23:55:59\n"
                "0001,4,23:56:30,23:55:59\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate overlap subject"):
                alignment.read_overlap(path)
            path.write_text("mesaid,line,linetime\n1,nope,23:56:00\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "columns"):
                alignment.read_overlap(path)

    def test_parse_stages_maps_binary_labels_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml = root / "stages.xml"
            write_xml(xml, [("Stage Wake|0", 0, 30), ("Stage 4 sleep|4", 30, 30), ("REM sleep|5", 60, 30)])
            intervals, metadata = alignment.parse_stages(xml)
            self.assertEqual([item[2] for item in intervals], [0, 1, 1])
            self.assertEqual(metadata["epoch_length_seconds"], 30)
            self.assertEqual(len(metadata["source_sha256"]), 64)

            write_xml(xml, [("Wake|0", 0, 31), ("Stage 1 sleep|1", 30, 30)])
            with self.assertRaisesRegex(ValueError, "30-second epochs"):
                alignment.parse_stages(xml)
            write_xml(xml, [("Wake|0", 0, 60), ("Stage 1 sleep|1", 30, 30)])
            with self.assertRaisesRegex(ValueError, "overlapping"):
                alignment.parse_stages(xml)
            write_xml(xml, [("Wake|0", 0, 30)], epoch=20)
            with self.assertRaisesRegex(ValueError, "must be 30"):
                alignment.parse_stages(xml)
            xml.write_text("<broken>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid NSRR XML"):
                alignment.parse_stages(xml)

    def test_recording_start_is_required_and_annotations_cannot_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            xml = Path(temporary) / "stages.xml"
            write_xml(xml, [("Wake|0", 0, 30)], include_recording=False)
            with self.assertRaisesRegex(ValueError, "Recording Start Time"):
                alignment.parse_stages(xml)
            write_xml(xml, [("Wake|0", 0, 60)], recording_duration=29)
            with self.assertRaisesRegex(ValueError, "beyond recording duration"):
                alignment.parse_stages(xml)

    def test_midnight_anchor_alignment_and_actigraphy_labels_do_not_drive_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            act = root / "mesa-sleep-0001.csv"
            xml = root / "mesa-sleep-0001-nsrr.xml"
            write_act(act, changes={line: {"wake": "1"} for line in range(1, 41)})
            write_xml(
                xml,
                [("Wake|0", 0, 600), ("Stage 2 sleep|2", 600, 600)],
                recording_clock="23:54:59",
            )
            row = {"mesaid": "1", "line": 1, "linetime": "23:55:00", "starttime_psg": "23:54:59"}
            arrays, qc, hashes = alignment.align_subject(act, xml, row, "0001")
            self.assertEqual(arrays["line"].tolist(), list(range(1, 41)))
            self.assertEqual(arrays["t_rel_sec"][[0, -1]].tolist(), [0, 1170])
            self.assertEqual(arrays["y_sleep"].tolist(), [0] * 20 + [1] * 20)
            self.assertEqual(arrays["y_actigraphy"].tolist(), [0] * 40)
            self.assertEqual(arrays["x"].shape, (40, 5))
            self.assertEqual(qc["psg_anchor_offset_seconds"], 1)
            self.assertEqual(qc["eligible_ten_minute_blocks"], 2)
            self.assertEqual(qc["psg_scored_rows"], 40)
            self.assertEqual(qc["recording_duration_seconds"], 1200)
            self.assertEqual(qc["annotated_span_seconds"], 1200)
            self.assertEqual(set(hashes), {"actigraphy_sha256", "xml_sha256", "overlap_row_sha256"})
            self.assertTrue(all(len(value) == 64 for value in hashes.values()))

    def test_duration_majority_and_exact_tie_boundary(self):
        intervals = [(0.0, 15.0, 0), (15.0, 30.0, 1)]
        self.assertEqual(alignment._label_interval(0.0, intervals, [0.0, 15.0]), (None, "psg_duration_tie"))
        intervals = [(0.0, 20.0, 1), (20.0, 30.0, 0)]
        self.assertEqual(alignment._label_interval(0.0, intervals, [0.0, 20.0]), (1, None))

    def test_unknown_and_missing_coverage_exclude_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            act = root / "mesa-sleep-0001.csv"
            xml = root / "mesa-sleep-0001-nsrr.xml"
            write_act(act, count=80, start_clock=0)
            # First block is valid; line 21 contains an unknown stage, and the
            # final block has no PSG coverage.
            events = [
                ("Stage 1 sleep|1", 0, 600), ("Unscored|9", 600, 30),
                ("Wake|0", 630, 1170),
            ]
            write_xml(xml, events)
            row = {"mesaid": "1", "line": 1, "linetime": "00:00:00", "starttime_psg": "00:00:00"}
            arrays, qc, _ = alignment.align_subject(act, xml, row, "1")
            self.assertEqual(len(arrays["line"]), 40)
            self.assertEqual(arrays["line"].tolist(), list(range(1, 21)) + list(range(41, 61)))
            self.assertEqual(arrays["y_sleep"][0], 1)
            self.assertGreaterEqual(qc["unknown_psg_stage_rows"], 1)
            self.assertGreaterEqual(qc["incomplete_psg_coverage_rows"], 1)
            self.assertEqual(qc["excluded_incomplete_or_invalid_ten_minute_blocks"], 2)

    def test_clock_residuals_do_not_shift_epoch_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            act = root / "mesa-sleep-0001.csv"
            xml = root / "mesa-sleep-0001-nsrr.xml"
            write_act(act, count=20, start_clock=0)
            write_xml(xml, [("Wake|0", 0, 30), ("Stage 2 sleep|2", 30, 570)])
            expected = None
            for residual in (-1, 0, 1, 14):
                starttime = clock(-residual)
                write_xml(
                    xml,
                    [("Wake|0", 0, 30), ("Stage 2 sleep|2", 30, 570)],
                    recording_clock=starttime,
                )
                row = {"mesaid": "1", "line": 1, "linetime": "00:00:00", "starttime_psg": starttime}
                arrays, qc, _ = alignment.align_subject(act, xml, row, "1")
                labels = arrays["y_sleep"].tolist()
                if expected is None:
                    expected = labels
                self.assertEqual(labels, expected)
                self.assertEqual(qc["psg_anchor_offset_seconds"], residual)
            self.assertEqual(expected, [0] + [1] * 19)

    def test_offwrist_bad_sensor_and_incomplete_block_are_removed_as_full_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            act = root / "mesa-sleep-0001.csv"
            xml = root / "mesa-sleep-0001-nsrr.xml"
            write_act(act, count=39, start_clock=0, changes={2: {"offwrist": "1"}, 22: {"activity": "-1"}})
            write_xml(xml, [("Stage 2 sleep|2", 0, 1200)])
            row = {"mesaid": "1", "line": 1, "linetime": "00:00:00", "starttime_psg": "00:00:00"}
            arrays, qc, _ = alignment.align_subject(act, xml, row, "1")
            self.assertEqual(len(arrays["line"]), 0)
            self.assertEqual(qc["offwrist_rows"], 1)
            self.assertEqual(qc["invalid_sensor_rows"], 1)
            self.assertEqual(qc["excluded_incomplete_or_invalid_ten_minute_blocks"], 2)

    def test_anchor_participant_cadence_and_filename_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            act = root / "mesa-sleep-0001.csv"
            xml = root / "mesa-sleep-0001-nsrr.xml"
            write_act(act, start_clock=0)
            write_xml(xml, [("Wake|0", 0, 1200)])
            base = {"mesaid": "1", "line": 1, "linetime": "00:00:00", "starttime_psg": "00:00:00"}
            with self.assertRaisesRegex(ValueError, "overlap participant"):
                alignment.align_subject(act, xml, {**base, "mesaid": "2"}, "1")
            with self.assertRaisesRegex(ValueError, "anchor line"):
                alignment.align_subject(act, xml, {**base, "line": 999}, "1")
            with self.assertRaisesRegex(ValueError, "exactly match"):
                alignment.align_subject(act, xml, {**base, "linetime": "00:00:01"}, "1")
            with self.assertRaisesRegex(ValueError, "PSG start"):
                alignment.align_subject(act, xml, {**base, "starttime_psg": "23:59:44"}, "1")

            write_xml(xml, [("Wake|0", 0, 1200)], recording_clock="00:00:01")
            with self.assertRaisesRegex(ValueError, "does not match overlap"):
                alignment.align_subject(act, xml, base, "1")

            write_xml(xml, [("Wake|0", 0, 1200)])
            write_act(act, start_clock=0, changes={3: {"linetime": "00:02:00"}})
            with self.assertRaisesRegex(ValueError, "cadence"):
                alignment.align_subject(act, xml, base, "1")

            other = root / "mesa-sleep-0002.csv"
            write_act(other, participant="1", start_clock=0)
            with self.assertRaisesRegex(ValueError, "filename participant"):
                alignment.align_subject(other, xml, base, "1")


if __name__ == "__main__":
    unittest.main()
