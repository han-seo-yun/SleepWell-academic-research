# SleepWell 연구용 데이터·실험 성능 보고

> 2026-10-10 당시 서로 다른 연구 과제의 동결 결과입니다. 현재 모델 파일·개인화·전향 평가 계약은 [CURRENT_MODEL_STATUS_2026-11-01.md](CURRENT_MODEL_STATUS_2026-11-01.md)를 우선합니다.

실행일: 2026-10-10. **연구용 분석이며 진단·추천 효과·한국 사용자 정확도의 검증이 아니다.**

## 요약

원본과 기존 전처리 자료를 보존하면서 과제별 모델용 데이터를 생성했다. 서로 다른 코호트의 사람이나 센서 단위를 무리하게 합치지 않았다. 현재 MESA ACT는 약한 라벨 재현, Walch는 PSG sleep/wake 분류, Samsung은 한 사람의 다음 수면 시각, NHANES는 별도 집단의 자기보고 수면시간이라는 서로 다른 과제다.

MESA·Walch·개인·NHANES에서 총 84개 사전 지정 후보/목표 조합의 평가를 완료했다(16+16+38+14). 별도로 MESA 학습 인원 4단계 비교와 boosting 반복별 검증곡선을 만들었다. CMI와 MMASH는 누수 위험을 제한한 모델용 데이터와 사람 분할까지 만들었으며, 이번 실행에서 성능을 측정하지 않았다.

## 1. 데이터와 분할

| 자료 | 새 모델용 자료 | Train / Validation / Test | 적용 범위 |
|---|---|---|---|
| MESA | 2,139명, 원본 50,473,269행 중 공통 유효 30초 39,862,740행 | 1,525 / 299 / 315명, 사람 hash 고정 | ACT 파생 wake 약한 정답, PSG 아님 |
| Walch | PSG 26,773 epoch + 검증된 센서 전처리, 비교 공통 구간 24,654행 | 22 / 5 / 4명 | PSG 기준 동시 수면·각성 분류 |
| Samsung | 196일, 완료된 과거 28일 내 최근 7개 이력 + 20시 이전 센서 | 시간순 137 / 29 / 30일 | 한 사람의 미래 수면 시작·끝 |
| NHANES 2017–18 | 원표 5,856명 중 정답·가중치 유효 5,782명 | 3,961 / 930 / 891명 | 별도 미국 조사 집단 수면시간 회귀 |
| MMASH | 16명, 6,999개 제한적 pre-Out Bed 행 | 11 / 2 / 3명; 4,957 / 841 / 1,201행 | 조건부 연구자료만 생성, 성능 미평가 |
| 보조 참고표 | 연령별 활동·걸음·조도·시간별 조도 798행 | 학습행으로 사용하지 않음 | 분포 참고, 자동 적용 금지 |
| CMI | 원센서 277개 파티션 중 유효 이벤트가 있는 269명, 30초 12,466,010행 재구축 | 186 / 39 / 44명, 사람 hash 고정 | 독립 PSG가 아닌 주수면 이벤트 과제, 성능 미평가 |

개인 기록은 한 사람이므로 사람 단위 분할을 가장할 수 없다. 날짜 순서로 분할했다. MESA/NHANES/CMI는 새 사람이 추가되어도 기존 사람의 hash 분할을 유지한다. Walch/MMASH는 소규모 최초 인원 명단을 고정하여 저장했으므로 추가 인원 때문에 기존 사람을 재분할하면 안 된다.

Sleep-EDF의 EEG/PSG는 손목 센서·웹 일지와 입력이 다르므로 이번 손목·일지 모델과 섞어 학습하지 않았다. AIHUB는 현재 로컬 폴더에서 실험 입력을 확보하지 못했다.

MESA는 사용자가 언급한 “약 5,000개”를 사람 수로 간주하지 않았다. 이번 고정 snapshot의 ACT 후보는 2,140개 파일이며, 읽는 동안 바뀐 1개는 제외했다. 변환 종료 시 19개 파일이 추가되어 2,159개가 보였지만 기존 실험에 중간 편입하지 않았다. 수치 결과는 고정된 2,139명에 대한 것이다. 10분 공통 완전 구간을 요구해 남은 30초 행은 원자료의 약 79.0%이며, 비착용·결측 및 구간 경계 제외를 포함한다. 이는 전체 기록 시간에서의 무조건적인 성능이 아니다.

