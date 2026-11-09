# 모델 입력·출력: 현재 실행 계약

원 초안: 2026-09-24 · 코드 대조 갱신: 2026-11-01

이 문서는 현재 `web-app`, `android-app`, `tools/model`의 모델 경계를 설명한다. 날짜가 붙은 실험 보고서는 해당 시점의 결과를 보존하며, 현재 실행 계약은 이 문서와 실행 코드가 우선한다.

## 1. 모델과 정책의 역할

| 구성 | 현재 버전·계약 | 입력 → 출력 | 적용 범위 |
|---|---|---|---|
| 웹 권장 정책 `engine.py` | `sleep-opportunity-v7-measurement-evidence`, `personalization-v5-measurement-evidence` | 프로필·완료 일지·당일 맥락 → 권장 취침·기상, S/C, 근거 상태 | 로컬 사용자 정책. 개선 효과 미검증 |
| 다음날 연구 추론 `fusion_forecast.py` | artifact schema 2, `robust-next-night-research-v2+gradual_v1` | 20시 전 가용 이력 → CMI/MESA 물리·데이터·융합 입면/기상 예상 | 연구용 비교. 일반 사용자 자동 승격 금지 |
| 사전 고정 평가 `prospective.py` | `prospective-behavior-v4-locked-monitoring`, `sleepwell_prospective_pre20_v4_locked_monitoring` | 20시 입력 스냅샷 → 고정 비교군·모니터링 단계·선택적 예측구간 | 전향 모니터링. 치료 효과 모델 아님 |
| 병렬 센서 S `sensor_process_s.py` | `research_shadow_sensor_s_v1` | 완료된 5분 센서 관측 → diary/binary/probability S 경로와 불확실성 | 연구 shadow 경로. 공식 권장 S는 diary 유지 |
| 추천 게시 시점 `recommendation_schedule.py` | `recommendation-timing-v1` | 계획 밤·예상 입면·현재 각성 확인 → 최초/시간별 게시 슬롯 | 표시·갱신 시점만 결정. 모델 cutoff 불변 |
| MESA PSG `tools/model/mesa_psg_experiments.py` | 동결 protocol/manifest 기반 | ACT·조도 → 같은 관측 구간의 PSG 수면/각성 분류 | 회고적 분류. 다음날 예측과 별개 |

Android `UserModelEngine.kt`는 웹 코드를 직접 실행하지 않는 별도 구현이다. 현재 Android 상수는 추천 엔진 `sleep-opportunity-v6-daily-personalization`, 개인화 `personalization-v4-daily-recency`, 추천 정책 `wake_constraint_plus_sleep_opportunity_v3_daily_personalization`이며 포터블 CMI/MESA 모델과 Android 연구 기준선을 함께 계산한다. 이름이 유사하다는 이유로 웹 v7/v5와 수치 parity를 가정하지 않고 제공된 JVM parity 검사를 기준으로 확인한다.

## 2. 웹 사용자 입력과 측정 의미

### 프로필

| 구분 | 필드·단위 | 의미 |
|---|---|---|
| 식별·가입 | `name`, `age`, `occupation`, `created_at`, 선택적 `enrollment_id` | 로컬 사용자와 가입 경계 |
| 시계 | `usual_bed`, `usual_wake`, `target_wake`, `weekend_wake`: `HH:MM` | 평소 패턴과 기상 제약 |
| 기준값 | `desired_sleep_h`, `usual_sleep_h`: 시간; `latency_min`, `exercise_min`: 분; `average_steps`: 걸음 | 사용자 선언 초기값 |
| 집단 초기값 | `sex`, `activity_hours_per_day` | NHANES 관측 평균 집단 선택. 개인 최적값 아님 |
| 과거 연결 | `link_history`, 선택적 `prior` | 명시적으로 연결한 본인 과거 자료의 참고 통계 |

### 완료 일지

`bed`와 `wake`는 침대 구간이다. `engine.py`는 원시 필드에서 매번 파생 측정을 재계산한다.

- `time_in_bed_min = wake - bed`.
- `sleep_start = bed + latency_min`은 입면잠복기가 있을 때만 계산한다.
- `sleep_min = time_in_bed_min - latency_min - awake_min`은 두 손실값이 모두 있을 때만 계산한다.
- 하나라도 없으면 `duration_status`에 결측 사유를 남기며 침대시간을 총수면시간으로 대체하지 않는다.
- `rested`는 1~5, `fatigue`, `sleepiness`, `burden`은 0~10이고 `sleepwell_custom_not_validated` 척도다.
- `survey_occurred_at`은 기상 이후부터 수신 시각 사이여야 한다.
- 서버 일지 입력은 `source=diary`, `received_at=observed_at=등록 시각`으로 저장한다. 사용자가 임의의 과거 관측 시각을 제출하지 않는다.

