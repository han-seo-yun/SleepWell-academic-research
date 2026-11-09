# epoch 30초 → 1분 변경 사항

**작성:** 정채윤 | **초판:** 2026-09-27 | **개정:** 2026-10-02 | **대상:** Two-Process 모델 입력 epoch 간격

> **2026-10-02 개정 요약**
>
> 1. **6절이 바뀌었다.** 앱 export 격자도 1분이 됐다(2026-09-26 회의). 초판의 "export 는 30초 유지" 는
>    더 이상 사실이 아니다. 나머지 절은 그대로여도 되고 6절만 읽으면 된다.
> 2. **기본값이 MIN1 로 바뀌었다.** `EpochInterval.DEFAULT` = `SEC30` → `MIN1`.
>    팀 기준이 1분인데 기본값만 30초면 인자를 빠뜨린 호출이 조용히 30초로 계산된다.
>    30초가 필요하면 `SEC30` 을 명시한다.
> 3. 소빈 통합본의 수정 2건을 저장소에 반영했다. `clockHours()` 버그 수정, 시각 해석 KST → UTC.
>    → **C 계열 수치가 전부 바뀌었다.** 새 수치는 `02_검증결과-30s-vs-1min.md` 참고.

---

## 1. 한 줄 요약

Two-Process 모델에 들어가는 epoch 타임라인을 **30초에서 1분으로 바꾸는 버전**을 새로 만들었다.
30초 버전은 파일을 한 글자도 안 건드리고 그대로 `src-epoch-30s/`에 동결해 뒀다.
두 버전은 폴더만 다를 뿐 **같은 함수 시그니처**라서, 바꿔 쓸 때는 폴더 통째로 교체하면 된다.

---

## 2. 왜 1분이 되는 게 의미가 있나

7일 구간 기준이라 체감은 크지 않지만, 아래가 실제로 달라진다.

| 항목 | 30초 | 1분 |
|---|---|---|
| 7일 epoch 수 | 20,160 | 10,080 |
| `scHistory` 점 수 | 20,160 | 10,080 |
| 계산량·메모리 | 기준 | 약 1/2 |
| 그래프 x축 점 | 촘촘 | 그래프 표시 간격(10~15분)에서는 동일 |

**그래프는 안 뜬다.** 전달 간격이 10~15분이므로 1분 epoch 로 바꿔도 화면에 그려지는 곡선은 사실상 같다.
눈에 띄는 차이는 epoch 수(전송량)와 계산 시간이고, 이건 1분이면 충분하다.

반대로 **단점**도 분명하다.

- 수면 단계는 1분 단위로 뭉개진다. 1분 안에 `LIGHT 40초 + WAKE 20초`가 같이 있으면 그 1분은 LIGHT 로 판정된다(가장 길게 겹치는 구간 우선). 즉 **1분 이하 짧은 각성 구간이 사라진다.**
- 입력 CSV 의 평균 구간 길이가 1분보다 짧다. 현재 실제 데이터는 1~22분이라 대개 문제없지만, 초 단위 깨어 있음이 많은 데이터셋에서는 정보가 뭉개진다.
- `Sleep-EDF` 평가는 30초 epoch 표준이다. 1분 모델을 30초 정답과 바로 붙이려면 **1분 → 30초 역집계**(각 30초를 1분 값으로 되돌림)를 해야 한다. 소빈 #2 파이프라인에서 이 변환이 필요해진다.

---

## 3. 폴더 구조

```
학술제 이번주 작업/
├── README.md
├── docs/                                  이 문서들
├── src-epoch-30s/app/sleepwell/health/    30초 버전 (동결, 현행과 동일)
│   ├── AnalysisResult.kt
│   ├── CsvFileSource.kt
│   ├── SleepAnalysisPipeline.kt
│   ├── SleepEpochConverter.kt
│   └── TwoProcessModel.kt
├── src-epoch-1min/app/sleepwell/health/   1분 버전 (신규)
│   ├── EpochInterval.kt                   ← 새로 추가한 파일
│   ├── AnalysisResult.kt
│   ├── CsvFileSource.kt                   (30초 버전과 동일, 변경 없음)
│   ├── SleepAnalysisPipeline.kt
│   ├── SleepEpochConverter.kt
│   └── TwoProcessModel.kt                 (30초 버전과 동일, 변경 없음)
└── tools/verify/                          컴파일·비교 검증 하네스 (앱 빌드 제외)
```

GitHub 저장소에서는 이렇게 대응된다.

| 저장소 경로 | 내용 |
|---|---|
| `src/app/sleepwell/health/` | 30초 버전 (기존 경로, 그대로 둠) |
| `src-epoch-1min/app/sleepwell/health/` | 1분 버전 (신규) |

