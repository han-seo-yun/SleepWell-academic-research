# 채윤님 다음 단계 정리 (소빈 합의 반영)
**작성일:** 2026-09-24 | **상태:** 소빈 합의 확정 기준

---

## 현재 상황 요약

소빈과 합의한 내용을 반영하면 이번 주 MVP의 모습은 다음과 같습니다:

1. 사용자가 CSV를 업로드하면 → 업로드 직후 백그라운드에서 전처리 + 분석이 자동으로 시작된다
2. 전처리는 CsvFileSource.kt로 CSV 파싱 → SleepSession 목록 → 30초 epoch 변환(**세션 사이 깨어 있던 시간까지 포함한 전체 타임라인**) → Kotlin으로 옮긴 Two-Process 계산 함수 호출 → S, C, propensity 계산
3. 결과는 AnalysisResult 객체로 소빈(프론트)에 직접 전달된다 (같은 Kotlin 앱 안이므로 별도 직렬화 불필요)
4. 결과 화면에는 수면 기록(전체) 그래프, S·C·propensity(계산 30초, 그래프 10~15분 간격), futureS와 권장 시각은 "준비 중"으로 표시된다
5. stage 표현은 AWAKE/LIGHT/DEEP/REM 문자열로 통일한다 (N2로 매핑하지 않음)
6. futureS와 권장 시각은 서윤 언니(모델 쪽) 담당이므로, 이번 주는 앱 쪽에 임시 배치하고 "준비 중"으로 둔다

---

## 채윤님이 지금 해야 할 일 (순서대로)

### 단계 1: CSV 파싱 확인 및 SleepSession 생성 (이미 준비된 코드 활용)
CsvFileSource.kt의 `parseSleepStageCsv()`와 `groupIntoSessions()`는 이미 구현되어 있다. `sleep_stage_cleaned.csv`를 읽어서 SleepSession 리스트가 잘 나오는지 확인한다. 이 부분은 새로 작성할 코드는 거의 없고, 기존 코드 동작 확인 위주로 진행한다.

단, **stage 매핑은 수정**한다: 현재 40002(Light) → N2로 매핑되어 있는데, 소빈 합의에 따라 **AWAKE/LIGHT/DEEP/REM 문자열로 변경**한다. CsvFileSource.kt의 `samsungCodeToSleepStage()` 함수에서 40002 → N2를 → LIGHT로 바꾼다. (SleepStage enum도 WAKE/LIGHT/DEEP/REM으로 정의)

### 단계 2: 30초 epoch 변환 함수 작성 (신규 작성)
SleepSession 리스트를 받아서 30초 epoch 리스트(`List<EpochInput>`)로 변환하는 함수를 새로 작성한다.

**핵심 주의사항 (소빈 지적):**
- epoch 변환은 **수면 단계 구간만 넣으면 안 된다**. 세션과 세션 사이, 즉 깨어 있던 시간(WAKE)까지 포함한 **전체 타임라인**을 만들어야 한다. 수면 구간만 넣으면 그 사이를 모두 수면으로 간주해서 S 계산이 틀어진다.
- 예: 1일차 세션 종료가 06:48, 2일차 세션 시작이 18:24라면, 그 사이 11시간 36분도 타임라인에 포함하고 isSleep=false로 처리해야 한다.
- epoch 경계(30초)에 딱 맞지 않는 마지막 조각은 포함시킨다. duration이 30초보다 짧아도 해당 구간 stage 기준으로 isSleep을 부여한다.

**함수 설계 예시:**
```
fun List<SleepSession>.toEpochTimeline(
  startTimeMs: Long,   // 타임라인 시작 (가장 이른 세션 시작 또는 사용자 지정)
  endTimeMs:   Long    // 타임라인 종료 (가장 늦은 세션 종료 또는 현재 시각)
): List<EpochInput> {
  // startTimeMs ~ endTimeMs를 30초 간격으로 순회
  // 각 epoch 시점마다 SleepSession의 stage 구간과 비교하여 isSleep 결정
  // 어느 세션에도 속하지 않는 시점 → isSleep = false (WAKE)
}
```

