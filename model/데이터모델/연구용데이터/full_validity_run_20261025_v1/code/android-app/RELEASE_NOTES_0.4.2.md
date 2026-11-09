# SleepWell User Android 0.4.2

## 변경 사항

- 기존 추천 시각이나 `robust-next-night-research-v2` 예측을 바꾸지 않고 연구용 기준선 9개를 추가했다.
- 기준선은 한국시간 20:00 cutoff 이전에 끝나고 보고된 수동 기록만 사용한다.
- 프로필 fallback을 TRAIN 집단 prior와 명확히 구분한다.
- 다음날 첫 일정 기준선은 cutoff까지 알려진 일정과 사용자 프로필 희망 수면시간만 사용한다. 일정 결측·늦은 입력·범위 밖 일정은 unavailable이며 소급 보간하지 않는다.
- 기준선은 최초 전향 스냅샷, 최초 수동 결과, 일일 백업과 realized comparison에 보존된다. 과거 스냅샷은 변경하지 않는다.
- 앱 버전은 `versionCode=6`, `versionName=0.4.2`이다.

## 검증 기록

- `:app:assembleDebug :app:lintDebug` — 성공, 47 tasks.
- `ResearchBaselinesParityTest` — 미래/정시 기록 누수, 자정 원형평균, 일정 경계, 스냅샷 불변·백업 계약 통과.
- `AdaptiveResidualParityTest` — 통과.
- `UserContractRulesTest` — 15 checks 통과.
- lint 보고서: `app/build/reports/lint-results-debug.html` 및 `.sarif`/`.xml`/`.txt`.
- APK: `app/build/outputs/apk/debug/app-debug.apk` (31,378,436 bytes at final verification).
- APK SHA-256: `8718791ba99d15ac5dd725ccadd0bcc77621e1a8d67661a36d4ccff6b27ba99b`.
- 서명 인증서 SHA-256: `72a3b8e32ecae55a73c840bc4d6bb4e74049f312b60b9bde0b782fb26e595333`; 0.4.1 debug APK와 동일.

## 미완료 검증

물리 Android 단말 설치, 20시 예약 실행, 날짜 전환, 실제 PC 백업 송수신은 수행하지 않았다. 이 버전은 debug 서명 연구 빌드이며 외부 서비스나 Drive를 변경하지 않았다.
