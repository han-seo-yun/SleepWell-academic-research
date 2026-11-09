[CmdletBinding()]
param(
    [switch]$NoShortcut,
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectDir '.venv'
$PythonExe = Join-Path $VenvDir 'Scripts\python.exe'

function Find-Python312 {
    $Candidates = @(
        @{ File = 'py'; Args = @('-3.12') },
        @{ File = 'python'; Args = @() }
    )
    foreach ($Candidate in $Candidates) {
        try {
            $Version = & $Candidate.File @($Candidate.Args) -c "import struct,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize('P')*8}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $Version -eq '3.12:64') { return $Candidate }
        } catch { }
    }
    throw 'Python 3.12 (64-bit)을 찾지 못했습니다. python.org에서 64-bit Python 3.12를 설치한 뒤 다시 실행하세요.'
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "명령 실행 실패 (종료 코드 $LASTEXITCODE): $FilePath"
    }
}

if ($Recreate -and (Test-Path -LiteralPath $VenvDir)) {
    $ResolvedProject = [IO.Path]::GetFullPath($ProjectDir)
    $ResolvedVenv = [IO.Path]::GetFullPath($VenvDir)
    if (-not $ResolvedVenv.StartsWith($ResolvedProject + [IO.Path]::DirectorySeparatorChar)) {
        throw '안전 검증 실패: 가상환경 경로가 프로젝트 폴더 밖입니다.'
    }
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    $Python = Find-Python312
    Write-Host '[1/5] 프로젝트 전용 Python 3.12 (64-bit) 환경을 만듭니다.'
    $CreateArgs = @($Python.Args) + @('-m', 'venv', $VenvDir)
    Invoke-Checked -FilePath $Python.File -Arguments $CreateArgs
}

Write-Host '[2/5] 설치 도구를 갱신합니다.'
Invoke-Checked -FilePath $PythonExe -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip')
Write-Host '[3/5] CPU 기본 기능의 고정 버전 의존성을 설치합니다.'
Invoke-Checked -FilePath $PythonExe -Arguments @('-m', 'pip', 'install', '--requirement', (Join-Path $ProjectDir 'requirements.txt'))
Invoke-Checked -FilePath $PythonExe -Arguments @('-m', 'pip', 'check')
Write-Host '[4/5] Tk·한글 PDF 글꼴·CPU 모델·저장소를 진단합니다.'
Invoke-Checked -FilePath $PythonExe -Arguments @('-X', 'utf8', (Join-Path $ProjectDir 'tools\diagnose_upgrade.py'))

if (-not $NoShortcut) {
    Write-Host '[5/5] 바탕 화면 바로가기를 만듭니다.'
    $Desktop = [Environment]::GetFolderPath('Desktop')
    $ShortcutPath = Join-Path $Desktop 'SleepWell Academic Workbench.lnk'
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $ProjectDir 'launch.cmd'
    $Shortcut.WorkingDirectory = $ProjectDir
    $Shortcut.Description = 'SleepWell 학술제 전용 수리·데이터·융합 모델 실험 도구'
    $Shortcut.Save()
} else {
    Write-Host '[5/5] 바로가기 생성을 건너뜁니다.'
}

Write-Host '설치가 완료되었습니다. launch.cmd 또는 바탕 화면 바로가기로 실행하세요.'
Write-Host '선택형 로컬 임베딩은 install_optional_ai.cmd를 별도로 실행해야 합니다.'
