# DATA_V2 33개 피처 설계 근거 감사

## 결론

DATA_V2는 **성능이 검증된 최소 변수 집합**이라기보다, 수면 이력·최근성·요일·변동성·기록 충분도를 누수 없이 표현하도록 만든 **사전 설계된 causal feature contract**다. 코드에는 계산식, 관측창, 결측 fallback이 명확히 있으나, 33개를 하나의 논문 근거표로 선정했다는 자료나 33개에 대한 사전 feature-selection/ablation 결과는 확인되지 않았다.

이번 CMI·MESA feature-group ablation은 이 설계 계약의 각 묶음이 Validation 성능에 실제로 기여하는지 확인하는 **후속 경험적 검증**으로 기록해야 한다. “33개가 연구적으로 최적이라고 이미 입증됐다”고 표현하면 안 된다.

## 직접 확인한 코드 근거

- `E:\학술제\소프트웨어\SleepWell Academic Workbench\vendor\sleepwell\robust_features.py:1-29`: `DATA_V2 = DATA_FEATURES + EXTRA`; 모듈 설명은 “Causal v2 history features”이며 `HYBRID_V2`는 여기에 `PHYSICAL_FEATURES` 6개를 추가한다.
- `...\vendor\sleepwell\night_core.py:1-41`: DATA 기본 15개와 물리 6개의 계약, local 20:00 의사결정 시점, offset 단위를 정의한다.
- `...\vendor\sleepwell\night_core.py:160-214`: 기본 피처의 실제 산출식과 fallback.
- `...\vendor\sleepwell\robust_features.py:60-110`: DATA_V2 확장 18개 산출식과 56일/최대 14기록 창.
- `E:\학술제\소프트웨어\SleepWell Academic Workbench\docs\POOLED_RETRAINING.md:32-35`: CMI/MESA pooled 학습에서 DATA 33 및 HYBRID 39를 사용하고, cohort ID·나이·성별을 입력에서 제외한 사실.
- `...\researchbench\resources\sleepwell_guideline_seed.json:196-213`: “전체 변수 선택의 연구 근거는 분산되어 있다”, 변수별 선정 근거 정리가 남은 과제라는 내부 감사 기록.
- `E:\학술제\데이터모델\연구용데이터\robust_next_night_run_20261020_v2\analysis\protocol.json`: 33/39 feature list, train-only 전처리, participant-disjoint CV와 후보 선택 규칙.

## 설계 해석

### 공통 단위와 인과성

시간 피처는 `local 20:00` 기준의 분(minute) offset이다. `normalize_history()`는 완료된 wake가 cutoff보다 앞서고 관측 시각도 cutoff보다 앞선 기록만 사용한다. 따라서 이 계약의 핵심 설계 근거는 “예측 시점 이후 정보가 들어가지 않도록 과거 기록만 사용”하는 것이다. 이는 코드로 확인되는 engineering property이며, 자체적으로 임상적 타당도나 feature importance를 입증하지 않는다.

### Shrinkage

`personal_weight = n/(n+14)`는 활성 개인화에서 population prior와 전체 이력 평균을 섞는 고정 shrinkage다. 여기서 14는 **14개 기록에 해당하는 prior strength**이며, 코드에 고정된 engineering 상수다. 이 저장소에서 DATA33 feature selection을 위해 최적화했다는 증거는 없다. `legacy14` 정책은 14개 이상 기록과 14일 경과 조건을 요구하고, `gradual_v1`은 유효 기록 1개부터 활성화할 수 있다.

### 중복처럼 보이는 피처의 이유

recent 7기록과 robust-v2의 recent 14기록, median·recency-weighted median·MAD·trend는 서로 상관될 수 있지만 각각 최근 중심, 시간 감쇠 중심, 산포, 변화 방향을 표현하려는 설계다. 상관이 높다는 이유만으로 사전 삭제했다는 문서는 없다. 따라서 이번 묶음 제거 실험에서 “중복이라 제거”가 아니라 Validation 성능·안정성으로 판단해야 한다.

