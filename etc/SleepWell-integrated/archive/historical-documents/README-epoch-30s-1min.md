# 학술제 이번 주 작업 — 정채윤

**초판:** 2026-09-27 | **개정:** 2026-10-02 | **주제:** Two-Process 모델 입력 epoch 30초 → 1분

> **2026-10-02 개정 요약**
>
> - 소빈 통합 v3(`통합 v3.zip`)를 빌드해 **APK 생성까지 확인**했다. Gradle 9.6.0 / 9.7.1 둘 다 성공.
> - 소빈이 지적해 준 **[통합 수정] 2건을 저장소에 반영**했다.
>   ① `TwoProcessModel.clockHours()` 버그 (분·초를 버리고 시만 반환하던 것) — kotlinc 로 5/5 불일치 재현 확인
>   ② `CsvFileSource` 시각 해석 KST → UTC (`SAMSUNG_TIMES_ARE_UTC` 플래그)
> - **기본값이 30초 → 1분**으로 바뀌었다(`EpochInterval.DEFAULT`).
> - **앱 export 격자도 1분이 됐다**(2026-09-26 회의). 초판에 쓴 "export 는 30초 유지" 는 정정했다.
>
> ⚠️ ① 때문에 **C 계열 수치가 전부 바뀌었다.** 아래 표는 새 수치다.
> **S 계열은 바뀌지 않았다.** 새 수치 근거는 `docs/02` 2-1·2-2 참조.
>
> 반영 후 소빈 v3가 독립적으로 돌린 값과 **소수점 6자리까지 일치**했다.

---

## 이번 주에 한 것

Two-Process 모델에 들어가는 epoch 타임라인을 30초에서 **1분으로 바꾸는 버전**을 만들었고,
30초 버전은 그대로 `src-epoch-30s/`에 동결해 뒀다.
이후 소빈 통합본을 받아 빌드 검증하고, 통합본에만 있던 수정 2건을 내 저장소에도 반영했다.

---

## 폴더 안내

```
학술제 이번주 작업/
├── README.md                                   ← 이 파일
├── docs/
│   ├── 01_epoch-30s-1min-변경사항.md            무엇을 왜 바꿨는지
│   ├── 02_검증결과-30s-vs-1min.md              실제 숫자로 확인한 내용 + 한계
│   └── 03_다음주-확인필요사항-정채윤.md          소빈·서윤·혜지한테 물어볼 것
├── src-epoch-30s/app/sleepwell/health/          30초 버전 (동결)
├── src-epoch-1min/app/sleepwell/health/         1분 버전 (EpochInterval.kt 추가)
└── tools/verify/                                컴파일·비교 검증 하네스 (앱 빌드 제외)
```

**읽는 순서:** `docs/01` → `docs/02` → 필요하면 `docs/03`.

---

## 결론부터

| | 30초 | 1분 |
|---|---|---|
| 7일 epoch 수 | 20,160 | 10,080 (정확히 1/2) |
| 현재 S | 0.289993 | 0.289342 (0.22% 차이) |
| 현재 C_sleep | 0.118209 | 0.120053 |
| 현재 propensity | 0.408203 | 0.409394 |
| C 진폭 | ±0.150000 | ±0.150000 (진폭을 꽉 채움 = 버그 수정 후) |
| S 최댓값 | 0.999755 | **0.999755 (완전히 동일)** |
| 세션 수 | 37 | 37 (동일) |
| 그래프 표시 | — | **사실상 동일** (전달 간격 10~15분이라) |

**"1분으로 바꿔도 결과가 같은데 계산량은 절반"** 이 확인됐다.
epoch 간격만 바뀌고 물리는 안 바뀌는 게 S 최댓값이 소수점 6자리까지 같다는 것으로 확인된다.

**C 진폭이 ±0.150000 으로 꽉 차는 게 `clockHours()` 버그 수정의 증거다.**
초판엔 ±0.149069 였다. 분·초를 버리고 정수로 뭉개졌기 때문에 진폭 극값에 못 미치던 것이다.

그래프는 눈에 띄게 달라지지 않는다. 눈에 띄는 차이는 epoch 수(전송량)와 계산 시간이고,
1분이면 충분하다. 단점은 **1분 이하 짧은 각성 구간이 1분으로 뭉개진다**는 것.
자세한 내용은 `docs/01` 2절.

---

## 코드에서 실제로 바뀐 것

**epoch 전환 (초판, 4곳)**

1. **`EpochInterval.kt` 신규** — `SEC30` / `MIN1` enum + `EpochGrid`(경계 정렬). 기본값은 `MIN1`.
2. **`SleepEpochConverter.toEpochTimeline()`** — epoch 간격을 인자로 받게 변경. 정렬 옵션 추가.
3. **`SleepAnalysisPipeline.applyDirectInput()`** — 하드코딩 `30_000L` 제거. **이게 핵심.**
   이걸 안 고치면 1분 모델에서 사용자가 입력한 취침·기상 시각이 절반 크기로만 반영된다.
