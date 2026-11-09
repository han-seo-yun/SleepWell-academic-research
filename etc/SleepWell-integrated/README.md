# SleepWell 통합 저장소

SleepWell의 로컬 웹앱, 연구 분석실, Android 사용자 앱, Windows/macOS 연구자용 패키징, iOS 프로토타입, 연구 모델 코드를 함께 관리합니다. 작업 루트는 `E:\학술제`, 이 저장소의 현재 위치는 `E:\학술제\기타\SleepWell-integrated`입니다.

최종 확인일: 2026-11-01

## 현재 구성

| 경로 | 역할 | 현재 기준 |
|---|---|---|
| `web-app` | 1인 로컬 웹앱, 사전 예측, 연구 분석실, 로컬 API | Python, 기본 포트 8765 |
| `android-app` | 사용자 기록·개인화·Health Connect/PC 동기화 | 0.4.5, versionCode 7, minSdk 29 |
| `desktop` | Windows 실행기·설치기와 macOS 패키징 | Windows 배포 0.4.7, macOS 배포 0.6.6 |
| `ios-app` | SwiftUI/HealthKit 프로토타입과 공통 코어 | iOS 16+, macOS 13+ |
| `tools/model` | MESA·CMI·개인 기록 연구 학습·검증 | 연구 전용 |
| `docs` | 모델 계약, 연구 프로토콜, 날짜별 검증 보고서 | 날짜가 붙은 문서는 시점별 기록 |
| `src`, `src-epoch-30s`, `src-epoch-1min` | Kotlin 분석 코드와 epoch 변형 | 변형별 계약을 유지 |

플랫폼별 버전 번호는 하나의 통합 릴리스 번호가 아닙니다. 배포 파일은 저장소가 아니라 `E:\학술제\어플리케이션(사용자용)\배포`와 `E:\학술제\소프트웨어(연구자용)\배포`에 보관합니다.

## 빠른 실행

### 로컬 웹앱

```powershell
cd E:\학술제\기타\SleepWell-integrated\web-app
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

브라우저에서 <http://127.0.0.1:8765>를 엽니다. 기본 DB는 `web-app/.local/sleepwell.sqlite3`이며 서버는 loopback 주소에만 바인딩됩니다. 다른 DB·포트·작업 루트를 쓰려면 `python server.py --help`에서 현재 옵션을 확인하세요.

### 무상태 연구 분석실

```powershell
cd E:\학술제\기타\SleepWell-integrated\web-app
.\.venv\Scripts\python.exe -m pip install -r requirements-research-lab.txt
.\.venv\Scripts\python.exe research_lab_server.py
.\.venv\Scripts\python.exe research_lab_cli.py --help
```

연구 분석실은 기존 사용자 DB와 분리된 평가 도구입니다. 합성 시연 결과는 실제 정확도나 앱 효과의 근거가 아닙니다.

### Android

Android Studio에서 `android-app`을 열거나 JDK 17 이상과 Android SDK를 지정한 뒤 실행합니다.

```powershell
cd E:\학술제\기타\SleepWell-integrated\android-app
.\gradlew.bat :app:assembleDebug :app:lintDebug
```

자세한 기능·권한·동기화 범위는 [Android 안내](android-app/README_소빈.md)를 참고하세요.

### 데스크톱과 iOS

- Windows 사용자 안내: [desktop/USER_GUIDE.md](desktop/USER_GUIDE.md)
- macOS 사용자 안내: [desktop/macos/USER_GUIDE_macOS.md](desktop/macos/USER_GUIDE_macOS.md)
- 패키징과 버전 일치 조건: [desktop/README.md](desktop/README.md)
- iOS 빌드·검증: [ios-app/README.md](ios-app/README.md)

## 검증

2026-11-01 현재 소스에서 확인한 최소 검증은 다음과 같습니다.

```powershell
cd web-app
python -B -X utf8 -m unittest discover -s tests -q
```

- 웹앱: 345개 통과, 1개 건너뜀.
- Android: `assembleDebug`는 성공했습니다. `lintDebug`는 U Extensions 7 API 조건 미선언 등 5개 오류로 실패했으며 세부 내용은 구성요소 README에 기록했습니다.
- 데스크톱 `test_build_package.py`와 `test_desktop_host.py`: `prepare`로 만든 `package/SleepWell`과 내장 런타임이 있어야 실행됩니다. 소스 트리에서 바로 실행하는 테스트는 `test_mobile_sync.py` 10개가 통과했습니다.
- 연구 모델 전체 테스트: 기본 Python 환경에는 `pyarrow`가 없어 98개 중 4개가 의존성 오류였습니다. 격리 환경에서 필요한 의존성을 설치한 뒤 다시 검증해야 합니다.

고정된 테스트 개수는 새 테스트 추가로 바뀔 수 있으므로 README의 수치보다 새 실행 결과를 우선합니다.

## 문서 기준

- 전체 문서 색인: [docs/README.md](docs/README.md).
- 현재 모델·개인화·전향 평가 계약: [docs/CURRENT_MODEL_STATUS_2026-11-01.md](docs/CURRENT_MODEL_STATUS_2026-11-01.md).
- 현재 실행법과 배포 버전: 이 README와 각 구성요소 README.
- 현재 모델 입력·출력: [docs/interface-schema-모델-입출력.md](docs/interface-schema-모델-입출력.md).
- 최신 개인화·타당성 기록: [docs/RESEARCH_UPDATE_2026-10-25.md](docs/RESEARCH_UPDATE_2026-10-25.md), [docs/FULL_TARGET_VALIDITY_2026-10-25.md](docs/FULL_TARGET_VALIDITY_2026-10-25.md), [docs/DAILY_PERSONALIZATION_2026-10-25.md](docs/DAILY_PERSONALIZATION_2026-10-25.md).
- PSG 검증: [docs/RESEARCH_PSG_RESULTS_2026-10-20.md](docs/RESEARCH_PSG_RESULTS_2026-10-20.md).
- `docs`의 날짜가 붙은 계획·보고서는 당시의 데이터와 판단을 보존한 기록입니다. 현재 상태를 자동 반영하지 않습니다.

## 데이터와 연구 경계

- 새 실험 결과는 `E:\학술제\데이터모델\연구용데이터` 아래 새 버전 폴더에 저장합니다. 기존 결과를 덮어쓰지 않습니다.
- MESA ACT 파생 라벨 재현, PSG 수면/각성 분류, 다음날 행동 시각 예측, 권장 정책의 효과는 서로 다른 주장입니다.
- 연구 모델을 자동으로 서비스 모델이나 추천 정책으로 승격하지 않습니다.
- 개인 원자료·개인 모델·DB·연결 비밀은 Git 또는 배포 패키지에 포함하지 않습니다.
- Google Drive 업로드나 팀원 전송은 별도 명시적 요청이 있을 때만 수행합니다.

## 현재 알려진 버전 주의사항

`desktop/build_package.py`의 공유 패키지 버전은 0.6.6이지만 Windows C# 실행기·설치기 소스는 0.4.7입니다. 따라서 현재 저장소 상태를 그대로 사용해 새 Windows 배포를 만들기 전에 `desktop/README.md`의 버전 일치 검사를 수행해야 합니다. 검증된 기존 Windows 0.4.7 배포 파일은 변경하지 않습니다.
