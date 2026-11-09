from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from researchbench import participant_revalidation as rv


def sample_frame():
    rows = []
    for cohort in ('CMI', 'MESA'):
        for person in range(10):
            for day in range(3):
                rows.append({'cohort': cohort, 'subject_id': f'{cohort}_{person}',
                             'split': ('train', 'validation', 'test')[person % 3],
                             'cutoff': f'2026-01-{day+1:02d}T20:00:00+09:00',
                             'bed': f'2026-01-{day+1:02d}T23:00:00+09:00',
                             'wake': f'2026-01-{day+2:02d}T07:00:00+09:00',
                             'observed_at': f'2026-01-{day+2:02d}T07:00:00+09:00',
                             'bedtime_min': 180.+person, 'wake_min': 660.+person, 'duration_min': 480.})
    return pd.DataFrame(rows)


class IdentityTransform:
    def transform(self, x):
        return np.asarray(x, float)


class ConstantEstimator:
    def __init__(self, value):
        self.value = value

    def predict(self, x):
        if not len(x):
            raise AssertionError('Empty cohort passed to predict')
        return np.tile([self.value, self.value+480.], (len(x), 1))


class ParticipantRevalidationTests(unittest.TestCase):
    def test_participant_partition_is_reproducible_and_ignores_historical_split(self):
        frame = sample_frame()
        first = rv.fold_assignments(frame, 5, 2, 42)
        changed = frame.copy()
        changed['split'] = 'test'
        second = rv.fold_assignments(changed, 5, 2, 42)
        pd.testing.assert_frame_equal(first.drop(columns='historical_split'), second.drop(columns='historical_split'))
        self.assertEqual(len(first), 40)
        self.assertTrue(first.groupby(['repeat', 'cohort', 'subject_id']).size().eq(1).all())
        for repeat in range(2):
            held_indices = []
            for fold in range(5):
                train, held = rv.fold_frames(frame, first, repeat, fold)
                self.assertFalse(set(train.subject_id) & set(held.subject_id))
                self.assertEqual(set(train.split), {'train'})
                self.assertEqual(set(held.split), {'out_of_fold'})
                self.assertEqual(held.groupby('cohort').subject_id.nunique().tolist(), [2, 2])
                held_indices.extend(held.index)
            self.assertEqual(sorted(held_indices), list(frame.index))

    def test_repeat_aggregation_averages_absolute_losses_not_canceling_predictions(self):
        rows = []
        for repeat, prediction in ((0, 10.), (1, -10.)):
            for cohort in ('CMI', 'MESA'):
                for night in range(2 if cohort == 'CMI' else 1):
                    rows.append({'repeat': repeat, 'model': 'model', 'cohort': cohort, 'subject_id': cohort,
                                 'predicted_onset_min': prediction, 'predicted_wake_min': prediction,
                                 'actual_onset_min': 0., 'actual_wake_min': 0.})
        _, people = rv.participant_losses(pd.DataFrame(rows))
        self.assertEqual(len(people), 2)
        self.assertTrue(people.joint_mae_min.eq(10.).all())
        summary, _, _ = rv.summarize_losses(people, {'CMI': 2, 'MESA': 1}, 100, 7)
        macro = next(row for row in summary if row['cohort'] == 'MACRO')
        self.assertEqual(macro['n_participants'], 2)
        self.assertEqual(macro['n_nights'], 3)
        self.assertEqual(macro['joint_mae_min'], 10.)

    def test_held_targets_do_not_enter_fold_prior_or_preprocessor(self):
        frame = sample_frame()
        assignments = rv.fold_assignments(frame, 5, 1, 42)
        train, held = rv.fold_frames(frame, assignments, 0, 0)
        prior = rv.pt.prior_from_train(train)
        expected = train.groupby(['cohort', 'subject_id'])[rv.pt.TARGETS].mean().groupby('cohort').mean().mean()
        self.assertAlmostEqual(prior['bed_offset'], expected.bedtime_min)
        changed = frame.copy()
        changed.loc[held.index, rv.pt.TARGETS] = 1e12
        train2, _ = rv.fold_frames(changed, assignments, 0, 0)
        self.assertEqual(prior, rv.pt.prior_from_train(train2))
        a = rv.cc.WeightedPreprocessor().fit(train[rv.pt.TARGETS], sample_weight=rv.pt.balanced_weights(train))
        b = rv.cc.WeightedPreprocessor().fit(train2[rv.pt.TARGETS], sample_weight=rv.pt.balanced_weights(train2))
        np.testing.assert_array_equal(a.mean_, b.mean_)
        with self.assertRaisesRegex(ValueError, 'training rows only'):
            rv.fit_fold(held, 42)

    def test_holdout_overlap_is_rejected_and_no_macro_for_single_cohort(self):
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'holdout.csv'
            frame.loc[frame.cohort.eq('MESA')].to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                rv.load_untouched_holdout(path, frame)
        index = pd.MultiIndex.from_tuples([('model', 'MESA', 'm')], names=['model', 'cohort', 'subject_id'])
        people = pd.DataFrame({name: [1.] for name in rv.METRICS}, index=index)
        metrics, _, _ = rv.summarize_losses(people, {'MESA': 1}, 100, 42)
        self.assertEqual([row['cohort'] for row in metrics], ['MESA'])

    def test_single_cohort_holdout_predicts_all_17_arms_with_correct_sources(self):
        frame = sample_frame().loc[lambda x: x.cohort.eq('MESA')].copy()
        priors = {'CMI': {'bed_offset': 100., 'wake_offset': 580.},
                  'MESA': {'bed_offset': 200., 'wake_offset': 680.}}
        bundles = {}
        for descriptors in ('data', 'hybrid'):
            for strategy in rv.cc.STRATEGIES:
                separate = strategy.startswith('separate')
                models = {c: ConstantEstimator(100. if c == 'CMI' else 200.) for c in rv.pt.COHORTS} if separate else {'ALL': ConstantEstimator(150.)}
                local = strategy == 'separate'
                bundles[strategy+'_'+descriptors] = {
                    'strategy': strategy, 'models': models, 'features': ['median14_bed'],
                    'source_encoding': list(rv.pt.COHORTS) if strategy == 'pooled_source' else [],
                    'preprocessors': {c: IdentityTransform() for c in rv.pt.COHORTS} if local else {'ALL': IdentityTransform()},
                    'priors': priors if local else {'ALL': {'bed_offset': 150., 'wake_offset': 630.}},
                }
        predictions, counts, recent = rv.evaluate_fold(frame, bundles)
        self.assertEqual(set(predictions), set(rv.MODELS))
        np.testing.assert_array_equal(predictions['cmi_data'][:, 0], 100.)
        np.testing.assert_array_equal(predictions['mesa_data'][:, 0], 200.)
        np.testing.assert_array_equal(predictions['mean50_data'][:, 0], 150.)
        np.testing.assert_array_equal(predictions['separate_shared_data'][:, 0], 200.)
        np.testing.assert_array_equal(predictions['recent_median'][np.asarray(recent) > 0], predictions['recent_median_cmi'][np.asarray(recent) > 0])

    def test_procedure_lock_rejects_code_config_and_fold_mutations(self):
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, audit = root/'db', root/'audit.json'
            database.write_text('fixed input', encoding='utf-8')
            audit.write_text('{}', encoding='utf-8')
            config = {'database_path': str(database), 'output_dir': str(root/'run'),
                      'participant_audit_path': str(audit), 'cv_folds': 2, 'cv_repeats': 1, 'bootstrap_replicates': 100}
            manifest = {'summary': [], 'time_contract': {}}
            with patch.object(rv.pt, 'load_database', return_value=(frame, manifest, rv.pt.sha256(database))):
                rv.prepare_revalidation(config)
            rv.verify_lock(config)
            with self.assertRaisesRegex(ValueError, 'settings differ'):
                rv.verify_lock(dict(config, cv_folds=3))
            with patch.object(rv, 'code_hashes', return_value={}):
                with self.assertRaisesRegex(ValueError, 'code changed'):
                    rv.verify_lock(config)
            path = root/'run'/'private_fold_assignments.csv'
            path.write_text(path.read_text()+'\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Prefit lock changed'):
                rv.verify_lock(config)

    def test_all_fold_and_full_model_sets_are_locked_before_any_evaluation(self):
        frame = sample_frame()
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, audit, holdout_path, builder = root/'db', root/'audit.json', root/'holdout.csv', root/'extractor.py'
            database.write_text('fixed input', encoding='utf-8')
            builder.write_text('fixed extractor', encoding='utf-8')
            rv.pt.write_json(audit, {'locked_input_sha256': {str(builder): rv.pt.sha256(builder)}})
            holdout = frame.loc[frame.cohort.eq('MESA') & frame.subject_id.eq('MESA_0')].copy()
            holdout['subject_id'] = 'MESA_unused'
            holdout.to_csv(holdout_path, index=False)
            config = {'database_path': str(database), 'output_dir': str(root/'run'), 'participant_audit_path': str(audit),
                      'untouched_holdout_path': str(holdout_path), 'cv_folds': 2, 'cv_repeats': 1, 'bootstrap_replicates': 100}
            manifest = {'summary': [], 'time_contract': {}}

            def fake_fit(train, seed, progress=None, cancelled=None):
                self.assertNotIn('MESA_unused', set(train.subject_id))
                events.append(('fit', len(train)))
                return {'fit_count': len(train)}

            def fake_evaluate(held, bundles, progress=None, cancelled=None):
                lock = json.loads((root/'run'/'all_fold_models_lock.json').read_text())
                self.assertEqual(len(lock['models_sha256']), 3)
                self.assertEqual(len([e for e in events if e[0] == 'fit']), 3)
                for filename, digest in lock['models_sha256'].items():
                    self.assertEqual(rv.pt.sha256(root/'run'/'fold_models'/filename), digest)
                events.append(('evaluate', len(held)))
                return {m: held[rv.pt.TARGETS].to_numpy()+10. for m in rv.MODELS}, [0]*len(held), np.zeros(len(held), int)

            with patch.object(rv.pt, 'load_database', return_value=(frame, manifest, rv.pt.sha256(database))):
                rv.prepare_revalidation(config)
                with patch.object(rv, 'fit_fold', side_effect=fake_fit), patch.object(rv, 'evaluate_fold', side_effect=fake_evaluate):
                    result = rv.run_participant_revalidation(config)
            self.assertEqual([e[0] for e in events], ['fit', 'fit', 'fit', 'evaluate', 'evaluate', 'evaluate'])
            self.assertEqual(events[2][1], len(frame))
            self.assertEqual(len(result['summary']), 51)
            self.assertEqual(len(result['raw']['untouched_holdout_metrics']), 17)
            self.assertEqual({r['cohort'] for r in result['raw']['untouched_holdout_metrics']}, {'MESA'})
            self.assertEqual({r['split'] for r in result['raw']['untouched_holdout_metrics']}, {'unused_mesa_holdout'})
            self.assertEqual({r['averaging'] for r in result['raw']['untouched_holdout_metrics']}, {'equal_participant'})
            # A second invocation must never overwrite the fitted/scored run.
            with self.assertRaisesRegex(FileExistsError, 'already started'):
                rv.run_participant_revalidation(config)
            holdout_path.write_text(holdout_path.read_text()+'\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Holdout changed'):
                rv.verify_lock(config)
            holdout.to_csv(holdout_path, index=False)
            builder.write_text('changed extractor', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'extractor changed'):
                rv.verify_lock(config)


if __name__ == '__main__':
    unittest.main()