## 33개 피처 묶음

정확한 feature별 정의·단위·창·fallback·코드 위치는 동 폴더의 `DATA33_설계근거.csv`에 기록했다. 아래 6개 묶음은 이번 ablation에서 **성능을 보기 전에 고정한 발표·실험 그룹**이다. 코드 원저자가 validated grouping으로 제시한 분류가 아니라, 33개를 설명하고 제거 실험하기 위한 사전 고정 분류다.

| 묶음 | 피처 | 설계 목적 |
|---|---|---|
| 평소 수면 시각 (6) | population_bed/wake, habit_bed/wake, weekday_bed/wake | 평소·개인·요일별 시각 |
| 최근 수면 시각 (8) | recent_bed/wake, previous_bed/wake, median14_bed/wake, ew_bed/wake | 최근 중심·직전값·감쇠 중심 |
| 수면 구간의 길이 (2) | recent_duration, previous_duration | 최근·직전 수면 길이 |
| 불규칙성과 변화 (7) | bed/wake_variability, bed/wake_mad, duration_mad, bed/wake_trend | 산포·절대편차·추세 |
| 요일 정보 (4) | weekend, next_day_sin/cos, next_day_weekend | 현재·다음 날 요일 주기 |
| 기록의 양과 간격 (6) | history_count_7, recent_count14, history_age_days, weekday_count, gap_days_mean, personal_weight | 기록량·최신성·간격·개인화 정도 |

이 6개 묶음은 성능을 확인하기 전에 고정되었다. 원 코드가 이 6개 그룹을 validated grouping으로 제시했거나, 이 그룹을 기준으로 최종 변수를 선택했다는 의미는 아니다.

## 실험·전처리 해석 경계

기존 protocol은 각 fold의 train 자료에서만 1/99 percentile clipping, constant removal, standard scaling을 적합한다. 따라서 cohort별로 상수인 피처는 모델 적합 전에 제거될 수 있다. 이는 DATA33 설계에서 해당 피처를 사전 제외했다는 뜻이 아니다. 이번 freshTrainfit과 existing-frozen sensitivity 결과는 이 차이를 별도 표기해야 한다.

특히 frozen preprocessing에서 CMI의 `population2`, MESA의 추가 `habit2`/`personal_weight`가 제거되었다는 관찰은 “raw feature contract”와 “실제 적합 후 surviving columns”를 구분해야 함을 보여준다. 이 관찰만으로 중요도나 일반화 기여도를 결론내릴 수 없다.

## 확인된 것과 확인되지 않은 것

| 항목 | 상태 |
|---|---|
| 33개 이름·순서 | 코드와 protocol에서 확인 |
| 각 피처 계산식·창·fallback | 코드에서 확인; CSV에 정리 |
| cutoff 이전 기록만 사용 | `normalize_history()` 및 cutoff 검사로 확인 |
| population prior·personal shrinkage 설계 | 코드와 주석에서 확인 |
| 33개를 논문 근거로 일괄 선정 | 확인되지 않음 |
| 33개를 대상으로 한 사전 ablation | 확인되지 않음 |
| 14일 shrinkage 상수의 데이터 최적화 근거 | 확인되지 않음 |
| 피처별 임상적 인과 효과 | 확인되지 않음 |
| CMI/MESA Validation에서 그룹별 기여도 | 이번 ablation 결과로만 판단 |

## 보고서에 권장하는 문장

> DATA 33은 과거 수면 이력, 최근성, 요일성, 변동성, 기록 충분도를 포괄하도록 사전 설계한 입력 계약이다. 본 연구에서는 이 고정 계약을 기준으로 CMI·MESA Train에서 모델을 적합하고 Validation에서 피처 그룹 제거 실험을 수행하여 각 정보군의 추가 기여도를 평가했다. DATA 33 자체가 사전에 최적 변수 집합으로 검증되었다고 가정하지 않았으며, 이번 실험에서 Test는 사용하지 않았다.

