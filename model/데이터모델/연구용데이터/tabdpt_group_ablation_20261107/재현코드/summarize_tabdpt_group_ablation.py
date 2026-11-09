"""Audit predictions, paired participant uncertainty, and presentation exports."""
from pathlib import Path
import hashlib, json, sys, platform
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

BASE = Path('E:/학술제/데이터모델/연구용데이터/tabdpt_group_ablation_20261107')
PRIMARY = BASE / 'train_refit_primary'
SRC = Path('E:/학술제/회의/Workbench_보고서_2026-11-06/deep_learning_comparison_20261105/private')
OUT = BASE / '발표자료'
SEED = 20261107
B = 5000
sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
read = lambda path: json.loads(Path(path).read_text(encoding='utf-8'))

def analyze(folder, sensitivity=False):
    protocol = read(folder / 'protocol.json')
    groups = protocol['groups']
    rows, audits, boot_means = [], [], {}
    for cohort_no, cohort in enumerate(['cmi','mesa']):
        with np.load(SRC/f'{cohort}.npz', allow_pickle=False) as source:
            m = source['splits'] == 'validation'
            expected_rows = source['row_ids'][m]
            expected_ids = source['groups'][m]
            expected_y = source['y'][m]
        with np.load(folder/'private'/f'{cohort}_all33.npz', allow_pickle=False) as baseline:
            ids = baseline['participant_id']
            base_error = np.abs(baseline['prediction'] - baseline['actual'])
        unique, inverse = np.unique(ids, return_inverse=True)
        counts = np.bincount(inverse)
        aggregate = lambda error: np.column_stack([np.bincount(inverse, weights=error[:,c])/counts for c in range(2)])
        base_person = aggregate(base_error)
        rng = np.random.default_rng(SEED + cohort_no)
        draws = rng.integers(0, len(unique), size=(B, len(unique)))
        for i in range(7):
            variant = 'all33' if i == 0 else f'drop_{i}'
            path = folder/'private'/f'{cohort}_{variant}.npz'
            meta = read(folder/f'{cohort}_{variant}_metadata.json')
            assert sha(path) == meta['sha256']
            with np.load(path, allow_pickle=False) as z:
                pred, actual = z['prediction'], z['actual']
                np.testing.assert_array_equal(z['row_id'], expected_rows)
                np.testing.assert_array_equal(z['participant_id'], expected_ids)
                np.testing.assert_allclose(actual, expected_y, atol=0)
                assert pred.shape == actual.shape == (len(expected_rows), 2)
                assert np.isfinite(pred).all()
                assert ((pred[:,0] >= 0) & (pred[:,0] <= 960)).all()
                assert ((pred[:,1] >= pred[:,0]+30) & (pred[:,1] <= np.minimum(1440, pred[:,0]+960))).all()
                err = abs(pred-actual)
            person = aggregate(err)
            crosscheck = pd.DataFrame({'id': ids, 'bed': err[:,0], 'wake': err[:,1]}).groupby('id')[['bed','wake']].mean().to_numpy()
            np.testing.assert_allclose(person, crosscheck, atol=1e-12)
            delta_person = (person-base_person).mean(axis=1)
            bootstrap = delta_person[draws].mean(axis=1)
            boot_means[(cohort, variant)] = bootstrap
            low, high = np.quantile(bootstrap, [.025, .975])
            adjusted_low, adjusted_high = np.quantile(bootstrap, [.05/24, 1-.05/24])
            bed, wake = person.mean(axis=0)
            db, dw = (person-base_person).mean(axis=0)
            row = {'cohort': cohort.upper(), 'variant': variant,
                   'removed_group': '전체 33개 (기준)' if i == 0 else groups[i-1]['group'],
                   'removed_raw_features': 0 if i == 0 else len(groups[i-1]['features']),
                   'remaining_raw_features': meta['remaining_raw'], 'effective_features': meta['effective'],
                   'n_participants': len(unique), 'n_nights': len(ids),
                   'bed_mae_min': float(bed), 'wake_mae_min': float(wake), 'joint_mae_min': float((bed+wake)/2),
                   'delta_bed_min': float(db), 'delta_wake_min': float(dw), 'delta_joint_min': float(delta_person.mean()),
                   'delta_95_low': float(low), 'delta_95_high': float(high),
                   'delta_bonf12_low': float(adjusted_low), 'delta_bonf12_high': float(adjusted_high),
                   'night_weighted_joint_mae_min': float(err.mean())}
            rows.append(row)
            audits.append({'cohort': cohort, 'variant': variant, 'prediction_sha256': meta['sha256'],
                           'row_alignment': True, 'finite_predictions': True, 'constraints': True,
                           'metric_independent_crosscheck': True})
    df = pd.DataFrame(rows)
    macro = []
    for variant in df.variant.unique():
        part = df[df.variant.eq(variant)]
        bs = (boot_means[('cmi',variant)] + boot_means[('mesa',variant)])/2
        lo, hi = np.quantile(bs, [.025, .975])
        macro.append({'variant':variant, 'removed_group': part.iloc[0].removed_group,
                      'joint_mae_min':float(part.joint_mae_min.mean()), 'delta_joint_min': float(part.delta_joint_min.mean()),
                      'delta_95_low':float(lo), 'delta_95_high':float(hi)})
    prefix = 'frozen_민감도' if sensitivity else '주분석'
    df.to_csv(BASE/f'{prefix}_피처제거_성능.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(macro).to_csv(BASE/f'{prefix}_코호트동일가중_성능.csv', index=False, encoding='utf-8-sig')
    return df, audits

def graphics(df):
    OUT.mkdir(exist_ok=True)
    font_manager.fontManager.addfont('C:/Windows/Fonts/malgun.ttf')
    plt.rcParams.update({'font.family':'Malgun Gothic', 'axes.unicode_minus':False,
                         'font.size':15, 'axes.titleweight':'bold', 'savefig.facecolor':'white'})
    groups = read(PRIMARY/'protocol.json')['groups']
    labels = [f"{g['group']} ({len(g['features'])}개)" for g in groups]
    fig, axes = plt.subplots(1,2,figsize=(16,9),dpi=240)
    fig.subplots_adjust(left=.19,right=.97,bottom=.265,top=.73,wspace=.22)
    limits = df[df.variant.ne('all33')][['delta_95_low','delta_95_high']].to_numpy()
    lower, upper = min(-1, limits.min()), max(1, limits.max())
    margin = (upper-lower)*.15
    for j,(ax,cohort) in enumerate(zip(axes,['CMI','MESA'])):
        sub = df[df.cohort.eq(cohort)]
        base = sub[sub.variant.eq('all33')].iloc[0]
        removed = sub[sub.variant.ne('all33')]
        ax.axvline(0,color='#253449',linewidth=1.6,zorder=1)
        for y,(_,r) in enumerate(removed.iterrows()):
            color = '#24679B' if r.delta_joint_min >= 0 else '#AA6C33'
            ax.errorbar(r.delta_joint_min,y,xerr=[[r.delta_joint_min-r.delta_95_low],[r.delta_95_high-r.delta_joint_min]],
                        fmt='o',color=color,ecolor=color,capsize=5,markersize=9,elinewidth=2.2,zorder=3)
            ax.annotate(f'{r.delta_joint_min:+.2f}',(r.delta_joint_min,y),xytext=(0,-24),
                        textcoords='offset points',ha='center',color=color,fontsize=13,fontweight='bold')
        ax.set_ylim(5.65,-.65); ax.set_xlim(lower-margin,upper+margin)
        ax.set_yticks(range(6),labels if j == 0 else ['']*6)
        ax.tick_params(axis='y',length=0,pad=12)
        ax.grid(axis='x',color='#DDE3EA',linewidth=.7)
        ax.set_axisbelow(True)
        for side in ['top','right','left']: ax.spines[side].set_visible(False)
        ax.spines['bottom'].set_color('#B6C1CF')
        ax.set_xlabel('전체 33개 대비 MAE 변화 (분)',labelpad=14,fontsize=15)
        ax.set_title(f'{cohort}\n전체 33개 MAE {base.joint_mae_min:.2f}분',fontsize=20,pad=23)
    fig.text(.055,.92,'어떤 피처 묶음이 예측에 기여했을까?',fontsize=29,fontweight='bold',color='#182E48')
    fig.text(.055,.865,'TabDPT · 피처 묶음을 하나씩 제거한 뒤 Validation 오차 비교',fontsize=18,color='#53677C')
    fig.text(.055,.142,'+ 제거 후 오차 증가 / - 오차 감소  |  구간이 0을 지나면 차이가 뚜렷하지 않음',fontsize=16,color='#203B58')
    fig.text(.055,.09,'점: 참여자 동일 가중의 취침·기상 평균 MAE 차이  |  선: 참여자 단위 bootstrap 95% 구간',fontsize=13,color='#5E6D7D')
    fig.text(.055,.054,'CMI 39명·691밤 / MESA 299명·2,062밤  ·  Train 전처리 재적합  ·  전체 기준: 상수 2개 제외, 실제 입력 31개',fontsize=12,color='#5E6D7D')
    fig.savefig(OUT/'CMI_MESA_TabDPT_피처묶음_기여도.png'); plt.close(fig)

    fig,axes = plt.subplots(1,2,figsize=(16,9),dpi=240)
    fig.subplots_adjust(left=.19,right=.96,bottom=.24,top=.76,wspace=.20)
    maxval = df.joint_mae_min.max()*1.22
    for j,(ax,cohort) in enumerate(zip(axes,['CMI','MESA'])):
        sub = df[df.cohort.eq(cohort)]
        vals = sub.joint_mae_min.to_numpy()
        ax.barh(range(7),vals,color=['#153F68']+['#B7D4E7']*6,height=.57)
        for y,v in enumerate(vals): ax.text(v+.5,y,f'{v:.2f}',va='center',fontsize=16,fontweight='bold' if y == 0 else 'normal')
        ax.set_yticks(range(7),(['전체 33개 (기준)']+[f"{g['group']} 제거" for g in groups]) if j==0 else ['']*7)
        ax.set_ylim(6.7,-.7); ax.set_xlim(0,maxval);ax.tick_params(axis='y',length=0,pad=12)
        ax.set_xlabel('취침·기상 평균 MAE (분) · 낮을수록 좋음',fontsize=14,labelpad=15)
        ax.set_title(cohort,fontsize=24,pad=20);ax.grid(axis='x',color='#E4E9EF');ax.set_axisbelow(True)
        for side in ['top','right','left']:ax.spines[side].set_visible(False)
        ax.spines['bottom'].set_color('#BCC7D1')
    fig.text(.055,.915,'전체 33개와 피처 묶음 제거 조건 비교',fontsize=29,fontweight='bold',color='#182E48')
    fig.text(.055,.86,'동일한 TabDPT checkpoint·Train 문맥·seed·ensemble 조건',fontsize=18,color='#53677C')
    fig.text(.055,.105,'참여자 동일 가중 · CMI 39명·691밤 / MESA 299명·2,062밤 · Validation 평가',fontsize=14,color='#52677C')
    fig.text(.055,.06,'전체 33개 중 상수 2개 제외(실제 입력 31개) · 작은 차이는 기여도 그래프의 불확실성 구간과 함께 해석',fontsize=12,color='#52677C')
    fig.savefig(OUT/'CMI_MESA_TabDPT_전체33개_제거조건_MAE.png');plt.close(fig)

def report(df,frozen,audits):
    protocol = read(PRIMARY/'protocol.json')
    lines = ['# TabDPT DATA 33 피처 묶음 제거 실험', '',
             '실제 CMI·MESA Train/Validation으로 실행한 탐색적 분석이다. 주 결과는 현재 Train 자료에 전처리를 다시 적합한 결과이며, 과거 전처리를 고정한 결과는 별도 민감도 분석으로 보존했다.', '',
             '## 비교 결과', '',
             'MAE는 참여자별로 취침·기상 절대오차를 평균한 뒤 참여자들을 동일하게 가중한 값(분)이다. ΔMAE = 제거 조건 − 전체 33개. 양수면 제거 시 악화, 음수면 제거 시 개선이다.', '',
             '| 제거한 묶음 | CMI MAE | CMI ΔMAE [95% 구간] | MESA MAE | MESA ΔMAE [95% 구간] |',
             '|---|---:|---:|---:|---:|']
    for variant in df.variant.unique():
        c = df[(df.cohort=='CMI') & (df.variant==variant)].iloc[0]
        m = df[(df.cohort=='MESA') & (df.variant==variant)].iloc[0]
        f = lambda r: '기준' if variant=='all33' else f"{r.delta_joint_min:+.2f} [{r.delta_95_low:+.2f}, {r.delta_95_high:+.2f}]"
        lines.append(f'| {c.removed_group} | {c.joint_mae_min:.2f} | {f(c)} | {m.joint_mae_min:.2f} | {f(m)} |')
    lines += ['', '## 보수적인 해석', '',
              '- 이 결과는 나머지 피처들이 있는 상태에서 해당 묶음을 제외한 추가 효과다. 개별 피처의 중요도나 수면에 대한 인과 효과는 아니다.',
              '- 7개/14개 기록 요약, 평소 시각, 변동성 등은 정보가 겹친다. 제거해도 변화가 작다고 정보 자체가 불필요하다는 뜻은 아니다.',
              '- 표의 95% 구간은 각각의 비교에 대한 구간이다. 12개(CMI/MESA × 6묶음)를 동시에 해석할 때는 CSV의 Bonferroni 보정 99.5833% 구간을 확인해야 한다.',
              '- 구간은 참여자를 다시 추출하는 paired bootstrap 5,000회다. 고정된 Train 자료와 checkpoint·seed에 조건부이며, 훈련 집합/seed 변화에 따른 불확실성을 포함하지 않는다.',
              '- 표와 그래프는 CMI/MESA를 각각 평가한 결과다. 두 코호트를 동일하게 가중한 별도 요약은 주분석_코호트동일가중_성능.csv에 저장했다.',
              '- Validation은 과거 연구에서도 사용된 자료이므로 독립적인 최종 검증이 아니다. 이번 결과로 곧바로 33개를 확정 축소하거나 최종 모델의 성능 향상을 주장하지 않는다.', '',
              '다중 비교를 보정한 구간이 0을 벗어나는 결과:', '']
    strong = df[(df.variant!='all33') & ((df.delta_bonf12_low>0) | (df.delta_bonf12_high<0))]
    if strong.empty: lines.append('- 없음. 관측된 차이는 후보를 정하는 탐색 결과로 해석한다.')
    for _,r in strong.iterrows():
        lines.append(f'- {r.cohort} / {r.removed_group}: Δ {r.delta_joint_min:+.2f}분, 보정 구간 [{r.delta_bonf12_low:+.2f}, {r.delta_bonf12_high:+.2f}].')
    lines += ['', '## 실행 계약', '',
              '- 실제 CMI Train 186명·3,263밤 → Validation 39명·691밤. MESA Train 1,525명·10,524밤 → Validation 299명·2,062밤. 각 코호트 내 참가자가 겹치지 않는다.',
              '- TabDPT v1.3 checkpoint, tabdpt 패키지 1.3.2, seed 20261105, ensemble 4. 취침과 기상은 각각 독립적인 회귀 출력이다.',
              '- 사전학습 신경망 가중치를 fine-tuning하지 않았다. 각 조건에서 전체 Train 사례를 문맥으로 제공하고 Validation을 예측했다(context_size=None).',
              '- 발표자료의 6개 묶음을 점수를 보기 전에 고정했다. 전체 33개 + 6개 제거 조건을 두 코호트에서 실행했다(14조건, 타깃별 총 28회).',
              '- 각 조건에서 현재 Train의 1/99 분위수 절단, 상수열 제거, 평균/표준편차를 적합했다. 결측 대치용 중앙값도 기록했으나 이번 raw 피처에는 결측이 없었다. Validation에는 같은 통계만 적용했다.',
              '- 그룹 제거 조건의 전처리를 별도로 적합하고, 전체 조건에서 해당 열만 제외한 것과 수치적으로 일치하는지 확인했다.',
              '- 원래의 시각 범위 제약을 모든 조건에 동일하게 적용했다. 추가적인 잔차 개인화 보정은 하지 않았다.',
              '- 야간 시각은 local 20:00 기준 연속 분 단위로 처리했다. 23시와 01시를 단순 시계 숫자로 빼는 계산을 사용하지 않았다.',
              '- 이번 실험은 Test 예측·점수 산출·피처 선택에 Test를 사용하지 않았다. 원본 캐시에는 Test가 함께 저장되어 있지만 Train/Validation 마스크만 사용했다. 기존 모델을 교체하지 않았다.', '',
              '## 33개인데 실제 입력은 왜 31개인가', '',
              '집단 기준 취침·기상 시각(population_bed, population_wake)은 각 코호트에서 동일한 상수다. 현재 Train 전처리는 이 두 열을 제외하므로 두 코호트 모두 전체 조건의 실질 입력은 31개다. 집단 기준값은 다른 피처의 fallback/개인화 계산에도 사용되므로 열 제거가 모든 prior 정보를 제거한다는 뜻은 아니다.', '',
              '기존 frozen 전처리는 CMI 31개, MESA 28개만 남겼다. MESA에서는 현재 값이 변하는 habit_bed, habit_wake, personal_weight도 과거 전처리의 분산 필터에 의해 빠졌다. 이 사실을 점수 확인 전에 발견해 현재 Train 재적합을 주 분석으로 정하고, frozen 실행은 보존했다. 두 실험 중 점수가 좋은 쪽을 사후 선택한 것이 아니다.', '',
              '| 데이터 | 주 분석 전체 조건 MAE | 기존 frozen 전체 조건 MAE |', '|---|---:|---:|']
    for cohort in ['CMI','MESA']:
        v = df[(df.cohort==cohort)&(df.variant=='all33')].iloc[0]
        f = frozen[(frozen.cohort==cohort)&(frozen.variant=='all33')].iloc[0]
        lines.append(f'| {cohort} | {v.joint_mae_min:.2f} | {f.joint_mae_min:.2f} |')
    lines += ['', '전처리와 추가 보정 조건이 다른 이전 발표용 MAE와 이번 값을 직접 비교하면 안 된다. 모든 기여도 비교는 각 실험 내부의 전체 조건을 기준으로 한다.', '',
              '## DATA 33 설정 근거', '',
              '코드에서 DATA_V2는 기본 15개에 확장 18개를 더한 고정 목록이다. 과거 수면 습관, 최근 시각, 수면 구간, 불규칙성과 변화, 요일, 기록의 양과 간격을 표현하려는 공학적 설계 근거는 확인된다. 그러나 상관계수나 중요도 평가로 33개를 최적 선별했다는 기록, 모든 피처와 창/상수의 논문별 근거표는 확인되지 않았다. 상세 정의와 근거 범위는 DATA33_설계근거.md / .csv에 정리했다.', '',
              '발표 권장 문장: **과거 수면 기록에서 33개 후보 특성을 구성하고, 실제 CMI·MESA 검증 데이터의 묶음 제거 실험으로 예측 기여도를 평가했다.**', '',
              '## 검증 및 재현', '',
              '입력·checkpoint·출력 SHA-256, 참가자 분리, Validation row/target 정렬, 유한 예측, 출력 범위, 독립적인 MAE 집계 일치를 검사했다. 전체 조건에서 query batch 4096과 8의 첫 8행 차이는 0.01분 미만이다. 확률구간과 그려진 값은 CSV의 같은 결과를 사용한다.', '',
              '- 실행: tmp/run_tabdpt_group_ablation_refit.py (주 분석), tmp/run_tabdpt_group_ablation.py (frozen)',
              '- 집계/그래프: tmp/summarize_tabdpt_group_ablation.py',
              '- protocol.json / train_refit_primary/protocol.json에 점수 확인 전 계약과 해시를 저장했다.',
              '- private 폴더의 행 단위 예측·참가자 ID는 로컬 감사용이다. 발표에는 집계 CSV와 PNG만 사용한다.', '']
    (BASE/'실험결과_보고서.md').write_text('\n'.join(lines),encoding='utf-8')
    proof={'prediction_checks':audits,'bootstrap_seed':SEED,'bootstrap_draws':B,'alpha_per_comparison':.05/12,
           'protocol_sha256':sha(PRIMARY/'protocol.json'),'analysis_script_sha256':sha(__file__),
           'python':platform.python_version(),'numpy':np.__version__,'pandas':pd.__version__,'matplotlib':matplotlib.__version__}
    (BASE/'검증기록.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    primary, audit = analyze(PRIMARY)
    frozen, faudit = analyze(BASE, sensitivity=True)
    graphics(primary); report(primary,frozen,{'primary':audit,'frozen':faudit})
    print(primary[['cohort','variant','joint_mae_min','delta_joint_min','delta_95_low','delta_95_high','delta_bonf12_low','delta_bonf12_high']].to_string(index=False))
    print('COMPLETE: 28 prediction files verified; reports and two 3840x2160 PNGs exported.')

if __name__ == '__main__':main()
