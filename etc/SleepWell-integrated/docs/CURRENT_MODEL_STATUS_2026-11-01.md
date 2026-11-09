# SleepWell 현재 모델·평가 계약

최종 대조일: 2026-11-01
코드 기준: `24cae65` 이후 현재 `main`

이 문서는 날짜별 연구 보고서를 대체하지 않고, 현재 실행 코드에 들어 있는 모델·개인화·전향 평가·연구 분석 기능을 한곳에서 찾기 위한 현행 색인이다. 성능 수치는 각 동결 실험 보고서를 따르며, 여기 적힌 “구현됨”은 정확도 향상이나 수면 개선 효과가 입증됐다는 뜻이 아니다.

## 1. 서로 다른 연구 질문

| 영역 | 현재 구현 | 주장할 수 없는 것 |
|---|---|---|
| MESA ACT 파생 라벨 | 활동계 구간의 수면/각성 분류 및 재현 실험 | PSG 단계 분류, 다음날 행동 예측, 수면 개선 효과 |
| MESA PSG 정답 | 공식 overlap과 30초 PSG 판독을 정렬한 별도 수면/각성 평가 | 앱 추천 정책의 우월성 |
| 다음날 행동 예측 | 20:00까지 이용 가능한 이력으로 주수면 입면·최종 기상 시각 예측 | TST·수면의 질·치료 효과 예측 |
| 개인화 | 관측 행동 이력의 최근성 가중과 초기값 수축 | 개인 생리 시간상수의 측정·식별 |
| 추천 정책 | 일정·수면 기회·고정 Two-Process 계산을 사용한 행동 제안 | 추천 때문에 결과가 좋아졌다는 인과효과 |
| 추천 후속 관측 | 취침 전에 실제로 본 추천과 이후 일지의 기술통계 | 무작위 대조 효과, p값, 개인 치료효과 |

## 2. 현재 실행 버전과 동결 산출물

| 구성요소 | 현재 계약·버전 | 코드·산출물 |
|---|---|---|
| 사용자 추천 엔진 | `sleep-opportunity-v7-measurement-evidence`, `personalization-v5-measurement-evidence` | `web-app/engine.py` |
| 연구용 다음날 특징 | `sleepwell_next_night_pre20_v1` | `web-app/night_core.py` |
| 휴대형 융합 모델 | schema 2, `robust-next-night-research-v2` | `web-app/fusion_forecast.py`, `web-app/models/robust_{cmi,mesa}_v2.json` |
| 현재 연구 추론 정책 | `robust-next-night-research-v2+gradual_v1` | 첫 유효 이력부터 `n/(n+14)` 수축 적용 |
| 사전 고정 예측 | `prospective-behavior-v4-locked-monitoring` | `web-app/prospective.py` |
| 온라인 잔차 비교 | 동결 identity·입력 hash를 요구하는 현재 계약 | `web-app/online_trial.py` |
| 센서 Process S | `research_shadow_sensor_s_v1` | `web-app/sensor_process_s.py` |
| 추천 표시 시점 | `recommendation-timing-v1` | `web-app/recommendation_schedule.py` |
| 연구 분석실 | schema 1, 최대 365행·10명·2 MiB CSV | `web-app/research_lab.py` |
| 연구 읽기 API | `/api/v1` | `web-app/research_api.py` |

동결 artifact의 `legacy14` Test 지표와 현재 `gradual_v1` 추론은 같은 결과가 아니다. 현재 런타임은 동결 계수를 재학습하지 않고 추론 정책만 분리해 기록한다. 저장된 과거 성능을 새 정책의 검증 성능으로 재표기하지 않는다.

## 3. 측정 근거를 구분한 개인화

사용자 추천 엔진은 첫 유효 일지부터 최근성 가중 기술통계를 갱신한다. 기록 영향은 기상 후 경과일을 기준으로 14일마다 절반이 되며, 초기값과의 혼합 비중은 `effective_records/(effective_records+14)`다.

