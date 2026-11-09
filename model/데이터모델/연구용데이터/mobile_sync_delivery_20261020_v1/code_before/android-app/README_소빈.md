# 소빈 파트 — 조립 안내서 (Android Studio 처음 쓰는 사람 기준)

작성: 2026-09-18
담당 범위: 데이터 모델 · 인터페이스 정의 · 가상 데이터 · 30초 epoch 변환 · 화면 · 그래프 · CSV/JSON 내보내기
담당하지 않는 범위: Samsung Health SDK 실제 연동 (채윤)

---

## 0. 이 폴더에 뭐가 있나

**이 폴더는 그 자체로 완성된 Android 프로젝트입니다.** 열면 바로 빌드됩니다.

```
소빈_kotlin/
├ gradlew / gradlew.bat            빌드 실행기 (Gradle wrapper)
├ gradle/
│  ├ wrapper/                      Gradle 9.7.1 을 자동으로 받아옴
│  └ libs.versions.toml            의존성 버전 목록
├ build.gradle.kts                 루트 빌드 설정
├ settings.gradle.kts
├ gradle.properties
└ app/
   ├ build.gradle.kts              앱 모듈 빌드 설정
   └ src/main/
      ├ AndroidManifest.xml        FileProvider 등록까지 되어 있음
      ├ res/                       themes / strings / file_paths
      └ java/app/sleepwell/
         ├ MainActivity.kt         화면 하나 (조회 → 요약 → 그래프 → 내보내기)
         ├ health/
         │  ├ HealthModels.kt      데이터 타입 + 시각 포맷 (앱 전체 공통 언어)
         │  ├ HealthDataSource.kt  ★ 인터페이스. 채윤과 소빈이 만나는 지점
         │  ├ FakeDataSource.kt    가상 데이터 생성기
         │  └ EpochAggregator.kt   ★ 30초 격자 변환. 연구 파이프라인 직결
         ├ export/
         │  └ Exporters.kt         CSV 4개 + meta.json + 통합 JSON
         └ ui/
            └ SleepStageChart.kt   hypnogram · 심박 그래프 (Canvas 직접 그림)
```

`local.properties` 는 **일부러 넣지 않았습니다.** SDK 경로가 PC마다 달라서, 있으면
오히려 다른 사람 PC에서 빌드가 깨집니다. Android Studio 가 프로젝트를 열 때
자동으로 만들어 줍니다.

### 의존성을 일부러 안 늘렸습니다

템플릿 기본 의존성만 씁니다. 버전 충돌로 시간을 날리지 않기 위한 제약입니다.

- `java.time` 대신 `Long`(epoch millis)  → core library desugaring 설정 불필요
- 차트 라이브러리 대신 Compose Canvas    → 의존성 추가 불필요
- JSON 라이브러리 대신 직접 문자열 조립   → 의존성 추가 불필요
- ViewModel 대신 `remember`               → 의존성 추가 불필요

(예외 하나: JSON **검증**에는 `org.json` 을 씁니다. 안드로이드 플랫폼에 기본 포함이라
Gradle 의존성이 늘지 않습니다.)

### 검증 상태 (2026-09-18)

- `assembleDebug` **성공** — AGP 9.4.0 / Gradle 9.7.1 / Kotlin 2.4.20 / compileSdk 37 / minSdk 26
- 이 폴더만 다른 위치에 복사해 **클린 빌드 성공** (APK 11.8MB)
- 에뮬레이터(`medium_phone`)에서 실행 → 7일 조회 → export 까지 확인
- export 한 `epoch_30s.csv` 6,014행을 **앱 밖에서** 다시 검증 → 11항목 전부 통과
  (`_작업\MVP_export검증.py`)

---

## 1단계. Android SDK 준비 — 사람이 직접 해야 하는 부분

Android Studio 첫 실행 마법사가 GUI 라서 자동화가 안 됩니다. **이미 하셨다면 넘어가세요.**

```powershell
Start-Process "C:\Program Files\Android\Android Studio\bin\studio64.exe"
```

