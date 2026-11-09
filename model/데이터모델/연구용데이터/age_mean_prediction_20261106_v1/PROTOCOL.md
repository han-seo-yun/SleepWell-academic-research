# 연령 평균 추가에 따른 다음 수면 시각 예측 비교 — 사전 고정 계획

작성일: 2026-11-06. 신규 모델 학습 및 신규 성능 결과 열람 전에 작성한다.

## 질문과 구분

현재 앱에서 참고용으로 보관하는 NHANES 연령별 관측 평균을 예측 입력으로 추가하면, 기존 수면 이력에 비해 다음 주요 수면(main sleep episode)의 시작·최종 종료 시각 예측 오차가 줄어드는가?

이는 수면시간 권장의 건강·피로도 효과, 최적 필요 수면시간, PSG 실제 수면시간 예측의 검증이 아니다. MESA 액티그래피에서 구성한 주요 수면 시작·종료 라벨을 사용한다. 구간 길이는 종료−시작이며 실제 잠든 총시간이나 권장 수면 기회시간과 구분한다.

## 데이터와 범위

- 모델 입력과 지원 범위 판정은 기존 MESA causal replay의 나이(조화 변수 nsrr_age)를 사용한다. 원본을 기존 익명 참여자 키로 연결해 nsrr_age와 정확히 일치하는지 검증하고, sleepage5c는 원천 나이 확인에 사용한다. sleepage5c와 차이는 원천 90세 초과에서 nsrr_age=90인 상한 처리만 허용한다. 특히 54–80세 값이나 연령군이 다르면 중단한다. 원본·앱·과거 실험을 수정하지 않는다.
- MESA 2,139명/14,761밤. 기존 분할 Train 1,525명/10,524밤, Validation 299명/2,062밤, Test 315명/2,175밤을 그대로 유지한다. 동일 사람이 여러 분할에 나타나면 중단한다.
- CMI 4,627개 기록은 나이를 확인할 수 없어 연령 효과의 평가 자료로 사용하지 않는다. 앱의 CMI 모델 기여분은 그대로 유지한다.
- 연령 범위 54–90세. 연구 비교 지원 범위는 54–80세. 앱의 45–59세 입력군 전체는 관측되지 않으므로 실제 적용 검토 범위는 60–80세만이다. 범위 밖 또는 연령 결측에서는 기존 출력으로 정확히 되돌아간다.
- 나이는 등록 시점에 알았다고 가정한다. 실제 설문 취득 시점 및 Health Connect 지연 도착은 이 자료로 검증하지 않는다.
- NHANES population_prior.json의 sex=any, activity_band=any 연령 평균만 사용한다. 성별·활동량·직업을 동시에 추가하지 않는다. 평균은 원래 NHANES Train 자료에서 계산된 고정 외부 참조이며 MESA 평가 라벨로 재계산하지 않는다.
- NHANES 값은 평일/근무일 자기보고 취침·기상에서 산출된 관측값이다. 최적 수면 필요량으로 해석하지 않는다.

## 고정 비교군

1. current_v8: 현재 robust MESA/CMI hybrid의 50:50 합성, GRADUAL_V1.
2. control: MESA 예측기만 같은 Hybrid39 입력+전원 동일 NHANES global mean으로 재학습. CMI는 고정.
3. age: 동일 재학습에 age-only mean을 추가. 범위 밖/결측 입력값은 global mean. 이 값은 관측 자료에서 사실상 세 번째 연령 상태(지원 밖/결측)를 구분할 수 있으므로 '순수한 수면시간 평균의 효과'로 해석하지 않는다. 학습 인원 및 다른 입력은 control과 동일.
4. band: 고정 NHANES 평균과 global mean의 오름차순 순위를 코드로 사용하는 진단군. MESA에서 관측된 45–59세 값 < global/지원 밖/결측 < 60–80세 값의 순서를 그대로 유지한다. 평균의 크기 자체를 제거하고 나무의 분기 순서는 보존하는 진단이다. 모델 선택에 사용하지 않는다. age와 같다면 숫자 평균 자체의 가치와 연령 분류 효과를 분리할 수 없다고 보고한다. one-hot 진단이나 다른 인코딩은 추가 탐색하지 않는다.

원래 Hybrid39 입력의 결측 대체 중앙값과 1%/99% clip 한계는 Train에서만 계산한다. 추가 연령 입력은 고정 참조값이다. 모든 Train 참여자/밤을 사용하며 밤별 가중치는 참여자 기록 수의 역수(평균 1로 정규화)이다.