- 침대 체류시간에서 입면잠복기와 중간각성 시간이 모두 명시된 경우에만 완전한 관측 수면시간을 계산한다.
- 둘 중 하나가 빠지면 침대 체류시간을 실제 수면시간으로 바꾸어 쓰지 않는다.
- 유효 기록 5일 미만, 최신 기상 후 14일 초과, 또는 수면 중간시각의 원형 집중도가 낮으면 보수 모드로 남긴다.
- 개운함·피로도 조건을 만족하는 완전 일지가 충분하지 않으면 목표 수면시간을 관측값으로 갱신하지 않는다.
- 걸음·운동처럼 결측 가능한 항목은 해당 값이 있는 날만 별도 유효 가중치를 계산한다.
- 고정값 `tau_wake=18.2h`, `tau_sleep=4.2h`, 일주기 진폭 `0.15`는 개인 기록에서 추정한 생리값이 아니다.

NHANES `population_prior.json`은 미국 성인의 자기보고 관측 수면시간 집단 평균이다. 개인의 최적 필요수면이나 다음날 정답이 아니며, 직업별 효과를 포함하지 않는다.

## 4. 센서 기반 Process S는 연구 그림자 경로

`sensor_process_s.py`는 완료되어 cutoff 전에 이용 가능했던 정확히 5분 길이 관측만 받는다. 일지 S, 이진 센서 S, 보정 확률 센서 S를 병렬 계산하되 공식 추천 경로는 계속 `S_diary`다.

- 관측 창이 겹치면 거부한다.
- 확률 경로는 calibration ID가 있는 검증 보정 확률만 점 추정 후보로 쓴다.
- 중간 결측 구간은 수면 가정과 각성 가정의 범위로 전파하며 midpoint·forward fill을 만들지 않는다.
- 불확실성 폭이 임계값을 넘거나 연속 지지가 없으면 일지 S로 되돌아간다.
- `auto`의 확률 경로 선호는 재사용 Test의 후향 S 복원 기술 결과이며 실제 20시 S나 다음날 MAE 우월성을 입증하지 않는다.

따라서 MESA PSG 분류기나 센서 S를 사용자 서비스 모델로 자동 승격한 상태가 아니다.

## 5. 사전 고정 모니터링과 예측구간

`prospective.py` v4는 등록 이후 첫 현지 20:00부터 변경 불가능한 모니터링 preset을 만든다. enrollment, 모델·특징·엔진 버전, artifact identity와 전체 preset을 SHA-256으로 고정한다.

- calibration: 30일
- held-out: 이어지는 30일
- 주 arm: `recent_adaptive`
- 최소 calibration: 적격 최초 일지 결과 20개
- 명목 joint coverage: 90%
- 반경: 입면·기상 절대잔차 중 큰 값의 유한표본 순서통계
- calibration은 첫 held-out 예측 전에 동결해야 한다.

반경이 계산됐다는 사실만으로 개인 90% 예측구간이 검증된 것은 아니다. held-out 실제 포함률과 폭을 별도로 보고해야 하며, 직렬 상관이 제거됐다고 가정하지 않는다. calibration 부족, identity 불일치, 늦게 이용 가능해진 라벨, 불완전 endpoint는 명시적으로 제외한다.

## 6. 추천 표시 시점과 효과 기록

표시 정책은 모델의 20:00 특징 cutoff를 바꾸지 않는다. 최초 미리보기는 예상 입면 2시간 전과 20:00 중 이른 시점이며, 20·21·22·23시 갱신은 직접 상호작용 또는 명시적인 현재 관측으로 깨어 있음이 확인된 경우에만 한 번씩 허용한다. 수면 기록이 없다는 이유만으로 각성이라고 추정하지 않는다.

추천 후속 결과는 같은 enrollment의 직접 일지 중, 추천을 실제 취침 전에 본 경우만 기술한다. 생성됐지만 보지 않은 추천, 취침 이후 노출, 시각을 확인할 수 없는 노출은 분리한다. 개운함·피로·졸림·부담 척도는 범위와 도구가 같은 경우에만 합친다.

