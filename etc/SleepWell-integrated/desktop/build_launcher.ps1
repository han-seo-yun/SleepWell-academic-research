[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "bin")
)

$ErrorActionPreference = "Stop"

$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$source = Join-Path $PSScriptRoot "SleepWell.cs"

if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw "C# compiler not found: $compiler"
}
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Launcher source not found: $source"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = Join-Path $OutputDirectory "SleepWell.exe"

$compilerArguments = @(
    "/nologo"
    "/target:winexe"
    "/platform:anycpu"
    "/optimize+"
    "/warn:4"
    "/reference:System.dll"
    "/reference:System.Core.dll"
    "/reference:System.Drawing.dll"
    "/reference:System.Windows.Forms.dll"
    "/reference:System.Web.Extensions.dll"
    "/out:$output"
    $source
)

& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed with exit code $LASTEXITCODE"
}

$selfTest = Start-Process -FilePath $output -ArgumentList "--self-test" -WindowStyle Hidden -Wait -PassThru
if ($selfTest.ExitCode -ne 0) {
    throw "Launcher self-test failed with exit code $($selfTest.ExitCode)"
}

$item = Get-Item -LiteralPath $output
$certOutput = Join-Path $OutputDirectory "SyncCertificate.exe"
& $compiler /nologo /target:exe /optimize+ /reference:System.dll /reference:System.Core.dll "/out:$certOutput" (Join-Path $PSScriptRoot "SyncCertificate.cs")
if ($LASTEXITCODE -ne 0) { throw "Certificate helper compilation failed" }
[PSCustomObject]@{
    Path = $item.FullName
    Version = "0.4.7"
    Bytes = $item.Length
    SelfTest = "Passed"
}
