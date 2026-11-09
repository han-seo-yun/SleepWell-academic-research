# CsvFileSource.kt 기준 앱 연동 구조 분석
**작성자:** 채윤 | **날짜:** 2026-09-24 | **상태:** 분석 문서

---

## 1. 현재 구조 요약

`CsvFileSource.kt`는 `HealthDataSource` 인터페이스를 구현하며, 삼성 헬스 수면 단계 CSV를 읽어 앱이 기대하는 공통 타입(`SleepSession`, `SleepStageSegment`)으로 변환한다.

**핵심 설계 의도:**
- SDK(`SamsungHealthSource`)를 대체하는 드롭인 교체 요소
- 인터페이스가 같으므로 기존 화면/그래프/export 코드는 수정 불필요
- 서윤 #8 공통 schema 확정 시 이 파일만 수정하면 됨

---

## 2. 파일 구성 분석

### 2-1. 의존성
```
package app.sleepwell.health
의존: HealthDataSource (인터페이스), HealthQuery, HealthResult,
      SleepStage, SleepSession, SleepStageSegment, DeviceInfo, DeviceType,
      HeartRateSample, StepSample (심박/걸음 — 현재 빈 목록 반환)
```

### 2-2. 클래스의 입력 경로
```
CsvFileSource(
  sleepStageFile:   File?   // sleep_stage_cleaned.csv 파일 경로
  sleepSummaryFile: File?   // sleep_summary_cleaned.csv (선택, 미사용)
  sleepStageStream: InputStream?  // assets 등에서 직접 입력
)
```

### 2-3. HealthDataSource 인터페이스 구현 메서드

| 메서드 | 현재 동작 | 비고 |
|---|---|---|
| `name` | `"CSV 파일"` | 소스 표시명 |
| `sourceTag` | `"csv"` | 소스 구분 태그 |
| `isAvailable()` | sleepStageFile 존재 또는 stream 있으면 true | 파일 유무 확인 |
| `hasPermissions()` | 항상 true | CSV는 권한 불필요 |
| `requestPermissions()` | Success(Unit) | 즉시 통과 |
| `readHeartRate()` | 빈 목록 반환 | **혜지 CSV에 심박 없음 → 추후 추가 필요** |
| `readSteps()` | 빈 목록 반환 | **혜지 CSV에 걸음 없음 → 추후 추가 필요** |
| `readSleepSessions()` | CSV 파싱 → SleepSession 리스트 반환 | 핵심 기능 |

---

## 3. CSV 파싱 흐름 (`readSleepSessions`)

```
[CSV 파일] 
  → parseSleepStageCsv()        // 행 단위 파싱
    → startMs, endMs, stage, sleepId 추출
    → samsungCodeToSleepStage() // 40001→WAKE, 40002→N2, 40003→N3, 40004→REM
  → groupIntoSessions()         // sleepId 기준 그룹화
    → SleepSession 생성
      (sessionId, startMs, endMs, stages[], device)
  → filtered (query 범위 필터)
  → HealthResult.Success(sessions)
```

### 3-1. `parseSleepStageCsv` 상세
- UTF-8 BOM 처리 (`\uFEFF` 제거)
- 열 인덱스 탐색: `start_time`, `end_time`, `stage`, `sleep_id`, `time_offset`
- `time_offset` 기반 타임존 적용: `+0900`→KST, `+0000`→UTC
- `SimpleDateFormat("yyyy-MM-dd HH:mm:ss")` / `.SSS` 버전 사용
- 결과: `List<SleepStageRow>` (startMs, endMs, stage, sleepId)

### 3-2. `samsungCodeToSleepStage` 매핑
```
40001 → WAKE
40002 → N2   (Light → N2, 팀 합의: 없는 N1 정보 만들지 않음)
40003 → N3   (Deep)
40004 → REM
else  → UNKNOWN
```

> **주의:** md #5 관련해 "Light를 N2로 표시(N1은 항상 0). md 기준 4단계(LIGHT) 표기로 바꿀지 확인 필요" — 팀장 확인 필요.

### 3-3. `groupIntoSessions`
- `sleep_id`로 그룹화 → 1개 세션
- 각 세션: startMs(첫 행), endMs(마지막 행), stages(모든 행 → SleepStageSegment[])
- 정렬: startMs 기준

---

## 4. 앱 내 연동 지점

### 4-1. 데이터 소스 교체
기존 `HealthDataSource` 구현체를 `SamsungHealthSource` → `CsvFileSource`로 교체.
프론트(소빈) 쪽 코드 변경 없음 (인터페이스 동일).