## 2. epoch별 비교

### MESA ACT 약한 라벨

각 시간 해상도에서 Validation이 선택한 모델은 모두 활동+조도 HGB(60회)였다. 평가 대상은 모든 후보에서 Validation 299명/5,555,520행, Test 315명/5,844,320행으로 같다. 수치는 공통 원본 30초 격자의 **사람별 balanced accuracy 평균**이다.

| epoch | Validation BA | Test BA |
|---|---:|---:|
| **30초** | **0.9469** | **0.9460** |
| 60초 | 0.9250 | 0.9242 |
| 120초 | 0.9239 | 0.9232 |
| 300초 | 0.9061 | 0.9044 |

선택은 **30초 활동+조도 HGB**다. Test 전체 행 기준 accuracy 0.9439, macro-F1 0.9438, 각성 특이도 0.9137, 수면 민감도 0.9801이다. Dummy BA 0.5 대비 사람별 BA 차이는 +0.4460, paired subject bootstrap 2,000회 95% 구간 [0.4445, 0.4474]다.

그러나 `wake` 자체가 활동계에서 파생된 정답이므로 **이는 ACT 알고리즘 라벨을 잘 재현한다는 결과이지, PSG 기준 94.6% 정확도나 미래 수면 예측 성능이 아니다.** 활동-only Logistic도 Test BA 0.9452로 매우 가깝다. 조도 추가나 복잡한 모델의 실질적 이득을 과장할 수 없다. 다른 길이의 다수결 정답에서 직접 계산하면 300초 HGB BA가 0.9576으로 보이지만, 원본 30초 동일 정답에 투영하면 0.9044다. 이 차이 때문에 공통 격자로 비교했다.

전원 모델용 데이터는 저장했고 학습은 메모리 한도에 따라 Train 1,525명 각각 131개 행, 총 199,775개로 표본화했다. Validation/Test는 해당 공통 유효 구간 전체를 평가했다. 따라서 “3,986만 행을 전부 학습했다”고 해석하면 안 된다.

학습 인원 증가 실험은 고정된 **60초 HGB/60초 다수결 정답**에서 Validation만 보았다. 위 30초 공통 격자 표와 지표 정의가 다르며, Test 선택에 사용하지 않았다.

| Train 인원 | 학습행 | Validation 사람별 BA |
|---:|---:|---:|
| 100 | 20,000 | 0.935369 |
| 300 | 60,000 | 0.935736 |
| 600 | 120,000 | 0.935748 |
| 1,525 | 199,775 | 0.935904 |

증가는 약 **0.0534%p**로 작았다. 이는 한 가지 고정 모델·단일 nested 표본 실험이며, 마지막 단계는 인당 표본 한도도 달라진다. 데이터 증가가 무의미하다는 증거가 아니라, 같은 약한 라벨의 행 수 확대만으로 큰 향상을 보장하기 어렵다는 신호다. 다음 우선순위는 독립 PSG 라벨, 센서 품질/결측/비착용 검증, 앱 사용자와 맞는 입력·목표 확보다.

### Walch PSG

아래는 **각 epoch에서 Validation이 선택한 모델**의 결과다. 주지표는 참가자별 balanced accuracy의 평균이며, 각성/수면 클래스 불균형을 고려한다. 긴 epoch에서도 예측을 원본 30초 격자로 투영하고 동일한 시점들에서 평가했다.

| epoch / 예측 가능 지연 | Validation 선택 모델 | Validation BA | Test BA |
|---|---|---:|---:|
| 30초 | HR+활동 Logistic | 0.7371 | 0.7108 |
| 60초 | 활동 Logistic | 0.7566 | 0.7105 |
| **120초** | **활동 Logistic** | **0.7735** | **0.7363** |
| 300초 | 활동 Logistic | 0.7258 | 0.7715 |

**선택은 120초**다. Test에서 300초가 더 높게 나왔지만 이를 보고 300초로 바꾸지 않았다. 120초는 해당 구간 종료 후의 회고적 판별이지, 120초 앞을 예측하는 모델이 아니다. 1분 웹 입력을 반드시 2분으로 바꿔야 한다는 결론도 아니다.

선택 모델의 전체 Test 행 기준 macro-F1 0.6432, 각성 특이도 0.5993, 수면 민감도 0.8699. 혼동행렬은 TN=160, FP=107, FN=434, TP=2,903이다. Test 4명의 BA 평균과 전체 행의 BA는 가중 방식이 달라 동일하지 않다.