4. **`AnalysisResult`** — epoch 메타데이터 4개 필드 추가. **모두 기본값 있음** → 화면 코드 수정 불필요.

**통합본 반영 (2026-10-02, 3곳)**

5. **`TwoProcessModel.clockHours()`** — `+` 연산자를 줄 끝으로 이동.
6. **`CsvFileSource.parseTimestamp()`** — 시각 해석을 UTC 로. `SAMSUNG_TIMES_ARE_UTC` 플래그로 되돌릴 수 있음.
7. **`EpochInterval.kt` 주석 / `DEFAULT`** — "export 30초 유지" 라는 잘못된 안내 정정, 기본값 `MIN1` 로.

`CsvFileSource` 를 제외한 **나머지 파일은 epoch 전환과 무관하게 동작한다.**
(S 최댓값이 동일한 이유가 이것이다)

---

## 바로 확인해야 할 것

**1. `SAMSUNG_TIMES_ARE_UTC = true` 는 아직 가설이다.**
삼성헬스 CSV 가 UTC 라는 건 소빈의 근거 기반 판단이고 혜지 확인이 남았다.
`Process S` 는 영향 없지만 **`C_sleep`·`phaseRef` 는 9시간 밀린다.**
확정 전 C 계열 절대 수치를 보고서에 쓰면 안 된다. → `docs/03` 1-4

**2. Sleep-EDF 1분 → 30초 역집계 규칙이 아직 없다.**
`epoch_idx` 단위가 30초 → 1분으로 바뀌었는데 GT 는 30초 기준이다.
평가코드가 `(subject_id, night, epoch_idx)` 로 inner join 하므로 **에러 없이 조용히 틀어진다**
(주석 기록: F1 −0.210 / Kappa −0.222). 소빈 몫. → `docs/03` 1-3

**3. 회귀 테스트 80개 추가** (`tools/verify/test/`, `run-tests.ps1`).
   `clockHours` 버그가 테스트 없이 통과했던 게 문제였으므로 만들었다.
   버그를 되돌린 복사본으로 **6개가 실제로 FAIL 하는 것**까지 확인했다.
   단, **통합본(소빈) 쪽 코드는 커버리지 밖**이다 — `EpochAggregator` / `Exporters` /
   `MainActivity` / `AnalysisScreen` 는 안 건드린다. → `docs/03` 4

**4. 소빈 쪽도 1분으로 바뀌는 걸 알고 있다.** 앱 export 격자까지 1분(`EPOCH_MS = 60_000L`,
`epoch_1m.csv`)이라 **모델 입력·export 모두 1분**이고 서로 같은 값이다. 30초는 `통합 v2` 에 보관.
그래서 초판에 쓴 "export 는 30초 유지" 는 이미 정정했다.

**정리하면 남은 건 3개다** (전부 내 저장소 밖):

| 남은 것 | 담당 | 왜 막혀 있나 |
|---|---|---|
| `SAMSUNG_TIMES_ARE_UTC` 확정 | 혜지 확인 | 소빈의 가설. 확정 전 C 절대 수치를 보고서에 못 씀 |
| Sleep-EDF 1분→30초 역집계 규칙 | 소빈 | epoch_idx 단위가 GT와 달라 조용히 틀어질 수 있음 |
| `interface-schema-전처리-프론트.md` 30초 표기 6곳 | 소빈/팀장 | 합의 문서라 내 권한 밖 |

---

## 검증 재현

```powershell
# 30초 vs 1분 결과 비교 + 통합본 교차확인
powershell -ExecutionPolicy Bypass -File tools\verify\verify-epoch.ps1

# 회귀 테스트 80개
powershell -ExecutionPolicy Bypass -File tools\verify\run-tests.ps1
```

kotlinc 를 컴파일러 jar 로 직접 실행한다(Android Studio bundled). 결과는
`tools/verify/out/result-*.txt` 에 쌓인다. 산출물은 커밋 대상이 아니다.

> `verify-epoch.ps1` 는 **UTF-8(BOM)** 으로 저장돼 있어야 한다. 메모장 기본 UTF-8 저장은
> 깨진다. 이미 BOM 으로 맞춰 뒀다.

---

## GitHub

https://github.com/iuniion3287-web/SleepWell

| 저장소 경로 | 내용 |
|---|---|
| `src/app/sleepwell/health/` | 30초 버전 (기존 경로) |
| `src-epoch-30s/app/sleepwell/health/` | 30초 버전 동결 스냅샷 |
| `src-epoch-1min/app/sleepwell/health/` | 1분 버전 |
| `docs/epoch-30s-1min/` | 이번 주 문서 3종 |
| `tools/verify/` | 검증 하네스 |

1분으로 실제 전환하려면 `src/` 를 `src-epoch-1min/` 내용으로 교체하면 된다.
되돌리기는 git 커밋으로 충분하다(30초 버전이 그대로 남아 있다).