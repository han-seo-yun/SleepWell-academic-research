# 통합 v6 모델·개인화 반영 감사

> 아래는 수정 전 감사 기록이다. 후속 구현과 재검증 결과는 [모델·정책 이식 기록](MODEL_POLICY_MIGRATION_2026-11-03.md)을 기준으로 한다.

- 감사 대상: `E:\학술제\앱_소빈\통합 v6`
- 기준일: 2026-11-03
- 최신 정책 기준: `E:\학술제\기타\SleepWell-integrated\docs\CURRENT_MODEL_STATUS_2026-11-01.md`
- canonical 저장소: `E:\학술제\기타\SleepWell-integrated` (`E:\학술제\WORKSPACE_CONTEXT.md:5,35-39`)
- 범위: 소스·아티팩트 정적 대조 및 아래 통합 검증. 일반 사용자/실제 워치 대상 성능시험은 포함하지 않는다.

## 결론

**부분 반영**이다. 통합 v6는 동결된 CMI/MESA 휴대형 모델과 NHANES 집단 사전값을 실제로 로드·실행하고, 계산 결과로 기존 예상·권장 값을 교체한다. 추천 엔진의 기상시각 및 목표 수면시간 개인화도 구현되어 있다.

다만 다음 이유로 “현재 연구 전체와 최신 개인화 정책이 완전히 반영됐다”고 말할 수는 없다.

1. Android 휴대형 융합모델 내부 개인화는 여전히 `legacy14`이며, 최신 연구 런타임의 `gradual_v1`과 다르다.
2. 앱 추천 개인화는 별도 경로에서 첫 기록부터 작동하지만, 워치 밤 기록을 승인된 앱 어댑터 선택인 `WATCH_AS_DIARY=true`로 `manual` 레코드로 변환한다. 이는 원래 연구 계약의 “일지 primary / wearable auxiliary”와 데이터 의미가 같다는 뜻은 아니다.
3. 통합 v6는 NHANES 관측 평균을 추천 목표 수면시간의 초기값으로 사용한다. 최신 canonical Android는 사용자의 `desiredSleepMinutes`를 초기 기준으로 사용하도록 바뀌었다.
4. PSG 분류, 센서 Process S, PCR 생리 모델은 사용자 추천 경로에 승격되지 않았다.
5. 아티팩트 성능은 연구 비교 성능이며 앱의 건강 개선 효과나 임상 효과를 입증하지 않는다.

## 1. 실제 실행 연결 경로

| 단계 | 실제 코드 근거 | 판정 |
|---|---|---|
| 앱 시작 시 모델 설치 | `app/src/main/java/app/sleepwell/MainActivity.kt:87-93`에서 `NextNightModel.install`로 assets loader를 설치하고 저장 프로필을 로드 | 연결됨 |
| 기존 분석 실행 | `analysis/AnalysisRunner.kt:102-107`에서 `SleepAnalysisPipeline.analyze` 실행 | 연결됨 |
| 새 엔진으로 결과 교체 | `analysis/AnalysisRunner.kt:108-110`에서 성공 결과에 `NextNightModel.applyTo` 적용 | 연결됨 |
| 사용자 모델 계산 | `analysis/NextNightModel.kt:83-96`에서 밤 기록·프로필·생활 맥락을 만들고 `UserModelEngine.compute` 호출 | 연결됨 |
| 화면용 예상·권장 교체 | `analysis/NextNightModel.kt:96-120`에서 research predictions와 recommendation을 `AnalysisResult`에 다시 기록 | 연결됨 |
| 동결 모델 추론 | `user/PortableFusionModel.kt:141-170`에서 CMI/MESA 각각 physical/data/hybrid 예측 산출 | 연결됨 |

모델 출력의 추천 기여 범위는 제한적이다. `user/UserModelEngine.kt:88-107`에서 CMI와 MESA hybrid 입면 오프셋 평균을 계산하고, 이를 최대 30분의 준비 시작 보정(`predictedDelay`)에 쓴다. 핵심 취침·기상·수면 기회는 같은 파일 `:94-104`의 개인화 기상시각, 일정 제약, 목표 수면시간으로 정한다. 코드도 CMI·MESA 평균이 검증된 새 앙상블이 아니라 행동 지연 안내용 참고값이라고 명시한다(`:135-136`).

