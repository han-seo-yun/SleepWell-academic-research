# SleepWell iOS prototype

`SleepWell.xcodeproj`를 Xcode에서 열고 `SleepWell` scheme을 선택합니다. iOS 16 이상이 대상입니다.

1. Xcode를 설치하고 최초 실행 설정을 완료합니다.
2. Signing & Capabilities에서 본인 개발 Team과 고유 Bundle Identifier를 지정합니다. HealthKit capability가 포함돼 있습니다.
3. 시뮬레이터에서는 샘플 모드로 확인합니다. 실제 수면·심박·걸음은 건강 기록이 있는 iPhone에서 확인합니다.
4. 샘플 모드를 끄고 건강 기록 연결·분석을 누릅니다. 수면·심박·걸음 읽기 권한을 선택합니다.
5. 연구용 CSV/JSON과 생성 시각을 포함한 예측 이력을 공유 메뉴로 저장할 수 있습니다.

## 범위

SwiftUI 화면, HealthKit 읽기, Watch 소스 필터, 중복 구간 정규화, Two-Process 계산, Ridge JSON 가져오기, 30초 연구 CSV, 원본·분석 JSON, 예측 로그를 구현했습니다. Core 수면은 N1/N2로 임의 변환하지 않고 `stage_label=-1`, `sleep_wake=1`로 저장합니다.

Ridge JSON은 기존 Python export schema 1의 수식을 지원합니다. 입력 시간 범위·단위는 `ios_pre20_v1`으로 명시했습니다. Android 입력 생성 규칙과 차이가 있어 동일 계수의 실서비스 배포 전 입력 일치 검증 및 재학습이 필요합니다. 학습된 모델 파일은 기본 제공하지 않습니다.

이 프로젝트는 초기 **행동 추정/물리모델 분석용 iOS 소스**입니다. 사용자가 이후 확정한 가입·2주 개인화·장기 피드백 기반 수면 개선 흐름은 현재 `../web-app`에서 구현했습니다. iOS 앱과 웹앱의 프로필·기록 동기화 및 새 추천 정책 이식은 남아 있습니다.

## 검증 결과와 한계

현재 Mac에는 Xcode가 없고 Command Line Tools만 있습니다. 핵심 Swift 코드 컴파일 및 7개 검증(물리식, 단계 중복, 미래 누수, 결측, Ridge, 자정, export)을 통과했습니다. 앱 소스 구문 검사와 plist/project 파일 구조 검사를 통과했습니다.

**이 Windows 작업 환경에서는 iOS SDK 빌드, 시뮬레이터 화면, HealthKit 실기기 실행 및 서명을 검증하지 못했습니다. 설치 가능한 IPA나 App Store 배포본은 아직 없습니다.** `.github/workflows/ios.yml`은 현재 원격 저장소에도 포함돼 있지만, workflow 존재 자체를 실기기 HealthKit·서명·배포 검증으로 간주하지 않습니다. 실행 결과는 해당 GitHub Actions run에서 별도로 확인해야 합니다.

```bash
bash scripts/verify-core.sh
# Xcode가 있는 환경:
swift test
xcodebuild -project SleepWell.xcodeproj -scheme SleepWell \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

HealthKit은 읽기 권한 거부 여부를 앱에 직접 알려주지 않으므로 조회 결과가 비었을 때 기록 부재와 권한 거부를 구분해 단정하지 않습니다. [Apple HealthKit authorization](https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data)
