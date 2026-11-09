# 모델 입력·출력: 현재 실행 계약

원 초안: 채윤, 2026-09-24. 코드 대조 갱신: 2026-10-20, 서윤 담당 모델 검증 작업.

이 문서는 현재 `web-app` 코드의 동작을 설명한다. 팀의 새로운 합의를 대신하지 않는다. Kotlin 임시 연결을 설명한 [이전 초안](../archive/historical-documents/interface-schema-모델-입출력.md)은 보존한다. 현재 웹앱에는 S·C 계산과 권장 시각 계산이 구현되어 있으므로 과거의 “준비 중” 설명을 현재 상태로 사용하지 않는다.

## 1. 모델 세 가지의 역할

| 구성 | 입력 → 출력 | 현재 적용 범위 |
|---|---|---|
| `web-app/engine.py` | 사용자 프로필·완료 수면 이력 → S, C, 조건부 미래 곡선, 권장 시각 | `sleep-improvement-v1` 연구용 정책. 수면 개선 효과는 미검증 |
| `web-app/forecast_model.py` 및 `research.py` | 과거 기록 → 이후 취침·기상 행동 시각 | 휴대형(portable) 모델과 구형(legacy) 개발 모델의 경로가 다름. 아래 계약 참조. 권장 시각 계산에는 미사용 |
| `tools/model/mesa_psg_experiments.py` | MESA 손목 활동·조도 → 해당 관측 구간의 PSG 수면/각성 분류 | 연구용 회고적 분류. 일지 입력 계약 및 다음날 예측과 다름 |

`server.py`는 `recommend()` 결과에 별도 `behavioral_forecast`를 붙인다. 연구 모델이 저장되어 있다는 사실만으로 웹에 연결되거나 효과가 검증된 것은 아니다.

## 2. 현재 정책 모델의 입력

`recommend(profile, records, now, prior=None)` 호출 전에 프로필과 수면 기록을 검증한다. 웹의 프로필/기록 API가 저장 시 검증하고, 서버가 가입 시각을 관리한다.

| 구분 | 필드·단위 | 의미 |
|---|---|---|
| 프로필 | `name`, `age`, `occupation`, `created_at` | 사용자 정보, 가입 시각 |
| 시계 시각 | `usual_bed`, `usual_wake`, `target_wake`, `weekend_wake`: `HH:MM` | 평소 패턴과 기상 제약 |
| 수면·활동 기준 | `usual_sleep_h`, `desired_sleep_h`: 시간; `latency_min`, `exercise_min`: 분; `average_steps`: 걸음 | 초기 정책 파라미터 |
| 과거 자료 연결 | `link_history`: boolean, 선택적 `prior` | 명시적으로 연결한 본인 과거 기록의 참고 통계 |
| 완료 기록 | `bed`, `wake`: ISO 8601 시각; 선택적 `latency_min`, `awake_min`, `fatigue`, `rested`, `steps`, `exercise_min` | 완료된 수면. `sleep_min`은 침대에 있던 시간에서 입면·각성을 뺀 값 |
| 서버가 부여하는 관측 출처 | `source`, `observed_at` | 현재 `/api/record`는 `source=diary`, `observed_at=등록 시각`으로 저장한다. 임의의 과거 관측 시각을 입력받지 않는다 |

내부 시각 기준은 `Asia/Seoul`이다. 오프셋이 있는 입력은 같은 절대 시각을 유지하며 KST로 변환하고, 오프셋이 없는 입력은 KST로 해석한다. Unix epoch는 시간대와 별개인 절대 시각이므로 “KST epoch”를 별도의 숫자 체계로 정의하지 않는다. 다른 앱과 연동할 때 초·밀리초·마이크로초·나노초를 명시적으로 변환한다. 이 웹 정책 경로는 pandas 정수 시각을 나노초로 가정하지 않고 `datetime` 차이의 초를 사용한다. 과거 노트북의 시간 단위 문제까지 수정했다는 의미는 아니다.

정규화된 기록을 받는 모델 함수에는 `source=healthkit` 지원이 있지만, 현재 서버에 HealthKit 수집 API가 구현되어 있다는 뜻은 아니다. 현재 기록 입력 API는 일지 경로다.

## 3. 물리식의 부호와 파라미터

현재 코드의 convention은 **`propensity = S + C_sleep`**이다.

- 수면 시: `S_next = S * exp(-Δt_hours / 4.2)`.
- 각성 시: `S_next = 1 - (1-S) * exp(-Δt_hours / 18.2)`.
- `C_sleep = 0.15 * cos(2π * (clock_min - phase_min) / 1440)`.
- S는 0~1의 정규화된 모델 상태, C는 −0.15~+0.15다. propensity는 두 값의 합이며 확률이 아니다. 이론상 −0.15~1.15가 가능하다.
- C의 양수 방향은 이 정의에서 수면 성향을 더한다. 각성 촉진 C를 쓰는 `S-C` 식과 부호만 혼용하면 안 된다. 위상과 C 정의를 함께 비교해야 한다.

S는 7일 전 0.5에서 시작해 5분 간격으로 재구성한다. 기록 없는 날은 사용자가 입력한 평소 패턴으로 보완한다. 따라서 직접 측정한 생리적 압력이 아니며, 낮잠과 밤중 각성의 실제 시각은 완전히 재현되지 않는다.

초기 위상은 평소 취침·기상의 중간 시각이다. 가입 후 경과 14일 및 최근 28일의 유효 관측 14일을 모두 만족하면 위상·수면 목표·입면시간·활동 기준을 제한적으로 보정한다. τw=18.2시간, τs=4.2시간 및 C 진폭은 고정이며 개인별로 학습한 생리 상수가 아니다.

