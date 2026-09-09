param(
    [string]$AppRoot = '',
    [string]$PackageRoot = ''
)

$ErrorActionPreference = 'Stop'

function Resolve-FullPath([string]$PathValue) {
    return [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PathValue).Path)
}

function Is-UnderRoot([string]$Candidate, [string]$Root) {
    $rootWithSlash = $Root.TrimEnd('\') + '\'
    return $Candidate.Equals($Root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or
        $Candidate.StartsWith($rootWithSlash, [StringComparison]::OrdinalIgnoreCase)
}

function Normalize-RelativePath([string]$Value) {
    $relative = ([string]$Value).Replace('/', '\').Trim()
    if (-not $relative -or [IO.Path]::IsPathRooted($relative) -or
        $relative.Contains('..') -or $relative.Contains(':')) {
        throw "Invalid update path: $Value"
    }
    return $relative
}

function Get-AllowedPath([string]$Relative) {
    $top = $Relative.Split('\')[0].ToLowerInvariant()
    if ($top -in @('src', 'assets', 'lib')) { return $true }
    return $Relative -in @(
        'VERSION.txt', 'BUILD_ID.txt', 'requirements.txt', 'requirements-v2.txt',
        'monitor_gui.ps1', 'update.ps1'
    )
}

function Get-FileSha256([string]$PathValue) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($PathValue)
        try {
            $digest = $sha256.ComputeHash($stream)
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
    return ([BitConverter]::ToString($digest) -replace '-', '').ToUpperInvariant()
}

function Write-Message([string]$Message) {
    Write-Host ("[code-update] " + $Message)
}

if (-not $PackageRoot) { $PackageRoot = $PSScriptRoot }
$packageFull = Resolve-FullPath $PackageRoot
$manifestPath = Join-Path $packageFull 'manifest.json'
$payloadRoot = Join-Path $packageFull 'payload'
if (-not (Test-Path -LiteralPath $manifestPath) -or
    -not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
    throw 'Update package is incomplete: manifest.json or payload is missing.'
}

if (-not $AppRoot) {
    if (Test-Path -LiteralPath (Join-Path $packageFull 'data') -PathType Container) {
        $AppRoot = $packageFull
    } elseif (Test-Path -LiteralPath (Join-Path (Split-Path $packageFull -Parent) 'data') -PathType Container) {
        $AppRoot = Split-Path $packageFull -Parent
    } else {
        $AppRoot = $packageFull
    }
}
$appFull = Resolve-FullPath $AppRoot

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$version = [string]$manifest.version
if (-not $version -or -not $manifest.files) { throw 'Update manifest is invalid.' }

$fileEntries = @()
$seen = @{}
foreach ($rawEntry in @($manifest.files)) {
    $relative = Normalize-RelativePath ([string]$rawEntry.path)
    if (-not (Get-AllowedPath $relative)) { throw "Protected path in update package: $relative" }
    $key = $relative.ToLowerInvariant()
    if ($seen.ContainsKey($key)) { throw "Duplicate update path: $relative" }
    $seen[$key] = $true
    $source = Join-Path $payloadRoot $relative
    $sourceFull = [IO.Path]::GetFullPath($source)
    if (-not (Is-UnderRoot $sourceFull ([IO.Path]::GetFullPath($payloadRoot)))) {
        throw "Update source escapes payload: $relative"
    }
    if (-not (Test-Path -LiteralPath $sourceFull -PathType Leaf)) {
        throw "Update file is missing: $relative"
    }
    $expectedHash = ([string]$rawEntry.sha256).ToUpperInvariant()
    if (-not $expectedHash -or (Get-FileSha256 $sourceFull) -ne $expectedHash) {
        throw "Update hash check failed: $relative"
    }
    $fileEntries += [PSCustomObject]@{
        Path = $relative
        Source = $sourceFull
        Hash = $expectedHash
    }
}

$guiProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and
    $_.CommandLine -match 'src[\\/]+ui2[\\/]+default_app\.py' -and
    $_.CommandLine -match [Regex]::Escape($appFull)
})
if ($guiProcesses.Count -gt 0) {
    throw 'The GUI is still running. Close the application and run the update again.'
}

$oldManifestPath = Join-Path $appFull 'code_update_manifest.json'
$oldEntries = @()
if (Test-Path -LiteralPath $oldManifestPath -PathType Leaf) {
    try {
        $oldManifest = Get-Content -LiteralPath $oldManifestPath -Raw | ConvertFrom-Json
        $oldEntries = @($oldManifest.files)
    } catch {
        throw 'The installed code manifest is invalid. Update was stopped for safety.'
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $appFull (Join-Path 'code_backups' $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$backupEntries = @()
$allPaths = @($fileEntries.Path) + @($oldEntries | ForEach-Object { [string]$_.path }) + 'code_update_manifest.json'
foreach ($rawPath in $allPaths) {
    if (-not $rawPath) { continue }
    $relative = Normalize-RelativePath $rawPath
    if ($relative -ne 'code_update_manifest.json' -and -not (Get-AllowedPath $relative)) {
        continue
    }
    $target = [IO.Path]::GetFullPath((Join-Path $appFull $relative))
    if (-not (Is-UnderRoot $target $appFull)) { throw "Target escapes application root: $relative" }
    $existed = Test-Path -LiteralPath $target -PathType Leaf
    $backupPath = Join-Path $backupRoot $relative
    if ($existed) {
        New-Item -ItemType Directory -Path (Split-Path $backupPath -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backupPath -Force
    }
    $backupEntries += [PSCustomObject]@{
        Path = $relative
        Target = $target
        Backup = $backupPath
        Existed = $existed
    }
}

$newKeys = @{}
foreach ($entry in $fileEntries) { $newKeys[$entry.Path.ToLowerInvariant()] = $true }
try {
    foreach ($entry in $fileEntries) {
        $target = [IO.Path]::GetFullPath((Join-Path $appFull $entry.Path))
        if (-not (Is-UnderRoot $target $appFull)) { throw "Target escapes application root: $($entry.Path)" }
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $entry.Source -Destination $target -Force
    }

    foreach ($oldEntry in $oldEntries) {
        $relative = Normalize-RelativePath ([string]$oldEntry.path)
        if (-not $newKeys.ContainsKey($relative.ToLowerInvariant()) -and
            (Get-AllowedPath $relative)) {
            $target = [IO.Path]::GetFullPath((Join-Path $appFull $relative))
            if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Is-UnderRoot $target $appFull)) {
                Remove-Item -LiteralPath $target -Force
            }
        }
    }

    $installed = [ordered]@{
        format = 1
        version = $version
        installed_at = (Get-Date).ToString('o')
        files = @($fileEntries | ForEach-Object {
            [ordered]@{ path = $_.Path.Replace('\', '/'); sha256 = $_.Hash }
        })
    }
    $installed | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $oldManifestPath -Encoding UTF8

    foreach ($entry in $fileEntries) {
        $target = [IO.Path]::GetFullPath((Join-Path $appFull $entry.Path))
        if ((Get-FileSha256 $target) -ne $entry.Hash) {
            throw "Installed hash check failed: $($entry.Path)"
        }
    }
    Write-Message ("Update completed: version " + $version)
    Write-Message ("Code backup: " + $backupRoot)
    Write-Message 'Personal data is preserved; restart will reload existing accounts and tasks.'
} catch {
    Write-Message 'Update failed; restoring the previous code files.'
    foreach ($entry in $backupEntries) {
        if ($entry.Existed) {
            if (Test-Path -LiteralPath $entry.Backup -PathType Leaf) {
                New-Item -ItemType Directory -Path (Split-Path $entry.Target -Parent) -Force | Out-Null
                Copy-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force
            }
        } elseif (Test-Path -LiteralPath $entry.Target -PathType Leaf) {
            Remove-Item -LiteralPath $entry.Target -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
