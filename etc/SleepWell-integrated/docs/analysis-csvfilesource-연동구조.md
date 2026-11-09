# CsvFileSource와 현재 Android 연동 구조

원문 작성: 2026-09-24 · 코드 대조 갱신: 2026-11-01

## 현재 결론

`CsvFileSource.kt`는 현재 Android 사용자 앱의 실행 소스가 아니다. 파일은 비교·보존용 변형인 `src/`, `src-epoch-30s/`, `src-epoch-1min/`과 `archive/legacy-root-kotlin/`에만 남아 있다. 현재 `android-app`의 일반 사용자 경로는 다음과 같이 분리된다.

| 목적 | 현재 구현 | 입력·출력 |
|---|---|---|
| 사용자 수면 가져오기 | `sync/HealthConnectSleepReader.kt` | Samsung Health가 Health Connect에 제공한 완료 수면 세션·단계 → `SyncSleepSession` |
| 걸음·운동 연동 | `sync/HealthConnectContextReader.kt` | Samsung Health의 `StepsRecord`, `ExerciseSessionRecord` → PC 생활 맥락 이벤트 |
| PC 수면 동기화 | `sync/SyncRepository.kt` | `/v1/sync`, 스키마 1, 세션 최대 100건·본문 1 MB |
| 로컬 연구 데모 | `health/FakeDataSource.kt` | 고정 seed의 가상 심박·걸음·수면 → `HealthSnapshot` |
| 연구 내보내기 | `health/EpochAggregator.kt`, `export/Exporters.kt` | 30초 격자 `EpochRow`와 CSV/JSON |

따라서 과거 문서의 “`CsvFileSource`를 SamsungHealthSource 대신 주입하면 현재 앱이 동작한다”, “걸음·심박 CSV를 추가하면 사용자 앱 연동이 완성된다”는 설명은 현재 구조에 적용하지 않는다.

## 현재 Health Connect 수면 계약

`HealthConnectSleepReader`는 Android 14(API 34) 이상에서 `READ_SLEEP` 권한이 있을 때만 실행한다. 조회 범위의 모든 페이지를 읽고 데이터 출처 패키지가 `com.sec.android.app.shealth`인 기록만 남긴다.

- 완료되지 않은 세션, 빈 원본 ID, 30분 미만 또는 16시간 초과 세션은 전송 대상에서 제외한다.
- 단계는 `awake`, `out_of_bed`, `sleeping`, `light`, `deep`, `rem`, `unknown` 문자열로 전송한다.
- 단계 구간이 세션 밖에 있거나 서로 겹치거나 500개를 넘으면 세션은 유지하되 단계 목록을 비운다. 없는 단계를 만들어 채우지 않는다.
- `metadata.lastModifiedTime`을 수정 버전으로 사용하며 전체 조회가 성공한 경우에만 PC의 과거 사본 삭제를 계산한다.
- 같은 기상일의 세션은 한 묶음으로 유지한다. 기상일은 마지막 수면 단계 종료 시각을 `Asia/Seoul`로 변환해 결정한다.

## 걸음·운동 계약

과거 `CsvFileSource.readSteps()`의 빈 목록은 현재 Health Connect 연동의 상태를 설명하지 않는다. `HealthConnectContextReader`는 별도 권한으로 다음 자료를 읽는다.

| 종류 | 권한 | 값 |
|---|---|---|
| 걸음 | `READ_STEPS` | 시작·종료 시각과 구간 합계 `count` |
| 운동 | `READ_EXERCISE` | 시작·종료 시각. 현재 운동 종류·강도 계수는 모델 입력으로 사용하지 않음 |

각 범위의 전체 페이지 조회가 성공한 종류만 `complete_scopes`로 표시할 수 있다. 권한이 없거나 조회가 실패한 종류는 빈 관측과 구분하며, 실패한 범위를 “기록 없음”으로 간주해 PC 자료를 삭제하면 안 된다. 현재 `HealthConnectContextReader`는 U Extensions 7 API인 `Record.metadata`를 사용하면서 확장 SDK 가드를 선언하지 않아 `lintDebug` 오류가 남아 있다.

## 연구 데모의 30초 epoch 계약

`HealthDataSource`/`HealthSnapshot`/`EpochAggregator`는 현재 `MainActivity`의 가상 데이터 연구 데모와 내보내기에 사용한다. 일반 사용자 `UserActivity`의 Health Connect 동기화 payload와 다른 계약이다.

```text
subject_id,night,session_id,epoch_idx,stage_label,time_iso,hr_mean,steps
```

- `night`: 로컬 정오부터 다음 정오까지를 한 밤으로 본 기준 날짜 `yyyyMMdd`.
- `epoch_idx`: 그 날짜 정오부터의 30초 좌표. 세션별 행 번호가 아니다.
- `stage_label`: `0=W, 1=N1, 2=N2, 3=N3, 4=REM`, 미분류 `-1`.
- 30초 칸을 가장 오래 덮은 단계를 사용하며 구간이 없으면 `-1`을 유지한다.
- 심박은 칸 안의 표본 평균, 걸음은 원 구간과 겹친 시간 비율로 배분한다.
- 같은 밤에 시간상 겹치는 세션이 있으면 `(night, epoch_idx)`가 중복될 수 있으므로 `selfCheck()`가 오류로 보고한다.

이 내보내기는 모델 평가용 좌표 규격이다. 사용자 수면 일지, 모바일 백업, PC `/v1/sync`, 생활 맥락 `/v1/context`와 합치지 않는다.

## 현재 확인된 연동 차단 사항

현재 Android `SyncRepository`의 생활 맥락 요청은 `schema_version=1`이며 이벤트에 `details` 필드를 넣지 않는다. 반면 `web-app/mobile_context_store.py`는 `schema_version=2`와 정확한 이벤트 필드 집합을 요구하고, 일정·낮잠·카페인에는 `night_id` 등의 `details`를 요구한다. 저장소 안에는 `/v1/context`를 제공하는 HTTP 수신 구현도 포함되어 있지 않다.

따라서 현재 소스만 기준으로는 수면 `/v1/sync`와 생활 맥락 `/v1/context`를 동일하게 “검증 완료된 연동”으로 표시하면 안 된다. 걸음·운동·직접 입력 생활 맥락의 종단간 전송은 Android 송신 스키마, PC 수신 라우트, `mobile_context_store` 스키마를 같은 버전으로 맞춘 뒤 다시 검증해야 한다.

## 보존된 CSV 자료의 역할

과거 `sleep_stage_cleaned.csv`와 `CsvFileSource.kt`는 전처리·프론트 초기 합의 및 epoch 변형 비교 자료다. 현재 사용자 앱에 다시 연결하려면 다음을 별도 결정해야 한다.

- 현재 `SleepStage` 코드(`0=W, 1=N1, 2=N2, 3=N3, 4=REM`)로의 명시적 매핑
- Unix epoch 단위와 입력 오프셋 검증
- Health Connect 자료와 CSV 자료의 출처 우선순위 및 중복 제거
- 사용자 동의·파일 선택·오류 표시 UI

현재 코드에는 이 어댑터가 없으므로 CSV를 자동 대체 입력으로 간주하지 않는다.
