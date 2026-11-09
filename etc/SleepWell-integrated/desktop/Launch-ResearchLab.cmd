@echo off
chcp 65001 >nul
setlocal
set "SLEEPWELL=%~dp0배포\Windows_연구자용_0.4.7\SleepWell.exe"
if not exist "%SLEEPWELL%" (
  echo SleepWell 연구자용 0.4.7 실행 파일을 찾을 수 없습니다.
  echo %SLEEPWELL%
  pause
  exit /b 1
)
start "" "%SLEEPWELL%" --research-lab
endlocal
