<#
    epoch 30초 / 1분 버전 컴파일 + 실제 CSV 비교 검증 스크립트.

    무엇을 하는가:
      1. 두 버전의 Kotlin 소스를 각각 별도 jar로 빌드한다. (같은 package·같은 클래스명이
         두 벌이라 한 jar에 못 넣으므로 나눠 빌드한다.)
      2. 실제 전처리 CSV(sleep_stage_cleaned.csv)로 두 버전을 각각 실행한다.
      3. DirectInput 시나리오도 실행한다. 여기서 epoch 격자 폭이 하드코딩돼 있으면
         1분 모델이 직접 입력 구간을 반씩 잘못 처리하는지 확인하려고,
         일부러 되돌린 빌드(buggy)와 나란히 비교한다.
      4. epoch 수·S/C 값·sanity 체크를 표로 비교한다.

    kotlinc 를 찾는 순서:
      -Kotlinc / $env:KOTLINC 지시 경로 → PATH 의 kotlinc → Android Studio bundled kotlinc
    찾은 경로가 kotlinc.bat 면 컴파일러 jar 를 직접 java 로 실행한다
    (이 bat 은 JDK 를 못 찾는 환경에서 "The system cannot find the file specified" 로 죽는다).

    사용:
      powershell -ExecutionPolicy Bypass -File tools\verify\verify-epoch.ps1
      powershell -ExecutionPolicy Bypass -File tools\verify\verify-epoch.ps1 -Csv "경로.csv"

    ★ 이 스크립트는 UTF-8 BOM 으로 저장돼 있어야 한다. PowerShell 5.1 은 BOM 없는
      .ps1 을 ANSI 로 읽어 한글 문자열이 깨진다. (메모장 "UTF-8" 저장 아님)
#>

param(
    [string]$Csv = "",
    [string]$Kotlinc = $env:KOTLINC,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$VerifyDir = $PSScriptRoot                                       # tools\verify
$Root      = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) # 작업 폴더 루트
$Src30s    = Join-Path $Root "src-epoch-30s\app\sleepwell\health"
$Src1min   = Join-Path $Root "src-epoch-1min\app\sleepwell\health"
$Stubs     = Join-Path $VerifyDir "stubs\SharedTypes.kt"
$Report    = Join-Path $VerifyDir "Report.kt"

# DirectInput 시나리오: 최근 7일 창 안, 두 세션(09-13 18:24~23:10, 09-14 19:11~23:41) 사이
# 깨어 있는 구간에 낮잠을 선언하는 경우. 시작을 :30에 두어 epoch 경계와 어긋나게 했다.
$DiStart = "2026-09-14 02:00:30"
$DiEnd   = "2026-09-14 03:59:30"

if (-not $OutDir) { $OutDir = Join-Path $VerifyDir "out" }
if (-not $Csv) {
    $guess = Get-ChildItem -Path (Join-Path $Root "*") -Recurse -Filter "sleep_stage_cleaned.csv" -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if (-not $guess) { throw "CSV를 찾지 못했습니다. -Csv 로 경로를 지정하세요." }
    $Csv = $guess.FullName
}

# ── kotlinc 와 컴파일러 jar 찾기 ────────────────────────────────
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
    if (-not $exe) { throw "kotlinc 를 찾지 못했습니다. -Kotlinc 로 경로를 지정하거나 KOTLINC 환경변수를 설정하세요." }

    $kcHome = Split-Path -Parent (Split-Path -Parent $exe)
    $jar    = Join-Path $kcHome "lib\kotlin-compiler.jar"
    if (-not (Test-Path -LiteralPath $jar)) { throw "kotlin-compiler.jar 를 찾지 못했습니다: $jar" }
    return @{ Exe = $exe; Home = $kcHome; CompilerJar = $jar }
}

$kc = Resolve-KotlinCompiler
Write-Host "[1/6] kotlinc : $($kc.Exe)" -ForegroundColor Cyan
Write-Host "      jar    : $($kc.CompilerJar)" -ForegroundColor DarkGray
Write-Host "[2/6] csv     : $Csv" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