현재 반환값은 항상 `causal_effect_estimate=null`, `randomized_assignment=false`다. 동의·사전 배정·미래 추적이 구현된 운영 시험이 아니므로 추천 효과나 통계적 유의성을 주장하지 않는다.

## 7. 연구 분석실과 관측 데이터 진단

연구 분석실은 원 DB를 재학습하거나 수정하지 않는 무상태 후향 재생 도구다. 동결 모델과 점진 개인화를 같은 causal row에서 비교하며 합성 예제는 기능 시연일 뿐 정확도 근거가 아니다.

`sleep_observation.py`는 모델 예측과 독립적으로 다음을 계산한다.

- SRI: 연속 관측 밤 쌍 7개 이상
- IS/IV: 관측 밤 7개 이상
- 주말·주중 중간시각 차: 각 2밤 이상
- 다음 20시 이전/이후 입력, prediction-only, 누락 구분
- 입력 지연 중앙값·90백분위와 관측 coverage

주수면 구간 밖을 각성으로 두는 기술통계이며 낮잠과 이차 수면은 입력 계약에 없으면 보이지 않는다. 활동량 기반 IS/IV나 임상 액티그래피 지표와 같은 값으로 해석하지 않는다.

`/api/v1`은 같은 참여자별 snapshot을 읽는 loopback 전용 읽기 API다. 참가자 요약, 상세, 표준 5열 CSV, 시스템·overview·OpenAPI를 제공한다. 쓰기는 기존 CSRF 보호 경로를 사용한다.

## 8. MESA PSG 재현 계약

현재 PSG 재현 코드는 `tools/model/mesa_psg_experiments.py`다. 기존 baseline artifact·manifest hash와 공식 overlap 파일을 고정하고, participant split을 보존하며, 비어 있거나 제외된 XML 비율을 감시한다. 30초 판독 overlap을 정답으로 사용하고 ACT 파생 sleep/wake와 구분한다.

현재 결과와 수치는 [PSG 검증 보고서](RESEARCH_PSG_RESULTS_2026-10-20.md)를 따른다. 이 변경은 재현성과 provenance를 강화한 것이며 새 외부 Test 성능이나 앱 추천 효과를 만든 것이 아니다.

## 9. 배포 경계

- Android에는 `population_prior.json`, `robust_cmi_v2.json`, `robust_mesa_v2.json`이 포함되지만 연구 모델의 출력과 추천 정책은 분리한다.
- 사용자 원자료, 개인별 학습 모델, 로컬 DB, 연결 비밀키는 Git과 공개 배포물에 넣지 않는다.
- 새 실험은 `E:\학술제\데이터모델\연구용데이터\새_버전_폴더`에 저장하고 기존 결과를 덮어쓰지 않는다.
- 연구 모델을 자동으로 서비스 모델이나 건강효과 모델로 승격하지 않는다.

## 10. 관련 문서

- 모델 필드와 시간 계약: [interface-schema-모델-입출력.md](interface-schema-모델-입출력.md)
- 최신 연구 보완 기록: [RESEARCH_UPDATE_2026-10-25.md](RESEARCH_UPDATE_2026-10-25.md)
- MESA PSG 결과: [RESEARCH_PSG_RESULTS_2026-10-20.md](RESEARCH_PSG_RESULTS_2026-10-20.md)
- 다음날 모델 결과: [NEXT_NIGHT_RESULTS_2026-10-20.md](NEXT_NIGHT_RESULTS_2026-10-20.md), [NEXT_NIGHT_IMPROVEMENT_2026-10-20.md](NEXT_NIGHT_IMPROVEMENT_2026-10-20.md)
- 전향 저장 구조: [PROSPECTIVE_APP_2026-10-20.md](PROSPECTIVE_APP_2026-10-20.md)
- 연구 분석실 운영: `../desktop/RESEARCH_LAB_GUIDE.txt`
