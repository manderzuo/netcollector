param(
    [string]$SourceRoot = '',
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'

if (-not $SourceRoot) { $SourceRoot = Split-Path $PSScriptRoot -Parent }
if (-not $OutputDir) { $OutputDir = Join-Path $SourceRoot 'temp' }
$sourceFull = (Resolve-Path -LiteralPath $SourceRoot).Path
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$outputFull = (Resolve-Path -LiteralPath $OutputDir).Path

$versionPath = Join-Path $sourceFull 'VERSION.txt'
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) { throw 'VERSION.txt is missing.' }
$version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
if (-not $version) { throw 'VERSION.txt is empty.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stage = Join-Path $outputFull ("code-update-v{0}-{1}" -f $version, $stamp)
$payload = Join-Path $stage 'payload'
New-Item -ItemType Directory -Path $payload -Force | Out-Null

foreach ($rootName in @('src', 'assets', 'lib')) {
    $sourcePath = Join-Path $sourceFull $rootName
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
        throw "Required code directory is missing: $rootName"
    }
    $targetPath = Join-Path $payload $rootName
    New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
    Copy-Item -Path (Join-Path $sourcePath '*') -Destination $targetPath -Recurse -Force
    Get-ChildItem -LiteralPath $targetPath -Recurse -Force -File | Where-Object { $_.Extension -in @('.pyc', '.pyo') -or $_.DirectoryName -like '*\__pycache__*' } | Remove-Item -Force
    Get-ChildItem -LiteralPath $targetPath -Recurse -Force -Directory -Filter '__pycache__' | Sort-Object FullName -Descending | Remove-Item -Recurse -Force
}

foreach ($fileName in @(
    'VERSION.txt', 'BUILD_ID.txt', 'requirements.txt', 'requirements-v2.txt',
    'monitor_gui.ps1', 'update.ps1', 'runtime_bootstrap.ps1', 'launcher.ps1',
    'start.bat', 'install_environment.ps1'
)) {
    $sourcePath = Join-Path $sourceFull $fileName
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        Copy-Item -LiteralPath $sourcePath -Destination $payload -Force
    }
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'apply_code_update.ps1') -Destination $stage -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'apply_code_update.bat') -Destination $stage -Force

$fileRecords = @()
foreach ($file in Get-ChildItem -LiteralPath $payload -Recurse -File) {
    $relative = $file.FullName.Substring($payload.Length).TrimStart('\', '/').Replace('\', '/')
    $fileRecords += [ordered]@{
        path = $relative
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
        bytes = [int64]$file.Length
    }
}
$manifest = [ordered]@{
    format = 1
    product = 'collector-workbench'
    version = $version
    generated_at = (Get-Date).ToString('o')
    files = @($fileRecords | Sort-Object path)
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $stage 'manifest.json') -Encoding UTF8

$zipPath = Join-Path $outputFull ("collector-code-update-v{0}-{1}.zip" -f $version, $stamp)
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host ("PACKAGE=" + $zipPath)
Write-Host ("STAGE=" + $stage)