모든 후보의 공통 평가 행은 Validation 4,240개, Test 3,604개다. 전체 PSG 채점 26,773행 중 센서 완전 행 25,497개(95.23%), 전체 시간 해상도 공통 평가 구간 24,654개(92.09%)다. 공통 구간의 수면 비율은 92.03%여서 전체 accuracy만 보면 각성 탐지 부족을 숨길 수 있다.

Dummy 대비 사람별 BA 차이 +0.2363, 사람 bootstrap 2,000회 구간 [0.2007, 0.2653]. **Test가 4명뿐이라 이 구간을 안정적인 모집단 추론으로 해석하지 않는다.**

첫 실행 이후 HGB의 내부 자동 조기 종료를 끄고 고정 60회 반복하도록 프로토콜 구현을 보정한 v2 결과다. 첫 Test를 이미 확인했으므로 최종 v2는 **탐색적 재평가**이며 최초의 독립·미열람 holdout이라고 부르지 않는다. 분할과 후보군은 유지했고 결과를 보고 튜닝하지 않았다.

## 3. 개인의 다음 취침·기상 예측

| 목표 | Validation 선택 모델 | Test MAE | 학습 중앙값 MAE | 해석 |
|---|---|---:|---:|---|
| 수면 시작 | 일지 군의 중앙값 기준선 | 157.50분 | 157.50분 | 복잡한 모델의 이득 없음 |
| 수면 끝 | 이력+센서 Ridge, alpha=100 | 166.82분 | 176.70분 | 9.88분(5.59%) 감소, 불확실 |

기상 평균 오차 감소의 7개 연속 관측일 block-bootstrap 95% 구간은 **−21.93~36.52분**이다. 0을 포함하므로 확실한 개선이라고 주장할 수 없다. 불규칙한 기록 간격의 한 사람 자료에 대한 탐색 구간이며 사람 간 외부 검증이 아니다.

분석 기준은 20:00 KST이고, 이후 16시간 내 시작하는 QC 주수면 구간을 맞힌다. HR은 1분 구간 끝, 걸음은 10분 구간 끝이 20시 이하여야 한다. 같은 10분 걸음을 1분 표에서 열 번 합산하지 않는다. 같은 날의 미래 수면 정답은 입력에서 제외하고 완료된 이력만 사용했다.

기존 분석과 달리 오래된 이력을 28일로 제한하고 Train만으로 최종 모델을 적합했다. 이전 보고의 Train+Validation 재적합 결과와 수치만 직접 비교하여 업그레이드로 부르지 않는다. 같은 개인의 기존 Test 기간이므로 새 holdout도 아니다. 원자료에 당시 동기화 시각이 없어, 과거의 실제 입수 가능성까지 검증한 것은 아니다.

센서 기록이 있는 날은 Train HR 133/137, Validation 26/29, Test 25/30일이다. 걸음 구간은 각 split 모든 날에 있지만 부분일 커버리지와 0 걸음/비착용의 동일성은 보장하지 않는다. 신뢰도·커버리지 정보의 추가 수집이 필요하다.

## 4. 보조 NHANES 활용 결과

별도 사람 단위 원표에서 평일 자기보고 수면시간을 예측했다. 연령·성별만 사용하는 모델, 활동 설문을 추가하는 모델, **Train만으로 만든 연령대 중앙값 prior**를 비교했다. 기존 전체 참고표는 Test 사람도 포함하므로 학습용 prior에 그대로 넣지 않았다.

Validation 선택: 연령·성별+활동 HGB 60회.

- Test MAE 1.1910시간(71.46분), 중앙값 기준선 1.2250시간(73.50분).
- 오차 감소 0.0340시간(약 2.04분, 2.78%). 단순 사람 bootstrap 구간 약 0.27~3.74분.
- 조사 가중치로 계산한 기술적 MAE는 1.0663시간, 기준선 1.1200시간.

이는 MESA·한국 학생·특정 앱 사용자 성능이 아니다. 가중치를 쓴 점 추정은 제공하지만 PSU/strata를 고려한 복합표본 설계 신뢰구간은 아니며, 운동 때문에 수면이 달라진다는 인과효과도 아니다. 다른 NHANES 주기의 사람을 연결하거나 개인 Samsung 자료와 합치지 않았다.