### 단계 3: Kotlin 임시 모델 구현 (Two-Process S·C·propensity 계산 부분)
공용 드라이브 Two-Process Python 코드에서 S, C, sleep_propensity를 계산하는 부분(약 20줄)을 Kotlin으로 옮긴다.

**입력:** 단계 2에서 만든 `List<EpochInput>` → `timestampsMs: List<Long>`, `isSleep: List<Boolean>`으로 분리
**출력:** `currentS: Float`, `currentC: Float`, `currentPropensity: Float`

**주의할 점:**
- Python 코드의 pandas 3.x 문제(마이크로초 단위 시각 → S 변화 거의 없음)를 인지한다. 팀장 공유 완료 상태. Kotlin으로 옮길 때 타임스탬프 처리를 신중히 한다.
- 파라미터(S0, tau, C 진폭, 주기 등)는 코드 기본값을 사용한다. 확정값이 아니므로 결과 표시 시 "임시 모델"로 표시한다.
- futureS(예측)와 권장 시각은 이 임시 모델에 **없다**. 해당 필드는 AnalysisResult에서 null/"준비 중"으로 둔다.

### 단계 4: AnalysisResult 데이터 클래스 정의
모델 출력을 받을 데이터 클래스를 Kotlin으로 정의한다. 소빈에게 직접 전달할 객체다.

```
data class AnalysisResult(
  val currentS:         Float,                    // 현재 Process S
  val currentC:         Float,                    // 현재 Process C
  val currentPropensity:Float,                    // 현재 S-C score
  val sleepRecords:     List<SleepRecord>,        // 최근 수면 기록 요약 (전체)
  val scHistory:        List<PropensityPoint>,    // 과거~현재 S·C·propensity 시계열
  val futureS:          List<PredictedPoint>?,    // null = "준비 중" (서윤 언니 담당)
  val recommendation:   Recommendation?,          // null = "준비 중"
)

data class SleepRecord(
  val sessionId:    String,
  val startMs:      Long,
  val endMs:        Long,
  val totalMin:     Float,
  val stageSummary: Map<SleepStage, Float>        // LIGHT/DEEP/REM별 분
)

data class PropensityPoint(
  val timestampMs: Long,
  val S:           Float,
  val C:           Float,
  val propensity:  Float
)

data class Recommendation(
  val recommendedBedTimeMs:   Long?,
  val recommendedWakeTimeMs:  Long?,
  val recommendedSleepMin:    Float?
)
```

**stage 표현:** SleepStage와 stageSummary의 키는 **AWAKE/LIGHT/DEEP/REM 문자열**로 통일. 숫자 코드는 export할 때만 사용.

### 단계 5: 전체 파이프라인 연결 (CSV → 결과 화면용 데이터)
단계 1~4를 하나의 흐름으로 연결한다:

```
fun analyzeSleepData(
  csvFile: File,
  directInput: DirectInput? = null   // 사용자 직접 입력 (선택)
): AnalysisResult {
  // 1. CSV 파싱
  val sessions = CsvFileSource(csvFile).readSleepSessions(...) as List<SleepSession>

  // 2. 타임라인 범위 결정 (가장 이른 시작 ~ 가장 늦은 종료, 또는 현재 시각까지)
  val timelineStart = minOf(sessions.minOf { it.startMs }, directInput?.sleepStartMs ?: 0)
  val timelineEnd   = maxOf(sessions.maxOf { it.endMs }, directInput?.sleepEndMs ?: 0, currentTimeMs)

  // 3. 30초 epoch 타임라인 변환 (전체 타임라인, 세션 사이 WAKE 포함)
  val epochTimeline = sessions.toEpochTimeline(timelineStart, timelineEnd)

  // 4. Kotlin 임시 모델 실행 (S, C, propensity)
  val (currentS, currentC, currentPropensity) = twoProcessKotlin(
    timestampsMs = epochTimeline.map { it.timestampMs },
    isSleep      = epochTimeline.map { it.isSleep }
  )

  // 5. scHistory 구성 (과거~현재 시계열)
  val scHistory = buildScHistory(epochTimeline, currentS, currentC, currentPropensity)

  // 6. futureS, recommendation은 null ("준비 중" — 서윤 언니 담당)
  //    (직접 입력이 있으면 임시 가이드라인 수준의 계산 가능하나, "임시 모델" 표시)

  // 7. sleepRecords 구성 (전체, 세션별 요약)
  val sleepRecords = sessions.map { session -> ... }

  return AnalysisResult(
    currentS = currentS,
    currentC = currentC,
    currentPropensity = currentPropensity,
    sleepRecords = sleepRecords,
    scHistory = scHistory,
    futureS = null,          // 준비 중
    recommendation = null    // 준비 중
  )
}
```