# ── 빌드 ────────────────────────────────────────────────────────
function Build-Jar([string]$jarName, [string]$srcDir, [string]$runner) {
    $jar = Join-Path $OutDir $jarName
    if (Test-Path -LiteralPath $jar) { Remove-Item -LiteralPath $jar -Force }
    $sources = @(Get-ChildItem -Path (Join-Path $srcDir "*.kt") | Select-Object -ExpandProperty FullName)
    $sources += $Stubs, $Report, $runner
    Write-Host ("      빌드 {0} ({1}개 파일)" -f $jarName, $sources.Count) -ForegroundColor DarkGray
    $log  = & java "-cp" $kc.CompilerJar org.jetbrains.kotlin.cli.jvm.K2JVMCompiler @sources "-d" $jar "-nowarn" "-include-runtime" 2>&1
    $errs = @($log | Where-Object { $_ -match "error:" })
    if ($errs.Count -gt 0) {
        $errs | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
        throw "컴파일 실패: $jarName"
    }
    if (-not (Test-Path -LiteralPath $jar)) { throw "jar 생성 실패: $jarName" }
    return $jar
}

Write-Host "[3/6] 빌드" -ForegroundColor Cyan
$jar30s  = Build-Jar "verify-30s.jar"  $Src30s  (Join-Path $VerifyDir "Run30s.kt")
$jar1min = Build-Jar "verify-1min.jar" $Src1min (Join-Path $VerifyDir "Run1Min.kt")

# ── 되돌린(버그 있는) 1분 빌드 ──────────────────────────────────
# applyDirectInput 의 epoch 폭을 30_000L 로 되돌린 복사본. 1분 모델에서
# 직접 입력 구간을 절반 크기로만 처리하는 버그가 실제로 어떻게 보이는지 확인용.
$buggyDir = Join-Path $OutDir "buggy-src"
if (Test-Path -LiteralPath $buggyDir) { Remove-Item -LiteralPath $buggyDir -Recurse -Force }
New-Item -ItemType Directory -Path $buggyDir -Force | Out-Null
Get-ChildItem -Path (Join-Path $Src1min "*.kt") | Copy-Item -Destination $buggyDir -Force

$buggyPipeline = Join-Path $buggyDir "SleepAnalysisPipeline.kt"
$buggyText = Get-Content -LiteralPath $buggyPipeline -Raw -Encoding UTF8
$buggyFixed = 'val intervalMs = epochInterval.intervalMs'
if (-not $buggyText.Contains($buggyFixed)) { throw "패치 대상 줄을 찾지 못했습니다: $buggyFixed" }
$buggyText = $buggyText.Replace($buggyFixed, 'val intervalMs = 30_000L   // 되돌린 버그 (검증용)')
Set-Content -LiteralPath $buggyPipeline -Value $buggyText -Encoding UTF8 -NoNewline
$jarBuggy = Build-Jar "verify-1min-buggy.jar" $buggyDir (Join-Path $VerifyDir "Run1Min.kt")

# ── 실행 ────────────────────────────────────────────────────────
Write-Host "[4/6] 실행: DirectInput 없음" -ForegroundColor Cyan
$out30s     = Join-Path $OutDir "result-30s.txt"
$out1min    = Join-Path $OutDir "result-1min.txt"
& java -jar $jar30s  $Csv $out30s  | Out-Null
& java -jar $jar1min $Csv $out1min | Out-Null

Write-Host "[5/6] 실행: DirectInput $DiStart ~ $DiEnd" -ForegroundColor Cyan
$out30sDI   = Join-Path $OutDir "result-30s-di.txt"
$out1minDI  = Join-Path $OutDir "result-1min-di.txt"
$outBuggyDI = Join-Path $OutDir "result-1min-buggy-di.txt"
& java -jar $jar30s  $Csv $out30sDI   $DiStart $DiEnd | Out-Null
& java -jar $jar1min $Csv $out1minDI  $DiStart $DiEnd | Out-Null
& java -jar $jarBuggy $Csv $outBuggyDI $DiStart $DiEnd | Out-Null