## 5. 학습 반복별 비교

신경망을 실행한 것이 아니므로 여기의 반복 횟수는 **gradient boosting iteration**이다. 센서 epoch 길이와 구분한다.

- 개인·NHANES: 사전에 정한 20/60/120회 모델을 비교하고, 120회 모델의 매 반복 Train/Validation MAE를 저장했다.
- 개인 취침은 HGB의 Validation 최소값이 1회 부근이었다. 더 반복한다고 좋아지지 않았고 최종 선택은 중앙값이다.
- 개인 기상 HGB의 검증 최소 반복은 이력-only 1회, 센서-only 78회, 결합 28회였지만 전체 후보 중 선택은 Ridge였다.
- NHANES 활동 추가 HGB 곡선 최소점은 72회였으나, Test 비교의 고정 후보 20/60/120회에서는 60회가 선택됐다. Test를 보고 72회로 재학습하지 않았다.
- Walch는 4개 시간 해상도 각각 HGB 1~60회 Validation 곡선을 저장했다.

학습 횟수의 보편적인 정답은 없다. 작은 개인 자료에서는 과적합이 빠르며, 분류·회귀·코호트마다 선택이 달랐다.

## 6. 전처리 재사용 시 발견한 문제

### CMI

기존 `subjects_final`은 269개 파일, 95,275,769개 5초 행이다. 처음 모든 행을 각성으로 둔 뒤 각 onset부터 다음 wakeup까지 수면으로 채우고 첫/마지막 유효 이벤트로 잘랐다. 이 방식은 미확인 밤을 각성으로 만들거나 다른 밤의 이벤트를 연결할 수 있다.

기존 `subjects_features/sc_score`는 미래 180 step의 ENMO 평균을 사용한다(약 15분 미래). 실시간 입력에서 제외했다. 원래 사람별 센서 파티션은 재활용하되 `series_id + night`의 완전한 onset/wakeup 쌍으로 정답을 다시 만들고, 결측 밤과 경계는 unknown으로 남겼다. 연속한 완전한 밤 사이의 구간만 주수면 바깥 구간으로 분류한다. 이는 실제 낮잠·짧은 각성을 판별하는 PSG 정답은 아니다.

새 30초 자료는 수면 4,972,880행, 주수면 바깥 7,493,130행으로 총 12,466,010행이다. 입력은 현재 구간 `anglez/enmo` 평균·표준편차 네 개뿐이다. 5초 센서 6개가 모두 있는 구간만 사용하고, 3:3 라벨 동률은 제외했다. 처리 대상 원센서에서 정답을 붙일 수 없었던 50,024,760개 5초 행은 각성으로 대체하지 않았다. 원 step 연속성·실제 timestamp 5초 간격·이벤트 시각 불일치는 모두 0건이었다. 배치 경계의 불완전 구간은 복구하지 않고 보수적으로 제외했다.

저장된 CMI Train/Validation/Test 행 수는 각각 **8,687,994 / 1,882,975 / 1,895,041**개다. 269명 처리 대상의 원센서는 124,822,080개 5초 행이며, 그중 74,797,320행에 보수적인 이벤트 기반 정답을 붙일 수 있었다.

CMI v2 실행의 입력 hash/stat은 각 사람 처리 후 수집했으므로 엄밀한 사전 snapshot은 아니다. 원본 277개 파일의 step schema가 모두 `uint32`임을 별도 확인했고, 처리 후 기록한 입력·출력 hash는 종료 후 검사에서 각각 269/269개 일치했다. 향후 실행용 사전 snapshot 검증 보강과 이 한계를 `BUILD_ADDENDUM.md`에 기록했다. 이를 소급해서 원 실행의 사전 불변성 검증으로 주장하지 않는다.

### MMASH

재사용한 clean 30초 표에서 15개 현재 구간 HR/활동/자세 특징만 허용했다. `minutes_to_out_bed`는 정답에만, 초 단위 잔여시간·post-event·wake_within은 입력에서 제외했다. 짧은 RR 선형 보간은 양 끝점을 사용하므로 RR/HRV 계열도 이번 보수적 패키지에서 뺐다.