## 2. 아티팩트 동일성 및 무결성

아래 SHA-256은 통합 v6, `SleepWell-research\models\current`, canonical integrated web/Android runtime에서 각각 대조했으며 모두 동일했다.

| 아티팩트 | SHA-256 |
|---|---|
| `models/robust_cmi_v2.json` | `ADD8BA4E23F9C4C9337B821406386664A054580D86C6E53C9B5E6F189BDFA2C7` |
| `models/robust_mesa_v2.json` | `757B939D756C9174EAB9304E0D8E872F4ED9EEAE34E99A3D921F29284207C620` |
| `models/population_prior.json` | `5CD8CC62078EE0A1B44D029513090E82588B5E71C46E6BD98DAC54220EF7D396` |

런타임도 해시를 단순 표시만 하지 않고 fail-close 검증한다.

- CMI/MESA: `user/PortableFusionModel.kt:43-45,66-71`
- population prior: `user/UserModelEngine.kt:48-54,299-308`
- 모델 계약 검증: `user/PortableFusionModel.kt:180-195`에서 schema 2, artifact type, feature contract, model version, cohort, 20:00 cutoff, physical provider, `research_comparison_only` 상태를 확인

휴대형 모델 계약은 `robust-next-night-research-v2`, `sleepwell_next_night_robust_v2`, `two_process_fixed_v1`이다(`user/PortableFusionModel.kt:35-41`). 통합 v6의 `PortableFusionModel.kt`는 감사 시점 canonical integrated Android 파일과 SHA-256 `7D180E77E3F1D5BFCD4E4CC40B0FEF8C32DBE64857E5E09F98F8D390CF5236DB`로 동일했다.

## 3. 최신 canonical과의 차이

### 3.1 융합모델 내부 개인화: `legacy14` 유지

통합 v6의 `user/PortableFusionModel.kt:308-340`은 다음 조건을 사용한다.

- `active = history.size >= 14 && elapsed >= 14`
- 활성 전 `personal_weight = 0`
- 활성 후 `history.size / (history.size + 14)`

따라서 14개 기록과 14일 경과 전에는 habit bed/wake가 집단 prior에 머문다. 반면 최신 정책 문서 `CURRENT_MODEL_STATUS_2026-11-01.md:25-26,34,38`은 `robust-next-night-research-v2+gradual_v1`, 즉 첫 유효 이력부터 `n/(n+14)` 수축을 적용한다고 규정한다. canonical 웹 구현은 `web-app/night_core.py:198-204`에서 `gradual_v1`일 때 `n > 0`부터 활성화하고, `web-app/fusion_forecast.py:321-331,390-408`에서 런타임 정책과 동결 `legacy14` 성능 범위를 구분한다.

즉 **동결 계수와 파일은 최신 canonical과 동일하지만 Android 추론 정책은 최신 웹 연구 런타임보다 뒤처져 있다.** 저장된 `legacy14` Test 지표를 `gradual_v1` 성능으로 재표기해서는 안 된다.

### 3.2 추천 엔진 개인화: 첫 기록부터 작동하는 별도 경로

추천 개인화는 융합모델 내부 개인화와 별도다. `user/UserModelEngine.kt:218-252`의 공식은 다음과 같다.

- 최근성 가중치: 기상 후 경과일 기준 14일 반감기
- 유효 질량: `effective = Σ recencyWeight`
- 초기값 혼합: `alpha = effective / (effective + 14)`
- 개인화 활성: 유효 primary 기록이 1개 이상이고 `effective > 0`
- 기상 보정: 프로필 기상시각 대비 관측 원형평균 차이를 ±30분으로 제한한 뒤 `alpha` 적용
- 목표 수면시간 보정: `rested >= 4 && fatigue <= 3`인 완전한 good-night 기록이 5개 이상일 때만 `goodMass/(goodMass+14)` 적용, 초기값 대비 ±30분 제한

