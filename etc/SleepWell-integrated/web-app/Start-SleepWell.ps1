param(
    [int]$Port = 8765,
    [string]$ForecastModel = '',
    [string]$MesaInventory = ''
)
$ErrorActionPreference = 'Stop'
$serverArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'server.py'), '--port', "$Port")
if ($ForecastModel) { $serverArguments += @('--forecast-model', $ForecastModel) }
if ($MesaInventory) { $serverArguments += @('--mesa-inventory', $MesaInventory) }
Write-Host "SleepWell: http://127.0.0.1:$Port (종료: Ctrl+C)"
& python @serverArguments
exit $LASTEXITCODE
