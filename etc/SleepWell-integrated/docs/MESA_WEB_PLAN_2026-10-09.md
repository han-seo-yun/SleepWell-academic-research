# MESA 활용 가능성·전체 프로젝트 분석·웹앱 구현 계획

> 2026-10-20 후속 검증: 이 문서는 10월 9일 당시의 자료 상태입니다. 이후 확보된 PSG XML과 연결표를 사용한 현재 분석은 [PSG 검증 보고서](RESEARCH_PSG_RESULTS_2026-10-20.md)에 기록했습니다. 아래의 “PSG 미발견” 설명은 현재 폴더 상태를 뜻하지 않습니다.

2026-10-09 / 사용자 지정 우선순위: **웹앱 → 공통 모델 → 필요 시 모바일**.

## 판단

**현재 MESA는 모델 연구에 사용할 가치가 있다. 다만 지금 보유한 ACT만으로 심박 기반 5단계 수면모델이나 수면 개선 효과를 검증할 수는 없다.** 다음날 취침·기상 예측용 과거 패턴과 활동·빛 특징을 만드는 것은 가능하다. 이 경우 ACT에서 파생된 수면/휴식 시각을 PSG 정답과 구분해야 한다. 성능 향상 여부는 기준선과 동일한 사람·미래 기간에서 비교하기 전까지 미확정이다.

웹앱의 현 추천은 생활 평균·기상 제약·Two-Process 기반 정책이다. MESA가 업로드되면 자동 학습되는 구조가 아니며, 지금까지의 개인 모델 성능을 MESA 성능으로 볼 수 없다. 이번 작업은 데이터 점검과 안전한 경량 모델 연결 경로를 추가하며, MESA 학습 완료나 추천 효과 검증 완료를 뜻하지 않는다.

## 1. 실제로 확보된 MESA는 무엇인가

조사 중 로컬 `데이터셋/MESA/actigraphy`의 CSV는 약 1,630개에서 계속 증가했다. 각 파일의 번호는 연속적인 사람 수가 아닌 MESA 식별번호다. **파일명에 5000번대가 있다고 5,000명이라고 셀 수 없다.** 다른 서버나 Drive에 별도 업로드된 수량은 이번 로컬 점검으로 확인하지 않았다.

정확한 고정 시점 집계와 검사 범위는 함께 제공하는 `MESA_INVENTORY_2026-10-09.json`을 기준으로 한다. 이후 업로드는 점검 도구를 다시 실행해 갱신한다. 크기·mtime이 읽기 중 바뀌면 해당 파일의 내용 검사를 제외한다. 파일 개수·헤더 검사와 일부 파일 내용 검사는 서로 다른 검사 범위다.

확인한 공통 헤더는 다음과 같다.

`mesaid, line, linetime, offwrist, activity, marker, whitelight, redlight, greenlight, bluelight, wake, interval, dayofweek, daybymidnight, daybynoon, date_shifted, actigraphy_month_start`

| 자료/항목 | 현재 로컬 상태 | 사용 판단 |
|---|---|---|
| ACT 활동·4종 빛·착용 플래그 | CSV에서 확인 | 활동·빛 패턴, QC, 과거 상태 특징에 사용 가능 |
| `wake`, `interval` | ACT 처리 결과 | 장치/채점 기반 약한 라벨. 독립 PSG 정답과 다름 |
| HR·HRV·걸음수 | ACT 헤더에 없음 | activity를 steps로 바꾸거나 HR을 0으로 채워 학습 금지 |
| PSG EDF·NSRR 단계 XML | 로컬 MESA 폴더에서 미발견 | 5-stage 학습과 PSG 정확도 평가는 대기 |
| 사람별 연령·임상·PSG QC/정렬표 | 현재 ACT만으로 미확인 | 대상군·동시기록 매칭과 제외 기준 확인에 필요 |
| 피로/개운함·추천 실행·이후 변화 | 현재 CSV에 없음 | 추천 효과의 정답을 생성할 수 없음 |

