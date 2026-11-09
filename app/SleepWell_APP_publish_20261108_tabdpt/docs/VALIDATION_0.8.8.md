# SleepWell 0.8.8 HGB 하이브리드 검증

저자: Han Seoyun · 2026-11-08

- 패키지: app.sleepwell.offlinehgb, versionCode 16.
- DATA33 33개 + Two-Process 6개 → 기존 HGB → 기존 개인화·권장 정책.
- 에뮬레이터 회귀 233개(217+시간대16) 통과. UI 설명 최종 수정 뒤 실기기 HGB 6개 재검증 통과.
- HGB golden 24개 코호트 입력과 원본 출력 일치, 28일 메모리 내 기록에서 history_count=28, personalization_weight=0.5067883983937148, HGB 권장 카드·구간 표시 확인.
- assembleDebug, assembleDebugAndroidTest, lintDebug 성공. lint 오류 0, 경고 13, 힌트 10. 경고 종류: AndroidGradlePluginVersion, DataExtractionRules, DefaultLocale, GradleDependency, InsecureBaseConfiguration, MissingApplicationIcon, NewerVersionAvailable, RedundantLabel, UseKtx.
- 기존 로컬 빌드 산출물 잠금으로 기본 출력 경로 clean 실패. 별도 Gradle buildDirectory로 전체 빌드·lint 수행. 앱 소스와 실행 설정은 동일.
- A52s(Android 14)에 adb install -r 업데이트 성공. 앱 삭제/데이터 초기화 없음. 설치 APK SHA-256과 배포 파일 일치, Activity 실행 Status: ok.
- 설치 직후 기존 파일 3개 모두 해시 동일. 검사·실행 후 사용자 profile.properties 동일; Android profileInstalled 최적화 메타데이터만 변경됨. 설치 전 이 패키지에는 수면/설문 저장 파일이 없었음. 개인 백업은 로컬에만 보존.
- 기존 수면 저장소·설문·프로필·DATA33·개인화 엔진·Health Connect·백그라운드 동기화·시간대 코드는 전환 전과 동일. 실물 워치의 새 수면 수신/장시간 백그라운드 동작은 별도 미검증.
- APK에 TabDPT 학습 문맥/ONNX Runtime 없음. 학습된 HGB 이진 모델 456,000바이트 포함. 시험 fixture/개인 기록/백업은 릴리스에 포함하지 않음.
- APK 크기: 40,313,512바이트. SHA-256: `4621038ef81053f0ead40dad7923772df2cac442c561db1e90b5b366944f555f`.

## 재현

Android SDK 37, Android Studio JBR, 기존 Gradle wrapper 사용. `:app:assembleDebug :app:assembleDebugAndroidTest :app:lintDebug` 실행. HGB golden fixture는 공개하지 않으므로 해당 테스트를 실행하려면 `tools/setup_legacy_hgb_tests.ps1`로 허가된 로컬 fixture를 준비해야 함. 일반 앱 빌드에는 저장소의 HGB 모델만 필요함.

에뮬레이터 검증은 `TabDptLiveServerTest` 및 캡처 전용 `V8UiEvidenceTest`, `HistoryCalendarUiEvidenceTest`를 제외함. 날짜/시간대 3개 클래스는 별도 실행. 사용자 저장소에 테스트 수면을 넣지 않음.
