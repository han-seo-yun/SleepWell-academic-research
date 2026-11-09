"""Independent read-only checks; aggregate output contains no participant tokens."""
if not __debug__:
    raise RuntimeError('Verification requires assertions enabled; do not run with -O or PYTHONOPTIMIZE.')

import json
from pathlib import Path
import sys

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

REPO = Path(r'E:\학술제\SleepWell-integrated')
sys.path.insert(0, str(REPO/'tools/model'))
import mesa_experiments as base
import mesa_psg_alignment as alignment
import mesa_psg_experiments as exp
from verify_research_run import verify_mesa

ROOT = Path(r'E:\학술제\데이터셋\MESA')
RUN = Path(r'E:\학술제\연구용데이터\mesa_psg_run_20261020_v2')
OLD = Path(r'E:\학술제\연구용데이터\mesa_run_20261010')
OUT = Path(__file__).parent/'verification.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    m, r, p = [read(RUN/name) for name in ['manifest.json','experiment_report.json','protocol.json']]
    assert exp.digest(RUN/'protocol.json') == r['protocol_sha256']
    for name, expected in p['source_code_sha256'].items():
        assert exp.digest(REPO/'tools/model'/name) == expected
    for name, field in [('manifest.json','baseline_ACT_manifest_sha256'),('experiment_report.json','baseline_report_sha256')]:
        assert exp.digest(OLD/name) == p[field]
    assert exp.digest(ROOT/'overlap/mesa-actigraphy-psg-overlap.csv') == m['overlap_sha256']
    oldsplit = {s['token']: s['split'] for s in read(OLD/'manifest.json')['subjects']}
    act, xml = exp.discover(ROOT)
    overlap = alignment.read_overlap(ROOT/'overlap/mesa-actigraphy-psg-overlap.csv')
    ids = {base.subject_token(s): s for s in set(act)&set(xml)&set(overlap)}
    assert len(ids) == m['inventory']['matched_candidates']
    assert set(ids) == {s['token'] for s in m['subjects']} | {s['token'] for s in m['excluded_private']}
    for s in m['excluded_private']:
        participant = ids[s['token']]
        if s['reason'] == 'empty_psg_xml':
            assert xml[participant].stat().st_size == s['source_snapshot']['xml_size'] == 0
            assert exp.digest(xml[participant]) == s['source_sha256']['xml_sha256']
        else:
            assert s['reason'] == 'no_complete_eligible_ten_minute_blocks'
            arrays,qc,_ = alignment.align_subject(act[participant],xml[participant],overlap[participant],participant)
            assert len(arrays['line']) == 0 and qc == s['qc']
    total_rows, psg_checked, files_checked = 0, 0, 0
    for i,s in enumerate(m['subjects'],1):
        token = s['token']; participant = ids[token]
        assert s['split'] == base.subject_split(participant)
        if token in oldsplit: assert s['split'] == oldsplit[token]
        archive = RUN/'subjects'/f'{token}.npz'
        assert exp.digest(archive) == s['output_sha256']
        for path,key in [(act[participant],'actigraphy_sha256'),(xml[participant],'xml_sha256')]:
            assert exp.digest(path) == s['source_sha256'][key]
            files_checked += 1
        arr = base._load_subject(archive)
        assert len(arr['line']) == s['eligible_native_rows']
        assert np.isfinite(arr['x']).all() and (arr['x'] >= 0).all()
        assert np.isin(arr['y_sleep'],[0,1]).all()
        assert (np.diff(arr['line']) > 0).all()
        assert np.array_equal(np.diff(arr['t_rel_sec']), np.diff(arr['line']).astype(np.int64)*30)
        blocks = arr['line'].reshape(-1,20)
        assert ((blocks[:,0]-1)%20 == 0).all() and (np.diff(blocks,axis=1)==1).all()
        intervals, meta = alignment.parse_stages(xml[participant])
        truth = np.full(int(max(end for _,end,_ in intervals)/30),255,dtype=np.uint8)
        for start,end,label in intervals:
            if label is not None: truth[int(start/30):int(end/30)] = label
        positions = arr['line'].astype(np.int64)-overlap[participant]['line']
        assert (positions>=0).all() and (positions<len(truth)).all()
        assert np.array_equal(truth[positions],arr['y_sleep'])
        psg_checked += len(positions); total_rows += len(positions)
        if i%200 == 0: print(json.dumps({'verification_subjects':i}),flush=True)
    assert total_rows == sum(m['rows_by_split'].values())
    standard = verify_mesa(RUN)
    chosen = r['selected_before_test']
    assert chosen == read(RUN/'selected.json')['selected_before_test'] == base._select(r['validation'])
    assert p['frozen_at_utc'] <= read(RUN/'selected.json')['selected_at_utc'] <= r['completed_at_utc']
    artifact = joblib.load(RUN/'selected_model.joblib')
    old, _ = exp.validate_baseline(OLD,p['baseline_ACT_model_sha256'])
    candidate = next(c for c in base.CANDIDATES if c['name']==artifact['candidate'])
    old_total = base.MetricAccumulator(); new_total = base.MetricAccumulator()
    selected_scores, old_scores = [],[]
    reproduced_rows, reproduced_subjects = 0,0
    for s in (s for s in m['subjects'] if s['split']=='test'):
        arr=base._load_subject(RUN/'subjects'/f"{s['token']}.npz")
        with np.load(RUN/'evaluation/test'/f"{s['token']}.npz",allow_pickle=False) as saved:
            assert np.array_equal(saved['line'],arr['line'])
            assert np.array_equal(saved['y_sleep'],arr['y_sleep'])
            epoch=base.aggregate_epoch(arr,artifact['epoch_seconds'])
            pred=artifact['model'].predict(base._candidate_x(epoch['x'],candidate)).astype(np.uint8)
            projected=np.repeat(pred,artifact['epoch_seconds']//30)
            assert np.array_equal(projected,saved['prediction'][saved['candidate'].tolist().index(chosen)])
            baseline_pred=old['model'].predict(arr['x']).astype(np.uint8)
            new_total.add(arr['y_sleep'],projected); old_total.add(arr['y_sleep'],baseline_pred)
            reproduced_rows+=len(projected); reproduced_subjects+=1
    assert new_total.report()==r['test_all_frozen_candidates_descriptive'][chosen]['projected_shared_native_30s_grid']
    assert old_total.report()==r['frozen_ACT_model_on_same_PSG_test']
    paired=base._paired_bootstrap(new_total.subject_balanced_accuracy,old_total.subject_balanced_accuracy)
    assert paired==r['PSG_selected_minus_frozen_ACT_paired_subject_bootstrap']
    result={'status':'passed','standard_verification':standard,'subjects_archives_verified':len(m['subjects']),
            'raw_source_hashes_verified':files_checked,'retained_PSG_labels_verified':psg_checked,
            'selected_model_test_subjects_reproduced':reproduced_subjects,
            'selected_model_test_predictions_reproduced':reproduced_rows,'prediction_mismatches':0,
            'prior_participant_splits_preserved':True,'baseline_metrics_and_paired_bootstrap_reproduced':True,
            'all_exclusion_reasons_rechecked':True,
            'protocol_and_source_hashes_matched':True,'verified_at_utc':base._utc_now()}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    with threadpool_limits(limits=2): main()
