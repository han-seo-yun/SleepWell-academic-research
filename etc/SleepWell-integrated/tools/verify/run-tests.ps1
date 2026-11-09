<#
    회귀 테스트 실행 스크립트.

    무엇을 하는가:
      1. src-epoch-1min (1분 버전) + 스텁 + 테스트 를 컴파일해 전체 테스트를 돌린다.
      2. src/ 와 src-epoch-30s (30초 버전) + 스텁 + clockHours 테스트 를 각각 컴파일해 돌린다.
         (이 둘은 EpochInterval 타입이 없어 나머지 테스트를 참조할 수 없다)

    왜 kotlinc 를 직접 호출하는가:
      이 폴더에는 Gradle 프로젝트가 없다. 그리고 gradlew.bat / kotlinc.bat 는 .bat 이라
      환경에 따라 실행이 안 될 수 있다. 컴파일러 jar 를 java -cp 로 직접 호출하면안전하다.

    사용:
      powershell -ExecutionPolicy Bypass -File tools\verify\run-tests.ps1
      powershell -ExecutionPolicy Bypass -File tools\verify\run-tests.ps1 -Kotlinc "경로\kotlinc.bat"
#>

param(
    [string]$Kotlinc = $env:KOTLINC,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$VerifyDir = $PSScriptRoot
$Root      = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TestDir   = Join-Path $VerifyDir "test"
$Stubs     = Join-Path $VerifyDir "stubs\SharedTypes.kt"

if (-not $OutDir) { $OutDir = Join-Path $VerifyDir "test-out" }

function Resolve-KotlinCompiler {
    $exe = $null
    if ($Kotlinc -and (Test-Path -LiteralPath $Kotlinc)) { $exe = (Resolve-Path -LiteralPath $Kotlinc).Path }
    if (-not $exe) {
        $cmd = Get-Command kotlinc -ErrorAction SilentlyContinue
        if ($cmd) { $exe = $cmd.Source }
    }
    if (-not $exe) {
        $guess = "C:\Program Files\Android\Android Studio\plugins\Kotlin\kotlinc\bin\kotlinc.bat"
        if (Test-Path -LiteralPath $guess) { $exe = $guess }
    }
    if (-not $exe) { throw "kotlinc 를 찾지 못했습니다. -Kotlinc 로 경로를 지정하세요." }
    $kcHome = Split-Path -Parent (Split-Path -Parent $exe)
    $jar    = Join-Path $kcHome "lib\kotlin-compiler.jar"
    if (-not (Test-Path -LiteralPath $jar)) { throw "kotlin-compiler.jar 를 찾지 못했습니다: $jar" }
    return $jar
}

$compilerJar = Resolve-KotlinCompiler
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
Write-Host "컴파일러: $compilerJar" -ForegroundColor Cyan

function Invoke-Suite(
    [string]$label,
    [string]$srcDir,
    [string]$runnerFile,
    [string[]]$testFiles
) {
    Write-Host ""
    Write-Host ("=" * 74) -ForegroundColor Cyan
    Write-Host "  $label" -ForegroundColor Cyan
    Write-Host ("=" * 74) -ForegroundColor Cyan

    # 0 = 통과, 1 = 실패, 2 = 건너뜀.
    # 건너뜀을 통과와 구분해야 한다 — 경로 오타로 아무것도 안 돌았는데
    # "전체 통과"가 나오면 그게 가장 나쁜 종류의 오류다.
    if (-not (Test-Path -LiteralPath $srcDir)) {
        Write-Host "  (건너뜀 — 폴더 없음: $srcDir)" -ForegroundColor Yellow
        return [int]2
    }

    $jar = Join-Path $OutDir ("test-" + ($label -replace '[^A-Za-z0-9]', '-') + ".jar")
    if (Test-Path -LiteralPath $jar) { Remove-Item -LiteralPath $jar -Force }

    $sources = @(Get-ChildItem -Path (Join-Path $srcDir "*.kt") | Select-Object -ExpandProperty FullName)
    $sources += $Stubs
    $sources += (Join-Path $TestDir "Check.kt")
    $sources += $testFiles
    $sources += (Join-Path $TestDir $runnerFile)

    # kotlinc 가 warning/error 를 stderr 로 내는데, PowerShell 이 그걸 terminating error 로
    # 보아 스크립트가 중간에 죽는다. 컴파일 호출 동안만 완화하고 결과는 파일로 판정한다.
    $so = Join-Path $OutDir "_compile.out"
    $se = Join-Path $OutDir "_compile.err"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & java "-cp" $compilerJar org.jetbrains.kotlin.cli.jvm.K2JVMCompiler @sources "-d" $jar "-nowarn" "-include-runtime" 1> $so 2> $se
    $ErrorActionPreference = $prevEap

    $log  = Get-Content -LiteralPath $so -Encoding UTF8 -ErrorAction SilentlyContinue
    $log += Get-Content -LiteralPath $se -Encoding UTF8 -ErrorAction SilentlyContinue
    $errs = @($log | Where-Object { $_ -match "error:" })
    if ($errs.Count -gt 0) {
        Write-Host "컴파일 실패:" -ForegroundColor Red
        $errs | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        return [int]1
    }

    $prevEap2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # Out-Host 로 빼야 테스트 출력이 함수의 반환값에 섞이지 않는다.
    # (그대로 두면 java 의 stdout 이 반환값이 되어 $fail 계산이 깨진다)
    & java -jar $jar 2>&1 | Out-Host
    $runCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap2
    if ($runCode -ne 0) { return [int]1 }
    return [int]0
}

$allTests = @(Get-ChildItem -Path (Join-Path $TestDir "*.kt") |
              Where-Object { $_.Name -notin @("RunTests30s.kt", "RunTests1Min.kt", "Check.kt") } |
              Select-Object -ExpandProperty FullName)

$results = @()

# 1) 1분 버전 — 전체 테스트
$results += [pscustomobject]@{
    Name = "src-epoch-1min (전체)"
    Code = [int](Invoke-Suite "src-epoch-1min (전체)" `
        (Join-Path $Root "src-epoch-1min\app\sleepwell\health") "RunTests1Min.kt" $allTests)
}

# 2) 30초 버전 2곳 — clockHours 회귀만 (EpochInterval 이 없어 나머지는 못 붙임)
$results += [pscustomobject]@{
    Name = "src (30초, clockHours 만)"
    Code = [int](Invoke-Suite "src (30초, clockHours 만)" `
        (Join-Path $Root "src\app\sleepwell\health") "RunTests30s.kt" `
        @((Join-Path $TestDir "TwoProcessModelTest.kt")))
}

$results += [pscustomobject]@{
    Name = "src-epoch-30s (clockHours 만)"
    Code = [int](Invoke-Suite "src-epoch-30s (clockHours 만)" `
        (Join-Path $Root "src-epoch-30s\app\sleepwell\health") "RunTests30s.kt" `
        @((Join-Path $TestDir "TwoProcessModelTest.kt")))
}

$fail = [int](($results | Where-Object { $_.Code -eq 1 } | Measure-Object).Count)
$skip = [int](($results | Where-Object { $_.Code -eq 2 } | Measure-Object).Count)
$ran  = [int](($results | Where-Object { $_.Code -ne 2 } | Measure-Object).Count)

Write-Host ""
Write-Host ("=" * 74) -ForegroundColor Cyan
$results | ForEach-Object {
    if ($_.Code -eq 0) { $mark = "PASS"; $color = "Green" }
    elseif ($_.Code -eq 2) { $mark = "SKIP"; $color = "Yellow" }
    else { $mark = "FAIL"; $color = "Red" }
    Write-Host ("  {0,-4} {1}" -f $mark, $_.Name) -ForegroundColor $color
}
Write-Host ("=" * 74) -ForegroundColor Cyan

if ($ran -eq 0) {
    Write-Host "  어떤 스위트도 실행되지 않았다 — 경로 설정 확인 (건너뜀 $skip 개)" -ForegroundColor Red
    exit 1
}
if ($skip -gt 0) {
    Write-Host "  건너뜀 $skip 개 — 해당 폴더가 없는 곳이다 (실행 $ran 개)" -ForegroundColor Yellow
}
if ($fail -eq 0) {
    Write-Host "  전체 통과 (실행 $ran 개 스위트)" -ForegroundColor Green
} else {
    Write-Host "  실패한 스위트 $fail 개" -ForegroundColor Red
}
exit $fail