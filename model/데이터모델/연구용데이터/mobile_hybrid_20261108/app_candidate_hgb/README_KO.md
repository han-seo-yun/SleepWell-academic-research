# 오프라인 HGB 앱 후보 (검토용, 미설치)

## 상태

- 후보 소스: `E:/sleepwell-builds/offline-hgb-app-candidate-20261108`
- 검증 전용 복사본: `E:/sleepwell-builds/offline-hgb-app-verification-20261108`
- 기준 정식 소스: `E:/학술제/앱_소빈/통합 v8`
- 정식 소스는 수정하지 않았다. 교체 대상 6개 정식 파일의 작업 전 SHA-256을 다시 확인했고 모두 그대로였다. 새 HGB 런타임·모델·테스트 파일도 정식 소스에는 존재하지 않는다.
- 아래 APK는 사용자 결정 전 검토용이다. 휴대폰과 실제 앱 패키지에는 설치하지 않았고 배포하지 않았다.

## 후보 동작

- 저장된 옛 서버 설정의 enabled 값이 false 또는 true여도 앱 조립 경로는 항상 기기 내 HGB를 사용한다.
- HGB는 33개 DATA 특징과 6개 생리 특징의 고정 39개 순서를 사용해 CMI와 MESA를 각각 계산한다.
- CMI·MESA 산술평균, 개인 보정, 권장 정책, 기록 억제, 범위 제한은 기존 흐름을 유지한다.
- 과거 재구성에서는 로컬 HGB도 실행하지 않는다.
- 모델 asset을 읽지 못하거나 추론 검증에 실패하면 앱 설치 자체가 실패하지 않고 예상 시각을 숨긴 채 기존 physical/Two-Process 권장으로 돌아간다.
- HTTP TabDPT 구현은 소스에 남아 있지만 이 후보의 기본 조립과 사용자 화면에는 노출하지 않는다. 로컬 HGB를 TabDPT로 표시하지 않는다.
- 결과 provenance는 `local_hgb_hybrid39`, `on_device_offline`, `network_required=false`를 기록한다. HGB에서는 기존 `tabdpt_*` preference 필드를 false/null로 두고 generic `prediction_*` 필드를 사용한다.

## APK

- 파일: `SleepWell-0.8.6-offline-HGB-candidate-debug.apk`
- 크기: 38,235,880 bytes
- SHA-256: `a0e8f54c4ce9f9d91be8fa7c7723b36483193197d287fc9af0e9b126019aab64`
- applicationId: `app.sleepwell` (정식 앱과 같으므로 사용자 결정 전 설치 금지)

## 검증 결과

- 컴파일: `:app:compileDebugAndroidTestKotlin` 성공.
- Lint: `:app:lintDebug` 성공.
- 빌드: `:app:assembleDebug` 성공.
- JVM unit test task: `:app:testDebugUnitTest`는 기존 프로젝트에 JVM test source가 없어 `NO-SOURCE`.
- 호스트 Java golden 검증: 컴파일된 앱 `HgbModel`로 CMI 12행 + MESA 12행을 실행해 raw 및 constrained 네 출력 모두 bit-exact 일치 (`24/24`, 최대 절대오차 0).
- 에뮬레이터 전체 instrumentation: 검증 복사본의 applicationId만 `app.sleepwell.hgbcandidate`로 바꿔 `emulator-5554`에서 `237/237`, failures 0, errors 0, skipped 0.
- 전체 결과 XML: `instrumentation-237.xml` (SHA-256 `288de453f7f4762b5086d37ff46ba6981df49ba928d964127c1fefc9ba2492db`).
- 마지막 metadata 보완 뒤 HGB 집중 instrumentation: `LocalHgbIntegrationTest` `5/5` 통과.
- 검증 복사본의 UI evidence 두 파일은 distinct package host를 사용할 수 있도록 검증 복사본에서만 package literal을 동적으로 바꿨다. 이 변경은 후보·정식 소스에 포함되지 않는다.
- 실제 A52s `R5CRC0XPS2K`에는 연결·설치·테스트하지 않았다. adb 서버와 에뮬레이터를 재시작하거나 종료하지 않았다.

## 적용 경계

- `ready_to_apply_manifest.json`은 정식 소스 기준 상대 경로, add/replace 동작, 기준 SHA-256 및 후보 SHA-256을 기록한다.
- 사용자 선택 전에는 이 manifest를 정식 소스에 적용하지 않는다.
- 정확도 비교 결과 및 허용 가능한 손실에 대한 사용자 결정 후에만 별도 적용·설치·배포 단계를 진행한다.