공식 MESA Sleep는 2,237명 참여, 약 1주간 ACT 수집이며 공개 ACT 원시 epoch 자료는 2,159명으로 안내된다. 따라서 5,000이라는 수량은 사람·파일·밤·행 또는 다른 코호트의 수량인지 구분해야 한다. 한 사람의 수만 epoch를 수만 독립 표본으로 계산하지 않는다. [NSRR ACT 소개](https://sleepdata.org/datasets/mesa/pages/actigraphy-introduction.md)

추가 자료 우선순위는 **PSG 단계 XML + 같은 사람의 EDF/정렬 정보 + 품질·대상군 정보**다. ACT 파일만 계속 늘리는 것은 사람 간 다양성에는 도움이 되지만, 빠진 정답·센서·개입 결과를 대신하지 않는다.

공식 코드북상 `wake=0`은 수면, `wake=1`은 각성이고 `REST-S`는 채점된 수면 시작~종료 구간이다. 이 값을 활동 입력과 함께 사용해 동일 epoch의 수면을 예측하면 라벨 누수가 생길 수 있다. `date_shifted`는 요일을 유지하도록 2000년 첫 주에 대응시킨 비식별 날짜이므로 실제 연도·공휴일·DST를 복원하는 데 쓰지 않는다. `actigraphy_month_start`는 기록 시작 월이며 실제 일자와 다르다. [NSRR 코드북](https://sleepdata.org/datasets/mesa/pages/actigraphy-introduction.md)

MESA 최초 모집의 45–84세와 수면 부속연구 당시 나이를 혼동하면 안 된다. 수면검사 당시 평균은 약 **68.4세**로, 한국 학생 대상 전이가 별도 과제다. 현재 ACT 파일에는 연령이 없어 개별 대상군 정보는 메타데이터 결합 후 확인해야 한다. [MESA Sleep 공식 가이드](https://sleepdata.org/datasets/mesa/files/documentation/MESA_Sleep_Data_Documentation_Guide.pdf)

## 2. 예측 과제별 가능성과 한계

| 과제 | 현재 자료로 시작할 수 있는 범위 | 필수 평가 / 남은 조건 |
|---|---|---|
| 활동계 sleep/wake | activity/light/clock → ACT 약한 라벨 기준 baseline | PSG 확보 후 독립 검증; balanced accuracy·wake specificity·F1·kappa |
| 5단계 W/N1/N2/N3/REM | 현재 MESA 단독으로 불가 | PSG 라벨과 센서 정렬; macro-F1·단계별 recall·혼동행렬 |
| 다음날 취침·기상 | 완료된 과거 수면/휴식 이력 + 결정시각 전 활동·빛 | 실제 sleep onset인지 REST 시작인지 고정; 원형 MAE·중앙/P90 오차·예측 가능률 |
| 개인화 | 집단 사전정보 + 짧은 기록의 보정 실험 | ACT 약 1주로 앱의 14일 이후 장기 개인화 검증 불가 |
| 권장 취침·기상 | 예측을 참고하고 기상 제약/수면 기회를 적용하는 정책 구현 | 개운함·피로·규칙성의 전향적 결과 필요 |
| 질환·컨디션 예측 | 현재 우선 범위 아님 | 목표 라벨·임상 설계·추가 검증 필요 |

MESA의 집단 규모는 단순 회귀·트리·가벼운 시계열 기준선을 연구하기에 의미가 있다. 그러나 유효 표본 수는 QC 후 **서로 다른 사람 수와 유효 밤 수**로 결정된다. ‘몇 천 개이면 딥러닝이 잘 된다’는 결론은 성립하지 않는다.

## 3. 프로젝트 전체 현황

아래 수량 중 MESA 스냅샷을 제외한 기존 데이터 수량·모델 성능은 저장된 manifest/보고서를 확인한 것이다. 이 Windows 환경에서 기존 원본 Parquet 학습 전체를 재실행한 수치가 아니다.

| 영역 | 확인한 근거 | 현재 상태 / 개선점 |
|---|---|---|
| CMI | 회의 9/12·9/26, CMI 전처리 notebook | onset/wakeup 과제. 9/26 물리 단독 AUC 약 .5는 회의 보고값; 이번 새 실험값 아님. 개인화만이 저성능 원인이라고 확정하지 말고 라벨·위상·점수 정의를 ablation |
| Sleep-EDF | `전처리데이터/GroundTruth-평가코드/평가코드.py:90` | PSG 5단계 평가 틀 있음. EEG 모델 성능을 손목 센서 성능으로 옮길 수 없음 |
| Walch | `260925/통합파이프라인/output/dataset_manifest.json` | 공통 표 26,773행. 실제 Apple Watch+PSG 기반으로 외부검증 가치가 있으나 사람 수·기기 차이 별도 확인 |
| MMASH | `전처리데이터/MMASH_전처리/MMASH_PREPROCESSING_REPORT.md` | QC 16명, 6,999행. Out Bed 약한 라벨·정답 중심 window이므로 실제 전체 밤 예측과 구별 |
| 개인 Samsung | `260925/한서윤데이터_전처리/PREPROCESSING_REPORT.md` | 1명, 주수면/QC 219일, 수면+주간 활동 연결 128일. movement까지 모두 있는 날은 7일 |
| 공통 파이프라인 | `260925/통합파이프라인/output/dataset_manifest.json` | 108,442 epoch, MESA 미포함. 키/라벨 검사 통과는 모델 정확도와 다름 |
| 개인 미래 예측 | `미래예측모델/output/validation_report.json` | 196일, train137/val29/test30. 아래 결과 참조 |
| Circadian/PCR | `물리모델/circadian.py`, `pcr.py`, `test_physics.py` | 수치 시뮬레이션과 검사 코드. 실측 개인 위상/각성도 추정의 정확도가 증명된 것은 아님 |
| Two-Process | `TWO_PROCESS_SIGN_CORRECTION_REPORT.md` | midsleep에서 양수인 C_sleep이면 S+C_sleep. 정의 없이 S−C와 혼용 금지. 실제 label로 S를 갱신한 AUC는 독립 예측 아님 |
| 보조 NHANES/국내 자료 | `E:\학술제\보조데이터셋\README.md`, `E:\학술제\보조데이터셋\processed\candidate_age_priors.json` | 관측 집단 참고치이며 수면 필요량/한국 학생 정답 아님. `auto_apply=false` 유지가 타당 |
| Android | `MainActivity.kt:98`, `:224` | 센서 소스가 FakeDataSource, 실제 trained asset 없음. 샘플 앱과 실측 모델을 명확히 구분 |
| 로컬 웹앱 | `web-app/server.py`, `engine.py`, `research.py` | 가입·일지·당일 추천·14일 관측 개인화·저장. 기존 연구 연결은 외부 폴더/선택 라이브러리 의존 |
| iOS | `ios-app/README.md`, `HealthKitSource.swift` | 소스 있음. Xcode/실기기 검증 미완료. 웹 데이터와 자동 동기화 없음 |
| GitHub | origin URL·원격 페이지·로컬 git status | 웹/iOS는 이번 시작 시 미추적 로컬 작업. 원격 쓰기 인증 미확보; 로컬 변경과 공개 완료를 구별 |

### 기존 행동 예측 성능

| 목표 | 선택 모델 | 테스트 MAE | 비교 기준선 | 해석 |
|---|---|---:|---:|---|
| 취침 | 학습 중앙값 | 156.97분 | 156.97분 | 복잡한 모델의 개선이 확인되지 않음 |
| 기상 | Ridge | 166.71분 | 173.77분 | 약 7.05분, 4.1% 차이. 신뢰구간 없는 단일인 30일 결과 |

이는 MESA 성능도, 신규 사용자 성능도, 추천을 따랐을 때의 효과도 아니다. 웹앱에는 주간 연속 센서값이 없어 기존 센서 모델의 상당수 특징이 대치된다(`web-app/research.py`). 따라서 웹 실사용 정확도로 위 수치를 표시하면 오해를 만든다. 센서 없는 **일지 전용 모델**과 센서 기반 모델을 분리해야 한다.

## 4. 우선 수정할 문제

1. **학습/추론 계약 불일치(P0).** Android의 `day_hr_mean_pre20`은 최근 24시간, 연구 코드는 당일 자정~20시. HR 표준편차 분모도 다르다. `prior_duration_median_7`은 Android에서 시간, 개인 연구에서 분이고, 취침/기상 특징도 clock-minute와 cutoff-relative hour가 다르다. 같은 이름이라고 같은 모델에 넣으면 안 된다. (`SnapshotAnalysisPipeline.kt:129–157`, `causal_sleep_timing_forecast.py:67–107`)
2. **미래/수집완료 정보 누수(P0).** Android 걸음 구간은 시작만 결정시각 이전이면 종료가 미래인 구간도 합산될 수 있다(`SnapshotAnalysisPipeline.kt:135`). 수면 종료뿐 아니라 동기화/입수 시각도 조건에 포함한다. S/C를 만들 때 평가 대상 미래 수면 라벨을 사용하지 않는다.
3. **모델 승격 기준 부재(P0).** 기존 Ridge trainer는 alpha 선택 후 항상 `status: trained`를 쓴다(`train_ridge_residual.py:155`). 물리/단순 기준선 대비 개선, 유한계수, all-missing feature, 적용 대상군 검사를 추가해야 한다. 새 웹 계약은 기존 artifact와 자동 호환시키지 않는다.
4. **평가 의미/단위(P0).** 기존 공통 `event_error`는 첫 수면 epoch 차이만 계산하며 기상 오차가 아니다(`evaluate_common.py:10`). onset/wakeup을 별도로 정의하고 예측 누락/거부율을 함께 보고한다. 미매칭 행을 제거한 높은 점수만 보고하지 않는다.
5. **실사용 특징 부족(P1).** 피로·카페인·낮잠·착용 커버리지·주간 활동의 구조화 수집이 필요하다. MESA에는 없는 설문 항목을 0이라고 만들지 않는다. 상태 불명과 각성을 구분한다.
6. **중복 모델 소스(P1).** 루트, `src`, `src-epoch-30s`, `src-epoch-1min`, Android, Swift, Python에 유사 계산이 분산되어 있다. 원본 동결본은 유지하되 향후 하나의 명세와 cross-language golden fixture로 단위·부호·경계가 일치하는지 검사한다.
7. **운영/재현성(P1).** Windows DB 연결 종료 문제를 고치고 실행 명령을 제공한다. 원본 연구 디렉터리 없이도 추천과 경량 추론이 실행되어야 한다. 모델과 데이터 snapshot을 버전으로 연결한다.
8. **효과 주장(P2).** ‘과거 행동을 맞춤’, ‘수면압력 시뮬레이션’, ‘권장 일정의 효과’를 UI·보고서에서 분리한다. 오래/늦게 자는 행동을 그대로 권장값으로 학습하지 않는다.

## 5. 선행연구에서 가져올 부분

| 근거 | 실제로 보여준 것 | SleepWell 적용 |
|---|---|---|
| [Palotti et al., 2019](https://www.nature.com/articles/s41746-019-0126-9) | MESA ACT와 PSG를 이용한 sleep/wake 연구. 정렬·제외 후 1,817명, 사람 단위 평가 | ACT의 큰 규모보다 독립 PSG 정답·정렬·사람 분리가 핵심. 동시 밤 판별을 미래 예측으로 바꾸어 주장하지 않음 |
| [Zhai et al., 2023](https://doi.org/10.1371/journal.pone.0285703) | 품질/중첩 조건을 충족한 808명, ACT+HR 3/4클래스 분류 | HR는 Actiwatch가 아닌 PSG ECG에서 추출. Galaxy PPG로 직접 전이하는 근거가 아님 |
| [Walch et al., 2019](https://pubmed.ncbi.nlm.nih.gov/31579900/) / [원자료](https://physionet.org/content/sleep-accel/1.0.0/) | 실제 Apple Watch HR·ACC와 PSG, 31명. 높은 전체 sleep/wake 정확도에도 wake specificity는 약 59.6% | 전체 정확도만 최적화하지 않고 각성 탐지·단계별 지표 보고. Galaxy 외부 검증 필요 |
| [Daan, Beersma & Borbély, 1984](https://pubmed.ncbi.nlm.nih.gov/6696142/) | 항상성·일주기 상호작용으로 수면 시점 설명 | S/C를 해석 가능한 특징/제약으로 사용. 기본 상수 고정이 모든 개인에 맞는다는 근거는 아님 |
| [Song et al., 2025](https://doi.org/10.1093/sleep/zsaf160) | *A digital, real-time, history-based sleep-management tool to enhance alertness* | 폴더의 ‘수면 논문.pdf’와 일치하는 핵심 앱 선행연구. 기록→생리모델→권장→실행/각성도 평가 흐름 참조 |

기존 `TWO_PROCESS_SIGN_CORRECTION_REPORT.md`는 Song 2023 논문을 인용하지만, 현재 ‘수면 논문.pdf’ 표지는 **2025년 zsaf160**이다. 서로 다른 논문이므로 인용과 결과를 섞지 않는다. 후성유전학 논문/메모는 현재 ACT 예측모델의 입력·정답·효과 검증을 직접 제공하지 않는다.

Song 2025의 후향 분석은 여성 교대근무자 71명, 전향 파일럿은 남성 응급구조 교대근무자 19명이다. 전향 연구는 무작위 대조시험이 아니고, 약 6일의 baseline 이후 약 20일 개입을 관찰했으며 준수도 그룹은 사후 구분했다. 앱이 사용한 이력·가장 이른 취침 가능시각·다음 근무시간과 매일의 각성도 기록 구조는 유용하다. 동일 생리 파라미터와 가정한 조도(각성 250 lux/수면 0 lux)를 사용했다는 제한도 함께 참고해야 한다. SleepWell에서는 추천 전에 알 수 있는 일정 제약과 다음날 결과를 저장하는 설계를 가져오고, 논문의 준수–각성도 연관성을 자체 효과로 주장하지 않는다. [Song et al., 원논문](https://doi.org/10.1093/sleep/zsaf160)

## 6. 회의 결정과 구현 정렬

- **9/12:** MESA 승인 후 전처리, 웨어러블+물리모델 개인화, 당시 Android 우선.
- **9/19–27 업무 문서:** 물리-only / wearable-only / hybrid 비교, 사용자별 분리, 향후 권장 취침·기상으로 연결. 단순 센서 수집 앱에서 최종 추천 제품으로 확대.
- **9/26:** 데이터 모델→물리 보정, 1분 앱 처리, UTC/KST 중복 변환 방지, 내부 CSV 연결 우선, 신규 사용자 자기보고·장기 사용자 본인 데이터 우선.
- **10/02 작업보고:** 30초 동결본과 1분 버전·회귀 테스트, 삼성 시간 해석 확인 과제.
- **이번 사용자 결정:** 웹앱 우선. 예전 Android 우선순위보다 최신 요구를 적용한다.

원본 30초 PSG 평가를 1분 앱 처리와 강제로 같게 만들 필요는 없다. `epoch_duration_sec`, 타임존/relative time, 라벨 출처를 명시한 변환 경계를 둔다. 30→60초 집계는 평균/합/최장겹침/동률 처리 규칙을 특징별로 고정하고 별도 평가한다.

## 7. 권장 실험 설계와 일정

구체적 작업·완료 기준은 워크스페이스 `.omx/plans/sleepwell-mesa-web-2026-10-09.md`에 저장했다. 다음은 우선순위다. 기간은 작업량 추정이며 데이터 승인·다운로드·실제 수집 기간을 보장하지 않는다.

| 단계 | 산출물 | 통과 조건 | 예상 |
|---|---|---|---|
| P0 자료/QC 고정 | snapshot, 코드북, 입력 명세 | 파일 안정성·정렬·필수 항목·결측 검사 | 1–2일 |
| P1 과제별 표 | ACT 일별 proxy / PSG epoch / 본인 일지 | 사람/밤 수, 라벨 출처, 미래값 차단 테스트 | 2–4일 |
| P2 비교 실험 | 기준선·Ridge·트리·S/C ablation | 사람 분리, 사람별 MAE/F1 및 bootstrap CI | 3–5일 |
| P3 앱 대상 검증 | 센서 없는 모델·센서 모델 별도 평가 | 한국 학생/사용자 전이, baseline 개선·거부율 확인 | 2–4일+수집 |
| P4 웹 연결 | 경량 JSON, 상태/예측/이력 | 학습과 추론 재현, 손상/결측/대상군 fallback | 2–3일 |
| P5 효과 파일럿 | 추천·실행·컨디션 종단 자료 | 사전 지정 주요 지표, 비교 설계 | 수 주 이상 |

### 데이터가 계속 늘어날 때

1. 업로드 완료한 파일만 고정 snapshot에 포함한다. 파일별 무결성/내용 hash는 최종 학습 manifest에 별도 보존한다.
2. 사람 기준 split을 고정하고 새로운 사람은 사전 정한 hash 규칙에 따라 배정한다. 현재 탐색 표본을 최종 잠긴 테스트처럼 취급하지 않는다.
3. QC 통과 사람 100/300/600/전체의 학습곡선을 같은 검증 집단에서 비교한다. 이 숫자는 실험용 부분집합 제안이며 확보 사실/충분표본 기준이 아니다.
4. validation으로 후보 선택 후 잠긴 test는 최종 1회 평가한다. 추가 업데이트에는 새로운 평가 버전과 holdout을 명시한다.
5. 사람별 bootstrap과 장치·연령·기록 충실도별 평가로 개선의 범위와 불확실성을 본다.

### 모델 연결 구조

`원본 → 안정된 snapshot/QC → 과제별 특징·정답 → 사람/시간 분리 → baseline·hybrid 비교 → 검증된 JSON → 웹 행동 예측`

웹에서는 **행동 예측 + 물리 상태 + 사용자 기상 제약 → 권장 정책 → 실제 수면·피드백 기록**으로 연결한다. 관찰 라벨을 최적 일정의 정답으로 삼지 않는다. 휴대폰에 수GB 원자료를 싣는 방식이 아니라 작고 버전이 있는 모델을 배포한다.

## 8. 이번 결과의 검증 범위와 미완료 항목

구현·테스트·실행 명령은 `WEB_MESA_DELIVERY_2026-10-09.md`에 기록한다. MESA 실학습, 성능 상승, 실제 학생 대상 전이 검증, 수면 개선 효과, Android/iOS 실기기 검증은 이번 결과에 포함되지 않는다.

Git 원격 확인은 인증이 요구되어 `could not read Username ... terminal prompts disabled`로 실패했다. 원격 페이지의 조회와 로컬 수정은 가능하지만, 인증 없는 상태에서 GitHub push 완료라고 보고하지 않는다. 개인/NSRR 원자료는 공개 저장소에 포함하지 않는다.

## 근거 파일의 범위

- 코드: `SleepWell-integrated`의 웹/Android/iOS/학습 코드 및 워크스페이스 공통 평가·개인 모델·물리 모델.
- 기존 결과: 개인 전처리·모델 JSON, 공통 manifest·검증 JSON, MMASH/NHANES 보고서.
- 회의: 9/12, 9/26 PDF 전체 본문 및 저장소 9/19–27 MD; 10/02 작업보고 PDF.
- 선행논문: 로컬 수면 논문의 제목/출처 대조, 공식 NSRR 및 위 원저자 논문.
- 제한: 대용량 원자료 전체 행/모든 notebook 재실행·모든 논문 전수 문헌고찰은 아님. 최신 Drive/외부 서버 자료를 확인한 것으로 간주하지 않는다.