결과 메타데이터도 `user/UserModelEngine.kt:411-430`에서 `daily_recency_shrinkage`, minimum history 1, 반감기 14일, shrinkage lambda 14로 기록한다.

따라서 “개인화가 14일까지 전혀 없다”는 설명은 추천 엔진에는 틀리고, 휴대형 융합모델 feature 개인화에는 맞다. 두 경로를 구별해야 한다.

### 3.3 `WATCH_AS_DIARY`: 승인된 앱 어댑터 선택이지만 연구 데이터 역할과는 구별

`analysis/NextNightModel.kt:34-46`은 앱에 별도 일지 입력란이 없기 때문에 `WATCH_AS_DIARY=true`를 선택했다고 명시한다. 실제 변환은 `:136-155`에서 2시간 이내 세션 병합, 3시간 이상 주 수면 선택, 기상일별 최장 세션 선택 후 워치 세션의 source를 `manual`로 지정한다.

이 선택 때문에 `user/UserModelEngine.kt:77-82`의 “manual만 primary” 필터를 워치 기록도 통과한다. 결과적으로 앱에서 “첫 유효 primary diary record”는 실제로 첫 워치 주 수면일 수 있다. 이는 기존에 승인된 통합 어댑터 선택이며 구현 누락은 아니지만, 연구 문서의 wearable auxiliary와 동일한 입력 의미라고 발표해서는 안 된다. 직접 입력 수면은 같은 기상일의 워치 기록을 대체하고 명시적으로 `manual`이 된다(`analysis/NextNightModel.kt:152-155`).

### 3.4 NHANES 초기값과 사용자 희망 수면시간

통합 v6는 `user/UserModelEngine.kt:222-241`에서 `population_prior.json`의 NHANES 평균을 성인 420~540분으로 제한해 추천 목표 수면시간 초기값으로 쓴다. 나이대·성별·활동량별 그룹 선택과 표본 30명 미만 fallback은 `:299-330`에 있다.

`analysis/ProfileStore.kt:9-13,30-49`는 나이대·성별·활동량만 저장하며 희망 수면시간은 받지 않고 기본값을 유지한다. 반면 최신 canonical Android `E:\학술제\기타\SleepWell-integrated\android-app\app\src\main\java\app\sleepwell\user\UserModelEngine.kt:234-244`는 NHANES를 기술적 prior로만 보존하고 `profile.desiredSleepMinutes`를 추천 baseline으로 사용한다.

연구 문서 `CURRENT_MODEL_STATUS_2026-11-01.md:45-47`에 따라 NHANES 값은 미국 성인의 자기보고 관측 평균이지 개인의 최적 필요수면이나 다음날 정답이 아니다. 통합 v6의 현행 동작은 구현상 의도된 과거 정책이지만 최신 canonical Android 정책과는 다르다.

## 4. 생리 모델과 개인화 상수

`user/PortableFusionModel.kt:35-45,338-345`의 생리 상수는 다음과 같다.

- `tau_wake = 18.2h`
- `tau_sleep = 4.2h`
- 일주기 진폭 `0.15`
- provider: `two_process_fixed_v1`

이 값은 개인 기록에서 추정한 생리값이 아니다. `user/UserModelEngine.kt:389-395`도 `constants_personally_estimated=false`, `PCR planned; not implemented`로 기록한다. 개인 기록은 위상·행동 습관 및 추천 통계에 반영되지만 생리 시간상수 자체를 개인별로 학습하지 않는다.

전향 오차 보정은 `user/AdaptiveResidualPersonalization.kt:10-15,81-106`과 `user/UserModelEngine.kt:450-490`에 구현되어 있으나, 동결 예측을 대체하지 않는 연구 sidecar다. 앱의 추천 취침·기상값에 직접 적용되지 않는다.

구형 Ridge 경로도 최종 모델로 간주하면 안 된다.

- `app/src/main/assets/ridge_residual.example.json:2-20`: `untrained-example`, status `untrained`, 계수 0
- `analysis/AnalysisRunner.kt:39-45,93-106`: residual model 기본값 `null`
- 이후 `analysis/AnalysisRunner.kt:108-110`과 `analysis/NextNightModel.kt:83-120`에서 새 모델 결과로 교체

