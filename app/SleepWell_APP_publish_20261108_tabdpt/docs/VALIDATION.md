# 통합 v8 검증 안내

## 0.8.7 경량 TabDPT 검증 — 2026-11-08

공개 저장소에는 TabDPT 가중치·CMI/MESA 학습 문맥·golden 입력을 포함하지 않습니다. `tools/setup_light_tabdpt.ps1`로 검증된 비공개 번들의 9개 파일을 해시 확인 후 준비한 환경에서 검증했습니다.

- 기존 기능 회귀 226개 통과
- 기기 시간대·DST·계산 중 UI 비차단 6개 통과
- 실제 ONNX golden 및 28일 DATA33→TabDPT→개인화 통합 2개 통과
- 앱·androidTest APK 빌드 및 lint 성공, lint 오류 0개
- 모델 검사 중 에뮬레이터 활성 네트워크 없음

생산 패키지는 `app.sleepwell.offlinehgb`, 테스트 패키지는 `app.sleepwell.offlinehgb.test`입니다. applicationId의 `offlinehgb`는 기존 설치 데이터와 업데이트 호환성을 위한 영구 식별자이며 0.8.7의 생산 예측기는 경량 TabDPT입니다.

실물 Samsung Health/Watch 신규 기록 수신과 장시간 백그라운드 실행은 이 에뮬레이터 검사 범위가 아닙니다. A52s 독립 모델 검사에서 관측된 지연도 완성된 앱의 성능 보증으로 사용하지 않습니다. 아래 2026-11-06 내용은 이전 0.8.4 검증 기록입니다.

검증일: 2026-11-06. 연령대 입력 제거 후 Android API 37 에뮬레이터에서 36개 회귀 테스트 클래스의 **208개 테스트가 통과**했습니다 (38.639초). 수동 실행 전용 `BackgroundJobLifecycleProbe`는 이 집계에서 제외했습니다. 별도 JVM 테스트 소스는 없으므로 `testDebugUnitTest`는 `NO-SOURCE`입니다.

## 빌드

```powershell
.\gradlew.bat :app:assembleDebug :app:assembleDebugAndroidTest :app:lintDebug
```

캐시가 준비된 검증 환경에서는 `--offline`을 사용했습니다. 최초 설정에서는 SDK와 Gradle 의존성 다운로드가 필요합니다. 연령대 입력 제거 최종 소스에서 앱·테스트 APK 빌드와 lint를 다시 실행했으며 오류 0개, 경고 8개, 힌트 9개였습니다.

## 회귀 테스트 실행 예시

아래 명령은 SDK의 `adb`가 PATH에 있고 에뮬레이터 한 대가 연결되어 있다는 전제입니다. 테스트용 에뮬레이터를 사용하세요. `install -r`는 기존 앱 데이터를 초기화하지 않지만 서명이 같아야 합니다.

```powershell
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
$classes = Get-ChildItem app/src/androidTest/java -Recurse -Filter '*Test.kt' | ForEach-Object {
    $packageLine = (Select-String -LiteralPath $_.FullName -Pattern '^package ').Line
    $packageName = $packageLine.Substring(8).Trim()
    "$packageName.$($_.BaseName)"
}
adb shell am instrument -w -r -e class ($classes -join ',') app.sleepwell.offlinehgb.test/android.test.InstrumentationTestRunner
```

이 명령은 현재 테스트 파일명과 클래스명이 같은 구조를 이용합니다. 수동 작업 상태를 검사하는 `BackgroundJobLifecycleProbe`는 선택되지 않습니다.

## CSV 과거 재분석 회귀 범위

- 실제 CSV 저장 → 원본별 모델 입력 로드 → 과거 분석 호출까지 연결 검증
- 선택 날짜가 바뀌면 입력 기록과 DATA33 누적 범위가 달라지고, 유효 CSV 내용 변경이 예측에 반영됨
- 후일 CSV·HC·일지·설문을 추가해도 같은 과거 날짜의 예측·DATA33은 동일함
- UTC에서 화면 다음 자정이 KST 모델 20시보다 빠른 경우, KST 20시 유지, 뉴욕 DST 25시간 날짜
- 경계 시각 HC·일지·직접 입력을 중복 정리 전에 제외하여 유효한 이전 기록을 밀어내지 않음
- CSV와 HC 중첩, 실시간 경로의 원래 수신 시각·우선순위 유지, 모델 전역 상태와 파일 원본 불변
- 예측 생성 방식·입력 경계의 스냅샷/결과 CSV 왕복 저장, 구버전 기본값, 같은 날짜 원본 예측과 재분석의 공존