| 화면 | 선택 |
|---|---|
| Import Settings | **Do not import settings** |
| Data Sharing | 아무거나 (Don't send 권장) |
| Install Type | **Standard** |
| UI Theme | 취향 |
| Verify Settings | **Next → Accept 전부 → Finish** |

Android SDK + 플랫폼 + 에뮬레이터 이미지를 내려받습니다. **약 3~6GB, 10~20분.**

> 확인 방법: `C:\Users\<사용자>\AppData\Local\Android\Sdk` 폴더가 생겼는지 보면 됩니다.

---

## 2단계. 이 프로젝트 열기 (만들지 않습니다)

`Welcome to Android Studio` → **`Open`** → 이 `소빈_kotlin` 폴더를 선택.
(`New Project` 아니고 `Open` 입니다.)

첫 Gradle Sync 가 돌아갑니다. **처음엔 Gradle 9.7.1(약 145MB)을 내려받아서 5~15분 걸립니다.**
`Trust Project?` 를 물으면 `Trust`.

끝나면 바로 실행 가능한 상태입니다. **소스 파일 복사도, Manifest 편집도 필요 없습니다.**

### 명령줄로 빌드하려면

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat assembleDebug
```

APK 는 `app\build\outputs\apk\debug\app-debug.apk` 에 나옵니다.

---

## 3단계. 겪게 될 함정 3개 (미리 적어둠)

### (1) `JAVA_HOME` 이 설정돼 있지 않습니다

Android Studio 안에서는 IDE 가 자기 JDK 를 쓰니까 문제가 없는데, **PowerShell 에서
`gradlew` 를 직접 돌리면** 이 오류가 납니다.

```
ERROR: JAVA_HOME is not set and no 'java' command could be found in your PATH.
```

Java 를 따로 설치하지 마세요. Android Studio 가 JDK 를 이미 갖고 있습니다.

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
```

(이 설정은 그 PowerShell 창에서만 유효합니다. 영구히 두려면 시스템 환경 변수에 등록.)

### (2) AGP 9 는 `org.jetbrains.kotlin.android` 를 거부합니다

인터넷 예제를 그대로 붙이면 이 줄이 들어 있는데, **AGP 9 부터는 오류입니다.**

```kotlin
// ❌ AGP 9 에서 빌드 실패
plugins { id("org.jetbrains.kotlin.android") }

// ✅ Compose 플러그인만 적용
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}
```

이 프로젝트의 `build.gradle.kts` 는 이미 맞춰져 있으니 **고치지 마세요.**

### (3) `sdkmanager` 는 폐기됐고 종료 코드가 거짓말을 합니다

CLI 로 SDK 를 설치할 때 `sdkmanager` 는 실패해도 성공(0)을 반환하는 경우가 있어서,
스크립트가 잘못된 상태로 계속 진행합니다. 대체 명령은 `android` 입니다. 다만 **위
2단계처럼 GUI 마법사로 설치하면 이걸 아예 안 만납니다** — 그게 권장 경로입니다.

---

## 4단계. 실행

에뮬레이터가 없으면: `Device Manager` → `Create Virtual Device` → **Pixel 7 / API 34** → Finish.

상단 초록 ▶ 버튼 → 앱이 뜹니다.

화면에서:
1. 피험자 ID 확인 (기본 `sobin01`)
2. 기간 **`7일`** 선택 (낮잠이 들어 있는 밤을 포함시키려면 7일 이상)
3. **조회** → 요약 카드와 그래프가 나옵니다
4. **CSV · JSON 내보내기** → 저장 경로와 파일 목록이 나옵니다

가상 데이터라서 에뮬레이터에서도 전부 확인됩니다. **Samsung Health 없이 여기까지 끝납니다.**

### 예상되는 그림 (7일 조회 기준, 실제 확인값)

- `수면 세션` 이 **9개 (7일 밤 · 낮잠 포함)** 로 나옵니다. 밤잠 7 + 낮잠 2 입니다.
  → 낮잠은 일부러 넣었습니다. 이유는 아래 "★ night / epoch_idx 규칙" 참고.
- 그래프가 **세션 단위**로 그려집니다. 같은 날 낮잠과 밤잠은 `night` 이 같은데도
  **별개 그래프 2개**로 나옵니다 (사이 9시간 공백을 한 그래프에 넣으면 아무것도 안 보임).
- 그래프 제목이 `2026-09-13 14:10 → 14:31 · night 20260913` 형태입니다.
  `night` 이 순번이 아니라 **날짜**입니다.
- hypnogram 에 90분 주기가 보이고, 새벽으로 갈수록 REM(보라)이 길어집니다
- 심박 그래프가 **3일에 한 번 끊겨 있습니다** — 일부러 넣은 결측입니다(시계 벗은 상황).
  선을 직선으로 이어 메우지 않는지 확인하는 용도입니다
- `심박이 채워진 epoch` 이 46% 입니다 — 정상입니다 (심박은 1분 간격, epoch 은 30초)
- 단계 분포 W 5.0 / N1 5.6 / N2 55.1 / N3 13.3 / REM 21.0 % — 교과서 범위 안입니다

---

## 5단계. 내보낸 파일 꺼내기

앱이 알려주는 경로는 대략 이렇습니다.

```
/storage/emulated/0/Android/data/app.sleepwell/files/exports/sleepwell_sobin01_20260918_2130_FAKE/
```

꺼내는 방법 두 가지:

- **앱 안에서**: `파일 공유` 버튼 → Drive/Gmail
- **PC 에서**: Android Studio 오른쪽 아래 `Device Explorer` → 위 경로로 이동 →
  폴더 우클릭 → `Save As`

### 나오는 파일

| 파일 | 내용 |
|---|---|
| `meta.json` | 피험자·기간·타임존·기기·요약·경고. **`source` 필드로 실제/가상 구분** |
| `hr.csv` | 심박 원본 표본 (시각, bpm, 기기) |
| `steps.csv` | 걸음 구간 (시작, 끝, 걸음수) |
| `sleep_stage.csv` | 수면 단계 구간 원본 (합쳐지기 전) |
| `epoch_30s.csv` | ★ **30초 격자. 기존 평가코드에 그대로 들어가는 형식** |
| `export.json` | 위 전부를 한 파일로 묶은 것 |

`epoch_30s.csv` 헤더:

```
subject_id,night,session_id,epoch_idx,stage_label,time_iso,hr_mean,steps
```

`subject_id` / `night` / `epoch_idx` / `stage_label` 이 **평가코드.py 요구 규격과 동일**합니다.
`stage_label` 은 `0=W, 1=N1, 2=N2, 3=N3, 4=REM`, 모르는 구간은 `-1` (평가에서 자동 제외).
`session_id` 는 참고 열이고 **매칭 키가 아닙니다** — 평가코드는 이 열을 쓰지 않습니다.

### ★ night / epoch_idx 규칙 — 반드시 지켜지는 것

| 값 | 뜻 |
|---|---|
| `night` | **그 밤이 속한 날짜 `yyyyMMdd`.** 정오~다음날 정오를 한 밤으로 봅니다 |
| `epoch_idx` | **`night` 날짜의 12:00 부터 센 30초 칸 번호.** `epoch_idx × 30 = 정오 기준 경과 초` |

둘 다 **행 번호가 아니라 시각 좌표**입니다. 그래서 세 가지가 보장됩니다.

1. **조회 범위를 바꿔도 값이 안 변합니다.** 9/17 밤은 3일 조회에서도 7일 조회에서도
   `night = 20260917` 입니다. (순번 1, 2, 3… 이면 조회할 때마다 달라집니다.)
2. **자정을 넘겨도 한 밤입니다.** 23:24 취침 → 06:08 기상이 전부 `night = 20260912`.
3. **같은 날 낮잠과 밤잠이 충돌하지 않습니다.** 정오 기준이라 낮잠 260~301,
   밤잠 1408~2373 으로 번호대가 갈립니다. 세션 시작을 0 으로 잡으면 둘 다 0 부터라
   `(night, epoch_idx)` 가 겹칩니다.

그리고 단계를 모르는 구간도 **번호를 그대로 소비**하고 `stage_label = -1` 로 남깁니다.
행을 절대 건너뛰지 않습니다.

#### 왜 이렇게까지 하나

2026-09-18 에 8월 `평가코드.py` 로 실측했습니다. 이 규칙이 깨진 예측을 넣으면
**F1(macro) −0.210 / Kappa −0.222** 만큼 점수가 틀어지는데 **오류 메시지가 한 줄도
안 납니다.** 매칭률은 97.7% 로 표시되고, 최대 407분 밀린 녹화가 섞여 있어도 겉보기가
정상입니다. 눈으로 잡을 수 없는 종류의 오류라서, 규격 자체를 안 깨지게 만드는 것 말고는
방법이 없습니다.

`EpochAggregator.selfCheck()` 가 내보내기 전에 세 가지를 자동 검사하고
(키 중복 / 30초 격자 정합 / 세션 안 번호 연속), 어기면 화면에 빨간 카드로 띄웁니다.

#### FakeDataSource 에 낮잠을 넣은 이유

하루에 세션이 하나뿐이면 `(night, epoch_idx)` 가 겹칠 일이 없어서 **그 검사가 한 번도
실행되지 않습니다.** 8월 평가코드가 `night` 버그를 놓친 원인이 정확히 이것이었습니다 —
검증용 더미에 문제 상황이 없어서 문제 경로가 테스트되지 않았습니다. 그래서 가상
데이터에 일부러 어려운 입력(낮잠, 심박 결측, 폰 기록 섞임)을 넣어둡니다.

---

## 7. 채윤 파트와 붙이는 곳 — 딱 두 군데

### ① 구현체 하나 추가

`health/` 안에 `SamsungHealthSource.kt` 를 만들어 `HealthDataSource` 를 구현합니다.
구현해야 하는 함수는 7개이고, 시그니처는 `HealthDataSource.kt` 에 이미 다 적혀 있습니다.
**화면·그래프·CSV 코드는 한 줄도 안 봐도 됩니다.**

지켜야 할 것 두 가지:

- SDK 타입을 밖으로 내보내지 않기. SDK 결과를 `HeartRateSample` / `StepSample` /
  `SleepSession` 으로 바꿔서 돌려줍니다.
- 실패를 예외로 던지지 말고 `HealthResult.PermissionDenied` / `Unavailable` / `Failure` 로
  돌려주기. 그래야 화면이 "무엇을 안내해야 하는지" 알 수 있습니다.
- `sourceTag = "samsung"` 으로 두기. 이 값이 `meta.json` 과 파일명 `_FAKE` 여부를 결정합니다.

### ② 스위치 한 줄

`MainActivity.kt` 의 `dataSourceOf()` 함수:

```kotlin
private fun dataSourceOf(useFake: Boolean, context: Context): HealthDataSource =
    if (useFake) FakeDataSource()
    else FakeDataSource()   // ← 이 줄을 SamsungHealthSource(context) 로 교체
```

**이게 통합 작업 전부입니다.**

---

## 8. 알려진 한계 (숨기지 않고 적어둠)

| 항목 | 현재 상태 |
|---|---|
| Samsung Health 실제 연동 | 미구현 (채윤 담당). 스위치를 끄면 지금은 가상 데이터가 나옵니다 |
| 5단계 수면이 실제로 내려오는가 | **미확인.** Samsung Health 가 REM/깊은잠까지 주는지 확인 필요 → 안 주면 `epoch_30s.csv` 의 `stage_label` 이 0/2 정도로만 채워집니다 |
| 에뮬레이터 검증 | 가상 데이터 경로만 검증 가능. SDK 경로는 **채윤의 실기기에서만** 확인됩니다 |
| 날짜 선택 | 캘린더가 아니라 `기간 칩 + 이전/다음` 버튼. DatePicker 는 MVP 범위 밖으로 뺐습니다 |
| ~~`night` 이 조회 순번~~ | **해결됨 (2026-09-18).** `night` = 날짜 `yyyyMMdd`, `epoch_idx` = 정오 기준 좌표로 바꿨습니다. 조회 범위와 무관하게 값이 고정됩니다 |
| 낮 시간 데이터 | `epoch_30s.csv` 에는 수면 세션 구간만 들어갑니다. 낮 데이터는 `hr.csv` / `steps.csv` 로 나갑니다 |
| **기기 타임존이 바뀌면** | `night` / `epoch_idx` 는 **로컬 시간의 정오** 기준입니다. 측정 중에 타임존이 바뀌면(해외 이동 등) 좌표가 어긋납니다. 이번 연구는 전원 국내라 문제가 없고, `meta.json` 의 `timezone` 필드에 기록은 남습니다. UTC 기준으로 바꾸면 이 문제는 없어지지만 "정오~정오가 한 밤"이라는 직관이 깨져서 지금은 로컬 기준을 유지했습니다 |
| 낮잠 판정 | 낮잠도 `stage_label` 에 그대로 들어갑니다. 밤잠만 쓰고 싶으면 `session_id` 로 걸러야 합니다 (`epoch_idx` 가 작은 쪽이 낮잠) |

## 9. 처음 빌드할 때 나올 수 있는 것

| 증상 | 원인 / 대응 |
|---|---|
| `JAVA_HOME is not set` | PowerShell 에서 `gradlew` 를 돌린 것. 3단계 (1) 참고 |
| `The term '.\gradlew.bat' is not recognized` | wrapper 가 없는 폴더에서 돌린 것. 이 `소빈_kotlin` 폴더 안에서 실행하세요 |
| `org.jetbrains.kotlin.android` 관련 오류 | AGP 9 가 이 플러그인을 거부합니다. 3단계 (2) 참고 |
| `Unresolved reference: HorizontalDivider` | Material3 버전이 낮음. `Divider` 로 바꾸면 됩니다 (기능 동일) |
| Gradle Sync 가 10분 넘게 안 끝남 | 첫 실행은 정상입니다 (Gradle 145MB 다운로드). 인터넷 끊김만 확인 |
| 앱이 켜지자마자 죽음 | Logcat 탭을 열고 빨간 줄 첫 부분을 저에게 보내주세요 |
