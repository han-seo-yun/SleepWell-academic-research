# SleepWell 연구자용 데스크톱 배포 빌드

Windows용 실행기·설치기, macOS 패키징, 독립 Python 실행 환경을 묶는 소스입니다. 기존 연구 웹앱이나 개인 DB를 변경하지 않고 별도의 배포 앱을 만듭니다.

## 현재 배포 상태 (2026-11-01)

| 플랫폼 | 검증된 최신 배포 | 위치 |
|---|---|---|
| Windows x64 | 0.4.7 | `E:\학술제\소프트웨어(연구자용)\배포` |
| macOS arm64/x86_64 | 0.6.6 | `E:\학술제\소프트웨어(연구자용)\배포\macOS_연구자용_0.6.6` |

플랫폼별 버전은 독립적으로 관리합니다. 현재 `build_package.py`의 공유 패키지 버전은 0.6.6이지만 `SleepWell.cs`, `SleepWellSetup.cs`, `build_launcher.ps1`의 Windows 버전은 0.4.7입니다. 이 상태는 기존 Windows 0.4.7 배포와 macOS 0.6.6 소스를 함께 보존하기 위한 것으로, 현재 HEAD를 새 Windows 릴리스로 바로 패키징할 수 있다는 뜻이 아닙니다.

## 구성

- `SleepWell.cs`: 제어창, 알림 영역 실행, 서버 준비 검증, 브라우저 앱 창, 자식 서버 종료
- `desktop_host.py`: 임시 loopback 포트, 사용자별 DB, 동시 실행 잠금, 부모 종료 감지
- `SleepWellSetup.cs`: 사용자별 설치, ZIP 경로·파일 해시 검사, 선택적 바로가기
- `build_package.py`: 공개 파일만 준비하고 경로·내용을 고정한 뒤 공유 payload 생성
- `macos/build_macos.py`: 공유 payload 또는 저장소 allowlist에서 macOS 앱·DMG·ZIP 생성
- `test_desktop_host.py`, `test_build_package.py`, `test_launcher_shutdown.ps1`: 실행·개인정보 경계·무응답 서버 회귀 검사
- `USER_GUIDE.md`: 사용자 설치·실행·기록 관리 안내

기본 기록 위치는 `%LOCALAPPDATA%\SleepWell`입니다. 연구 웹앱의 `.local` 폴더, 개인 모델, 원자료는 배포하지 않습니다. 집단 평균은 정해진 스키마의 NHANES 익명 집계만 허용합니다. 배포 호스트는 원자료 탐색용 `ResearchBridge`를 생성하지 않습니다.

## 재현 빌드

새 작업 폴더를 만들고 이 소스 디렉터리를 그 아래 `desktop`으로 복사합니다. 빌드 작업 폴더의 구조는 `desktop/`, `downloads/`, `package/`, `build/`입니다. 기존 배포 폴더를 입력 자료나 백업 저장 위치로 재사용하지 마세요.

필요한 빌드 환경은 Python 3.12 이상, 고정 wheel 목록의 `tzdata` 2026.5, Windows에 포함된 .NET Framework C# 컴파일러입니다. 최종 사용자 PC에는 Python 설치가 필요하지 않습니다.

공식 내장형 Python을 `downloads/python-3.14.7-embed-amd64.zip`에, 대응 SBOM을 `downloads/python-runtime.spdx.json`에 저장합니다.

- [공식 Python 3.14.7 배포 페이지](https://www.python.org/downloads/release/python-3147/)
- [Windows x64 내장형 ZIP](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip)
- [구성요소 SBOM](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip.spdx.json)
- 고정 SHA-256: `d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15`

새 작업 폴더에서 아래 순서로 검증합니다. `PROJECT_PATH`는 원본 `E:\학술제\기타\SleepWell-integrated` 경로입니다. Windows 릴리스 빌드 전에는 `build_package.py`, `SleepWell.cs`, `SleepWellSetup.cs`, `build_launcher.ps1`, `test_build_package.py`, `test_desktop_host.py`의 버전이 하나의 목표 버전으로 일치해야 합니다. 현재 소스는 이 조건을 만족하지 않으므로 버전 정합성 수정과 회귀 검증 없이 새 Windows 설치기를 배포하지 마세요.

```powershell
$targetVersion = 'X.Y.Z' # 위 파일들의 버전을 먼저 이 값으로 일치시킵니다.
& desktop/build_launcher.ps1
python -B -X utf8 desktop/build_package.py prepare --repository "PROJECT_PATH"
python -B -X utf8 -m unittest discover -s desktop -p "test_*.py" -v
python -B -X utf8 desktop/build_package.py seal
& desktop/build_setup.ps1 -PayloadZip SleepWellPayload.zip -OutputPath "build/SleepWell-Setup-$targetVersion-x64.exe"
```

`prepare`는 기존 패키지 폴더가 있으면 중단합니다. 준비된 파일 경로와 해시는 배포 폴더 밖 `prepared-manifest.json`에 고정됩니다. `seal`은 임의의 추가 파일이나 수정된 준비 파일이 발견되면 중단합니다. 압축에는 고정 목록에 있는 파일만 포함됩니다. 수정 후에는 새 작업 폴더에서 재빌드·검증해야 합니다.

검증 모드:

- 실행기 `--self-test`: URL·인자·단일 실행 규칙 자체 검사
- 실행기 `--smoke-test --data-dir ABSOLUTE_TEST_DIRECTORY`: 독립 임시 DB로 시작·정상 종료
- 설치기 `--verify-package`: 내장 ZIP과 모든 해시 검사
- 설치기 `--test-install ABSOLUTE_PATH`: 경로에 `work` 다음 `test`가 있고 그 하위인 새 폴더에 설치, 바로가기·자동 실행 없음

설치기는 기존 설치를 덮어쓰지 않습니다. 자동 업데이트, Windows ‘설치된 앱’ 제거 항목, 코드 서명은 초기 버전에 포함하지 않습니다.

## 검증 범위

2026-11-01 소스 트리에서 `test_mobile_sync.py` 10개가 통과했습니다. 웹앱 테스트는 345개가 통과하고 1개가 건너뛰어졌습니다. `test_build_package.py`와 `test_desktop_host.py`는 `prepare`가 만든 `package/SleepWell` 및 내장 Python 런타임을 검사하므로 소스 트리에서 바로 실행하면 실패하는 것이 정상입니다. 전체 배포 검증은 새 빈 작업 폴더에서 `prepare` 이후 실행해야 합니다.

기존 Windows 0.4.7과 macOS 0.6.6 배포 폴더에는 당시의 manifest·해시·기능 검증 기록이 함께 보관되어 있습니다. 고정된 과거 테스트 개수를 현재 HEAD의 검증 결과로 재사용하지 않습니다.

설치 패키지 검증·새 경로 설치·기존 설치 보호·변조 해시와 경로 탈출 거부를 확인했습니다. 실제 별도 물리 PC나 Windows 10/11의 모든 버전에서 테스트했다는 뜻은 아닙니다. 독립 실행 환경으로 현재 Windows PC에서 검증했습니다.