구형 `InstrumentationTestRunner`의 전체 DEX 자동 검색은 이 환경에서 시작 ANR을 일으켰습니다. 위처럼 테스트 클래스를 명시하여 실행하면 208개 모두 통과합니다. 테스트 실패를 숨기거나 건너뛴 것이 아니라, 실행 전 클래스 탐색 단계를 대체한 것입니다.

## 남은 lint 경고

| 검사 | 개수 | 파일 |
|---|---:|---|
| DefaultLocale | 3 | Exporters.kt, MainActivity.kt |
| RedundantLabel | 1 | AndroidManifest.xml |
| DataExtractionRules | 1 | AndroidManifest.xml |
| MissingApplicationIcon | 1 | AndroidManifest.xml |
| UseKtx | 2 | SleepStageChart.kt |

전체 lint 보고서는 빌드 후 `app/build/reports/lint-results-debug.html`에서 확인합니다. 위 경고는 이번 저장·복원 통합에서 수정하지 않은 코드에 있습니다.

## 데이터 보존 및 한계

기존 프로필·설문·저장 결과·수면 원본·모델 입력 기록·합친 수면 CSV·계산 CSV 총 7개 영구 파일이 검증 전후 바이트 단위로 동일했습니다. 저장 결과가 참조하지 않는 이전 프로세스의 임시 계산 사본은 기존 lease 정리 정책으로 해제됐으며, 해당 원본 CSV는 보존됐습니다. 원본 기록과 사적인 검증 증거는 이 저장소에 포함하지 않습니다. UI 테스트의 예시 데이터는 격리된 메모리 또는 테스트 디렉터리에서만 사용합니다.

실물 삼성 계정과 Galaxy Watch의 신규 데이터 수신, 제조사 배터리 제한 상태의 장시간 동기화, 예측의 통계적 정확도는 이번 테스트 통과로 검증되지 않습니다. 모델 가중치와 기존 점진 개인화 정책은 통합 과정에서 변경하지 않았습니다.

## v0.8.2 기기 시간대 검증

KST 기본 모델 parity, UTC 입력·07:30 기상, 뉴욕 DST 종료, 시드니 DST 시작과 고정 golden 검증, DATA33 현지 시각·원본 Instant 불변, 기존 설문·일지 보존을 포함한 10개 테스트를 추가했습니다. 모델 자산·가중치는 동일합니다. 앱 실행 중 KST→UTC→KST 변경 시 계산과 표시가 함께 갱신되는 것을 확인했습니다. 한국 시간 화면의 권장은 11/6 23:07→11/7 07:30이었고, UTC에서도 평소 기상 목표는 현지 07:30으로 계산됐습니다. 기존 데이터 6개 파일은 업데이트 전후 동일한 SHA-256을 유지했습니다.


## v0.8.3 연령대 입력 제거 검증

- 초기 설정과 내 정보·수정 화면에서 연령대 입력과 필수 조건을 제거했습니다. 새 프로필은 빈 연령을 저장하고, 기존 연령 문자열과 과거 스냅샷은 보존합니다.
- `ProfileOnboardingRegressionTest`: 연령 누락·과거 문자열 보존, 연령 없이 완료·재로딩, 성별 검증 유지.
- `V7LaunchRegressionTest`: 연령 선택지 없음, 초기 설정 다음/저장 활성화와 빈 연령 저장, 기존 프로필 편집 시 연령 불변. 화면 접근성 준비를 기다려 검사합니다.
- `AgeOptionalPredictionRegressionTest`: 빈 이력 및 21개 일지·웨어러블 이력에서 빈 연령과 기존 4개 연령군의 예측·권장·DATA33·개인화 결과가 동일합니다. 비교 제외 항목은 프로필 연령 메타데이터와 설명용 집단 평균뿐이며, `used_for_target=false`도 확인합니다.
- 모델 자산 3개의 SHA-256, DATA33 계산식, 모델 가중치, 개인화·Health Connect·백그라운드 동기화 코드는 변경하지 않았습니다.
- 최종 208개/36클래스 계측 테스트 통과. 앱·테스트 APK 빌드 성공, lint 오류 0·기존 경고 8·힌트 9. JVM 테스트는 별도 소스가 없어 NO-SOURCE입니다.
- 기존 서명과 동일한 APK로 삭제 없이 업데이트 설치하고 앱 실행을 확인했습니다. 내 정보·수정 화면에서 연령이 사라진 실제 화면도 확인했습니다.
- 첫 전체 실행에서 새 편집 UI 테스트 1개가 렌더링 대기 부족으로 실패했으며, 제한 시간 내 화면 제목과 버튼을 기다리도록 수정한 후 전체 208개가 통과했습니다. 검사를 삭제하거나 우회하지 않았습니다.
- 실제 삼성 계정/워치의 신규 수신과 장기간 OS 동기화는 이번 에뮬레이터 테스트로 새롭게 확인한 것이 아닙니다.
