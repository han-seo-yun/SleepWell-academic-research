param(
    [string]$ProjectRoot = "E:\학술제\etc\SleepWell-integrated",
    [string]$OutputJson = "C:\Users\pyomo\Documents\Codex\2026-10-20\new-chat\work\continuous_20261025\android_verification.json"
)

$ErrorActionPreference = "Stop"
$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $ProjectRoot "android-app\app\src\main\java\app\sleepwell\user"
$assets = Join-Path $ProjectRoot "android-app\app\src\main\assets"
$cache = Join-Path $env:USERPROFILE ".gradle\caches\modules-2\files-2.1"
$jdk = if ($env:JAVA_HOME) { $env:JAVA_HOME } else {
    "C:\Users\pyomo\OneDrive\문서\ChatGPT\학술제\work\mobile_sync\toolchain\jdk\jdk-21.0.12.1+1"
}

function Find-Jar([string]$pattern) {
    $value = Get-ChildItem -LiteralPath $cache -Recurse -File -Filter $pattern | Sort-Object FullName | Select-Object -Last 1
    if (-not $value) { throw "Required cached jar not found: $pattern" }
    $value.FullName
}

$compiler = Find-Jar "kotlin-compiler-embeddable-*.jar"
$stdlib = Get-ChildItem -LiteralPath (Join-Path $cache "org.jetbrains.kotlin\kotlin-stdlib") -Recurse -File -Filter "kotlin-stdlib-*.jar" |
    Where-Object { $_.Name -match '^kotlin-stdlib-[0-9]' } | Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
$coroutines = Find-Jar "kotlinx-coroutines-core-jvm-*.jar"
$annotations = Get-ChildItem -LiteralPath (Join-Path $cache "org.jetbrains\annotations") -Recurse -File -Filter "annotations-*.jar" | Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
$gson = Find-Jar "gson-*.jar"
$classes = Join-Path $harness "classes"
New-Item -ItemType Directory -Force -Path $classes | Out-Null

& (Join-Path $jdk "bin\javac.exe") -d $classes (Join-Path $source "UserContractRules.java")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$sources = @(
    (Join-Path $harness "src\android\content\Context.kt"),
    (Join-Path $harness "src\org\json\JsonCompat.kt"),
    (Join-Path $harness "src\app\sleepwell\user\UserModelEngineDailyVerification.kt"),
    (Join-Path $source "DailyContext.kt"),
    (Join-Path $source "UserModels.kt"),
    (Join-Path $source "PortableFusionModel.kt"),
    (Join-Path $source "AdaptiveResidualPersonalization.kt"),
    (Join-Path $source "ResearchBaselines.kt"),
    (Join-Path $source "UserModelEngine.kt")
)
& (Join-Path $jdk "bin\java.exe") -cp "$compiler;$stdlib;$coroutines;$annotations" org.jetbrains.kotlin.cli.jvm.K2JVMCompiler `
    -no-stdlib -no-reflect -jvm-target 17 -classpath "$stdlib;$gson;$classes;$annotations" -d $classes @sources
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& (Join-Path $jdk "bin\java.exe") -cp "$classes;$stdlib;$gson" `
    app.sleepwell.user.UserModelEngineDailyVerificationKt $assets $OutputJson
exit $LASTEXITCODE