### 단계 6: 업로드 직후 백그라운드 실행 구조 (통합 시 소빈과 맞추기)
CSV 업로드가 완료되면 분석 파이프라인이 백그라운드에서 실행되도록 구조를 잡는다. 결과는 소빈의 결과 화면으로 직접 전달된다. 사용자 조건(목표 기상·예정 취침)이 변경되면 재계산이 일어나도록 콜백을 설계한다.

**이번 주에 어디까지 하면 되는지:**
- CSV 파싱 → epoch 변환 → Kotlin 임시 모델(S·C·propensity) → AnalysisResult 생성까지 **하나의 함수가 제대로 동작하는지 확인**하면 된다
- 실제 화면에 표시하는 UI 코드(그래프는 소빈 담당)는 채윤님 몫이 아님
- 통합 테스트에서 "CSV 업로드 → AnalysisResult 반환"까지 확인하면 됨

---

## 하지 않아도 되는 것 (이번 주 제외)

- **futureS(예측) 구현**: 서윤 언니 담당. "준비 중"으로 표시
- **권장 취침·기상·수면시간 계산**: 서윤 언니 담당. "준비 중"으로 표시
- **Python과 HTTP 연동**: Kotlin으로 옮긴 임시 모델을 사용하므로 불필요
- **모델 입력을 Python용 별도 형식으로 변환**: Kotlin epoch 리스트를 그대로 사용
- **심박·걸음 데이터 처리**: 혜지 원본 확보 전이므로 일단 제외 (추후 확장)
- **서윤 #8 공통 스키마 적용**: 서윤 확정 전이므로 현재는 sleep_id로 대체
- **JSON 직렬화**: 같은 Kotlin 앱 내이므로 객체 직접 전달

---

## 소빈과 같이 확인할 것 (통합 테스트 전)

1. epoch 변환 함수가 **전체 타임라인(세션 사이 WAKE 포함)**으로 잘 동작하는지 → 소빈 그래프에 사용될 SleepSession과 epoch 데이터가 충돌하지 않는지 확인
2. Kotlin 임시 모델의 S·C·propensity 값이 합리적인 범위(0~1)로 나오는지
3. AnalysisResult 객체가 소빈에게 문제없이 전달되는지 (객체 직접 전달 방식)
4. **시간대**: CsvFileSource.kt는 KST 기준 파싱. 소빈의 시간대 오류 수정(TZ=UTF → KST)과 맞는지 확인

---

## 팀장·서윤 언니 확인 필요 사항 (채윤님이 따로 챙길 것)

1. 서윤 #7 Hybrid 모델 일정 → Kotlin 임시 모델 교체 시점 파악
2. 심박·걸음 원본 CSV 확보 일정 → 혜지에게 문의 (이번 주 회의 때 언급)
3. (팀장) Two-Process 코드 Kotlin 이식 허가/방향 확인 (이미 소빈이 "사용 확정" 제안했지만 팀장 확인 필요할 수 있음)

---

## 한 줄 요약

할 일: **CsvFileSource.kt stage 매핑을 LIGHT로 수정 + 30초 epoch 변환 함수 작성(전체 타임라인) + Two-Process S·C·propensity 계산 부분 Kotlin으로 이식 + AnalysisResult 클래스 정의 + 파이프라인 연결**. 여기까지 되면 CSV 업로드 → AnalysisResult 반환까지 흐름이 완성되고, 통합 테스트 준비가 된다. futureS·권장 시각은 "준비 중"으로 두고, 서윤 언니 모델 확정 시 교체한다.
