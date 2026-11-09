# 시연 영상과 설치 앱 버전 확인

확인일: 2026-11-08.

- 원본 영상: `C:/Users/pyomo/Downloads/SleepWell_시연영상_Pretendard_무음.mp4`.
- 00:00 대표 프레임의 왼쪽 제목 아래에 `PERSONALIZED SLEEP PLAN / v0.8.4`가 표시된다.
- 00:40 대표 프레임의 오른쪽 앱 ‘내 정보’ 화면에도 `SleepWell 0.8.4`가 표시된다.
- 따라서 영상에서 표시하는 버전은 **0.8.4**다. 파일 생성 날짜나 일반적인 영상 제작 계획에서 추정한 결론이 아니다.
- 이번 A52s 설치 패키지는 `app.sleepwell.offlinehgb`, 표시 이름 `SleepWell 오프라인`, versionName **0.8.6**, versionCode **14**다. Android package manager로 확인했다.
- 두 버전은 동일하지 않다. 설치본은 별도 패키지로 HGB 모델을 기기 내에서 실행하도록 연결한 버전이다. 영상 자체는 현재 설치본의 HGB 실행 증거가 아니며, 모델 동작은 실기기 검사로 별도 확인한다.
- 설치본 ‘내 정보’의 버전 표시는 문자열을 고정한 것이 아니라 package manager의 versionName을 읽는다(`MyInformationScreen.kt`).

영상은 스마트폰 테두리 안의 화면과 설명 문구를 배치한 발표용 구성이다. 화면의 버전 표시는 확인할 수 있지만, 영상만으로 녹화에 사용된 APK의 서명이나 binary hash까지 확인한 것은 아니다.

근거 이미지: `frame_00s.png`, `frame_40s.png`. 추가 대표 프레임은 5초 간격으로 같은 폴더에 보존했다.