`source`가 `diary`가 아니거나 가입 정체성과 맞지 않는 기록, 기상 전·현재 이후에 관측된 기록, 관측 시각이 없거나 잘못된 기록은 삭제하지 않고 개인화 계산에서 제외한다. 같은 기상일의 여러 버전은 가용 시각과 canonical 내용으로 결정한 최신 한 건만 사용한다.

## 3. 권장 정책의 개인화

현재 웹 개인화는 유효 기록 14일을 기다리는 이진 전환이 아니다.

- 유효 기록 1건부터 `effective_records/(effective_records+14)`로 초기값에 수축해 점진 반영한다.
- 기상 후 경과일에 14일 반감기를 적용하고 Kish 유효 표본 수와 필드별 유효 가중치를 별도 기록한다.
- 위상은 유효 가중치 5 미만, 마지막 기록 14일 초과, 원형 resultant 0.5 미만이면 보수적으로 프로필 기준을 유지한다.
- 수면 목표 보정에는 `sleep_min`이 완전하고 개운함 4 이상·피로 3 이하인 날이 최소 5 유효 가중치 필요하다. 사용자 목표에서 ±30분으로 제한한다.
- 평일·주말 중심은 각 2건 이상일 때 기술 통계로만 표시하며 추천을 바꾸지 않는다.
- `τw=18.2h`, `τs=4.2h`, C 진폭 0.15는 고정 공학 파라미터이며 개인 생리 상수 추정값이 아니다.

주요 출력은 `cumulative_valid_days`, `effective_records`, `kish_effective_n`, `field_effective_records`, `personalization_weight`, `evidence_quality`, `unknown_observation_time_excluded_count`, `weektype_pattern`이다.

## 4. 웹 권장 출력

| 필드 | 형식·해석 |
|---|---|
| `policy_version`, `parameter_version`, `generated_at`, `plan_date` | 계산 버전과 기준 |
| `bed`, `wake`, `wind_down` | 오프셋 포함 ISO 8601 권장 계획 |
| `sleep_opportunity_min` | 권장 취침~기상 구간. 실제 총수면시간이 아님 |
| `target_sleep_min`, `feasible` | 정책상 목표와 확보 가능 여부 |
| `current_s` | 일지·가정으로 재구성한 모델 상태. 생리 측정값이 아님 |
| `chart` | 10분 간격 24시간 조건부 S/C/propensity 경로 |
| `parameters`, `reasons`, `warnings`, `evidence`, `context_flags` | 출처·결측·제약·근거 상태 |

`propensity = S + C_sleep`이고 C의 양수 방향이 수면 성향을 더한다. S는 수면 중 `S·exp(-Δt/4.2)`, 각성 중 `1-(1-S)·exp(-Δt/18.2)`로 갱신한다. `chart`의 미래 S는 계속 깨어 있다는 조건부 경로이지 실제 행동 예측이 아니다.

다음날 일정은 기상 제약과 점유 구간에 사용할 수 있고 완료된 낮잠은 S 재구성에 포함할 수 있다. 카페인은 기록만 보존하며 현재 개인 효과 계수나 섭취 권고량이 없다.

## 5. 다음날 연구 예측

공유 특징 계약은 `sleepwell_next_night_pre20_v1`, 강건 모델 특징 계약은 `sleepwell_next_night_robust_v2`다.

- 결정 시각은 `Asia/Seoul` 20:00이다.
- `bed`가 침대 진입이면 명시된 `latency_min`을 더해 입면을 만든다.
- 입면과 최종 기상 모두 20시 전 끝나고 20시 전 관측 가능했던 완료 기록만 특징에 들어간다.
- 목표는 다음 주수면 **입면과 최종 기상 시각**이다. PSG 단계·총수면시간·건강 개선을 예측하지 않는다.
- CMI/MESA 각 집단의 `physical`, `data`, `hybrid` 출력을 함께 보존한다. 대표 모델 선택은 학습 당시 검증 규칙이며 Test나 현재 사용자 결과로 재선택하지 않는다.
- 포터블 artifact는 중복 JSON 키, 비유한수, 특징 순서·전처리 크기·tree 인덱스, source hash를 엄격히 검증한다.
- 런타임 `gradual_v1`은 첫 유효 기록부터 수축 개인화를 적용한다. artifact에 저장된 `legacy14` Test 지표를 새 런타임 정책의 검증 성능으로 표시하지 않는다.

## 6. 병렬 센서 Process S

연구 경로는 `S_diary`, `S_sensor_binary`, `S_sensor_probability`를 같은 diary anchor에서 병렬 계산할 수 있다.

센서 관측은 정확히 5분 구간이어야 하고 `sleep_binary`, 0~1 `sleep_probability`, `available_at`, `observation_model_id`를 포함한다. 확률 경로를 점으로 사용하려면 `probability_calibrated=true`와 일관된 `calibration_id`가 필요하다.