그러나 표본 자체가 실제 Out Bed 이전 4시간으로 선택되어 있다. 일반적인 하루·밤의 기상 예측 성능을 내는 자료가 아니므로 이번에는 모델용 표/분할만 만들고 성능 주장을 하지 않았다. `mmash_wake_window_1s.csv.gz`는 gzip 종료 표식 EOF 오류가 있어 사용하지 않았다. 원본에서 재생성 후 전체 기록에서 평가해야 한다.

## 7. 웹앱 연결 판단

모든 저장 모델은 **research_only**다. 이번 결과를 자동으로 사용자에게 권장 취침시각으로 적용하지 않았다.

Walch/MESA 모델은 현재 센서 구간 sleep/wake 판별이고, 웹의 일지 기반 다음 취침·기상 예측 계약과 다르다. 개인의 기상 선택 모델도 센서 결합 모델이므로 센서 없는 웹 모델인 것처럼 연결하면 안 된다. 현재 결과로 기존 권장 정책을 교체할 근거가 부족하다.

다음 승격 조건은 일지-only 계약에 맞춘 다인 사용자 자료, 신규 holdout, 기준선 대비 신뢰구간을 동반한 개선, 웨어러블 입력의 시간/단위 일치다. 물리-only / hybrid의 전향적 비교는 연속 이력·빛 노출·당시 입력 가능성 확보 후 별도 수행한다. 관측 행동을 잘 맞히는 것과 수면을 개선하는 것은 다르다.

## 8. 산출물과 재현

코드는 `tools/model/`, 개인·NSRR 연구 자료와 모델은 저장소 밖 `E:/학술제/데이터모델/연구용데이터/`에 있다. 원본을 수정하거나 GitHub에 업로드하지 않았다.

| 폴더 | 핵심 파일 |
|---|---|
| `walch_run_20261010_v2` | `aggregate_metrics.json`, `model_ready_*_private.csv.gz`, `split_manifest_private.json`, `test_predictions_private.csv.gz`, `hgb_validation_learning_curve.csv`, `selected_model_research_only.joblib` |
| `tabular_run_20261010/personal` | `model_data.parquet`, `splits.csv`, `experiment_metrics.csv`, `boosting_learning_curves.csv`, `test_predictions.parquet`, 목표별 모델 |
| `tabular_run_20261010/nhanes` | 같은 구조, 개인 단위 별도 코호트 |
| `support_run_20261010_v3` | `mmash_conditional/{train,validation,test}.parquet`, 798행 참고표 Parquet, `auto_apply=false` 후보 |
| `mesa_run_20261010` | `manifest.json`, `subjects/*.npz`, `protocol.json`, `selected.json`, `experiment_report.json`, `evaluation/{validation,test}/*.npz`, `selected_model.joblib` |
| `cmi_run_20261010_v2` | `subjects_30s/*.parquet`, `split_manifest_private.json`, `subject_qc_private.json`, `public_aggregate.json`, `provenance_private.json` |

구버전 산출물은 덮어쓰지 않고 남겼다. 최종 분석은 위 버전을 사용한다. 로컬에서 직접 만든 신뢰할 수 있는 모델 파일만 읽는다. 외부 joblib/pickle은 임의 코드를 실행할 수 있으므로 열지 않는다.

Walch v2 실행 뒤 유한값 검증·추후 실행 provenance·재평가 문구를 보강했다. 원 실행 코드 hash와 현재 코드 hash 차이는 `provenance_addendum.json`에 기록했다. 저장된 입력의 유한성 및 기존 성능·모델·예측 hash를 재검증했으며 수치 결과는 바꾸지 않았다. CMI 첫 실행은 QC 구현 보강을 위해 중단한 불완전본이므로 사용하지 않는다.

MESA는 원본 변환 완료 후, 느린 집계 루프를 동등성 회귀 테스트가 있는 벡터 연산으로 바꾸고 저장된 snapshot에서 학습을 재개했다. 당시 Test 평가 전이었고 후보·분할은 유지했다. `resume_validation.json`에 manifest/전체 2,139개 NPZ hash와 전후 코드 hash를 남겼다. 검증 예측에서 사람별 BA를 다시 계산하고 모든 Test 후보의 혼동행렬을 다시 합산했다. 실행 담당 검증은 선택 모델을 Test 315명 전체에 다시 적용해 5,844,320행 예측 불일치 0건을 확인했고, 별도 통합 검증도 표본 3명에서 재현했다. 실행 후 도달 불가능한 중복 코드만 삭제한 hash 변경은 `post_run_code_provenance.json`에 기록했다.

