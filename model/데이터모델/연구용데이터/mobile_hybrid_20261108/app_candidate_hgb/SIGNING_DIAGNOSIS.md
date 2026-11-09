# A52s 기존 앱 서명 호환성 점검

## 결론

- 기존 A52s 앱과 같은 인증서로 후보 APK를 서명할 수 있는 로컬 개인 키를 찾지 못했다.
- 따라서 별도 재서명 후보는 만들지 않았다. 기존 HGB 후보 APK, manifest, 정식 소스는 변경하지 않았다.
- 사용자 결정 뒤 기존 앱을 데이터 보존 상태로 업데이트하려면 SHA-256 `1b0054af033c2c132163a4ce76f4bfd6802250a45c14b92a0905175efc32a3a7` 인증서에 대응하는 원래 개인 키가 추가로 필요하다.

## 확인된 공개 인증서

- A52s에서 보존한 기존 APK `installed_before_0.1.apk`
  - APK SHA-256: `ebb6912ea68fb6f0729df610d34f9717b301537f46a49b399ba2d512e7214d02`
  - 서명 인증서 SHA-256: `1b0054af033c2c132163a4ce76f4bfd6802250a45c14b92a0905175efc32a3a7`
- 오프라인 HGB 후보 APK
  - 서명 인증서 SHA-256: `72a3b8e32ecae55a73c840bc4d6bb4e74049f312b60b9bde0b782fb26e595333`
- 현재 `C:/Users/pyomo/.android/debug.keystore`
  - 공개 인증서 SHA-256: `72a3b8e32ecae55a73c840bc4d6bb4e74049f312b60b9bde0b782fb26e595333`
  - 즉 현재 debug keystore는 후보 서명과 일치하지만 기존 A52s 앱과는 일치하지 않는다.

## 읽기 전용 검색 범위

- 정식 앱 프로젝트와 v7/v8 로컬 프로젝트의 Gradle signing 설정
- `E:/학술제`, `E:/sleepwell-builds`
- 사용자 OneDrive, Desktop, Downloads, Documents
- 사용자 Gradle/Android Studio/Android SDK 관련 로컬 디렉터리
- 로컬 C:/E: 휴지통에서 접근 가능한 keystore 파일

프로젝트별 `signingConfig`, `storeFile`, custom key alias 설정은 발견되지 않았고, 현재 debug keystore 외에 `.keystore` 또는 `.jks` 개인 키 파일도 발견되지 않았다. 비밀번호나 개인 키 내용은 출력·복사하지 않았다.

## 경계

- 휴대폰 앱 설치·제거 없음.
- APK 재서명 없음.
- adb 서버나 기기 상태 변경 없음.
- 원래 `1b00…` 개인 키가 발견되기 전에는 동일 패키지 업데이트 APK를 준비할 수 없다.