## 5. 반영되지 않은 연구 분기

| 연구 분기 | 통합 v6 상태 | 근거 및 한계 |
|---|---|---|
| MESA PSG 분류/재현 | 사용자 추천 모델로 미승격 | `CURRENT_MODEL_STATUS_2026-11-01.md:51-59,99-103`. 앱의 워치 단계는 PSG 정답이 아니며 export 주석도 이를 구분한다(`export/Exporters.kt:18-19,126-167`). |
| 센서 Process S | 추천 경로 미반영, research shadow | 최신 연구는 일지 S, 이진 센서 S, 보정확률 센서 S를 병렬 비교하지만 공식 추천은 계속 `S_diary`다(`CURRENT_MODEL_STATUS_2026-11-01.md:51-59`). Android 모델은 고정 Two-Process 재구성을 쓴다. |
| PCR 생리 provider | 미구현 | `user/UserModelEngine.kt:391`의 `PCR planned; not implemented` |
| 광노출·각성 기반 개인 생리 추정 | 미구현 | 통합 v6 모델 feature 목록(`user/PortableFusionModel.kt:47-63`)과 추천 메타데이터에 해당 provider가 없으며 고정 상수만 사용 |
| 온라인 잔차 보정 | 연구 sidecar만 | 계산·저장은 하지만 원본 예측과 추천을 대체하지 않음(`user/UserModelEngine.kt:454-490`) |

## 6. 성능 주장 범위

아티팩트의 `research_metrics`는 휴대형 포트 parity와 동결 연구 비교 결과다. 두 파일 모두 `status=research_comparison_only`이며, Test 설명도 재사용된 Validation/Test의 adaptive analysis로 새 외부 확인이 아니라고 명시한다.

따라서 다음을 구분해야 한다.

- 말할 수 있음: 동결 CMI/MESA 모델 파일이 정확한 해시로 앱에 포함되어 있고 Kotlin 추론 경로가 실제 호출된다.
- 말할 수 있음: population prior와 최근 기록 기반 추천 개인화가 코드에 구현되어 있다.
- 제한적으로만 말할 수 있음: CMI/MESA 연구 데이터에서 보고된 다음날 수면 시각 오차.
- 말할 수 없음: 통합 v6의 CMI/MESA 평균이 검증된 새 앙상블이라는 주장.
- 말할 수 없음: `gradual_v1`의 성능이 기존 `legacy14` Test 수치로 검증됐다는 주장.
- 말할 수 없음: 앱 추천이 수면의 질, 피로, 건강 결과를 개선한다는 임상·전향 효과 주장.
- 말할 수 없음: PSG 분류 성능이 곧 사용자 추천 성능이라는 주장.

`CURRENT_MODEL_STATUS_2026-11-01.md:6,34,103,107`도 “구현됨”은 정확도 향상이나 수면 개선 효과 입증을 뜻하지 않으며, Android에 모델이 포함되어도 연구 출력과 추천 정책은 분리해야 한다고 명시한다.

## 7. 검증 상태와 남은 증거

### 이번 수정

- `ui/AnalysisScreen.kt`: 기기 시계를 주기적으로 읽고 오늘 날짜까지 선택 가능하게 변경. 오늘 권장은 현재 시각으로 갱신하고 지난 날짜는 과거 조회로 유지한다.
- `analysis/DemoDates.kt`: 시연용 합성 수면·설문만 실행일에 맞춰 같은 일수만큼 이동한다. 실제 가져온 기록의 날짜는 보존한다. 새벽에는 아직 끝나지 않은 합성 수면을 만들지 않는다.
- `analysis/DirectInputForm.kt`, `NextNightModel.kt`, `AnalysisRunner.kt`: 현재 알람을 과거 마지막 기록 날짜로 해석하지 않게 수정. 현재 권장 계산 시각을 전달하고, 36시간 넘게 기록이 없으면 그래프를 장기 연속 각성으로 연장하지 않는다.
- `user/DailyContext.kt`, `UserModelEngine.kt`: 오래됐거나 이미 준비 마감이 지난 일정은 권장 기상 제약에서 제외한다. 과거 일정 때문에 취침과 기상이 모두 현재로 잘려 0분이 되던 경로를 차단했다.
- `health/AnalysisResult.kt`, `analysis/AnalysisDisplay.kt`: 생성 시각·실현 가능 여부·설명을 연결하고 날짜가 있는 권장 구간을 표시한다. 생리 상수·모델 파일·개인화 연구 정책은 이번 오류 수정에서 변경하지 않았다.
- `app/build.gradle.kts`, `src/androidTest/AndroidManifest.xml`, 두 회귀 테스트 파일: 외부 의존성 없이 내장 Android 계측 검증을 구성했다.

