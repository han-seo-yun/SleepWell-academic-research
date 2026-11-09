# 기기 시간대 자동 연동 — 2026-11-05

## 동작

- 수동 한국 시간/UTC 선택을 없애고 `시간대 자동 · <기기 IANA ID>`를 표시한다.
- 앱 시작·재개와 Android `ACTION_TIMEZONE_CHANGED` 수신 시 표시 상태를 갱신한다.
- 날짜, 현재 시각, 분석·권장 일정, 수면 그래프와 새 저장 결과를 기기 시간대로 표시한다. 서머타임과 30분·45분 시차를 포함한다.
- 일지의 절대 시각과 연구 모델의 Asia/Seoul 날짜·cutoff 계약은 유지한다. 해외 사용자용 모델 검증이나 생체리듬의 여행 적응 정책을 변경한 것은 아니다.

## 저장 호환성과 입력

- 새 설문에는 `sourceZoneId`, `sourceDate`, `targetWakeMs`, `lastCaffeineMs`, `napEndMs`를 추가한다. 카페인·낮잠은 입력 시각 이전의 가장 최근 해당 시각을 절대 시각으로 보존하여 자정 이후에도 날짜가 어긋나지 않는다.
- 기존 설문은 종전 계약인 Asia/Seoul을 따른다. 새 저장 결과는 모델·권장·기준 epoch와 원본 시간대를 저장한다.
- 절대 시각이 없는 기존 결과 문자열은 임의로 변환하지 않고 저장 당시 표시라는 안내를 붙인다. 결과에 첨부된 설문 요약 문자열도 저장 당시 시간대를 명시한다.
- 저장된 평소 패턴은 날짜가 없는 시계 중간값이므로 다른 시간대에서는 원본 시간대를 명시해 표시한다. 임의 날짜를 붙여 서머타임 시차를 추측하지 않는다.
- 작성 도중 시간대가 바뀐 미완성 입력은 원래 시간대에 묶어 두고 안내한다. 이를 현재 시간대로 재해석해 수면 기록이 이동하는 일을 방지한다.
- 서머타임으로 존재하지 않는 직접 입력 시각은 오류로 안내한다. 중복되는 시각은 앞선 절대 시각으로 해석한다.

## 변경 파일

- `health/HealthModels.kt`, `ui/UiZone.kt`, `MainActivity.kt`: 자동 시간대 상태·생명주기·상단 표시.
- `health/LocalTimeTicks.kt`, `ui/AnalysisScreen.kt`, `ui/SleepStageChart.kt`: 지역 시각 눈금·캐시·입력과 알람 복원.
- `analysis/DirectInputForm.kt`, `SurveyCsv.kt`, `SurveyStore.kt`: 원본 시간대 보존·DST 검증.
- `analysis/AnalysisDisplay.kt`, `ResultStore.kt`, `ui/SavedResults.kt`: 원본 일정 시각 보존과 표시 변환.
- 새 회귀 테스트: `DeviceTimezoneRegressionTest`, `SurveyTimezoneRegressionTest`, `SavedResultTimezoneRegressionTest`.

## 검증

- `:app:assembleDebug`, `:app:assembleDebugAndroidTest`, `:app:lintDebug` 성공. lint 오류 0, 기존 경고 14, 힌트 6.
- Android API 37 에뮬레이터에서 전체 64개 instrumentation 테스트 통과. [원본 실행 결과](verification/timezone-instrumentation-2026-11-05.txt).
- 실행 중 기기 시간대 Asia/Seoul → America/Los_Angeles 변경 시 상단 시간대 표시와 현재 날짜·시각이 11/5 14시대 → 11/4 21시대로 자동 갱신되는 것을 화면에서 확인했다.
- 검증 후 에뮬레이터는 현재 사용자 환경인 Asia/Seoul로 설정하고 앱을 재실행했다. 앱 코드가 한국 시간으로 고정된 것은 아니다.
- APK SHA-256: `1A03D50D4560A478AEF6ED7644346307D5EA30CA86B1408B4C660D75A61AE37B`.
- 검사 중 파일 수정이 겹쳐 첫 lint가 도구 내부 오류로 중단되어 파일 변경 완료 후 재실행했다. 최종 lint는 통과했다. 구형 JUnit3가 Kotlin 생성 메서드를 테스트로 잘못 찾던 부분도 보완한 뒤 전체 테스트를 재실행했다.

구현 참고: [Android Developers — Broadcasts overview](https://developer.android.com/develop/background-work/background-tasks/broadcasts).
