# SleepWell Android 사용자 앱

현재 기준: 2026-11-01 · 앱 버전 0.4.5 (`versionCode=7`)

이 폴더는 사용자 기록, 수면 일정 추천, 연구용 다음 주수면 예상, Health Connect 가져오기, Windows 연구자용 앱 동기화를 포함한 Android 소스입니다. 과거의 가상 데이터 조립 안내서는 현재 구조와 달라 이 문서로 교체했습니다.

## 지원 환경

- `compileSdk=37`, `targetSdk=37`, `minSdk=29`
- AGP 9.4.0, Gradle 9.7.1, Kotlin 2.4.20
- 빌드 JDK 17 이상
- Health Connect 읽기와 PC 동기화 UI는 Android 14 이상에서 사용
- 현재 배포 APK: `E:\학술제\어플리케이션(사용자용)\배포\SleepWell-User-0.4.5-debug.apk`

현재 APK는 debug 서명 연구 빌드입니다. Play Store 배포본이나 의료기기가 아닙니다.

## 주요 기능

- 프로필과 수면 일지를 휴대폰 내부에 저장하고 권장 취침·기상 시각을 계산합니다.
- 다음날 첫 일정, 준비·이동시간, 낮잠 1회, 카페인 1회를 기록합니다.
- 권장 일정과 연구 모델의 다음 주수면 예상 시각을 분리해 표시합니다.
- 최초 예측, 최초 실제 결과, 수정 결과, 추천 노출을 덮어쓰지 않고 구분해 보존합니다.
- Samsung Health가 Health Connect에 저장한 수면을 사용자가 화면에서 직접 요청할 때 가져옵니다.
- Windows 연구자용 앱과 연결하면 최근 30일 수면은 `/v1/sync`로 전송하고, 걸음·운동 및 SleepWell 직접 입력 생활 정보는 `/v1/context` 전송을 시도합니다. 후자는 아래의 현재 스키마 차단 사항을 먼저 해결해야 합니다.
- 자동 로컬 백업과 실패한 PC 전송 재시도 자료를 Android Keystore로 보호합니다.

카페인 입력에는 개인별 효과 계수나 섭취량 권고가 없습니다. Process S는 고정 시간상수로 계산한 모델값이며 생리 측정값이 아닙니다. 최근 기록의 전후 비교는 기술 통계이고 추천 효과의 인과 검증이 아닙니다.

현재 코드 감사에서 수면·백업과 생활 맥락의 완료 상태가 다름을 확인했습니다. 수면은 `/v1/sync` 스키마 1, 사용자 상태는 `/v1/backup`으로 송신합니다. 걸음·운동·일정·낮잠·카페인의 `/v1/context`는 Android 송신 스키마 1과 PC 저장 스키마 2가 일치하지 않고 저장소에 수신 라우트가 없어 종단간 미검증입니다. 생활 맥락을 실제 PC 모델 입력으로 사용하기 전에 송수신 계약을 맞춰야 합니다.

## 프로젝트 구조

- `app/src/main/java/app/sleepwell/user`: 사용자 화면, 저장소, 개인화, 예측·추천 계약, 백업
- `app/src/main/java/app/sleepwell/sync`: Health Connect 읽기, PC 연결, TLS 고정, 암호화 상태
- `app/src/main/java/app/sleepwell/health`: 가상 데이터 연구 데모, epoch 집계, Two-Process·하이브리드 분석
- `app/src/main/java/app/sleepwell/export`: 연구용 CSV/JSON 내보내기
- `app/src/main/assets/models`: 앱에 포함된 집단 초기값·연구 모델
- `tools`: JVM에서 실행하는 계약·parity 검사

런처 화면은 `UserActivity`입니다. `MainActivity`의 가상 데이터 분석 화면은 내부 연구 데모이며 일반 사용자 런처가 아닙니다.

## 빌드

Android Studio에서 이 `android-app` 폴더를 엽니다. 명령줄에서는 JDK 17 이상과 Android SDK 경로가 필요합니다.

```powershell
cd E:\학술제\기타\SleepWell-integrated\android-app
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat :app:assembleDebug
.\gradlew.bat :app:lintDebug
```

APK는 `app/build/outputs/apk/debug/app-debug.apk`에 생성됩니다. 실제 SDK 설치 경로가 다르면 위 환경 변수만 맞게 바꿉니다. 시스템의 전역 Java 설정을 바꿀 필요는 없습니다.

## 현재 검증 상태

2026-11-01에 JDK 17과 로컬 Android SDK를 지정해 확인했습니다.

- `:app:assembleDebug`: 성공
- `:app:lintDebug`: 실패 — 5 errors, 21 warnings, 2 hints
- 첫 lint 오류: `HealthConnectContextReader.kt`가 U Extensions 7 이상이 필요한 `Record.metadata` API를 사용하지만 확장 SDK 최소 버전 검사가 선언되지 않음
- lint 보고서: `app/build/reports/lint-results-debug.html`

