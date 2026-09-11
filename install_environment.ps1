$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bootstrap = Join-Path $AppRoot 'runtime_bootstrap.ps1'
if (-not (Test-Path -LiteralPath $Bootstrap -PathType Leaf)) { throw 'runtime_bootstrap.ps1 not found.' }
. $Bootstrap
try {
    $Python = Ensure-QmlRuntime $AppRoot
    Write-Host ('Environment ready: ' + $Python)
    & (Join-Path $AppRoot 'start.bat')
} catch {
    Write-Host ('Environment setup failed: ' + $_.Exception.Message)
    Write-Host ('See logs\runtime_bootstrap.log for details.')
    Read-Host 'Press Enter to exit'
    exit 1
}
