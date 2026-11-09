# 삼성 헬스 수면 자동 누적과 개인화 연결

> 후속 요청으로 앱 종료 중 백그라운드 동기화도 추가했다. 이 문서의 백그라운드 작업/권한 미구현 제한은 [후속 구현 기록](BACKGROUND_SLEEP_SYNC_2026-11-05.md)으로 대체한다. 누적·DATA33·설문 정책은 유지한다.

직접 일지를 다시 작성하지 않아도 삼성 헬스에서 Health Connect로 전달된 수면과 기존 삼성 헬스 CSV를 기기에 누적하고, DATA33과 점진 개인화 예측에 사용하도록 통합 v6를 수정했다. 기존 직접 일지·설문·프로필·분석·저장 화면을 그대로 연결한다.

## 입력부터 예측까지

1. 기존 수면 읽기 권한이 있으면 분석 화면 진입·복귀 때 Health Connect를 조회한다. 화면이 활성화되어 있는 동안 15분마다 다시 조회한다. CSV 가져오기도 같은 누적 경로를 사용한다.
2. 원본 CSV를 보존하고 `filesDir/sleep_data/sleep_imported_model.json`에 모델용 수면 기록을 병합한다. 재실행 후 다시 읽으며 최근 30일 조회가 기존의 더 오래된 기록을 삭제하지 않는다.
3. 출처·제공자 ID·실제 가져온 시각·제공자 수정 시각을 보존한다. 같은 수면을 다시 읽어도 중복되지 않으며, 같은 ID의 짧아진 수정본도 반영한다. 모델에는 기상 날짜별 3시간 이상인 주 수면 하나를 사용하고 같은 밤의 직접 일지를 우선한다.
4. 앱 실사용 경로는 `diary_and_wearable_v1` 정책을 명시적으로 선택한다. 취침·기상 시각은 설문이 없어도 DATA33, 행동 위상, 점진 예측 개인화에 반영한다. DATA33은 원 기록에서 매번 다시 계산하는 33개 특징이며, 새 가중치 재훈련이나 매일의 특징 벡터 보관 기능은 아니다.
5. 개운함·피로도·운동과 빠진 입면 지연/각성 시간은 기존 입력 폼에서 **보완할 수면 기록**을 선택해 저장한다. 수면의 시각을 다시 입력하거나 가상의 직접 일지를 만들지 않는다. 응답은 특정 기상 날짜·시각과 연결된다.

## 측정 누락과 시간 처리

- 수면 단계가 없거나 부분적으로만 있으면 없는 구간을 얕은 수면 또는 각성 0분으로 채우지 않는다. 전체 구간이 유효한 단계로 덮여 있을 때만 각성 합계를 사용한다. 전체가 수면 단계여도 잠들기까지 걸린 시간이 0분이라고 추정하지 않는다.
- 센서 측정값이 있으면 우선하고 비어 있는 값만 설문으로 보완한다. 수면 단계 그래프를 만들 수 없어도 유효한 수면 시각이 있으면 예측·권장 카드를 제공한다.
- 개운함·피로도·운동은 실제로 저장된 설문만 사용한다. 목표 수면시간/관측 부족분 보정에는 측정 완전성·주관 응답 등 기존 근거 조건이 추가로 필요하다. 워치 기록 개수만으로 목표 수면시간을 바꾸지 않는다.
- 설문 시각은 해당 기상 이후이면서 계산 기준 이전이어야 한다. 나중에 작성한 설문이나 나중에 가져온 기록을 과거 예측에 소급 사용하지 않는다. 20시 이후의 실제 사용에서는 현재까지 가져온 과거 수면을 사용할 수 있으며, 과거 20시 기준 평가와 구분한다.
- 알람·카페인·낮잠 같은 그날의 계획/생활 맥락과 특정 수면의 사후 설문을 분리 저장한다. 수면 설문만 저장해 기존 목표 기상을 지우지 않는다. 자동 갱신 중 작성하던 폼과 선택한 날짜도 유지한다.
- 화면 시각은 기기 시간대이며 연구/모델의 기상 날짜 기준은 기존 Asia/Seoul 계약을 유지한다. 해외 시간대에서도 선택한 수면의 실제 기상 시각을 표시한다.

## 연구 정책과 구분

연구 기본값 `DIARY_ONLY`, 기존 모델 자산·특징 순서·골든 출력은 유지한다. 서비스 모델 표기는 `robust-next-night-research-v2+gradual_v1+wearable_v1`, 검증 범위는 `not_validated_for_gradual_v1_wearable_runtime`이다. 따라서 기존 연구 성능이나 임상 효과를 이번 워치 서비스 경로에서 검증했다고 주장하지 않는다. 기기 내 동작·입력 연결·회귀 검증과 모델의 예측 성능 검증은 별개다.