## 4. 현재 정책 출력과 해석

| 필드 | 실제 형식·해석 |
|---|---|
| `policy_version`, `objective`, `generated_at`, `plan_date` | 버전·목적·생성 기준 |
| `bed`, `wake`, `wind_down` | 오프셋 포함 ISO 8601. 기상 제약과 수면 기회를 반영한 권장 계획 |
| `sleep_opportunity_min` | 권장 취침~기상 사이의 시간. 실제로 잠든 시간의 보장이 아님 |
| `target_sleep_min`, `feasible` | 정책상 목표 및 시간 확보 가능 여부 |
| `current_s` | 과거 이력과 보완 가정으로 재구성한 S |
| `chart` | 10분 간격 145개 점, 현재부터 24시간. 각 점은 `time`, `s`, `c`, `propensity` |
| `parameters`, `reasons`, `warnings`, `evidence` | 계산 기준·입력 출처·제약·검증 수준 |

`chart`의 미래 S는 **계속 깨어 있다고 가정한 조건부 경로**다. 실제 취침/기상 행동을 예측하여 수면 중 S 감소를 반영한 곡선이 아니다. 권장 시각은 목표 기상·수면시간·입면 여유·최근 부족분·규칙성에 따른 정책 비교 결과다. 데이터에서 관측된 취침 시각을 맞히는 회귀 모델의 정답으로 취급하지 않는다.

이전 Kotlin 설계의 `currentC`, `futureS`, `recommendedBedTimeMs`와 현재 웹의 필드 이름·구조는 다르다. 어댑터 없이 이름만 맞춰 연결하지 않는다. 현재 C와 propensity는 `chart[0]`에 있다.

## 5. 행동 예측 계약

### 5-1. 휴대형 모델

`forecast_model.py`의 계약은 `web_completed_history_pre20_v1`, 파일 유형은 `sleepwell_behavior_clock_linear`다. 최근 28일 안에서 최대 7회의 완료된 diary/HealthKit 기록을 사용한다. `wake`와 `observed_at`이 모두 결정 기준 20:00보다 이른 기록만 사용할 수 있다.

정해진 여섯 특징은 과거 취침/기상 offset 중앙값, 과거 수면 구간 길이 중앙값, 직전 구간 길이, 이력 수, 주말 여부다. offset은 20:00 이후 분 단위다. 해당 길이는 `bed`~`wake` 구간 길이이며 PSG 총수면시간과 동일하지 않다.

정확한 특징 순서·단위·전처리·두 target·집단 범위·평가 gate를 통과한 파일만 불러온다. 이 모델 함수는 적용 집단이나 이력이 맞지 않으면 `available=false`와 이유를 반환한다. 성공 시 `bed`, `wake`, `cutoff`, `model_version`, `feature_contract`, `observed_history_records`를 반환하고 “과거 행동패턴 참고 · 권장 시각 계산에는 미사용” 역할을 명시한다.

### 5-2. 현재 앱에 남아 있는 구형 개발 모델 경로

`research.py`는 portable 예측이 실패하거나 없을 때에도, 사용자가 본인 과거 자료 연결을 켰고 구형 로컬 모델 두 개를 읽었다면 legacy 경로를 사용한다. 따라서 portable의 거절이 최종 앱 응답의 `available=false`를 항상 뜻하지는 않는다.

legacy는 20:00 이전 bed/wake의 최근 7개 기록을 사용하지만 28일 범위와 `observed_at` 조건을 검사하지 않는다. 센서가 없는 웹 입력에서는 해당 특징을 기존 학습 중앙값으로 대치한다. 출력은 `available`, `cutoff`, `bed`, `wake`, `role`, `warning`이며 portable의 버전·특징 계약·관측 개수 필드가 없다. 별도 모델 상태 응답에는 `source=legacy`, `model_version=legacy-local-development`가 표시된다.

이 경로를 portable의 정보 누수 방지·집단 적용 규칙을 통과한 모델처럼 인계하면 안 된다. 현재 UI의 경고처럼 기존 테스트 성능을 그대로 적용할 수 없는 개발용 참고 결과다. 이번 작업에서는 실행 동작을 바꾸지 않고 차이를 문서화했다. 검증된 휴대형 모델로 전환할 때 legacy fallback의 유지 여부를 별도로 정해야 한다.

MESA 모델은 센서 입력이 필요하므로 이 계약의 대체 파일로 사용할 수 없다. 개인 센서 결합 모델도 일지-only 모델로 변환 없이 연결할 수 없다.

## 6. 서윤 담당 인계 기준

현재 코드와 문서를 기준으로 S·C 부호/단위/위상, 입력 시각, 결측 보완, 출력 의미를 일치시켰다. 물리 정책의 수면 개선 효과, 새로운 사용자의 다음 취침/기상 예측 개선, 개인별 τ 식별은 이번 코드 확인이나 MESA PSG 평가만으로 완료되지 않는다.

이후 후보를 교체할 때는 같은 입력·같은 목적에서 기존 기준선과 비교하고, 새로운 사용자/기간의 평가 자료를 분리하며, 모델 버전과 전처리·파라미터·불확실성을 함께 기록한다. 임의로 `research_only` 표시를 해제하지 않는다. 자세한 최신 분석은 [PSG 검증 보고](RESEARCH_PSG_RESULTS_2026-10-20.md)를 참조한다.