### 4-2. 결과 화면 흐름 (현재 구조 기반)
```
CsvFileSource.readSleepSessions(q)
  → List<SleepSession>
  → [프론트] SleepSession 데이터로 그래프 표시
      (현재 수면 단계 타임라인, 세션 요약)
  → [백엔드/모델] 세션 데이터를 epoch 변환 → 모델 입력
  → [모델] 분석 결과 반환
  → [프론트] AnalysisResult로 결과 화면 표시
```

### 4-3. export (연구자 화면)
- `readSleepSessions` 결과를 CSV/JSON으로 내보내기
- 연구자 화면: 30초 epoch·수면 그래프·export → 기존 기능 유지 (팀장 답변)

### 4-4. 사용자 직접 입력 화면
- CsvFileSource는 CSV 기반 데이터만 제공 → 직접 입력은 별도 UI/데이터 구조 필요
- 직접 입력 데이터는 `HealthDataSource` 인터페이스를 통하지 않고 별도입력 방식

---

## 5. 현재 한계 및 수정 필요 사항

### 5-1. 심박·걸음 데이터 부재
- 현재 `readHeartRate()`, `readSteps()`는 빈 목록 반환
- 혜지 전처리 데이터는 수면 단계만 포함 → 심박·걸음 원본 CSV 필요
- 추후 원본 확보 시: 새 CSV 파서 추가 또는 CsvFileSource에 파싱 로직 확장

### 5-2. 시간대 오류 (확인됨)
- 에뮬레이터 시간대: UTC → 수면 시각이 9시간 앞당겨 표시됨
- 실제: 2026-09-13 18:24~23:10 KST → 화면: 09:24~14:10
- night 값도 하루 어긋남 (20260912 vs 20260913)
- **CsvFileSource.kt 시간대 처리:** `TimeZone.getTimeZone("Asia/Seoul")`로 고정+offset 동적 조정 → 코드 자체는 KST 대응
- **원인:** 에뮬레이터(또는 앱 실행 환경) 시간대 설정이 UTC인 것이 원인일 가능성 → 통합 테스트 전 수정 예정

### 5-3. epoch 변환 부재
- 현재 CsvFileSource는 SleepSession(수면 단계 구간) 단위로 반환
- Two-Process 모델 입력용 **30초 epoch 리스트**는 CsvFileSource에 없음 → 별도 변환 로직 필요
- **위치:** 백엔드 분석 모듈 또는 데이터 변환 계층 (CsvFileSource 수정 또는 새 컴포넌트)

### 5-4. Light → N2 표시 문제
- 현재 삼성 코드 40002(Light) → N2로 매핑
- md 기준 LIGHT 4단계 표기로 변경 여부: 팀장 확인 필요
- N1은 삼성 헬스에서 제공되지 않음 (항상 0)

### 5-5. sleep_summary_cleaned.csv 미사용
- `sleepSummaryFile` 파라미터는 있으나 현재 파싱 로직 없음
- sleep_score, sleep_duration 등 요약 정보 필요시 추가 활용 가능

---

## 6. 통합 테스트 전 체크리스트

| # | 항목 | 상태 | 담당자 |
|---|---|---|---|
| 1 | 심박 CSV 원본 확보 및 CsvFileSource 확장 | 미완료 | 채윤 (혜지에게 원본 요청) |
| 2 | 걸음 CSV 원본 확보 및 CsvFileSource 확장 | 미완료 | 채윤 (혜지에게 원본 요청) |
| 3 | 앱 시간대 오류 수정 (KST 고정 확인) | 계획 | 소빈 |
| 4 | 30초 epoch 변환 모듈 추가 (모델 입력용) | 미완료 | 채윤 |
| 5 | Light → LIGHT 4단계 표기 여부 확인 | 확인 필요 | 팀장 |
| 6 | sleep_summary 데이터 활용 여부 결정 | 미결정 | 채윤·소빈 |
| 7 | 서윤 #8 공통 스키마 확정 시 CsvFileSource 수정 | 예정 | 채윤 |

---

## 7. 참고 파일
- `CsvFileSource.kt` — 현재 앱 데이터 소스 코드
- `sleep_stage_cleaned.csv` (혜지 전처리본) — 파싱 대상 형식
- `com.samsung.health.sleep_stage.*.csv` (원본) — 삼성 헬스 내보내기 원본 형식
- `sleep_summary_cleaned.csv` (혜지) — 요약 정보 (현재 미사용)
