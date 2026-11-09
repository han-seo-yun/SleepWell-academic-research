# 통합 v6 최신 모델·개인화 정책 이식

대상: `E:\학술제\앱_소빈\통합 v6` / 2026-11-03

기준: `E:\학술제\기타\SleepWell-integrated\docs\CURRENT_MODEL_STATUS_2026-11-01.md`, 같은 저장소의 `web-app/night_core.py`, `fusion_forecast.py`, `engine.py`.

## 반영 결과

| 경로 | 현재 구현 |
|---|---|
| CMI/MESA 휴대형 예측 | `robust-next-night-research-v2+gradual_v1`. 첫 유효 이력부터 `n/(n+14)` 수축 개인화. 원본 동결 계수 유지. |
| 추천 개인화 | measurement-evidence v5. 14일 반감기, 질량/(질량+14), 유효 질량 5 미만·14일 초과 오래된 이력·위상 일관성 0.5 미만이면 선언한 위상/목표 유지. |
| 목표 수면 | 사용자가 저장한 희망 7~9시간. 충분하고 완전한 good-night 일지에서만 ±30분 범위 보정. NHANES는 집단 참고값으로 유지. |
| 측정 완전성 | 입면 지연과 중간각성 분이 둘 다 명시된 경우에만 체류시간에서 차감해 관측 수면시간 계산. 빈칸을 0으로 간주하지 않음. |
| 추천 core | web v7의 Android 지원 입력 이식: 실제 현재시각/자정 이후 밤, 7일 관측 부족분에 대한 최대 30분 확장, 입면 여유, 고정 Two-Process/위상/규칙성 점수의 5분 후보 검색, 완료 낮잠 반영. |
| 입력 출처 | 직접 저장한 일지는 primary, 실제 워치는 `samsung_health` auxiliary. 합성 `demo:` 기록은 실사용 일지와 분리. |
| 앱 저장/화면 | 희망 수면시간·평소 취침/기상 저장, 직접 일지 영속 저장, 워치 없이도 추천. 근거 없는 센서 그래프와 S/C 숫자는 표시·결과 저장하지 않음. |
| 시간 계약 | live 추천은 실제 now에 알려진 입력을 사용. 연구 예측은 별도 20:00 미만의 이력을 사용. 과거 조회는 실제 savedAt으로 필터하고 명시적 비전향 재구성으로 분류. 지난 알람으로 0분 추천을 만드는 경로를 차단. |

개인화되는 값은 습관 feature, 수면 위상, 목표 수면, 입면 지연 및 관측 활동 통계다. 생리 시간상수 τ_wake=18.2h, τ_sleep=4.2h, 진폭 0.15는 공식 고정값을 유지하며 개인별 생리 학습을 구현했다고 주장하지 않는다.

## 변경 파일

- 추론: `user/PortableFusionModel.kt`, `UserModelEngine.kt`, `UserModels.kt`
- 정책: `user/MeasurementPersonalization.kt`, `MeasurementRecommendationPolicy.kt`
- 연결·저장: `analysis/NextNightModel.kt`, `DirectInputForm.kt`, `DiaryStore.kt`, `ProfileStore.kt`, `AnalysisRunner.kt`, `SurveyObservationPolicy.kt`
- 화면·결과: `ui/AnalysisScreen.kt`, `analysis/AnalysisDisplay.kt`, `health/AnalysisResult.kt`
- 검증: `app/src/androidTest/`, `tools/generate_portable_fusion_gradual_fixture.py`

새 외부 의존성은 추가하지 않았다. 측정 근거가 없는 기존 워치→일지 재분류와 NHANES 목표 덮어쓰기를 제거하고, 새벽 밤 경계는 하나의 정책 함수로 공유했다.

## 아티팩트 무결성