## 주요 변경 파일

| 영역 | 파일 |
| --- | --- |
| 원본 수집·누적 | `analysis/HealthConnectSleep.kt`, `HealthConnectCsv.kt`, `SleepDataStore.kt`, 신규 `ImportedSleepStore.kt` |
| 모델 연결·정책 | `analysis/NextNightModel.kt`, `user/UserModels.kt`, `UserModelEngine.kt`, `PortableFusionModel.kt` |
| 설문 연결·분리 저장 | `analysis/SurveyCsv.kt`, `SurveyStore.kt`, `SurveyObservationPolicy.kt`, `DirectInputForm.kt` |
| 화면·자동 갱신 | `ui/AnalysisScreen.kt`, `SavedResults.kt`, `analysis/AnalysisDisplay.kt` |
| 신규 회귀 검증 | `ImportedSleepStoreRegressionTest.kt` 11개, `WearableInputPolicyRegressionTest.kt` 7개, `WearableSurveyRegressionTest.kt` 6개 |

## 검증

- 최종 `assembleDebug`, `assembleDebugAndroidTest`, `lintDebug` 성공. lint 오류 0, 경고 14, 힌트 7. [빌드 로그](verification/wearable-personalization-build-2026-11-05.txt).
- 최종 APK에서 전체 instrumentation **99개 통과**: [실행 로그](verification/wearable-personalization-instrumentation-2026-11-05.txt). 신규 회귀 24개와 기존 75개를 포함한다.
- 에뮬레이터 화면에서 단계가 없는 시험용 워치 수면 8개와 직접 일지 0개로 예상 23:00→06:59, 권장 23:30→07:30을 생성했다. 선택한 수면에 개운함 4/5·피로도 2/7을 저장하고, 앱 재실행 후 누적 8개와 연결된 설문 1개가 유지됨을 확인했다. 이 수치는 합성 시험 결과다.
- 시험 후 기존 앱 파일 4개를 바이트 단위로 복원하고 시험 생성 파일 3개를 제거했다. 최신 앱을 다시 실행해 원래의 최초 기본정보 입력 화면을 확인했다. [완료 확인 기록](verification/wearable-personalization-final-2026-11-05.json).
- 최종 APK: `app/build/outputs/apk/debug/app-debug.apk`; SHA-256 `15cb31438b915391117d1e6ceb5402cc64ec77848ca4b873b829a48953737eed`.

## 현재 범위와 제한

- 삼성 헬스가 Health Connect에 수면을 공유하고 앱의 수면 읽기 권한이 허용되어 있어야 한다. 앱이 직접 워치와 통신하거나 수면을 감지하지는 않는다.
- 앱이 닫힌 동안 실행하는 주기 작업은 없다. 다음 분석 화면 진입 때 새 기록을 읽는다. 새 백그라운드/과거 전체 조회 권한이나 라이브러리는 추가하지 않았다.
- 기본 조회는 최근 30일이다. 처음 연결하기 전의 더 오래된 기록은 기존 CSV 가져오기를 사용할 수 있다. 이미 가져온 기록은 로컬에 남는다.
- 제공자에서 삭제한 수면을 로컬에서 자동 삭제하는 동기화는 구현하지 않았다. 기존 보관 기록 지우기를 사용한다. 하루 여러 수면 중 주 수면만 모델에 넣으며 낮잠은 별도 입력이다.
- 실제 삼성 계정·실물 워치에서 수집한 데이터로 끝까지 연결한 검증은 수행하지 않았다. 에뮬레이터, 합성 CSV/저장 자료, 자동화 회귀 테스트 범위다.

## 공식 API 근거

- [SleepSessionRecord](https://developer.android.com/reference/kotlin/androidx/health/connect/client/records/SleepSessionRecord): 단계가 없는 세션과 단계가 연속하지 않는 세션의 처리.
- [Metadata](https://developer.android.com/reference/kotlin/androidx/health/connect/client/records/metadata/Metadata): 출처와 마지막 수정 시각.
- [Health Connect 데이터 읽기](https://developer.android.com/health-and-fitness/health-connect/read-data): 기본 과거 조회 범위와 추가 권한.

변경 전 소스와 에뮬레이터 파일 백업: `E:\학술제\기타\정리기록\wearable-personalization-20261105`.
