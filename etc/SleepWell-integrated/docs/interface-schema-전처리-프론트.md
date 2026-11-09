# 전처리·Android·연구 프론트 인터페이스

원문 작성: 2026-09-24 · 현재 코드 대조: 2026-11-01

## 계약 분리

초기 문서는 삼성 헬스 CSV 하나를 앱 프론트와 모델에 전달하는 합의용 초안이었다. 현재 구현은 목적별 계약이 나뉘므로 하나의 “공통 스키마”로 합치지 않는다.

| 계약 | 현재 생산자 | 현재 소비자 | 상태 |
|---|---|---|---|
| 사용자 수면 일지 | `UserRepository`, `UserModels` | `UserModelEngine`, 사용자 화면·백업 | 현재 사용자 경로 |
| Health Connect 수면 | `HealthConnectSleepReader` | `SyncRepository`의 `/v1/sync` | Android 송신 구현 |
| 모바일 생활 맥락 | `HealthConnectContextReader`, `DailyContext` | `/v1/context` → `mobile_context_store.py` | 송수신 스키마 불일치로 종단간 미검증 |
| 연구 데모 스냅샷 | `FakeDataSource` → `HealthSnapshot` | `MainActivity`, `EpochAggregator`, `Exporters` | 가상 데이터 연구 데모 |
| 연구 분석실 입력 | 5열 canonical row | `research_lab.py`, `research_api.py` | 무상태 후향 재생·기술 통계 |
| 과거 CSV 어댑터 | `src*/CsvFileSource.kt` | 과거 Kotlin 변형 | 현재 Android 빌드에 미포함 |

## 사용자 앱 저장 계약

`UserState`는 Android Keystore로 보호되는 로컬 상태에 저장한다. 핵심 식별자는 무작위 16바이트를 32자리 소문자 hex로 만든 `participant_key`다.

### 프로필·기록

- `UserProfile`: 연령대, 성별, 활동시간, 평소 취침·기상, 희망 수면시간, 가입시각, 직업, 선택적 `daily_context`.
- `SleepRecord`: ID, 기상일, 취침·기상 시각, 구간 길이, 선택적 개운함·피로, 출처, `modified_at`.
- 직접 일지와 Health Connect 가져오기는 출처를 구분한다. 웨어러블 수면 단계에서 주관적 개운함·피로를 추정하지 않는다.
- 최초 예측, 최초 결과, 수정 결과, 추천 노출은 별도 배열로 보존한다. 최신 화면값이 과거 스냅샷을 덮어쓰는 근거가 되어서는 안 된다.

### 추천 게시 시점

웹 `recommendation_schedule.py`의 공개 정책은 `recommendation-timing-v1`이다. 예상 입면 2시간 전과 20:00 중 이른 시각에 최초 결정을 만들고, 20·21·22·23시에 사용자가 깨어 있음이 직접 확인된 상호작용에서만 조건부 갱신할 수 있다.

- 수면 기록 부재만으로 “깨어 있음”을 추정하지 않는다.
- 지난 슬롯을 소급 생성하지 않고 같은 슬롯을 두 번 게시하지 않는다.
- 게시 시점 정책은 연구 모델의 20:00 특징 cutoff를 이동시키지 않는다.
- Android `UserRepository`도 `recommendation_timing`, `recommendation_publications`, `evaluated_recommendation_slots`를 별도 보존한다.

## Health Connect → PC 수면 계약

수면 요청은 스키마 1이다.

```json
{
  "schema_version": 1,
  "sessions": [
    {
      "id": "Health Connect metadata id",
      "modified_at": "offset timestamp",
      "start": "offset timestamp",
      "end": "offset timestamp",
      "stages": [{"start": "...", "end": "...", "stage": "light"}]
    }
  ],
  "deleted_ids": []
}
```

- Android 14 이상 및 `READ_SLEEP` 권한이 필요하다.
- Samsung Health 패키지 출처만 전송한다.
- 최대 100세션·1 MB이며 같은 기상일을 묶음 중간에서 나누지 않는다.
- 조회 범위 전체 페이지가 성공한 경우에만 사라진 원본 ID를 `deleted_ids`로 보낸다.
- 수정 버전과 삭제 저널은 PC 성공 응답 전까지 유지한다.