| 파일 | SHA-256 (변경 없음) |
|---|---|
| robust_cmi_v2.json | ADD8BA4E23F9C4C9337B821406386664A054580D86C6E53C9B5E6F189BDFA2C7 |
| robust_mesa_v2.json | 757B939D756C9174EAB9304E0D8E872F4ED9EEAE34E99A3D921F29284207C620 |
| population_prior.json | 5CD8CC62078EE0A1B44D029513090E82588B5E71C46E6BD98DAC54220EF7D396 |

`legacy14` 동결 연구 지표와 `gradual_v1` 실행 정책을 메타데이터에서 구분한다. 예전 Test 성능 수치를 새 정책의 성능으로 재표기하지 않는다.

## 검증

- Android APK·테스트 APK 빌드 성공. `lintDebug` 오류 0개; 기존 경고 14개와 힌트 6개.
- Android API 37 에뮬레이터에서 **44개 instrumentation 테스트 통과**, 실패 0개. [실행 로그](verification/model-policy-instrumentation-2026-11-03.txt).
- CMI/MESA는 canonical Python에서 만든 9개 golden 사례(기록 0/1/5/13/14/30개, 결측·불규칙·미래/늦은 입력)의 feature snapshot·개인화·physical/data/hybrid 출력을 비교해 통과했다. 숫자 허용 오차 1e-7.
- 추천 core는 Python v7의 기본/자정 이후, 충분한 완전 일지+개인화 위상+수면부족+낮잠, 오래된 불완전 일지 사례와 비교했다. phase/latency/S 수치는 1e-12 허용 오차이며 Android 시각은 millisecond, 수면 기회는 정수 분 표현이다. 별도 정책 회귀 테스트도 통과했다.
- 저장·마이그레이션, 프로필 희망 목표 변경, 직접 일지 인과적 시각, 워치/데모 출처 분리, 결측 측정, 정확한 20:00 경계, 재시작용 설문 선택과 과거 관측 제외를 검사했다.
- 새로운 런타임의 잔차 보정은 런타임 버전·개인화 정책·연구지표 범위·artifact hash가 모두 일치하는 관측만 받는다. 구형 결과 혼합 방지 테스트 통과.
- 실행 화면 확인: 11/03 현재 기기 시각 연동. 워치 0기록 상태에서도 일지 전용 권장이 표시되며, 기본 목표 8시간에서 **11/03 23:15 → 11/04 07:30, 수면 기회 8시간 15분**을 표시했다. 15분은 입면 여유다.
- Android Studio에 통합 v6 프로젝트를 열고 에뮬레이터에 최종 APK를 설치했다. 실제 갤럭시/헬스 커넥트 데이터로 하는 실기기 시험은 이번 검증 범위에 포함되지 않았다.

재현 명령은 `gradlew.bat :app:assembleDebug :app:assembleDebugAndroidTest :app:lintDebug`와 APK 설치 후 `adb shell am instrument -w -e class <검증 로그의 8개 클래스> app.sleepwell.test/android.test.InstrumentationTestRunner`다. Windows 메모리 제약으로 검증 시 `--max-workers=1`, Gradle `-Xmx1024m`을 사용했다.

## 범위와 남은 한계

- Android 화면이 제공하는 단일 첫 일정·준비시간·낮잠·카페인 입력에 대해 이식했다. 웹의 다중 근무/수업 점유 구간, 진행 중 일정, 별도 주말 기상 설정까지 포함한 전체 웹 추천 동등성은 아니다.
- 만료된 일정은 Android에서 평소 기상으로 되돌린다. 이 동작은 만료 일정에도 infeasible을 반환하는 웹 경로와 의도적으로 다르다.
- PSG 분류, 센서 S, PCR은 공식 서비스 모델로 승격하지 않았다. sensor S는 연구 shadow이며, 앱 공식 추천은 일지와 고정 Two-Process 경로다.
- 연구 아티팩트와 수치 일치·앱 회귀 테스트는 구현 검증이다. 개인에게 최적 수면을 찾았다거나 건강 개선 효과를 입증한 결과가 아니다.
- 손상된 일지는 명시적 오류를 표시하고 원본 파일을 보존한다. 손상 행을 자동으로 버리거나 고쳐 기록하지 않는다.
