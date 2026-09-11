$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Gui = Join-Path $AppRoot 'src\ui2\default_app.py'
$Bootstrap = Join-Path $AppRoot 'runtime_bootstrap.ps1'

if (-not (Test-Path -LiteralPath $Gui -PathType Leaf)) { throw 'src/ui2/default_app.py not found.' }
if (-not (Test-Path -LiteralPath $Bootstrap -PathType Leaf)) { throw 'runtime_bootstrap.ps1 not found.' }

. $Bootstrap
try {
    $Python = Ensure-QmlRuntime $AppRoot
    $PythonWindowed = Get-QmlWindowedPython $Python
    Start-Process -FilePath $PythonWindowed -ArgumentList @($Gui) -WorkingDirectory $AppRoot
} catch {
    Add-Content -LiteralPath (Join-Path $AppRoot 'logs\runtime_bootstrap.log') -Value ('launcher_failed ' + $_.Exception.Message) -Encoding UTF8
    Write-Host ('QML startup failed: ' + $_.Exception.Message)
    Write-Host ('See logs\runtime_bootstrap.log for details.')
    exit 1
}
