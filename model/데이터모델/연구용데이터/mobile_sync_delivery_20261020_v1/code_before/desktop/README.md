# SleepWell Windows 배포 빌드

Windows용 실행기·설치기와 독립 Python 실행 환경을 묶는 소스입니다. 기존 연구 웹앱이나 개인 DB를 변경하지 않고 별도의 배포 앱을 만듭니다.

## 구성

- `SleepWell.cs`: 제어창, 알림 영역 실행, 서버 준비 검증, 브라우저 앱 창, 자식 서버 종료
- `desktop_host.py`: 임시 loopback 포트, 사용자별 DB, 동시 실행 잠금, 부모 종료 감지
- `SleepWellSetup.cs`: 사용자별 설치, ZIP 경로·파일 해시 검사, 선택적 바로가기
- `build_package.py`: 공개 파일만 준비하고 경로·내용을 고정한 뒤 설치 패키지 생성
- `test_desktop_host.py`, `test_build_package.py`, `test_launcher_shutdown.ps1`: 실행·개인정보 경계·무응답 서버 회귀 검사
- `USER_GUIDE.md`: 사용자 설치·실행·기록 관리 안내

기본 기록 위치는 `%LOCALAPPDATA%\SleepWell`입니다. 연구 웹앱의 `.local` 폴더, 개인 모델, 원자료는 배포하지 않습니다. 집단 평균은 정해진 스키마의 NHANES 익명 집계만 허용합니다. 배포 호스트는 원자료 탐색용 `ResearchBridge`를 생성하지 않습니다.

## 재현 빌드

새 작업 폴더를 만들고 이 소스 디렉터리를 그 아래 `desktop`으로 복사합니다. 빌드 작업 폴더의 구조는 `desktop/`, `downloads/`, `package/`, `build/`입니다. 기존 배포 폴더를 입력 자료나 백업 저장 위치로 재사용하지 마세요.

필요한 빌드 환경은 Python 3.12 이상, 기존 `tzdata` 2026.2, Windows에 포함된 .NET Framework C# 컴파일러입니다. 최종 사용자 PC에는 Python 설치가 필요하지 않습니다.

공식 내장형 Python을 `downloads/python-3.14.7-embed-amd64.zip`에, 대응 SBOM을 `downloads/python-runtime.spdx.json`에 저장합니다.

- [공식 Python 3.14.7 배포 페이지](https://www.python.org/downloads/release/python-3147/)
- [Windows x64 내장형 ZIP](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip)
- [구성요소 SBOM](https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip.spdx.json)
- 고정 SHA-256: `d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15`

새 작업 폴더에서 아래 순서로 빌드합니다. `PROJECT_PATH`는 원본 SleepWell-integrated 경로입니다.

```powershell
python -B -X utf8 desktop/build_package.py prepare --repository "PROJECT_PATH"
& desktop/build_launcher.ps1
python -B -X utf8 -m unittest discover -s desktop -p "test_*.py" -v
python -B -X utf8 desktop/build_package.py seal
& desktop/build_setup.ps1 -PayloadZip SleepWellPayload.zip -OutputPath build/SleepWell-Setup-0.1.0-x64.exe
```

`prepare`는 기존 패키지 폴더가 있으면 중단합니다. 준비된 파일 경로와 해시는 배포 폴더 밖 `prepared-manifest.json`에 고정됩니다. `seal`은 임의의 추가 파일이나 수정된 준비 파일이 발견되면 중단합니다. 압축에는 고정 목록에 있는 파일만 포함됩니다. 수정 후에는 새 작업 폴더에서 재빌드·검증해야 합니다.

검증 모드:

- 실행기 `--self-test`: URL·인자·단일 실행 규칙 자체 검사
- 실행기 `--smoke-test --data-dir ABSOLUTE_TEST_DIRECTORY`: 독립 임시 DB로 시작·정상 종료
- 설치기 `--verify-package`: 내장 ZIP과 모든 해시 검사
- 설치기 `--test-install ABSOLUTE_PATH`: 경로에 `work` 다음 `test`가 있고 그 하위인 새 폴더에 설치, 바로가기·자동 실행 없음

설치기는 기존 설치를 덮어쓰지 않습니다. 자동 업데이트, Windows ‘설치된 앱’ 제거 항목, 코드 서명은 초기 버전에 포함하지 않습니다.

## 검증 범위

CPython 3.14.7 독립 런타임에서 기존 앱 테스트 81개가 통과했습니다. 배포 호스트와 패키지 경계 테스트 12개, 실행기 자체 검사, 정상·무응답 종료 검사도 통과했습니다. 무응답 검사에서는 직접 생성한 서버만 종료되고 별도 Python 프로세스는 유지됐습니다.

설치 패키지 검증·새 경로 설치·기존 설치 보호·변조 해시와 경로 탈출 거부를 확인했습니다. 실제 별도 물리 PC나 Windows 10/11의 모든 버전에서 테스트했다는 뜻은 아닙니다. 독립 실행 환경으로 현재 Windows PC에서 검증했습니다.
