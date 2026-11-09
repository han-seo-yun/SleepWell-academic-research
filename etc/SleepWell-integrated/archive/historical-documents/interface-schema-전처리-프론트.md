# 전처리 결과 ↔ 프론트 인터페이스 (공통 스키마)
**작성자:** 채윤 | **날짜:** 2026-09-24 | **상태:** 합의용 초안

---

## 1. 배경
- 삼성 헬스 수면 단계 CSV → 전처리 → 앱 입력 형식 변환 후 프론트(소빈)로 전달
- 혜지 전처리 데이터(`sleep_stage_cleaned.csv`) 기준, 서윤 #8 공통 스키마 확정 시 맞춘다
- 이 문서는 **채윤(백엔드) → 소빈(프론트)** 전송 데이터의 열·형식·의미를 정의한다

---

## 2. 전처리 결과 데이터 형식

### 2-1. 소스 데이터: `sleep_stage_cleaned.csv`
혜지 전처리 완료본 열 구성:

| 열 이름 | 형식 | 설명 |
|---|---|---|
| `start_time` | `yyyy-MM-dd HH:mm:ss` | 구간 시작 시각 |
| `end_time` | `yyyy-MM-dd HH:mm:ss` | 구간 종료 시각 |
| `duration_min` | float (분) | 구간 지속 시간 |
| `stage` | int (삼성 코드) | 40001=WAKE, 40002=LIGHT, 40003=DEEP, 40004=REM (N2로 매핑하지 않음) |
| `stage_name` | string | 한글 단계명 |
| `sleep_id` | UUID | 하룻밤 세션 식별자 |
| `datauuid` | UUID | 행 고유 식별자 |
| `time_offset` | string | `UTC+0900` (KST) |

### 2-2. 백엔드 파싱 후 내부 형식 (`SleepStageRow`)
`CsvFileSource.kt` 기준:

```
SleepStageRow(
  startMs: Long,       // epoch ms (KST 기준)
  endMs:   Long,       // epoch ms (KST 기준)
  stage:   SleepStage, // WAKE / LIGHT / DEEP / REM
  sleepId: String      // 세션 UUID
)

SleepStage: WAKE / LIGHT / DEEP / REM (문자열 enum)
(참고: md 기준 Galaxy Watch 수면 단계는 AWAKE/LIGHT/DEEP/REM 4단계로 통일. PSG와 비교할 때만 매핑)
```

### 2-3. SleepSession 으로 그룹화
동일 `sleepId` → 1개 `SleepSession`:

```
SleepSession(
  sessionId: String,
  startMs:   Long,
  endMs:     Long,
  stages:    List<SleepStageSegment>,
  device:    DeviceInfo(sourceName="Samsung Health CSV", deviceType=WATCH)
)

SleepStageSegment(
  startMs: Long,
  endMs:   Long,
  stage:   SleepStage
)
```

---

## 3. 프론트로 전달할 데이터 (결과 화면용)

결과 화면(소빈)에서 그래프·기록 표시에 사용하는 데이터.

### 3-1. 수면 기록 (최근 수면 및 활동 기록)
- `List<SleepSession>` 전체 또는 최근 N개
- 각 세션: 시작·종료 시각, 단계별 구간 목록 (startMs, endMs, stage)
- **용도:** 수면 단계 타임라인 그래프, 총 수면시간, 단계별 비율 계산

### 3-2. 30초 epoch 변환 데이터 (모델 입력 겸용)
Two-Process 모델 입력을 위해 세션의 stage 구간을 30초 epoch로 분할:

```
EpochRecord(
  timestampMs: Long,  // epoch 시작 시각 (KST, 30초 간격)
  isSleep:     Boolean // 해당 epoch에 수면 중이면 true
)
```

- 단계 구간은 startMs~endMs 사이의 30초 경계로 분할
- WAKE → isSleep=false, LIGHT/DEEP/REM → isSleep=true
- 사용자가 입력한 직접 데이터(취침·기상 시각 등)도 이 형식으로 변환하여 모델에 함께 전달

### 3-3. 직접 입력 데이터 (사용자 입력 화면)
프론트가 사용자로부터 받아 백엔드로 전달하는 항목:

| 항목 | 형식 | 예시 |
|---|---|---|
| 취침 시각 | `Long` (epoch ms, KST) | 2026-09-13 23:00:00 KST |
| 기상 시각 | `Long` (epoch ms, KST) | 2026-09-14 07:00:00 KST |
| 목표 기상 시각 (선택) | `Long` (epoch ms, KST) | 2026-09-14 08:00:00 KST |
| 예정 취침 시각 (선택) | `Long` (epoch ms, KST) | 2026-09-13 24:00:00 KST |

> 직접 입력은 CSV 업로드가 없을 때 또는 보완 입력으로 사용. CSV가 있으면 CSV 데이터가 우선.

---

## 4. 인터페이스 합의 포인트 (채윤 ↔ 소빈)

| # | 항목 | 현재 상태 | 합의 필요 |
|---|---|---|---|
| 1 | 전처리 결과 데이터 전달 방식 | **확정: 객체 직접 전달** (같은 Kotlin 앱 내) | 추가 작업 없음 |
| 2 | epoch 30초 경계 처리 기준 | **확정: 마지막 조각(30초 미만)도 포함**. 해당 구간 stage 기준으로 isSleep 부여 | 소요 시간 거의 없음 (구현 시 주의) |
| 3 | 시각화용 stage 구간 vs epoch 데이터 | SleepStageSegment 리스트(그래프용) + epoch 리스트(모델용) 별도 | 그래프용 stage 구간만으로도 그래프 충분 → epoch는 모델 전용 |
| 4 | 데이터 정렬 기준 | startMs 오름차순 | 전송 범위: 모델=최근 7일, 기록 화면=전체 |
| 5 | 시간대 | KST 고정 (CSV time_offset=UTC+0900) | 에뮬레이터 UTC 오류 수정 후 재확인 필요 |

---

## 5. 참고: 서윤 #8 공통 스키마 반영 예정 항목
- `subject_id` 추가 (현재는 sleep_id로 대체)
- 심박·걸음 데이터 열 추가 (원본 확보 후)
- 30초 epoch 통일 표기

> 서윤 #8 확정 시 이 문서의 열 구성을 수정한다.