- 종료 또는 가용 시각이 cutoff 이후인 관측은 제외한다.
- 관측 구간이 겹치거나 한 재구성 안에서 모델·보정 provenance가 바뀌면 거절한다.
- 지지 구간에 틈이 생기면 sleep/wake 양 극단으로 진행시켜 bounds만 남기며 midpoint, forward fill, 임의 sleep/wake 가정을 선택하지 않는다.
- 자동 연구 선택은 보정된 확률 경로가 연속이고 불확실성 한도를 넘지 않을 때만 가능하다.
- 공식 권장 경로는 항상 `S_diary`다. 코드에 든 후향 PSG 기술 MAE는 실제 20시 S, 다음날 예측, 사용자 개선 우월성의 증거가 아니다.

## 7. 사전 고정 모니터링과 예측구간

`prospective.py`의 기본 비교군은 `profile_baseline`, `recent_adaptive`, `context_candidate`이며 사전 선택 주 모델은 `recent_adaptive`다. 유효한 fusion artifact가 제공된 경우 CMI/MESA의 물리·데이터·융합 arm을 추가 보존한다.

예측 스냅샷은 프로필, 시점별 일지 버전, 당일 맥락, 파라미터, 특징, artifact 정체성, 입력 SHA-256을 저장한다. 20:00~20:15에 생성된 것만 `primary_window`이며 과거 밤을 소급 생성하지 않는다.

고정 모니터링을 등록하면 다음 규칙을 사용한다.

- `monitoring_lock`은 등록 이후 첫 20시부터 시작하며 enrollment, artifact identity, 엔진 파라미터 버전, 주 arm을 고정한다.
- calibration 30일과 held-out 30일을 분리한다.
- calibration에서 주 arm의 입면·기상 절대 오차 중 큰 값으로 paired residual을 만들고 최소 20건이 있을 때 finite-sample q90 반경을 동결한다.
- 보정은 첫 held-out 예측 전에 끝나야 하며 held-out 결과를 다시 반경 선택에 쓰지 않는다.
- 예측구간은 주 arm에만 붙고 사전 정의한 입면·기상 지지 범위로 자른다. 관측 포함률은 held-out에서 별도 평가한다.

이 구간은 개인의 임상적 90% 수면 범위나 권장 시각의 효과 구간이 아니다.

## 8. 추천 게시 시점

`recommendation-timing-v1`은 예상 입면 2시간 전과 20:00 중 이른 시각을 `initial_decision_at`으로 둔다. 이후 20·21·22·23시 슬롯은 현재 각성이 직접 확인될 때만 한 번 실행한다.

- 과거 슬롯을 다음 상호작용에서 소급하지 않는다.
- 수면 기록이 아직 없다는 이유만으로 각성으로 간주하지 않는다.
- `RecommendationPublication`은 night, revision, slot, kind, scheduled/published 시각, 입력 fingerprint, 당시 추천을 보존한다.
- 추천 게시·조회는 모델 성능이나 정책 효과가 아니다. 노출 전후 결과는 대조군 없는 기술 통계이며 `causal_effect_estimate`로 승격하지 않는다.

## 9. 모바일·연구 API 경계

Android Health Connect 수면, 걸음·운동, 직접 입력 맥락은 서로 다른 권한과 payload를 사용한다. 현재 생활 맥락은 Android 송신 스키마 1과 PC 저장 스키마 2가 불일치하고 `/v1/context` 수신 라우트가 저장소에 없어 종단간 완료 상태가 아니다. 자세한 계약은 [전처리·프론트 인터페이스](interface-schema-전처리-프론트.md)와 [CsvFileSource/현재 연동 구조](analysis-csvfilesource-연동구조.md)를 따른다.

로컬 읽기 API `research_api.py`는 분석실과 같은 참여자별 스냅샷을 사용해 기술 통계와 CSV를 제공한다. `participant_id`는 `pc` 또는 연결 기기에서 파생된 가명 ID이며, 쓰기는 기존 CSRF 보호 경로를 사용한다. API의 SRI·IS·IV·완결성은 관측 기록 설명이지 모델 정확도·임상 판단·추천 효과가 아니다.

## 10. 해석 경계

- MESA ACT 파생 라벨 재현, MESA PSG 수면/각성 분류, 다음날 입면·기상 예측, 추천 정책 효과는 서로 다른 주장이다.
- 집단 artifact·후향 재생·합성 데이터 성능을 실제 사용자 미래 성능으로 일반화하지 않는다.
- 연구 모델을 서비스 주 모델이나 권장 정책으로 자동 승격하지 않는다.
- 모델 파일에는 계수·tree·집단 통계·provenance만 포함하고 원자료·개인 DB·연결 비밀은 포함하지 않는다.
- 현재 융합 모델이 데이터 단독보다 우수하거나 추천이 수면을 개선한다는 결론은 확인되지 않았다.
