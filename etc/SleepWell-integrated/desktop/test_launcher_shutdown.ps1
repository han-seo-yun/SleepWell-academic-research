[CmdletBinding()]
param(
    [string]$PackageRoot = ''
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($PackageRoot)) {
    $PackageRoot = Join-Path (Split-Path $PSScriptRoot -Parent) "package\SleepWell"
}

$launcherSource = Join-Path $PSScriptRoot "bin\SleepWell.exe"
$runtimeSource = Join-Path $PackageRoot "runtime"
$pythonSource = Join-Path $runtimeSource "python.exe"
if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) { throw "Build the launcher first." }
if (-not (Test-Path -LiteralPath $pythonSource -PathType Leaf)) { throw "Packaged Python runtime not found." }

$workspaceRoot = Split-Path $PSScriptRoot -Parent
$testRoot = Join-Path $workspaceRoot "testdata"
$caseRoot = Join-Path $testRoot ("shutdown-regression-" + [Guid]::NewGuid().ToString("N"))
$package = Join-Path $caseRoot "SleepWell"
$appDirectory = Join-Path $package "app"
$runtimeDirectory = Join-Path $package "runtime"
$dataDirectory = Join-Path $caseRoot "data"
$unrelated = $null
$result = $null

$fixture = @'
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlparse

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", type=Path, required=True)
parser.add_argument("--ready-file", type=Path, required=True)
args = parser.parse_args()
args.data_dir.mkdir(parents=True, exist_ok=True)
instance_id = secrets.token_hex(24)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/api/desktop-health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "app": "SleepWell", "pid": os.getpid(),
            "instance_id": instance_id, "version": "test",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
temporary = args.ready_file.with_suffix(".tmp")
temporary.write_text(json.dumps({
    "app": "SleepWell", "pid": os.getpid(), "instance_id": instance_id,
    "url": f"http://127.0.0.1:{server.server_port}/",
}), encoding="utf-8")
os.replace(temporary, args.ready_file)

def ignore_control_pipe():
    for _line in sys.stdin:
        pass

threading.Thread(target=ignore_control_pipe, daemon=True).start()
server.serve_forever()
'@

try {
    New-Item -ItemType Directory -Path $appDirectory,$dataDirectory -Force | Out-Null
    Copy-Item -LiteralPath $runtimeSource -Destination $runtimeDirectory -Recurse
    Copy-Item -LiteralPath $launcherSource -Destination (Join-Path $package "SleepWell.exe")
    [System.IO.File]::WriteAllText(
        (Join-Path $appDirectory "desktop_host.py"),
        $fixture,
        [System.Text.UTF8Encoding]::new($false)
    )

    $unrelatedInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $unrelatedInfo.FileName = $pythonSource
    $unrelatedInfo.Arguments = '-B -c "import time; time.sleep(30)"'
    $unrelatedInfo.UseShellExecute = $false
    $unrelatedInfo.CreateNoWindow = $true
    $unrelated = [System.Diagnostics.Process]::Start($unrelatedInfo)

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $smoke = Start-Process -FilePath (Join-Path $package "SleepWell.exe") `
        -ArgumentList @("--smoke-test", "--data-dir", ('"' + $dataDirectory + '"')) `
        -Wait -PassThru
    $timer.Stop()

    $readyFile = Join-Path $dataDirectory "desktop-ready.json"
    if (-not (Test-Path -LiteralPath $readyFile -PathType Leaf)) {
        throw "Forced termination should leave the owned host's stale ready file for PID validation."
    }
    $ready = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
    $ownedStillRunning = $null -ne (Get-Process -Id ([int]$ready.pid) -ErrorAction SilentlyContinue)
    $unrelated.Refresh()

    $result = [PSCustomObject]@{
        LauncherExitCode = $smoke.ExitCode
        ElapsedSeconds = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
        UnresponsiveOwnedChildStopped = -not $ownedStillRunning
        UnrelatedPythonStillRunning = -not $unrelated.HasExited
        StaleReadyPreserved = $true
    }
    if ($smoke.ExitCode -ne 0) { throw "Smoke test exit code: $($smoke.ExitCode)" }
    if ($timer.Elapsed.TotalSeconds -lt 7.5 -or $timer.Elapsed.TotalSeconds -gt 15) {
        throw "Shutdown did not observe the bounded graceful timeout: $($timer.Elapsed.TotalSeconds) seconds"
    }
    if ($ownedStillRunning) { throw "The unresponsive owned child is still running." }
    if ($unrelated.HasExited) { throw "An unrelated Python process was terminated." }
}
finally {
    if ($null -ne $unrelated) {
        try {
            $unrelated.Refresh()
            if (-not $unrelated.HasExited) {
                $unrelated.Kill()
                $unrelated.WaitForExit(3000) | Out-Null
            }
        }
        finally {
            $unrelated.Dispose()
        }
    }
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    $resolvedCaseRoot = [System.IO.Path]::GetFullPath($caseRoot)
    if ($resolvedCaseRoot.StartsWith($resolvedTestRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedCaseRoot -PathType Container)) {
        Remove-Item -LiteralPath $resolvedCaseRoot -Recurse -Force
    }
}

$result | Format-List