실행 환경: Python 3.12.1, numpy 2.4.1, pandas 3.0.5, scikit-learn 1.8.0. 기존 전처리에 필요한 PyArrow 26.0.0을 현재 위치 `데이터모델/연구용데이터/.venv`의 격리 환경에 설치했다. 앱의 의존성 파일/시스템 환경은 변경하지 않았다. 프로세스당 native thread 2개로 제한했다.

PowerShell, 작업 경로 `E:/학술제`:

```powershell
& '데이터모델/연구용데이터/.venv/Scripts/python.exe' -X utf8 '기타/SleepWell-integrated/tools/model/research_tabular.py' --workspace 'E:/학술제' --output 'E:/학술제/데이터모델/연구용데이터/tabular_new_snapshot'
& '데이터모델/연구용데이터/.venv/Scripts/python.exe' -X utf8 '기타/SleepWell-integrated/tools/model/mesa_experiments.py' --root 'E:/학술제/데이터셋/MESA' --output 'E:/학술제/데이터모델/연구용데이터/mesa_new_snapshot'
& '데이터모델/연구용데이터/.venv/Scripts/python.exe' -X utf8 '기타/SleepWell-integrated/tools/model/verify_research_run.py' --research-root 'E:/학술제/데이터모델/연구용데이터'
& '데이터모델/연구용데이터/.venv/Scripts/python.exe' -X utf8 -m unittest discover -s '기타/SleepWell-integrated/tools/model' -p 'test_*.py' -v
```

재실행은 기존 폴더를 덮어쓰지 않으므로 새 output 이름이 필요하다. Test 결과를 확인한 이후의 반복 실험은 새로운 외부 검증으로 부르지 않는다.

### 최종 검증

- 연구 도구 회귀 테스트 43/43 통과, 기존 웹앱 테스트 20/20 통과.
- Python compileall 및 웹 JavaScript 문법 검사 통과. Git diff 공백 검사 통과(기존 파일의 LF/CRLF 경고는 남음).
- `verify_research_run.py` 통합 검사 통과: 개인/NHANES 저장 모델·예측·MAE 일치, Walch 사람 분할·동일 평가 시점·모델 확률 재현, MMASH 실제 세 split, MESA 검증 선택 재계산·Test 혼동행렬 재합산·모델 재현, CMI 전체 269개 파일의 특징/라벨/시간/분할/수량 확인.
- 기존 Ridge 코드의 pandas `Timestamp.utcnow` deprecation 경고가 있으나 테스트 실패는 아니다. 별도 보안 인증·임상 검증·외부 코호트 검증을 수행했다는 의미는 아니다.

이번 변경 파일은 `tools/model/{research_tabular,mesa_experiments,walch_experiments,cmi_research_dataset,package_research_support,verify_research_run}.py`, 해당 테스트, 이 보고서·사전 설계 및 웹 README다. 새 외부 서비스 연결, 원자료 수정, 웹 추천 정책 교체, GitHub push는 하지 않았다.

## 9. 방법·출처

- [사전 고정 실험 설계](RESEARCH_PROTOCOL_2026-10-10.md), [앞선 연구·웹 계획](MESA_WEB_PLAN_2026-10-09.md).
- [NSRR MESA ACT 코드북](https://sleepdata.org/datasets/mesa/pages/actigraphy-introduction.md): ACT wake와 날짜 해석.
- [Walch 공개 자료](https://physionet.org/content/sleep-accel/1.0.0/): 손목 센서와 PSG 라벨.
- [CMI 공식 과제](https://www.kaggle.com/competitions/child-mind-institute-detect-sleep-states): onset/wakeup 이벤트 정의.
- [scikit-learn 누수 방지 지침](https://scikit-learn.org/stable/common_pitfalls.html), [GroupKFold](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupKFold.html), [학습곡선](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.learning_curve.html). 공식 현재 문서는 1.9.1, 실제 실행 환경은 1.8.0으로 구분했다.
- 소프트웨어 방법 참고: Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*. [arXiv:2609.00065](https://doi.org/10.48550/arXiv.2609.00065). 2026-10-10 조회, 현재 v2, 동료심사 전 preprint. `scikit-learn` 및 자원 점검 스킬은 train-only 전처리·검증 분리·메모리 제한 설계에 사용했다.