따라서 현재 소스는 debug APK를 만들 수 있지만 lint가 깨끗한 릴리스 상태는 아닙니다. lint 오류를 해결하거나 명시적으로 검토하기 전에는 새 APK를 최종 배포본으로 승격하지 않습니다. 기존 `어플리케이션(사용자용)/배포`의 0.4.5 APK는 보존합니다.

## Health Connect와 PC 동기화

1. Windows 연구자용 앱에서 모바일 연결 수신 기능을 켭니다.
2. Windows 앱이 표시한 `sleepwell://pair?...` 주소를 Android의 **PC 연결 설정**에 붙여넣습니다.
3. 수면·걸음·운동 권한과 로컬 네트워크 권한을 허용합니다.
4. **지금 동기화**를 누릅니다.

동기화는 사용자가 누르는 전경 동작만 지원합니다. 같은 사설망의 IPv4 주소만 허용하고, 자체서명 TLS 인증서를 SHA-256 leaf 인증서 지문으로 고정합니다. 연결 코드는 43자의 URL-safe 비밀과 64자리 인증서 SHA-256을 검사합니다. 한 수면 요청은 최대 100세션·1MB입니다. 수면 전송 대기 자료는 성공 응답 전까지 보존하고, 범위 전체 조회가 끝난 뒤에만 삭제 ID를 계산합니다. 수면 값이나 연결 토큰은 로그에 남기지 않으며 `android:allowBackup=false`입니다.

Health Connect 권한은 수면, 걸음, 운동을 별도로 요청합니다. 걸음·운동 범위는 전체 페이지 조회가 성공한 종류만 완료 범위로 표시해야 하며, 권한 거절이나 조회 실패를 관측값 0으로 바꾸지 않습니다. 현재 `HealthConnectContextReader.kt`의 U Extensions 7 lint 오류와 `/v1/context` 스키마 불일치 때문에 생활 맥락은 검증 완료 기능이 아닙니다. 자세한 상태는 [휴대폰 연결 안내](../desktop/MOBILE_SYNC.md)와 [현재 연동 구조](../docs/analysis-csvfilesource-연동구조.md)를 따릅니다.

## 추천 게시 시점과 보존

추천은 예상 입면 2시간 전과 20:00 중 이른 시각에 최초 게시할 수 있고, 20·21·22·23시에는 사용자가 현재 깨어 있음이 확인된 상호작용에서만 조건부 갱신합니다. 수면 기록이 아직 없다는 이유로 깨어 있다고 추정하거나 지나간 슬롯을 소급 실행하지 않습니다.

Android 상태는 `recommendation_timing`, `recommendation_publications`, `evaluated_recommendation_slots`를 별도 저장합니다. 게시물에는 계획 밤, 슬롯, 종류, 예정·실제 게시 시각, 입력 fingerprint와 당시 추천을 보존합니다. 추천 노출 기록은 정책 효과의 인과 추정값이 아닙니다.

## 연구용 내보내기 계약

가상 데이터 연구 데모의 내보내기는 `meta.json`, `hr.csv`, `steps.csv`, `sleep_stage.csv`, `epoch_30s.csv`, `export.json`으로 구성됩니다. `epoch_30s.csv`의 핵심 열은 다음과 같습니다.

```text
subject_id,night,session_id,epoch_idx,stage_label,time_iso,hr_mean,steps
```

- `night`: 주수면이 속한 날짜 `yyyyMMdd`
- `epoch_idx`: 해당 날짜 정오부터 30초 단위 좌표
- `stage_label`: `0=W, 1=N1, 2=N2, 3=N3, 4=REM`, 미분류 `-1`

이 내보내기는 연구 데모 계약입니다. Health Connect 동기화 payload 및 사용자 백업 스키마와 혼동하지 않습니다.

## 알려진 제한

- Health Connect 읽기·PC 동기화는 Android 14 이상에서만 실제 동작합니다. `minSdk=29`는 앱 설치 하한이며 모든 기능 지원 하한이 아닙니다.
- Health Connect에서 제공되는 단계와 출처는 기기·Samsung Health 설정에 따라 달라질 수 있습니다.
- 타임존이 측정 중 바뀌면 로컬 정오 기준 `night`·`epoch_idx` 좌표가 어긋날 수 있습니다.
- 자동 백그라운드 Health Connect 가져오기는 지원하지 않습니다.
- 생활 맥락 `/v1/context`는 현재 Android 스키마 1과 PC 저장 스키마 2가 불일치하고 수신 라우트가 없어 종단간 동작하지 않습니다.
- 실기기 장기 실행, 20시 전후 조건부 갱신, 날짜 전환, PC 송수신 장애 복구는 배포 전 별도 검증이 필요합니다.

0.4.2의 당시 APK 크기·해시·검증 결과는 [RELEASE_NOTES_0.4.2.md](RELEASE_NOTES_0.4.2.md)에 과거 릴리스 기록으로 보존합니다.