### 실행 증거

2026-11-03 통합 검증 결과:

- `:app:assembleDebug`, `:app:assembleDebugAndroidTest` 성공.
- `:app:lintDebug` 성공. 기존 CSV BOM 리터럴을 동일한 `\uFEFF` 이스케이프로 바꿔 오류를 해결했다. 기존 locale/의존성/manifest 등의 경고 14개는 남아 있다.
- API 37 에뮬레이터에서 명시적 테스트 클래스 실행: **OK (8 tests)**. 오래된 일정의 0분 경로, 준비시간이 자정을 넘는 일정, 지난 준비 마감, 현재 시각 기준 알람, 자정 날짜 전환, 합성 날짜 이동을 검증했다.
- 세 가지 실제 데모 asset(`regular`, `irregular`, `new`) 모두 `AnalysisRunner.runCurrent` → `NextNightModel` → CMI/MESA 추론까지 실행했다. 현재 날짜의 최신 기록, `fusion_cmi_mesa`, 실제 계산 시각, 양수 수면 기회, 취침<기상, 수면시간과 구간 길이 일치를 확인했다.
- 엔진 초기화 시 동결 JSON 해시와 내장 reference self-test도 실행된다. 최신 canonical Python `gradual_v1`과의 동등성을 증명하는 테스트는 아니다.
- 20:57 KST 실제 화면 확인: 날짜 선택 **11/3**, 마지막 수면 기록 **11/3 07:08**, 권장 **11/3 23:11 → 11/4 07:00**, **7시간 49분**. Android Studio에 프로젝트를 열고 API 37 에뮬레이터에 수정 앱을 실행해 두었다.

빌드 중 호스트 메모리 부족으로 Gradle/Studio가 종료되어 Gradle worker 수를 1로 제한한 후 성공했다. Studio는 기존 캐시의 남은 소켓 문제를 피해 이번 실행에 한해 `%TEMP%/sleepwell-studio-recovery-20261103/system` 캐시로 복구했다. 프로젝트와 기존 IDE 설정은 보존했다.

검증 실행 명령:

```text
gradlew.bat :app:assembleDebug :app:assembleDebugAndroidTest :app:lintDebug --max-workers=1
adb -s emulator-5554 shell am instrument -w -e class app.sleepwell.user.RecommendationRegressionTest,app.sleepwell.analysis.LiveDateRegressionTest app.sleepwell.test/android.test.InstrumentationTestRunner
```

초기 Gradle connected runner는 테스트 런타임 선언 누락으로 0개를 보고하여 통과 증거에서 제외했다. `src/androidTest/AndroidManifest.xml`에 Android 내장 test runner/base를 선언한 뒤 APK를 재설치하고 위 명령으로 실제 8개 실행 결과를 확인했다. 외부 테스트 의존성은 추가하지 않았다.

남은 검증: 최신 Python 런타임과 Android의 전체 수치 parity, 개인화의 모든 경계(첫 기록/5개 good-night/14일), 실제 워치 수집과 장기 사용, 실제 사용자 대상 추천 효과. 이는 날짜·0분 오류 회귀 통과와 별개다.

최종 발표 문구는 **“동결 연구모델과 추천 개인화 로직이 통합 v6의 실제 실행 경로에 연결되어 있다”**가 적절하다. **“최신 연구의 모든 모델과 개인화 정책이 완전히 반영됐고 효과가 입증됐다”**는 표현은 현재 증거 범위를 넘는다.
