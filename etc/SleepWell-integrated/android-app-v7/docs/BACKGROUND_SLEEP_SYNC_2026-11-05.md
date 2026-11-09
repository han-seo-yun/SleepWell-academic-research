# 앱 종료 중 수면 자동 누적

통합 v6에 Android JobScheduler 기반의 백그라운드 수면 동기화를 추가했다. Health Connect 백그라운드 읽기를 지원하는 기기에서 사용자가 수면 읽기와 백그라운드 읽기를 허용하면, 화면이 없어도 수면 기록을 기존 로컬 저장소에 누적한다. 다음 앱 실행 때 기존 `ImportedSleepStore` → `NextNightModel` 경로로 읽어 DATA33·점진 개인화 예측에 반영한다.

## 사용 흐름

1. 기본정보를 저장하고 기존 **삼성 헬스 연결하기**로 수면 읽기를 허용한다. 삼성 헬스에서 Health Connect에 수면 공유도 켜져 있어야 한다.
2. **앱을 닫아도 자동 누적 허용** 버튼에서 백그라운드 읽기를 추가로 허용한다. 이 버튼은 기기의 기능 지원 여부와 기존 수면 읽기 권한을 확인한 후 표시된다. 권한 요청은 사용자 버튼 조작에서만 실행한다.
3. 앱은 고유 반복 작업 하나를 예약한다. 설정은 1시간 간격·15분 flex이며 Android가 실행 시점을 정한다. 앱을 최근 앱 목록에서 닫아도 작업이 가능하고 재부팅 후에도 예약이 유지된다.
4. 다음 실행 때 누적 기록을 다시 읽어 예측한다. 화면 진입·복귀와 화면 사용 중 15분 간격의 기존 조회도 유지한다. 백그라운드 미지원·권한 거부 기기는 기존 화면 조회를 계속 사용한다.

## 처리와 실패 복구

- `SleepSyncJobService`는 별도 Activity나 알림 창 없이 기존 `HealthConnectSleep.readAsCsv`와 `SleepDataStore.saveHealthConnect`를 호출한다. 모델이나 설문 UI의 전역 상태는 백그라운드 작업에서 바꾸지 않는다.
- 매번 기능·수면 권한·백그라운드 권한을 점검하고 읽는 중 권한이 취소됐는지도 저장 전 다시 확인한다. 거부·미지원은 데이터를 읽거나 저장하지 않고 예약을 취소한다. 다음 앱 복귀/권한 허용 시 다시 예약한다.
- 제공자 일시 오류·저장 오류는 재시도한다. 60초 제한과 Android 중단 요청에 코루틴 취소로 응답한다. 실패하거나 조회 결과가 0개여도 기존 수면 기록은 지우지 않는다.
- 같은 ID로 작업이 다시 시작되면 이전 실행을 취소한다. 완료 시 현재 실행과 일치할 때만 실행 상태를 제거한다.
- 원본 수면과 정규화 모델 저장소는 같은 프로세스 잠금을 사용한다. 화면과 작업이 동시에 기록을 가져오거나 모델을 읽을 때 임시 파일 충돌·부분 JSON 읽기·서로 다른 밤의 유실을 막는다.
- 기본 30일 조회를 기존 누적 기록과 병합한다. 출처·실제 관측 시각, 중복 제거, 직접 일지 우선, 설문 연결·누수 방지, 단계 누락 처리 등 이전 정책은 그대로 유지한다.
- 화면에 예약 상태와 마지막 백그라운드 확인 시각을 표시한다. 확인 시각은 수면이 새로 추가됐다는 의미가 아니라 조회·처리가 성공했다는 의미다.

## 변경 파일

| 파일 | 역할 |
| --- | --- |
| `analysis/BackgroundSleepSync.kt` (신규) | 권한 상태, 고유 반복 작업, 조회·기존 저장 경로, 마지막 확인 상태 |
| `analysis/SleepSyncJobService.kt` (신규) | Activity 없는 실행·취소·시간 제한·재시도 |
| `analysis/HealthConnectSleep.kt` | Health Connect 백그라운드 기능·권한 상수 |
| `MainActivity.kt`, `ui/AnalysisScreen.kt` | 복귀 시 예약 확인, 별도 권한 버튼·상태·기존 전경 동기화 |
| `analysis/SleepDataStore.kt`, `ImportedSleepStore.kt` | 전경/백그라운드 공통 저장 잠금 |
| `AndroidManifest.xml`, `ui/HealthPermissionsRationaleActivity.kt` | 백그라운드 읽기·재부팅 유지 권한, 시스템만 바인딩 가능한 서비스, 사용 안내 |
| `BackgroundSleepSyncRegressionTest.kt` (신규) | 저장→재로드→DATA33/개인화, 권한·취소·실패·빈 조회, 실제 시스템 예약 |
| `BackgroundStoreConcurrencyRegressionTest.kt` (신규) | 동시 저장·로드 회귀 |
| `BackgroundJobLifecycleProbe.kt` (테스트 전용) | 프로세스 종료 후 시스템 작업 실행 확인용 명시적 probe; 일반 테스트 목록에서 제외 |

