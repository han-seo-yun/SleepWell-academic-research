# 모델 입력·출력 형식 설계
**작성자:** 채윤 | **날짜:** 2026-09-24 | **상태:** 합의용 초안

---

## 1. 배경
- 실제 모델(서윤 #7 S-C + Wearable Hybrid)은 아직 확정되지 않음
- 이번 주 MVP: **연결 구조만 잡는다**. 실제 Python 연결은 통합 단계에서 수행
- 아래 형식은 **참고 기준**: 공용 드라이브 Two-Process 코드 + md #5 결과 항목 요구 사항 기반

---

## 2. 모델 입력 형식

### 2-1. Two-Process 기준 입력 (현재 공용 코드)
공용 드라이브 Two-Process 코드가 예상하는 입력:

| 항목 | 형식 | 설명 |
|---|---|---|
| `timestamp` | 배열 (epoch 초 또는 ms) | 30초 간격 시계열 |
| `is_sleep` | 배열 (0/1) | 해당 시점에 수면 중이면 1, 아니면 0 |

예시:
```
timestamp = [t0, t0+30s, t0+60s, ...]
is_sleep  = [0,   0,      1,      ...]
```

> **주의:** pandas 3.x에서는 시각이 마이크로초 단위로 읽혀, 기존 코드가 가정한 나노초 기준과 달라진다. → S 값이 거의 변하지 않는 오류 발생 (예: 2시간 수면 후 기대 0.31 → 실제 0.4998). 팀장 공유 완료.

### 2-2. 앱(Kotlin) 기준 입력 데이터 구성
백엔드가 모델(Python)에 전달할 데이터:

**A. 수면 이력 (CSV 기반)**
```
List<EpochInput>:
  - timestampMs: Long    // KST epoch ms
  - isSleep:     Boolean // 수면 중 여부
```
→ `sleep_stage_cleaned.csv`의 stage 구간을 30초로 분할하여 생성

**B. 사용자 직접 입력 (선택)**
```
DirectInput:
  - sleepStartMs: Long?   // 취침 시각 (KST epoch ms)
  - sleepEndMs:   Long?   // 기상 시각 (KST epoch ms)
  - targetWakeMs: Long?   // 목표 기상 시각 (선택)
  - plannedBedMs: Long?   // 예정 취침 시각 (선택)
```

### 2-3. 입력 구성 요약
```
모델 입력 = [
  { timestampMs: 2026-09-13 18:24:00 KST → epoch ms, isSleep: false },
  { timestampMs: 2026-09-13 18:24:30 KST → epoch ms, isSleep: false },
  { timestampMs: 2026-09-13 18:25:00 KST → epoch ms, isSleep: true  },
  ...
]
+ (직접 입력 있으면) sleepStartMs, sleepEndMs, targetWakeMs, plannedBedMs
```

---

## 3. 모델 출력 형식

### 3-1. md #5 결과 항목 요구 사항
결과 화면에 표시해야 하는 항목:

| # | 항목 | 설명 |
|---|---|---|
| 1 | 최근 수면 및 활동 기록 | 수면 세션 요약, 단계별 분포 |
| 2 | 현재 Process S (수면 압력) | 현재 시점의 수면 압력 값 (0~1) |
| 3 | 현재 Process C (일주기 리듬) | 현재 시점의 일주기 리듬 값 |
| 4 | S-C / sleep propensity 변화 | 수면 성향 변화 (S-C 점수 시계열) |
| 5 | 앞으로의 수면 압력 변화 | 향후 24시간 Process S 예측 시계열 |
| 6 | 예상/권장 취침 시각 | 모델로 계산한 권장 취침 시각 |
| 7 | 권장 기상 시각 | 모델로 계산한 권장 기상 시각 |
| 8 | 권장 수면시간 | 권장 수면 지속 시간 |
| 9 | (가능하다면) 목표 기상/예정 취침 입력 시 계산 | 사용자 입력 조건 기반 취침·수면시간 |

### 3-2. 모델 출력 구조 (설계안)

```
AnalysisResult:
  // 현재 상태
  currentS:        Float        // 현재 Process S 값 (0~1)
  currentC:        Float        // 현재 Process C 값
  currentPropensity: Float      // 현재 S-C score / sleep propensity

  // 과거 이력 (최근 수면 기준)
  sleepRecords:    List<SleepRecord>
    SleepRecord:
      - sessionId:   String
      - startMs:     Long
      - endMs:       Long
      - totalMin:    Float
      - stageSummary: Map<SleepStage, Float>  // 단계별 시간(분)

  // S-C 시계열 (과거 ~ 현재)
  scHistory:       List<PropensityPoint>
    PropensityPoint:
      - timestampMs: Long
      - S:           Float
      - C:           Float
      - propensity:  Float

  // 미래 예측 (향후 24시간)
  futureS:         List<PredictedPoint>
    PredictedPoint:
      - timestampMs: Long
      - predictedS:  Float

  // 권장 사항
  recommendation:
    - recommendedBedTimeMs:   Long?   // 권장 취침 시각 (KST epoch ms)
    - recommendedWakeTimeMs:  Long?   // 권장 기상 시각 (KST epoch ms)
    - recommendedSleepMin:    Float?  // 권장 수면 시간 (분)
    - conditionedBedTimeMs:   Long?   // 목표 기상/예정 취침 입력 시 계산값
    - conditionedSleepMin:    Float?

  // 계산 기준 정보
  - modelVersion:    String
  - calculationTimeMs: Long   // 계산 수행 시각
  - inputPeriodStartMs: Long
  - inputPeriodEndMs:   Long
```

### 3-3. 출력 값의 범위 및 의미

| 값 | 범위 | 의미 |
|---|---|---|
| S (Process S) | 0 ~ 1 | 0=완전히 rested, 1=극도로 졸림 |
| C (Process C) | -1 ~ 1 | 일주기 리듬 위상 (음수=수면 적합, 양수=각성 적합) |
| sleep propensity (S-C) | 해석적 | S+C 또는 S-C 점수, 수면 가능성 지표 |
| 권장 취침 시각 | epoch ms (KST) | 수면 압력이 낮아질 것으로 예측되는 시각 |
| 권장 기상 시각 | epoch ms (KST) | 일주기 리듬 + 수면 압력 기준 적절한 기상 시각 |

---

## 4. 모델 연결 방식 (현재 미확정 부분)

### 4-1. 현재 상태
- 공용 드라이브 Two-Process 코드: Python, `timestamp` + `is_sleep(0/1)` 입력 → `S, C_sleep, sleep_propensity` 출력
- **한계:** 과거 기록의 S·C만 계산. 미래 예측·권장 시각 계산 기능 없음

### 4-2. 연결 방식 (소빈 답변 반영)

**합의된 방향:**
- Two-Process 공용 코드 사용. S·C·propensity 계산 부분(약 20줄)만 **Kotlin으로 옮겨서 임시 모델**로 사용
- 모델 입력을 python용 형식으로 따로 만들 필요 없음 — Kotlin에서 만든 epoch 리스트를 그대로 사용
- 파라미터는 코드 기본값 사용, 확정값이 아니므로 "임시 모델"로 표시

**연결 파이프라인 (Kotlin 내부):**
```
CSV → SleepSession 리스트
  → 30초 epoch 변환 (timestampMs, isSleep) — 세션 사이 깨어 있던 시간 포함 전체 타임라인
  → Kotlin으로 옮긴 Two-Process 계산 함수 호출
  → S, C, propensity 계산 (과거~현재)
  → futureS, 권장 시각은 아직 없음 → "준비 중" / 임시 정의(가이드라인 수준)
  → AnalysisResult 생성 → 소빈(프론트) 전달
```

**모델 호출 시점:** CSV 업로드 직후 백그라운드 계산. 사용자 조건(목표 기상·예정 취침) 변경 시 재계산. 결과 화면 진입 시 이미 계산되어 있음.

**미래 기능 담당:**
- 미래 S 예측·권장 시각 계산: **서윤 언니 담당** (모델 쪽)
- 이번 주 MVP: 앱 쪽에 **임시 배치**. 서윤 언니 정의가 나오면 교체·맞춤
- 권장 시각·futureS 필드는 "준비 중" 표시, 임시값 사용 시 "임시 모델(분석 화면 출력을 확인하는 정도)"이라고 명시

---

## 5. 소빈 ↔ 채윤 모델 입출력 합의 포인트

| # | 항목 | 내용 (소빈 답변 확정) |
|---|---|---|
| 1 | 모델 호출 시점 | **CSV 업로드 직후 백그라운드 계산**. 사용자 조건 변경 시 재계산. 결과 화면 진입 시 이미 준비됨 |
| 2 | 모델 출력 형식 | Kotlin 객체 직접 전달 (같은 앱 내). JSON 직렬화 불필요 |
| 3 | S·C·propensity 단위/범위 | Two-Process 코드 기본값. "임시 모델"로 표시 |
| 4 | futureS 그래프 간격 | **계산은 촘촘하게(30초), 전달·그래프는 10~15분 간격** |
| 5 | 권장 시각 없을 때 처리 | **"준비 중" 표시**. 임시값 사용 시 "임시 모델(분석 화면 확인용)" 명시 |
| 6 | 입력 타임라인 구성 | **수면 구간만이 아닌, 세션 사이 깨어 있던 시간까지 이어진 전체 타임라인**. 수면 칸만 넣으면 사이를 전부 수면으로 계산해 S가 틀어짐 |

---

## 6. 참고
- Two-Process 코드: S·C·propensity 계산 부분(약 20줄)을 Kotlin으로 옮겨 임시 모델로 사용
- 입력 형식: Kotlin에서 만든 epoch 리스트를 그대로 사용 (Python용 별도 형식 불필요)
- 서윤 #7 Hybrid 모델 확정 시 이 문서의 입력/출력 구조를 재검토하고, 임시 Kotlin 모델을 교체
- 서윤 #8 공통 스키마 확정 시 데이터 열 구성을 맞춘다
- 미래 S 예측·권장 시각: 서윤 언니 담당. 이번 주는 앱 쪽 임시 배치 → 서윤 정의 후 교체
