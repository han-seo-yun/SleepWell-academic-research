[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$PayloadZip,

    [Parameter(Mandatory = $false)]
    [string]$OutputPath = (Join-Path $PSScriptRoot 'SleepWellSetup.exe')
)

$ErrorActionPreference = 'Stop'

$sourcePath = Join-Path $PSScriptRoot 'SleepWellSetup.cs'
$compilerPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$frameworkPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$compressionPath = Join-Path $frameworkPath 'System.IO.Compression.dll'
$compressionFileSystemPath = Join-Path $frameworkPath 'System.IO.Compression.FileSystem.dll'
$webExtensionsPath = Join-Path $frameworkPath 'System.Web.Extensions.dll'

foreach ($requiredPath in @($sourcePath, $compilerPath, $compressionPath, $compressionFileSystemPath, $webExtensionsPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "필수 빌드 파일을 찾을 수 없습니다: $requiredPath"
    }
}

$resolvedPayload = (Resolve-Path -LiteralPath $PayloadZip).Path
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($resolvedOutput)
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}

$compilerArguments = @(
    '/nologo',
    '/target:winexe',
    '/platform:x64',
    '/optimize+',
    '/checked+',
    "/out:$resolvedOutput",
    "/resource:$resolvedPayload,SleepWellPayload.zip",
    "/reference:$compressionPath",
    "/reference:$compressionFileSystemPath",
    "/reference:$webExtensionsPath",
    '/reference:System.dll',
    '/reference:System.Core.dll',
    '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll',
    $sourcePath
)

& $compilerPath @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "설치기 컴파일에 실패했습니다. csc 종료 코드: $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $resolvedOutput -PathType Leaf)) {
    throw "컴파일러가 출력 파일을 만들지 않았습니다: $resolvedOutput"
}

$builtHash = (Get-FileHash -LiteralPath $resolvedOutput -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "SETUP_BUILT path=$resolvedOutput sha256=$builtHash"
