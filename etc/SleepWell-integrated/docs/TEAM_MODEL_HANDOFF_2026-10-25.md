# SleepWell 연구 모델 인계 (2026-10-25 기준 / 2026-11-01 현재 구현 보완)

이 문서는 Android 사용자 앱과 Windows/웹 연구자용 소프트웨어 개발자가 현재 연구 모델을 같은 버전으로 사용할 수 있도록 경로와 검증 상태를 정리한다.

2026-10-25에 만든 모델·집계 결과는 역사적 검증 자산으로 보존한다. 현재 소스는 2026-10-31 `software-update` 이후 계약을 사용하므로 아래 버전·경계를 우선한다.

## 기준 구현

- Android 실행 엔진: `android-app/app/src/main/java/app/sleepwell/user/`
- Android 동결 모델: `android-app/app/src/main/assets/models/`
- 연구자용 실행 엔진: `web-app/`
- 권장·측정 엔진: `web-app/engine.py` (`sleep-opportunity-v7-measurement-evidence`)
- 공통 다음날 특징·Process S: `web-app/night_core.py`, `web-app/sensor_process_s.py`
- 포터블 CMI/MESA 추론: `web-app/fusion_forecast.py`
- 잠금 전향 모니터링·예측구간: `web-app/prospective.py`, `web-app/online_trial.py`
- 노출 후 기술통계: `web-app/habit_outcomes.py`
- 관측 규칙성·완결성: `web-app/sleep_observation.py`
- 연구자용 동결 모델·집계 검증: `web-app/models/`
- 재학습·재검증 코드: `tools/model/`
- Windows 연구자용 런처·동기화 소스: `desktop/`
- 연구 설계와 집계 결과: `docs/`

## 현재 탑재 모델

- `robust_cmi_v2.json`: CMI 기반 다음날 수면 시각 데이터/융합 모델
- `robust_mesa_v2.json`: MESA 기반 다음날 수면 시각 데이터/융합 모델
- `population_prior.json`: 신규 사용자 집단 초기값
- Android는 위 세 파일을 앱 assets에서 직접 읽는다.
- 연구자용 소프트웨어는 `web-app/models/`의 동일 모델과 개인화·타당도 집계 파일을 읽는다.

### 현재 실행 식별자

| 구분 | 현재 계약 | 해석 |
|---|---|---|
| 권장 엔진 | `sleep-opportunity-v7-measurement-evidence` | 일지 측정 완전성과 보고 시각을 명시적으로 구분 |
| 개인화 매개변수 | `personalization-v5-measurement-evidence` | 결측 일지를 완전한 수면시간으로 보간하지 않음 |
| 동결 코호트 모델 | `robust-next-night-research-v2` | CMI/MESA 연구용 모델 파일 |
| 런타임 추론 | `robust-next-night-research-v2+gradual_v1` | 첫 유효 과거 기록부터 점진 반영; `legacy14` 지표와 구분 |
| 전향 모니터링 | `prospective-behavior-v4-locked-monitoring` | 등록·아티팩트·기간·입력 해시가 잠긴 미래 예측 |
| 센서 S 연구 | `research_shadow_sensor_s_v1` | 5분 센서 관측 병렬 복원; 공식 경로는 계속 `S_diary` |

전향 모니터링은 등록 후 미래 30일 calibration과 다음 30일 held-out을 고정한다. calibration 결과 20개 이상에서만 사전 지정 q90 공동 반경을 동결하며, 명목 90%는 개인별 보장이나 효과 추정이 아니다. 새 정책의 예측구간은 저장 당시 값으로 held-out 포함률과 폭을 평가하며 재적합하지 않는다.

## 개인화

- Android: `AdaptiveResidualPersonalization.kt`, `UserModelEngine.kt`
- 연구자용: `adaptive_personalization.py`, `personalization.py`
- 첫 해결된 기록부터 매일 갱신하며 최근 기록과 기록 공백을 감쇠 가중치로 반영한다.
- 웨어러블 기록은 보조 지표이며, 개인화의 주 결과는 시간 순서가 확인된 사용자 결과를 사용한다.
- `gradual_v1`은 활성 문턱을 없애지만 적은 기록을 충분한 검증으로 바꾸지 않는다. 과거 `legacy14` Test 지표는 점진 런타임의 성능 근거가 아니다.
- 일지 측정은 침상시간, 입면잠복기, 중간각성 및 기타 각성시간의 완전성을 보존한다. 관측·설문 발생 시각이 없으면 기상시각으로 조용히 대체하지 않는다.
- 센서 Process S는 모델 ID와 보정 ID가 일관된 인과적 5분 관측만 병렬 계산한다. 공백 뒤에는 구간만 유지하고 공식 추천 입력으로 자동 선택하지 않는다.

## 연구 출력의 분리

- PSG 수면/각성 분류: 독립 PSG 30초 라벨과 손목 센서 분류를 비교한다.
- 다음날 행동 시각 예측: 20시 이전 이력으로 다음 주수면 입면·최종 기상을 예측한다. PSG 분류 성능과 다른 목표다.
- 개인화: 동결 모델을 교체하지 않고 시간순 이력의 점진 반영 또는 실험적 잔차 sidecar를 별도 버전으로 저장한다.
- 추천 효과: 실제 취침 전 노출 뒤 결과를 기술할 뿐 `causal_effect_estimate=None`이다. 무작위 또는 통제 설계 전에는 개선 효과를 주장하지 않는다.

## 검증 상태

- 2026-11-01 웹 전체 테스트: 345개 통과, 1개 건너뜀.
- 데스크톱 모바일 동기화 테스트: 10개 통과.
- Android `assembleDebug`: 성공. `lintDebug`: Health Connect U Extensions 7 API 조건 5건 때문에 실패했으므로 lint 통과로 기록하지 않는다.
- `tools/model` 기본 환경 실행: 98개 중 4개가 `pyarrow`/parquet 엔진 부재로 오류. 이는 통과가 아니며 연구 의존성을 설치한 격리 환경에서 재검증해야 한다.
- 기존 2026-10-25의 104개·205개 수치는 당시 범위의 기록이며 현재 HEAD 전체 검증 개수로 사용하지 않는다.

## 데이터 및 보안 경계

- 개인 삼성헬스 원자료, MESA 원자료, 참여자 DB와 휴대폰 백업은 Git에 포함하지 않는다.
- 모델 파일에는 학습된 계수·트리, 집단 통계, provenance hash와 집계 성능만 포함한다.
- APK/EXE, 컴파일 클래스, 인증서, 키, 로컬 DB는 Git에 포함하지 않는다.
- 모델은 연구 비교용이며 전향적 사용자 효과가 확인되기 전에는 치료 효과나 수면 개선이 입증되었다고 표현하지 않는다.
- 실험적 잔차 보정은 `experimental_sidecar_not_promoted`, 추천 노출 분석은 `observational_only`로 유지한다. 두 결과 모두 서비스 모델 승격 근거가 아니다.
