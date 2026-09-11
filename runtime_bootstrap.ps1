# ASCII-only runtime bootstrap for Windows PowerShell 5.1.
# It repairs or creates a Python environment for the QML UI without touching
# the existing data, config, logs, or legacy .venv contents.

$script:RuntimeBootstrapLog = $null

function Initialize-RuntimeBootstrap([string]$AppRoot) {
    $logDir = Join-Path $AppRoot 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $script:RuntimeBootstrapLog = Join-Path $logDir 'runtime_bootstrap.log'
    Write-RuntimeBootstrapLog ('bootstrap_start root=' + $AppRoot)
}

function Write-RuntimeBootstrapLog([string]$Message) {
    if (-not $script:RuntimeBootstrapLog) { return }
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Message
    Add-Content -LiteralPath $script:RuntimeBootstrapLog -Value $line -Encoding UTF8
}

function Invoke-RuntimeCommand([string]$Python, [string[]]$Arguments) {
    try {
        $output = @(& $Python @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) {
            Write-RuntimeBootstrapLog ([string]$line)
        }
        return $exitCode
    } catch {
        Write-RuntimeBootstrapLog ('command_failed error=' + $_.Exception.Message)
        return 1
    }
}

function Test-PySide6([string]$Python) {
    if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    & $Python -c 'import PySide6' *> $null
    return $LASTEXITCODE -eq 0
}

function Test-Pip([string]$Python) {
    if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    & $Python -m pip --version *> $null
    return $LASTEXITCODE -eq 0
}

function Install-QmlRequirements([string]$Python, [string]$AppRoot) {
    if (Test-PySide6 $Python) { return $true }

    if (-not (Test-Pip $Python)) {
        Write-RuntimeBootstrapLog ('pip_missing python=' + $Python)
        $ensureCode = Invoke-RuntimeCommand $Python @('-m', 'ensurepip', '--upgrade')
        Write-RuntimeBootstrapLog ('ensurepip_exit=' + $ensureCode)
    }
    if (-not (Test-Pip $Python)) {
        Write-RuntimeBootstrapLog 'pip_unavailable_after_ensurepip'
        return $false
    }

    $requirements = Join-Path $AppRoot 'requirements.txt'
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        Write-RuntimeBootstrapLog ('requirements_missing path=' + $requirements)
        return $false
    }

    Write-RuntimeBootstrapLog ('install_start python=' + $Python + ' requirements=' + $requirements)
    $installCode = Invoke-RuntimeCommand $Python @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '--timeout', '120', '-r', $requirements
    )
    Write-RuntimeBootstrapLog ('install_exit=' + $installCode)
    return $installCode -eq 0 -and (Test-PySide6 $Python)
}

function Get-SystemPythonPath {
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($version in @('-3.11', '-3')) {
            $result = @(& $pyLauncher.Source $version -c 'import sys; print(sys.executable)' 2>$null)
            if ($LASTEXITCODE -eq 0) {
                $candidate = ($result | Where-Object { $_ -and (Test-Path -LiteralPath ([string]$_).Trim() -PathType Leaf) } | Select-Object -Last 1)
                if ($candidate) { return ([string]$candidate).Trim() }
            }
        }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $result = @(& $pythonCommand.Source -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $candidate = ($result | Select-Object -Last 1)
            if ($candidate -and (Test-Path -LiteralPath ([string]$candidate).Trim() -PathType Leaf)) {
                return ([string]$candidate).Trim()
            }
        }
    }
    return $null
}

function Ensure-QmlRuntime([string]$AppRoot) {
    $root = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $AppRoot).Path)
    Initialize-RuntimeBootstrap $root

    $legacyVenvPython = Join-Path $root '.venv\Scripts\python.exe'
    $managedVenvPython = Join-Path $root '.runtime\Scripts\python.exe'
    $bundledPython = Join-Path $root 'runtime\python.exe'

    foreach ($candidate in @($legacyVenvPython, $managedVenvPython, $bundledPython)) {
        if (Test-PySide6 $candidate) {
            Write-RuntimeBootstrapLog ('runtime_ready python=' + $candidate)
            return $candidate
        }
    }

    if (Test-Path -LiteralPath $legacyVenvPython -PathType Leaf) {
        Write-RuntimeBootstrapLog 'repair_existing_venv=true'
        if (Install-QmlRequirements $legacyVenvPython $root) {
            Write-RuntimeBootstrapLog ('runtime_repaired python=' + $legacyVenvPython)
            return $legacyVenvPython
        }
    }

    if (Test-Path -LiteralPath $managedVenvPython -PathType Leaf) {
        Write-RuntimeBootstrapLog 'repair_existing_managed_runtime=true'
        if (Install-QmlRequirements $managedVenvPython $root) {
            Write-RuntimeBootstrapLog ('runtime_repaired python=' + $managedVenvPython)
            return $managedVenvPython
        }
    }

    $systemPython = Get-SystemPythonPath
    if (-not $systemPython) {
        throw 'No usable Python runtime was found. Install Python 3.11 or newer.'
    }
    if (Test-PySide6 $systemPython) {
        Write-RuntimeBootstrapLog ('runtime_ready system_python=' + $systemPython)
        return $systemPython
    }

    $managedRoot = Join-Path $root '.runtime'
    if (-not (Test-Path -LiteralPath $managedVenvPython -PathType Leaf)) {
        New-Item -ItemType Directory -Path $managedRoot -Force | Out-Null
        Write-RuntimeBootstrapLog ('create_managed_runtime python=' + $systemPython)
        $venvCode = Invoke-RuntimeCommand $systemPython @('-m', 'venv', $managedRoot)
        if ($venvCode -ne 0 -or -not (Test-Path -LiteralPath $managedVenvPython -PathType Leaf)) {
            throw 'Failed to create the managed Python runtime.'
        }
    }
    if (Install-QmlRequirements $managedVenvPython $root) {
        Write-RuntimeBootstrapLog ('runtime_ready managed_python=' + $managedVenvPython)
        return $managedVenvPython
    }
    throw 'PySide6 installation failed. Check logs/runtime_bootstrap.log and network access.'
}

function Get-QmlWindowedPython([string]$Python) {
    if ($Python -and $Python.EndsWith('\python.exe', [StringComparison]::OrdinalIgnoreCase)) {
        $pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
        if (Test-Path -LiteralPath $pythonw -PathType Leaf) { return $pythonw }
    }
    return $Python
}