모든 재학습군: MultiOutputRegressor(HistGradientBoostingRegressor(loss='absolute_error', max_leaf_nodes=7, min_samples_leaf=20, learning_rate=.05, max_iter=120, l2_regularization=10, early_stopping=False, random_state=20261106)). 쓰레드 2. 임의 탐색이나 결과를 본 후 조정하지 않는다. 기존 constrain을 적용한 MESA 결과와 고정 CMI를 50:50 합성한다.

## 인과적 입력과 재현성

이미 해시가 고정된 replay를 재사용하되 원본 episodes, MESA 원본, 모델 및 Python reference 파일의 과거 해시를 확인한다. 나이 연결도 원본 sleepage5c와 검증한다. 과거 replay는 예측 후에만 해당 밤을 이력에 추가한다. 별도 소규모 재연산으로 캐시와 현재 참조 엔진을 확인하고 기준 fixture parity를 새로 실행한다. 원시 관측 시간대 증거가 없으므로 기존 wall-clock 기준 +09:00 연구 재현을 유지하며 모든 지역 정확도를 주장하지 않는다.

PROTOCOL, 실험 코드, 테스트 코드, 재사용 코드 및 입력 해시를 학습 전에 봉인한다. Train 학습 후 모델과 Validation 결과를 해시 봉인하고, 별도 단계에서 Test를 한 번 평가한다. 단일 사전 지정 age 후보이며 Test를 보고 선택/튜닝하지 않는다. 과거 연구에서 Test가 사용되었으므로 새로운 독립 외부 검증이 아니다.

## 지표 및 판정

- 일차: 각 참여자의 평균 onset/wake 절대오차를 평균한 joint MAE(분), 다시 참여자 균등 평균. 밤이 많은 사람에 과도한 비중을 주지 않는다.
- 이차: onset MAE, wake MAE, 수면 구간 길이 MAE. 권장 수면 효과와 별도다.
- 차이: comparator−age, 양수면 개선. 동일 참여자 단위 paired bootstrap 5,000회, seed=20261106, percentile 95% CI. 독립 표본처럼 밤을 재추출하지 않는다.
- 연구 범위 54–80, 실제 적용 검토 범위 60–80 각각: 전체(밖은 기존 모델) 및 지원 범위만 지표를 모두 표시한다. 연구 범위 결과만으로 적용 범위를 넓히지 않는다.
- 적용 검토는 60–80 경로에서만 한다. 전체와 지원 범위 모두에서 Validation/Test joint MAE가 current보다 최소 1분 개선, Validation은 control보다 나쁘지 않고, Test 개선 CI 하한이 current 및 control 모두 대비 0 초과여야 한다.
- 실행 결과 키는 scopes.deployable_60_80.{validation,test}.views.{all_with_exact_fallback,eligible_only}로 고정한다. scientific_54_80 결과는 도입 판정에 사용하지 않는다. Test의 control 비열등 평균 조건도 확인한다(CI 조건에 포함되는 중복 안전 검사).
- 지원군의 onset/wake와 첫 기록, 이력 1–2개/3개 이상 각 층(30명 이상)에서 current 및 control 대비 1분 초과 악화가 없어야 한다. 이 기준도 Validation/Test 모두 확인한다.
- 1분은 프로젝트 공학적 도입 문턱이며 임상적 유의 기준이 아니다. 모든 조건의 교집합으로 판단하며 진단군/하위군 결과 중 좋은 것만 선택하지 않는다.
- 조건 미충족 시 앱·모델·연령 입력 정책을 변경하지 않는다. 충족 시에도 실제 Android 출력의 변환/개인화 정책과 회귀 검증을 통과한 뒤에만 지원 연령에 한정 적용한다.

## 보고와 보존

최종 보고는 분할별 표본 수, 모든 비교군 오차, 참여자 CI, 구간 오차, 연령 정보의 제약, 적용 판단을 공개한다. 원시 개인 기록/참여자 예측은 로컬 private 파일로만 보관한다. 자료 수집이나 현재 앱의 수면 권장 효과를 입증한 것으로 표현하지 않는다. 입력 해시와 앱 git 상태를 마지막에 재검사한다.

공식 참고:
- https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2017/DataFiles/SLQ_J.htm
- https://scikit-learn.org/stable/modules/cross_validation.html