소빈 화면 코드는 `src/` 를 보고 있다. 1분으로 바꾸려면 `src/` 를 `src-epoch-1min/` 내용으로 교체하면 되고,
되돌리려면 git 에서 이전 커밋으로 돌아가면 된다.

---

## 4. 실제로 바뀐 코드 (4곳)

### 4-1. `EpochInterval.kt` (신규 파일)

```kotlin
enum class EpochInterval(val intervalMs: Long, val seconds: Int, val label: String) {
    SEC30(30_000L, 30, "30초"),
    MIN1(60_000L, 60, "1분");

    companion object {
        val DEFAULT: EpochInterval = SEC30
        fun fromSeconds(seconds: Int): EpochInterval? = ...
    }
}
```

하드코딩 `30_000L` 을 한 곳에 모은 게 목적이다. 1분만 필요한 게 아니라
"나중에 또 5분으로 바꾸자"가 생겨도 여기 한 줄이면 되는 구조로 만들었다.

`EpochGrid` 도 같이 넣었다. `floorTo` / `ceilTo` / `isAligned` — epoch 경계 정렬용.

### 4-2. `SleepEpochConverter.toEpochTimeline(...)` — 간격 인자 추가

```kotlin
fun toEpochTimeline(
    sessions: List<SleepSession>,
    startTimeMs: Long,
    endTimeMs: Long,
    epochInterval: EpochInterval = EpochInterval.DEFAULT,   // 기본 MIN1 (30초는 SEC30 명시)
    alignToEpochBoundary: Boolean = true                    // 기본 정렬
): List<EpochInput>
```

epoch 을 만드는 루프, 두 포인터 겹침 판정(`isSleepAtTwoPointer`), "가장 긴 겹침 우선" 규칙은
30초 버전과 **완전히 같은 코드**다. 바뀌는 건 epoch 폭을 상수에서 인자로 읽는 한 줄뿐이다.

`alignToEpochBoundary` 를 넣은 이유는 다음과 같다.

> 기준 모델 `TwoProcessModel` 이 epoch 시각의 **시계 시각**(시 + 분/60 + 초/3600)을 그대로
> `C_sleep` 에 넣는다. 타임라인 시작이 epoch 경계에 안 떨어지면 모든 epoch 시각이 예컨대
> `:30` 에서 시작하므로 `분/60` 이 0.5 만큼 어긋나고, C 가 최대 `interval/2` 만큼 밀린다.
> 30초면 최대 15초, 1분이면 최대 30초 차이다. 하루 리듬 위상 계산에 그대로 남으므로,
> 1분 버전에서는 기본적으로 정렬한다.

30초 동작을 그대로 재현해야 하면 `alignToEpochBoundary = false` 를 넘기면 된다.

### 4-3. `SleepAnalysisPipeline.applyDirectInput(...)` — 하드코딩 제거 (가장 중요)

```kotlin
// 30초 버전 (변경 전) — 여기 30_000L 이 박혀 있었다
val eEnd = e.timestampMs + 30_000L

// 1분 버전 (변경 후)
val intervalMs = epochInterval.intervalMs
val eEnd = e.timestampMs + intervalMs
```

이게 **`SleepEpochConverter` 와 파이프라인 두 군데에 박혀 있던 `30_000L` 중 남아 있던 쪽**이다.
`SleepEpochConverter` 만 1분으로 바꾸고 이걸 그대로 두면, 사용자가 직접 입력한 취침·기상 시각이
**1분 모델에서는 절반 크기로만 반영**된다. 겉보기엔 멀쩡하게 실행되므로 조용히 틀린다.

검증은 `docs/02_검증결과-30s-vs-1min.md` 의 B항 참조.

### 4-4. `AnalysisResult` — epoch 메타데이터 4개 필드 추가

```kotlin
val epochIntervalMs: Long = 30_000L,
val epochIntervalLabel: String = "30초",
val inputPeriodStartMs: Long = 0L,
val inputPeriodEndMs: Long = 0L
```

**모두 기본값이 있다.** 소빈 화면 코드가 `AnalysisResult(...)` 를 직접 만드는 곳이 없으므로
화면 수정이 필요 없다. 읽기만 하는 코드는 그대로 컴파일된다.

왜 넣는가: 30초·1분 결과가 같은 화면에 함께 나올 수 있게 됐다. 간격이 결과에 없으면
그래프 x축 눈금이 점 개수로 찍혀야 하고, `Smax = 0.999755` 같은 값이
"몇 시간 수면한 결과"인지 "몇 일 수면한 결과"인지 알 수 없다.

`inputPeriodStartMs` / `inputPeriodEndMs` 은 `interface-schema-모델-입출력.md` 3-2 절에
이미 정의돼 있던 필드라, 이번에 실제로 채웠다.

