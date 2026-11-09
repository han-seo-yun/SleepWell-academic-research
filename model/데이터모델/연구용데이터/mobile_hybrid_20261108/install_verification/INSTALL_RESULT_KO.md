# A52s 오프라인 앱 설치·모델 확인 결과

2026-11-08, 사용자 선택과 설치 요청에 따라 실행했다.

## 설치

- 기기: Galaxy A52s SM-A528N, Android 14.
- 새 앱: **SleepWell 오프라인**, `app.sleepwell.offlinehgb`, 0.8.6/code 14.
- 설치와 실제 MainActivity 실행 성공. 검사 후 다시 실행해 초기 설정 화면을 확인했다.
- 설치 APK SHA-256: `65b68d2df62a99997294326645e1d0feb60986d0f3943d0a1ddba90109bee43b`.
- 기존 앱 `app.sleepwell`은 0.1/code 1, 마지막 업데이트 2026-10-29 12:44:57 그대로다. 서명이 다른 기존 앱을 삭제하거나 덮어쓰지 않았다.

## 기존 기록 보존

기존 앱의 files/shared_prefs 저장 파일 3개를 설치 전에 로컬 private 폴더에 보관했다. 검사 후 같은 파일 목록·길이·SHA-256을 다시 계산했고 모두 일치했다. 기존 앱 기록을 새 앱으로 복사하거나 초기화하지 않았다.

새 앱은 별도 저장 공간으로 시작한다. 사용자 프로필을 임의로 입력하지 않았으며, 28일 검증용 수면 기록은 메모리에서만 생성했다. 최종 새 앱 files 폴더에는 Android의 profileInstalled 파일만 있고, 검증용 일지·분석 결과는 저장되지 않았다. 따라서 실사용을 시작하려면 새 앱에서 초기 설정과 기록 불러오기/연결을 진행해야 한다.

## 설치된 앱의 모델 검사

실제 A52s에서 Wi-Fi와 모바일 데이터를 모두 끄고 Android의 `Active default network: none`을 확인한 상태로 6개 instrumentation 검사를 실행했다. **6/6 통과**했다.

1. CMI·MESA 각 12개 기준 입력의 네 예측 출력이 기준값과 정확히 일치.
2. 39개 특징의 순서·개수 오류 차단.
3. 모델 엔진의 코호트 배선과 local/offline 출처 기록 확인.
4. 모델 asset 읽기 실패 시 생리 모델 추천으로 복귀.
5. 과거 재구성 시 로컬 모델 호출 억제.
6. 실제 `NextNightModel.install(context, settings)` → `applyTo` 경로에 28일 검증 기록을 넣어 HGB·개인화·생리 추천 연결 확인. 옛 서버 설정을 enabled로 주고 유효하지 않은 주소를 넣어도 기기 내 모델로 성공했다.

실제 반환값:

- model_id: `hgb-hybrid39-equal-budget-20261107-trial2`
- status: `hgb_hybrid39_personalized_two_process`
- source: `local_hgb_hybrid39`
- history_count: `28`
- 입면·기상 예측 시각 모두 반환, 엔진 오류 없음.

이 검사는 설치된 앱의 모델 경로 검증이며 사용자 본인의 수면 기록으로 추천 결과를 생성한 검사는 아니다. 모델 통계적 정확도는 상위 `REPORT_KO.md`의 참여자 분리 평가를 따른다.

검사 종료 후 Wi-Fi·모바일 데이터를 원래 켜진 상태로 복원했고, 검사용 보조 APK만 제거했다. 사용자용 오프라인 앱과 기존 앱은 모두 남아 있다.

근거: `a52s_install_verification.json`, `a52s_offline_instrumentation.txt`, `first_launch.png`, `final_launch.png`.

## 첨부 시연 영상의 버전

`C:/Users/pyomo/Downloads/SleepWell_시연영상_Pretendard_무음.mp4`의 시작(00:00)과 ‘내 정보’ 장면(00:40)에는 **0.8.4**가 표시된다. 이번 설치 앱은 **0.8.6 오프라인 HGB**로, 영상과 동일한 버전은 아니다. 버전 표시는 직접 확인했지만 영상만으로 해당 APK binary hash까지 식별한 것은 아니다.

자세한 근거: `../video_comparison/VERSION_COMPARISON_KO.md`, `frame_00s.png`, `frame_40s.png`.