## 모바일 생활 맥락 계약

의도된 이벤트 종류는 `steps`, `exercise`, `schedule`, `nap`, `caffeine`이다. 센서 출처는 `samsung_health`, 직접 입력 출처는 `manual_android`다.

PC 저장 계약(`mobile_context_store.py`)은 스키마 2이며 다음 원칙을 강제한다.

- `participant_key`: 32자리 소문자 hex.
- 모든 시각: 오프셋 포함 ISO 8601. 발생 시각, 원본 수정 시각, 기기 관측 시각, PC 최초 수신 시각을 구분한다.
- `available_at`: PC가 해당 버전을 처음 받은 시각이다. 과거 사건을 오늘 받았다고 과거 20시 예측에 소급하지 않는다.
- `complete_scopes`: 전체 조회가 성공한 `steps`/`exercise` 범위에만 사용한다. 해당 범위에서 빠진 기존 이벤트를 삭제할 수 있으므로 부분 실패 시 선언하지 않는다.
- 직접 입력은 `group_id`와 `modified_at`으로 최신 버전·명시적 삭제를 처리한다.
- 일정은 `night_id`, `schedule_kind`, `preparation_minutes`; 낮잠·카페인은 `night_id`를 `details`에 포함해야 한다.

현재 Android 송신 코드는 스키마 1이며 `details`를 보내지 않으므로 이 PC 계약을 통과하지 못한다. 또한 저장소에 `/v1/context` HTTP 수신 라우트가 없다. 이 부분은 구현 완료로 표시하지 않고 종단간 차단 사항으로 관리한다.

## 연구 데모 30초 격자

`EpochAggregator`의 현재 열은 다음과 같다.

```text
subject_id,night,session_id,epoch_idx,stage_label,time_iso,hr_mean,steps
```

| 필드 | 의미 |
|---|---|
| `night` | 로컬 정오~다음 정오 기준 날짜 `yyyyMMdd` |
| `epoch_idx` | 해당 날짜 정오부터의 30초 시각 좌표 |
| `stage_label` | `0=W, 1=N1, 2=N2, 3=N3, 4=REM`, 미분류 `-1` |
| `hr_mean` | 칸 안의 심박 평균, 표본이 없으면 빈 값 |
| `steps` | 겹친 시간 비율로 배분한 구간 걸음 합계 |

마지막 30초 미만 조각도 그 격자의 행으로 남고, 단계가 없으면 `-1`로 좌표를 보존한다. 이 규격은 연구 데모 내보내기용이며 사용자 앱 추천 모델이나 모바일 동기화 payload가 아니다.

## 연구 분석실 canonical row

연구 분석실의 수면 입력은 다음 다섯 필드를 정확히 사용한다.

```text
participant_id,cutoff,sleep_start,sleep_end,observed_at
```

- `participant_id`: 1~64자의 가명 ID.
- `cutoff`: 정확히 `20:00:00 Asia/Seoul`에 해당하는 오프셋 시각.
- 결과가 없는 예측 행은 `sleep_start`, `sleep_end`, `observed_at`을 모두 null로 둔다.
- 결과가 있으면 세 필드를 모두 채우고 `observed_at >= sleep_end`를 만족해야 한다.
- 이 입력은 현재 동결 모델을 후향 재생하고 관측 자료를 기술하는 용도다. 원본 DB를 수정하거나 새 모델을 학습하지 않는다.

## 시간 표현 원칙

Unix epoch는 절대 시각이며 “KST epoch”라는 별도 숫자 체계가 아니다. Android 연구 데모 내부의 `Long`은 epoch milliseconds이고, API·JSON 시각은 오프셋 포함 ISO 8601을 사용한다. 화면의 날짜·`night` 분류에만 명시된 현지 시간대 규칙을 적용한다. 초·밀리초·나노초를 이름 없이 혼용하지 않는다.
