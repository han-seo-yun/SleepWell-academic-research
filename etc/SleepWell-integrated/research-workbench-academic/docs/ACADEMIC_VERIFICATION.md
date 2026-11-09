# SleepWell 학술제 전용판 검증 기록

검증일: 2026-11-01 (Asia/Seoul)

## 검증 범위

이 검증은 `samples/sleepwell_50days.csv`의 명시적 합성 참여자 1명, 50일 자료만 사용했다. 실제 임상·참여자 자료나 가짜 관측 결과를 추가하지 않았다. 원본 SleepWell 산식은 `vendor/sleepwell`의 SHA-256 검증된 소스를 직접 실행했으며, 일반적인 scikit-learn 모델로 바꾸지 않았다.

- 입력 SHA-256: `38b2817df93615866af0eb4de341ef6d9536ded27ffaa8a022af236c3fda77ba`
- 원본 소스 매니페스트 SHA-256: `1fc27b3e89fcfc7ae7b259d39bb4fb65601af64e57330af224caf6fea129a394`
- 런타임 모델: `robust-next-night-research-v2+gradual_v1`
- 개인화 정책: `gradual_v1`
- 저장 프로젝트: `projects/SleepWell_Academic.swa`
- 결과 및 내보내기: `example_output_academic`
- 기계 판독 검증 기록: `example_output_academic/verification_summary.json`

실행 명령은 다음과 같다.

```powershell
.\.venv\Scripts\python.exe scripts\verify_academic_release.py
```

스크립트에는 `if __name__ == "__main__"` 보호가 있으며, 기존 검증 레코드가 있으면 immutable terminal record를 덮어쓰지 않는다.

## 실행 결과

총 14개 실행이 완료됐고 실패는 없었다. 프로젝트를 다시 열면 결과 화면에서 후향 평가가 선택되며, `joint_mae_min` 비교 그래프에 9개 arm이 표시된다.

| 구분 | 결과 |
| --- | ---: |
| 저장된 실행 | 14 |
| 완료 | 14 |
| 실패 | 0 |
| 직접 요청한 주 내보내기 | 212 파일 |
| CSV companion을 포함한 전체 내보내기 | 552 파일 |

실행 목록:

- 50일 후향 평가
- 50일 점진적 개인화 평가
- 50일 전향 chronological playback
- 연구 진단 보고서
- 측정 과정 감사
- 타깃 타당성 감사
- 휴대폰 동결 기준선 평가 계약 확인
- 인구집단 사전값 선택
- CMI 단일 다음날 모델
- Samsung 개인 연결 단일 다음날 모델
- 연속 상수, 전체 타당성 표, 개인화, 타깃 타당성 저장 평가 산출물 4종

## 후향·개인화 성능

50행 중 `recent7`이 최초 밤에는 계산될 수 없으므로 모든 arm의 직접 비교 대상은 공통 49밤이다. MAE 단위는 분이며 참여자 동일가중이다. 후향 모드와 개인화 모드는 같은 원본 replay와 입력을 사용하므로 아래 값이 일치하고, 개인화 화면은 추가로 밤별 가중치와 상태를 보여준다.

| 모델 arm | 입면 MAE | 기상 MAE | 공동 MAE | 공통 밤 |
| --- | ---: | ---: | ---: | ---: |
| `cmi.physical_fixed` | 47.83 | 24.29 | 36.06 | 49 |
| `cmi.physical_personal` | 24.06 | 18.65 | 21.35 | 49 |
| `cmi.data` | 19.32 | 33.30 | 26.31 | 49 |
| `cmi.hybrid` | 21.01 | 26.70 | 23.86 | 49 |
| `mesa.physical_fixed` | 38.49 | 42.09 | 40.29 | 49 |
| `mesa.physical_personal` | 19.13 | 24.75 | 21.94 | 49 |
| `mesa.data` | 13.58 | 15.24 | 14.41 | 49 |
| `mesa.hybrid` | 13.53 | 14.99 | 14.26 | 49 |
| `baseline.recent7` | 10.65 | 11.48 | 11.06 | 49 |

이 결과는 합성 표본과 해당 실험 조건에서 나온 예측 오차다. 피처나 모델의 인과적 효과, 임상적 유용성 또는 일반 모집단 우승 모델을 의미하지 않는다.

## 전향 재생 성능

`prospective_demo`는 미래 결과가 실제로 수집된 전향시험이 아니라, 시간 순서와 사전 저장 규칙을 검증하는 합성 chronological playback이다. 50개 forecast가 모두 이후 outcome과 평가됐으며 총 13개 arm이 나왔다.