# ── 비교 ────────────────────────────────────────────────────────
Write-Host "[6/6] 비교" -ForegroundColor Cyan
function Field([string]$path, [string]$key) {
    $line = Select-String -LiteralPath $path -Pattern "^$key=" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($line) { return $line.Line.Substring($key.Length + 1) }
    return "<없음>"
}

$rows = @(
    @{ k = "status";               n = "실행 결과" },
    @{ k = "epochIntervalLabel";   n = "epoch 라벨" },
    @{ k = "epochIntervalMs";      n = "epoch 간격(ms)" },
    @{ k = "epochCount";           n = "epoch 수" },
    @{ k = "futurePointCount";     n = "futureS 점 수" },
    @{ k = "sessionCount";         n = "세션 수" },
    @{ k = "inputPeriodStartText"; n = "타임라인 시작" },
    @{ k = "inputPeriodEndText";   n = "타임라인 끝" },
    @{ k = "firstEpochText";       n = "첫 epoch 시각" },
    @{ k = "lastEpochText";        n = "마지막 epoch 시각" },
    @{ k = "currentS";             n = "현재 S" },
    @{ k = "currentC";             n = "현재 C_sleep" },
    @{ k = "currentPropensity";    n = "현재 propensity" },
    @{ k = "Smin";                 n = "S 최솟값" },
    @{ k = "Smax";                 n = "S 최댓값" },
    @{ k = "Cmin";                 n = "C 최솟값" },
    @{ k = "Cmax";                 n = "C 최댓값" },
    @{ k = "propensityMin";        n = "propensity 최솟값" },
    @{ k = "propensityMax";        n = "propensity 최댓값" },
    @{ k = "sRiseCount";           n = "S 상승 구간 수" },
    @{ k = "sFallCount";           n = "S 하강 구간 수" },
    @{ k = "sFlatCount";           n = "S 평탄 구간 수" }
)

Write-Host ""
Write-Host "  A) DirectInput 없음 — 30초 vs 1분"
Write-Host ("  {0,-20} {1,-26} {2,-26}" -f "항목", "30초", "1분") -ForegroundColor White
Write-Host ("  " + ("-" * 76))
foreach ($r in $rows) {
    $a = Field $out30s  $r.k
    $b = Field $out1min $r.k
    Write-Host ("  {0,-20} {1,-26} {2,-26}" -f $r.n, $a, $b) -ForegroundColor Gray
}

Write-Host ""
Write-Host "  B) DirectInput 있음 — 고정된 1분 vs 되돌린(버그) 1분"
Write-Host ("  {0,-20} {1,-26} {2,-26} {3,-26}" -f "항목", "30초", "1분(고정)", "1분(되돌림)") -ForegroundColor White
Write-Host ("  " + ("-" * 100))
foreach ($k in @("status", "epochCount", "currentS", "currentPropensity", "Smin", "Smax")) {
    $a = Field $out30sDI   $k
    $b = Field $out1minDI  $k
    $c = Field $outBuggyDI $k
    Write-Host ("  {0,-20} {1,-26} {2,-26} {3,-26}" -f $k, $a, $b, $c) -ForegroundColor Gray
}

Write-Host ""
Write-Host "  C) 세션별 수면 요약 (epoch 와 무관 — sanity 확인)"
$a = @(Get-Content -LiteralPath $out30s  | Select-String -Pattern "^session,").Count
$b = @(Get-Content -LiteralPath $out1min | Select-String -Pattern "^session,").Count
$same = if ($a -eq $b) { "정상" } else { "다름 → 확인 필요" }
Write-Host ("    30초 {0} 개 / 1분 {1} 개  {2}" -f $a, $b, $same) -ForegroundColor Gray

Write-Host ""
Write-Host "산출 파일:" -ForegroundColor White
@($out30s, $out1min, $out30sDI, $out1minDI, $outBuggyDI) | ForEach-Object { Write-Host "  $_" }