`modelVersion` 문자열에도 간격을 붙였다.

```
30초: "Two-Process Kotlin 모델 (기준 코드 일치)"
1분 : "Two-Process Kotlin 모델 (기준 코드 일치) · epoch 1분"
```

---

## 5. 바뀌지 않은 것 (의도적)

| 파일 | 이유 |
|---|---|
| `TwoProcessModel.kt` | S·C_sleep 계산은 `dt` 를 실제 timestamp 차이로 계산한다. `dtH = (t[i] - t[i-1]) / 3_600_000` 이라 epoch 가 30초든 1분이든 상관없이 물리는 같다. **이게 1분 전환이 안전한 근거**다. |
| `CsvFileSource.kt` | CSV 파싱은 epoch 와 무관. 1분이라고 30초 CSV 를 1분으로 다시 쪼갤 필요 없다. 원본 구간을 1분 epoch 위에서 겹침 판정하면 된다. |
| 세션 요약(`SleepRecord`) | 세션 단위 합계라 epoch 격자와 무관. 검증 결과 37개 세션이 두 버전에서 동일하다. |
| `futureS` | 15분 간격 24시간 예측이라 점 수 96개로 두 버전 동일. |
| 앱 export 격자 | 아래 6항 참조. |

---

## 6. ★ 앱 export 격자 — 초판과 달리 1분으로 바뀌었다

> **2026-10-02 개정.** 이 절은 초판(2026-09-27) 기준이고, 더 이상 사실이 아니다.
> 2026-09-26 회의에서 **"데이터셋과 앱을 모두 1분 기준으로 통일"** 로 결정되었고,
> 소빈이 `HealthModels.EPOCH_MS` 를 `60_000L` 로 바꾸는 반영을 끝냈다.
> 즉 **모델 입력과 export 가 둘 다 1분**이고 서로 같은 값이다.
> 초판에 내가 "export 는 30초 유지가 안전하다"고 쓴 건 회의 결정 이전 판단이었다.

앱 export 격자:

```kotlin
// HealthModels.kt (소빈 관리, 통합 v3 기준)
const val EPOCH_MS = 60_000L          // 1분
const val EPOCH_TEXT = "1분"

// EpochAggregator.kt — epoch_idx 정의도 1분 기준으로 바뀜
"epoch_idx = 그 밤 정오부터 몇 번째 1분 칸. epoch_idx × 60 = 정오 기준 경과 초"
```

30초 버전은 소빈이 `통합 v2` 폴더에 그대로 보관해 뒀다. 되돌릴 때는 두 곳을 같이 바꿔야 한다:
`EpochInterval.DEFAULT` 와 `HealthModels.EPOCH_MS`.

### 남은 위험 — Sleep-EDF 평가 join (아직 미해결)

`epoch_idx` 의 단위가 30초 → 1분으로 바뀌었는데, Sleep-EDF 정답(`onset_sec`)은 30초 기준이다.
평가코드는 `(subject_id, night, epoch_idx)` 로 inner join 하므로 **단위만 다르면 에러 없이
조용히 틀어진다.** `EpochAggregator` 주석에 그대로 기록돼 있다:

> night 이 어긋나면 F1(macro) −0.210 / Kappa −0.222, 오류 메시지 0줄

1분 예측을 30초 정답과 비교하려면 **1분 → 30초 역집계**(각 30초 정답 epoch 를 대응하는 1분 값으로
되돌리는 규칙)가 먼저 정해져야 한다. 소빈이 "평가코드 쪽에서 정한다"고 맡았다.

**그 규칙이 정해지기 전에는 1분 성능 수치를 발표자료에 올리면 안 된다.**

---

## 7. 1분 버전을 쓰는 법

```kotlin
// 1분으로 계산 (2026-10-02 부터 기본값이 이쪽 — 인자 생략 가능)
SleepAnalysisPipeline.analyze(
    input = csvStream,
    epochInterval = EpochInterval.MIN1,
    alignToEpochBoundary = true
)

// 30초로 계산 — 반드시 SEC30 을 명시한다. (생략하면 1분으로 간다)
SleepAnalysisPipeline.analyze(
    input = csvStream,
    epochInterval = EpochInterval.SEC30,
    alignToEpochBoundary = true
)

// 30초 버전과 완전히 동일한 동작 (정렬까지 없음 — 초판 30초 동작 재현)
SleepAnalysisPipeline.analyze(
    input = csvStream,
    epochInterval = EpochInterval.SEC30,
    alignToEpochBoundary = false
)
```

`EpochInterval` 에 5분 같은 걸 추가하려면 enum 에 한 줄 넣고 `MIN5` 처럼 쓰면 된다.
`applyDirectInput` 이나 다른 곳을 다시 손볼 필요는 없다.