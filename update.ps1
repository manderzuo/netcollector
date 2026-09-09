param(
    [switch]$Force,
    [int]$WaitForPid = 0,
    [switch]$Restart,
    [switch]$Quiet,
    [string]$AppRoot = ''
)

# This script intentionally uses ASCII-only PowerShell syntax. Windows PowerShell
# 5.1 may read a UTF-8 script with the machine code page; keeping the parser
# ASCII-only prevents the old Chinese-encoding failure from disabling updates.
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$UpdaterRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $AppRoot) { $AppRoot = $UpdaterRoot }
if (-not (Test-Path -LiteralPath $AppRoot -PathType Container)) {
    throw 'Application root does not exist.'
}
$AppRoot = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $AppRoot).Path)
$ConfigPath = Join-Path $AppRoot 'config\update.json'
$VersionPath = Join-Path $AppRoot 'VERSION.txt'
$BuildPath = Join-Path $AppRoot 'BUILD_ID.txt'
$RuntimeRoot = Join-Path $AppRoot 'runtime'
if (-not (Test-Path -LiteralPath $VersionPath -PathType Leaf)) {
    $runtimeVersionPath = Join-Path $RuntimeRoot 'VERSION.txt'
    if (Test-Path -LiteralPath $runtimeVersionPath -PathType Leaf) { $VersionPath = $runtimeVersionPath }
}
if (-not (Test-Path -LiteralPath $BuildPath -PathType Leaf)) {
    $runtimeBuildPath = Join-Path $RuntimeRoot 'BUILD_ID.txt'
    if (Test-Path -LiteralPath $runtimeBuildPath -PathType Leaf) { $BuildPath = $runtimeBuildPath }
}
$LogPath = Join-Path $AppRoot 'logs\update.log'
$UpdateMarkerPath = Join-Path $AppRoot '.update_pending'
$DefaultManifestUrl = 'https://www.gemstory.cn/release/latest.json'

function Decode-Text([string]$Encoded) {
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
}

function Write-UpdateLog([string]$Message) {
    try {
        $logDir = Split-Path -Parent $LogPath
        if (-not (Test-Path -LiteralPath $logDir -PathType Container)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') + ' ' + $Message) -Encoding UTF8
    } catch { }
}

function Read-TextFile([string]$PathValue, [string]$Fallback) {
    if (Test-Path -LiteralPath $PathValue -PathType Leaf) {
        $value = (Get-Content -LiteralPath $PathValue -Raw).Trim()
        if ($value) { return $value }
    }
    return $Fallback
}

function Get-LocalVersion { return Read-TextFile $VersionPath '0.0.0' }
function Get-LocalBuild { return Read-TextFile $BuildPath '' }

function Get-VersionParts([string]$Value) {
    $matches = [regex]::Matches([string]$Value, '\d+')
    if ($matches.Count -eq 0) { throw "Invalid version: $Value" }
    return @($matches | ForEach-Object { [int]$_.Value })
}

function Compare-Version([string]$Left, [string]$Right) {
    $a = @(Get-VersionParts $Left)
    $b = @(Get-VersionParts $Right)
    $width = [Math]::Max($a.Count, $b.Count)
    for ($i = 0; $i -lt $width; $i++) {
        $av = if ($i -lt $a.Count) { $a[$i] } else { 0 }
        $bv = if ($i -lt $b.Count) { $b[$i] } else { 0 }
        if ($av -gt $bv) { return 1 }
        if ($av -lt $bv) { return -1 }
    }
    return 0
}

function Add-CacheBuster([string]$UrlValue) {
    $separator = if ($UrlValue.Contains('?')) { '&' } else { '?' }
    return $UrlValue + $separator + '_client_check=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Get-UpdateManifest([string]$UrlValue) {
    $url = ([string]$UrlValue).Trim()
    if (-not $url) { $url = $DefaultManifestUrl }
    if (-not ($url -match '^https?://')) { throw 'Invalid update manifest URL.' }
    $requestUrl = Add-CacheBuster $url
    Write-UpdateLog ("manifest_request url=" + $url)
    $response = Invoke-WebRequest -UseBasicParsing -Uri $requestUrl -TimeoutSec 15 -Headers @{ 'Cache-Control' = 'no-cache' }
    $manifest = $response.Content | ConvertFrom-Json
    if (-not $manifest.version -or -not $manifest.download_url -or -not $manifest.sha256) {
        throw 'Update manifest is incomplete.'
    }
    [void](Get-VersionParts ([string]$manifest.version))
    if (-not ([string]$manifest.download_url -match '^https?://')) { throw 'Invalid package URL.' }
    if (-not ([string]$manifest.sha256 -match '^[0-9a-fA-F]{64}$')) { throw 'Invalid package SHA256.' }
    Write-UpdateLog ("manifest_response version=" + [string]$manifest.version + " build=" + [string]$manifest.build_id)
    return $manifest
}

function Test-UpdateAvailable($Manifest, [string]$CurrentVersion, [string]$CurrentBuild) {
    $versionResult = Compare-Version ([string]$Manifest.version) $CurrentVersion
    if ($versionResult -gt 0) { return $true }
    if ($versionResult -lt 0) { return $false }
    $remoteBuild = ([string]($Manifest.build_id | ForEach-Object { $_ })).Trim()
    return ($remoteBuild -and $remoteBuild -ne ([string]$CurrentBuild).Trim())
}

function Show-UpdatePrompt([string]$CurrentVersion, [string]$LatestVersion, [string]$Reason) {
    if ($Quiet) { return $true }
    try {
        Add-Type -AssemblyName PresentationFramework
        $message = (Decode-Text '5Y+R546w5pu05paw') + ': ' + $LatestVersion + "`n" +
            (Decode-Text '5b2T5YmN54mI5pys') + ': ' + $CurrentVersion + "`n" + $Reason + "`n`n" +
            (Decode-Text '5piv5ZCm56uL5Y2z5pu05paw77yf')
        return [System.Windows.MessageBox]::Show($message, (Decode-Text '5aSa5bmz5Y+w6YeH6ZuG5bel5L2c5Y+w5pu05paw'), 'YesNo', 'Information') -eq [System.Windows.MessageBoxResult]::Yes
    } catch {
        Write-UpdateLog ("prompt_failed error=" + $_.Exception.Message)
        return $false
    }
}

function Wait-ForProcessExit([int]$ProcessId) {
    if ($ProcessId -le 0) { return }
    Write-UpdateLog ("wait_for_pid pid=" + $ProcessId)
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        throw 'The application did not exit before the update timeout.'
    }
}