| 모델 arm | 공동 MAE(분) | 평가 밤 |
| --- | ---: | ---: |
| `profile_baseline` | 37.57 | 50 |
| `recent_adaptive` | 26.00 | 50 |
| `context_candidate` | 26.00 | 50 |
| `physical_cmi` | 21.26 | 50 |
| `data_cmi` | 26.55 | 50 |
| `hybrid_cmi` | 24.42 | 50 |
| `physical_mesa` | 22.07 | 50 |
| `data_mesa` | 14.90 | 50 |
| `hybrid_mesa` | 14.74 | 50 |
| `online_data_cmi` | 24.08 | 50 |
| `online_hybrid_cmi` | 21.61 | 50 |
| `online_data_mesa` | 13.86 | 50 |
| `online_hybrid_mesa` | 13.69 | 50 |

전향 demo의 성능 수치는 독립적인 실제 미래 검증 근거가 아니다. 진짜 전향 평가는 20:00~20:15에 저장된 예측과 그 이후 처음 관측된 같은 밤 결과를 append-only 방식으로 축적해야 한다.

## 보고서 입력과 근거 경계

측정 감사와 타깃 타당성 감사에는 같은 50개 canonical 행을 다음과 같이 명시적으로 매핑했다.

- `sleep_start` → `bedtime`
- `sleep_end` → `wake_time`
- `observed_at` → `modified_at`
- `sleep_end - sleep_start` → 반올림된 `duration_minutes`
- 원본 범주값이 없으므로 검증 설정에서 명시한 `diary` → `source`
- `participant_id + source + row` → 고유 record ID

타임스탬프 값은 바꾸지 않았다. 두 감사 모두 입력 50개를 유효 diary 50개로 처리했다. 타깃 타당성 감사의 diary 관측일 coverage는 50/50이었지만, wearable/PSG 쌍이 없으므로 소스 간 정확도나 gold-standard 일치도는 계산하지 않았다.

CMI 및 Samsung 단일 모델은 동일한 50개 과거 기록으로 실행됐다. CMI는 research scope에서 `hybrid`, Samsung은 명시적인 `link_history=True` 조건에서 `data` arm을 선택했고 각각 50개 이력 기록을 사용했다. 이것은 단일 다음날 예측 확인이며 새로운 성능 검증이 아니다.

휴대폰 기준선 평가는 사전 동결된 Android `research_baselines` envelope가 입력에 없으므로 matched night 0, 모델 결과 0으로 남겼다. canonical CSV에서 이 구조를 만들어 내거나 0을 성능처럼 표시하지 않았다. 실제 평가는 앱이 결과보다 먼저 저장한 원본 phone forecast/outcome JSON이 필요하다.

저장 검증 산출물 4종은 `retrospective_reused_test` 또는 각 원본 evidence status를 그대로 표시한다. 이 실행에서 재학습·재계산된 결과가 아니다. 외부 검증 계획은 완료된 근거로 표시하지 않는다.

## 내보내기 검증

완료된 각 실행에서 현재 기록된 값만 사용해 표와 그림을 만들었다.

- 언어: 한국어, 영어
- 그림: 고해상도 PNG, 벡터 PDF, SVG
- 표: CSV, XLSX
- 주요 그림: 성능 비교, 실제값-예측값, 개인화 추이, 반복 오차 분포, 출처/원본 보고서
- CSV는 요약 외에도 예측값, 개인화, 모델 정보, provenance, metadata 및 경고를 companion 파일로 내보낸다.

`verification_summary.json`의 `exports` 배열에는 552개 파일의 상대 경로, 바이트 크기, SHA-256 및 primary 여부가 기록돼 있다. 한국어와 영어 전환은 표시 문구만 바꾸며 수치와 원본 열 이름을 바꾸지 않는다.

## 추가 검증

- 학술 백엔드 단위 테스트: 8개 통과
- 원본 API와 adapter 동일 입력 비교: `elapsed_ms`를 제외하고 동일
- gradual 개인화 가중치: `n/(n+14)` 확인
- CSV 보고서 변환: 비어 있지 않은 입력, 명시적 source, 단일 참여자 및 provenance 확인
- 다중 참여자 CSV를 단일 참여자 보고서로 자동 합치지 않고 오류 처리
- 프로젝트 terminal record는 저장 후 수정 불가
- 프로젝트 시작 화면: 결과 비교, 후향 50일 실행, `joint_mae_min`, comparison

## 남은 제한사항

- 이 릴리스 검증 자료는 합성이므로 임상 성능, 실제 사용자 효능 또는 외부 일반화 근거가 아니다.
- 휴대폰 기준선의 실제 성능은 올바른 사전 동결 phone envelope가 없어서 검증하지 않았다.
- wearable-vs-diary 및 PSG 일치도는 대응 관측이 없어 계산하지 않았다.
- 저장된 과거 검증표의 원자료가 번들에 없으므로 표를 새로 학습·재현하지 않고 엄격히 검증해 표시했다.
- GUI의 영어 경고와 원본 보고서 문구는 원본 근거를 보존한다. 번역된 scope 안내와 함께 표시되며 원본 계산을 변경하지 않는다.