현재 Health Connect 1.1.0 API와 플랫폼 JobScheduler를 사용하며 새 라이브러리는 추가하지 않았다. 반복 스케줄은 WorkManager를 요구하는 작업 체인이 없어 플랫폼 기능으로 구현했다. 모델 가중치·연구 정책·개인화 수식은 바꾸지 않았다.

## 검증

- `assembleDebug`, `assembleDebugAndroidTest`, `lintDebug` 성공. lint 오류 0, 기존 경고 14, 힌트 7. [빌드 로그](verification/background-sync-build-2026-11-05.txt).
- 전체 instrumentation **109개 통과**: 기존 99개 + 백그라운드 동작 8개 + 저장 동시성 2개. [실행 로그](verification/background-sync-instrumentation-2026-11-05.txt). 최초 테스트의 특징 수 기대값을 DATA33+물리 6개=39개 진단 출력에 맞게 수정한 후 통과했다.
- 화면 없이 기존 누적 경로에 시험 수면 7개 저장 → 파일 재로드 → 새 엔진에서 예측 입력 7개·개인화 입력 7개·`recent_count14=7`을 확인했다. 직접 일지는 생성하지 않았다. 반복 조회 후 중복 없음·30일 이전 기록 보존·최초 관측 시각 유지도 확인했다.
- 별도 시스템 실행 probe 1개 성공. 앱 프로세스를 종료하고 `pidof` 결과가 없는 것을 확인한 다음 `cmd jobscheduler run -f app.sleepwell 53100`으로 예약 작업을 실행했다. Android가 새 서비스 프로세스를 시작했고, 현재 추가 권한이 없는 환경에서 `BLOCKED`로 종료하며 해당 예약을 제거했다. 이는 실행 시점을 앞당긴 에뮬레이터 검증이며 1시간 자연 실행·Doze 실험은 아니다. [probe 로그](verification/background-sync-job-probe-2026-11-05.txt), [프로세스 증거](verification/background-sync-process-probe-2026-11-05.json).
- 화면에서 **앱을 닫아도 자동 누적 허용** 버튼이 표시되고 Health Connect의 추가 권한 화면으로 연결됨을 확인했다. 추가 권한은 에이전트가 허용하지 않았다. 실제 삼성 데이터가 있는 권한 허용 상태의 서비스 종단간 수집은 미검증이다.
- 임시 기본정보와 실행 상태를 제거하고 기존 앱 파일 4개를 바이트 단위로 복원했다. 최신 APK로 재실행해 최초 기본정보 화면을 확인했다. [최종 확인 기록](verification/background-sync-final-2026-11-05.json).
- APK: `app/build/outputs/apk/debug/app-debug.apk`; SHA-256 `e0b1b83730e367806a5889dfdace4866c2735fc28aeb72fd87eee0660e5849f1`.

## 범위와 제한

- 1시간은 예약 간격이며 정시 실행이나 최대 지연 시간을 보장하지 않는다. 배터리 절약·Doze·앱 제한·제조사 정책으로 늦어질 수 있다.
- Android 설정에서 **강제 종료**하면 앱을 다시 열 때까지 동기화할 수 없다. 일반적인 화면 종료/최근 앱 닫기와 구분한다.
- 기능 미지원·권한 거부에서는 백그라운드 수집을 우회하지 않는다. 지원되는 Health Connect와 사용자의 추가 권한 허용이 필요하다.
- 삼성 헬스가 아직 공유하지 않은 기록은 가져올 수 없다. 제공자 삭제를 로컬에 전파하는 기능과 최초 연결 전 30일보다 오래된 기록의 추가 권한은 이번 범위에 없다.
- 실제 삼성 헬스·실물 워치로 수집한 데이터 및 재부팅/장시간 절전 상황은 별도 실기기 검증이 필요하다. 기능 연결 검증은 예측 정확도나 건강 효과 검증이 아니다.

## 공식 API 근거

- [HealthPermission](https://developer.android.com/reference/androidx/health/connect/client/permission/HealthPermission): 백그라운드 읽기 권한과 포그라운드에서의 요청.
- [HealthConnectFeatures](https://developer.android.com/reference/androidx/health/connect/client/HealthConnectFeatures): 백그라운드 읽기 기능 가용성 확인.
- [Health Connect 시작하기](https://developer.android.com/health-and-fitness/health-connect/get-started): 기능/권한 확인 후 반복 읽기 예약.
- [JobInfo.Builder](https://developer.android.com/reference/android/app/job/JobInfo.Builder): 반복 작업·재부팅 유지·backoff.
- [JobScheduler](https://developer.android.com/reference/android/app/job/JobScheduler): 시스템 제약과 강제 종료에 따른 실행 지연.

변경 전 백업: `E:\학술제\기타\정리기록\background-sync-20261105`.