function Get-FileSha256([string]$PathValue) {
    return (Get-FileHash -LiteralPath $PathValue -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Update-Application($Manifest) {
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('collector-update-' + [guid]::NewGuid().ToString('N'))
    $zipPath = Join-Path $tempRoot 'package.zip'
    $extractPath = Join-Path $tempRoot 'package'
    $backupPath = Join-Path $tempRoot 'backup'
    $managed = @()
    try {
        Write-UpdateLog ("download_start version=" + [string]$Manifest.version + " build=" + [string]$Manifest.build_id)
        New-Item -ItemType Directory -Path $extractPath, $backupPath -Force | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri ([string]$Manifest.download_url) -OutFile $zipPath -TimeoutSec 120
        $actualHash = Get-FileSha256 $zipPath
        $expectedHash = ([string]$Manifest.sha256).ToUpperInvariant()
        if ($actualHash -ne $expectedHash) { throw 'Package SHA256 verification failed.' }
        Write-UpdateLog ("download_verified sha256=" + $actualHash)
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
        $bundleExe = Join-Path $extractPath '多平台采集工作台.exe'
        $bundleRuntime = Join-Path $extractPath 'runtime'
        $sourceEntry = Join-Path $extractPath 'src\ui2\default_app.py'
        $sourceVersion = Join-Path $extractPath 'VERSION.txt'
        $isBundlePackage = (Test-Path -LiteralPath $bundleExe -PathType Leaf) -and
            (Test-Path -LiteralPath $bundleRuntime -PathType Container)
        if ($isBundlePackage) {
            $bundleVersion = Join-Path $bundleRuntime 'VERSION.txt'
            if (-not (Test-Path -LiteralPath $bundleVersion -PathType Leaf)) {
                $bundleVersion = $sourceVersion
            }
            if (-not (Test-Path -LiteralPath $bundleVersion -PathType Leaf)) {
                throw 'Bundled update package is missing VERSION.txt.'
            }
        } elseif (-not (Test-Path -LiteralPath $sourceEntry -PathType Leaf) -or
            -not (Test-Path -LiteralPath $sourceVersion -PathType Leaf)) {
            throw 'Update package structure is incomplete.'
        }
        Write-UpdateLog ("package_shape=" + $(if ($isBundlePackage) { 'bundled' } else { 'source' }))

        # User data, local configuration and the virtual environment are never replaced.
        $preserve = if ($isBundlePackage) { @('data', 'logs') } else { @('data', 'config', '.venv', 'logs') }
        $preservedExisting = @($preserve | Where-Object {
            Test-Path -LiteralPath (Join-Path $AppRoot $_)
        })
        $preservedText = $preservedExisting -join ','
        if (-not $preservedText) { $preservedText = 'none' }
        Write-UpdateLog ("preserve_paths=" + $preservedText)
        $managed = @(Get-ChildItem -LiteralPath $extractPath -Force | Where-Object { $preserve -notcontains $_.Name })
        foreach ($item in $managed) {
            $current = Join-Path $AppRoot $item.Name
            if (Test-Path -LiteralPath $current) { Copy-Item -LiteralPath $current -Destination $backupPath -Recurse -Force }
        }
        foreach ($item in $managed) {
            Copy-Item -LiteralPath $item.FullName -Destination $AppRoot -Recurse -Force
        }

        $installedVersion = Read-TextFile $VersionPath ''
        if ($installedVersion -ne ([string]$Manifest.version).Trim()) {
            throw ("Version verification failed: expected " + [string]$Manifest.version + ', got ' + $installedVersion)
        }
        $remoteBuild = ([string]$Manifest.build_id).Trim()
        if ($remoteBuild -and (Read-TextFile $BuildPath '') -ne $remoteBuild) {
            throw 'Build verification failed.'
        }
        foreach ($preservedName in $preservedExisting) {
            if (-not (Test-Path -LiteralPath (Join-Path $AppRoot $preservedName))) {
                throw ("Protected user data path disappeared: " + $preservedName)
            }
        }
        Write-UpdateLog 'protected_data_check=passed; restart_will_reload_existing_accounts_and_tasks'
        Write-UpdateLog ("update_completed version=" + $installedVersion + " build=" + (Read-TextFile $BuildPath ''))
        if (-not $Quiet) {
            try {
                Add-Type -AssemblyName PresentationFramework
                [System.Windows.MessageBox]::Show((Decode-Text '5pu05paw5a6M5oiQ77yM6L2v5Lu25bCG6YeN5paw5ZCv5Yqo44CC'), (Decode-Text '5pu05paw5a6M5oiQ'), 'OK', 'Information') | Out-Null
            } catch { }
        }

        if ($Restart) {
            $launcher = Join-Path $AppRoot 'launcher.ps1'
            $bundledExe = Join-Path $AppRoot '多平台采集工作台.exe'
            if (Test-Path -LiteralPath $launcher -PathType Leaf) {
                Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launcher) -WorkingDirectory $AppRoot
            } elseif (Test-Path -LiteralPath $bundledExe -PathType Leaf) {
                Start-Process -FilePath $bundledExe -WorkingDirectory $AppRoot
            } else {
                Write-UpdateLog 'restart_skipped launcher_missing'
            }
        }
    } catch {
        Write-UpdateLog ("update_failed error=" + $_.Exception.Message)
        foreach ($item in $managed) {
            $target = Join-Path $AppRoot $item.Name
            $backup = Join-Path $backupPath $item.Name
            if (Test-Path -LiteralPath $backup) {
                if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
                Copy-Item -LiteralPath $backup -Destination $AppRoot -Recurse -Force -ErrorAction SilentlyContinue
            } elseif (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not $Quiet) {
            try {
                Add-Type -AssemblyName PresentationFramework
                [System.Windows.MessageBox]::Show(((Decode-Text '5pu05paw5aSx6LSl77yM5pyq5L+u5pS55Liq5Lq65pWw5o2u44CC') + "`n`n" + $_.Exception.Message), (Decode-Text '5pu05paw5aSx6LSl'), 'OK', 'Warning') | Out-Null
            } catch { }
        }
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

try {
    Write-UpdateLog ("check_start root=" + $AppRoot + " force=" + [bool]$Force)
    $config = $null
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        try { $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json } catch { Write-UpdateLog 'config_invalid_using_default' }
    }
    if (-not $Force -and $config -and $config.check_on_start -eq $false) {
        Write-UpdateLog 'check_disabled'
        return
    }
    $manifestUrl = if ($config -and $config.manifest_url) { [string]$config.manifest_url } else { $DefaultManifestUrl }
    $currentVersion = Get-LocalVersion
    $currentBuild = Get-LocalBuild
    $manifest = Get-UpdateManifest $manifestUrl
    $available = Test-UpdateAvailable $manifest $currentVersion $currentBuild
    Write-UpdateLog ("compare current_version=" + $currentVersion + " current_build=" + $currentBuild + " available=" + [bool]$available)
    if ($available) {
        $reason = if ((Compare-Version ([string]$manifest.version) $currentVersion) -gt 0) { (Decode-Text '5paw54mI5pys') } else { (Decode-Text '5ZCM54mI5pys5L+u5aSN5YyF') }
        if ($Force -or (Show-UpdatePrompt $currentVersion ([string]$manifest.version) $reason)) {
            Wait-ForProcessExit $WaitForPid
            Update-Application $manifest
        } else {
            Write-UpdateLog 'update_declined'
        }
    } else {
        Write-UpdateLog 'already_latest'
    }
} catch {
    # Update failures must not prevent the application from starting.
    Write-UpdateLog ("check_failed error=" + $_.Exception.GetType().FullName + ': ' + $_.Exception.Message)
}
if (Test-Path -LiteralPath $UpdateMarkerPath -PathType Leaf) {
    Remove-Item -LiteralPath $UpdateMarkerPath -Force -ErrorAction SilentlyContinue
}
