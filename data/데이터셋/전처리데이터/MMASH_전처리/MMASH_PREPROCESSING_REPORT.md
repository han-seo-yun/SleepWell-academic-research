# MMASH 전처리 검수 보고서

## 목적
학술제의 **물리기반 생체리듬 baseline + 잔차학습** 구조에서 MMASH를 보조학습/집단 baseline 추정용으로 쓰기 위해, `Out Bed` 전후의 HR·HRV·ACC·Steps·자세 시계열을 정리했다. 학습용 피처는 모두 **현재 시점까지의 과거값만 사용**한다.

## 결과
- 원본 22명 / sleep label 보유 21명
- wake transition 주변 센서 품질 통과: **16명**
- 30초 causal 학습행: 7,680행 → 품질필터 후 **6,999행**, 결측 0
- 1초 RR+Actigraph join: 259,216행

### 제외 피험자
subject_id                           exclude_reason
    user_3 wake_transition_not_observed_within_5min
    user_4 wake_transition_not_observed_within_5min
    user_9 wake_transition_not_observed_within_5min
   user_10 wake_transition_not_observed_within_5min
   user_11                           no_sleep_label
   user_20 wake_transition_not_observed_within_5min

## 발견·수정한 원본 문제
1. **day=-29 오류**: user_8, user_9의 Actigraph와 RR 모두에서 확인. day1 23:59:59 직후 00:00:00부터 연속되는 구간이라 `day=2`로 복구.
2. **sleep.csv 자정 rollover 오류**: user_3, user_5, user_13, user_19. `Out Bed - In Bed = Total Minutes in Bed`가 정확히 성립하도록 Out Bed 날짜를 +1일 보정. 시각 자체는 변경하지 않음.
3. **user_1 분할수면**: 00:46-03:31 / 03:57-07:30, gap 26분 → 하나의 overnight episode로 병합, 최종 wake label 07:30.
4. **RR artifact**: IBI 0.30~2.00 s 밖은 HRV 계산에서 제외(원본은 보존), 피험자별 비율은 `mmash_qc_report.csv`에 기록.
5. **Actigraph 실제 gap**: ACC/Steps/Posture는 임의 보간하지 않고 coverage를 feature로 남김. RR만 event 자료 특성상 최대 2초 gap을 1Hz 보간.
6. **wake 주변 데이터 부재**: user_3/4/9/10/20은 Out Bed 전후 5분 내 센서가 충분히 연결되지 않아 학습에서 제외. user_11은 sleep label 자체가 없음.

## 학습 데이터 설계
- window: Out Bed **-240분 ~ +30분**
- 실제 모델 학습: **Out Bed 이전 240분만** 사용, post-wake 데이터 제외
- epoch: 30초
- HR: Actigraph HR + RR-derived HR
- HRV: beat-level meanNN / SDNN / RMSSD / pNN50 (30초, trailing 5분)
- ACC: Vector Magnitude, 3축 변동성, Steps
- 자세: Off/Standing/Sitting/Lying 모두 사용. 검수 결과 `Lying`만으로 수면을 정의하면 안 됨.
- target: `minutes_to_out_bed`, `wake_within_5m/15m/30m`
- 검증: **row random split 금지**, subject 단위 GroupKFold/LOSO만 사용

## 중요한 연구상 한계
`Out Bed`는 PSG 독립 정답이 아니라 ActiGraph가 산출한 **device-derived weak label**이다. 따라서 MMASH는 보조학습/집단 baseline/feature 검증용으로 사용하고, 최종 성능 주장은 Walch-PSG 같은 독립 기준으로 해야 한다. 또한 Out Bed 중심 window는 label-centered sampling이므로 최종 평가는 전체 수면 시간축에서 wake time을 예측한 뒤 subject 단위 시간오차로 평가해야 한다.

## 산출물
- `mmash_preprocess.py` : 재현 가능한 전처리 코드
- `mmash_qc_report.csv` : 피험자별 오류/보정/제외 근거
- `mmash_sleep_labels_corrected.csv` : 보정/병합된 sleep episode
- `mmash_wake_window_1s.csv.gz` : RR 1Hz + Actigraph join
- `mmash_transition_30s.csv(.gz)` : -4h~+30m 진단용
- `mmash_model_preout_30s_allrows.csv` : causal rows + quality flag
- `mmash_model_preout_30s_clean.csv(.gz)` : 바로 학습 가능한 결측 0 dataset
- `feature_manifest.json` : feature/target 그룹 + Galaxy Fit3 전이 가능성
