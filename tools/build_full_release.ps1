param(
    [string]$SourceRoot = '',
    [string]$BaselineDir = '',
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'

if (-not $SourceRoot) { $SourceRoot = Split-Path $PSScriptRoot -Parent }
if (-not $OutputDir) { $OutputDir = Join-Path $SourceRoot 'temp\release_v2.1.1' }
if (-not $BaselineDir) {
    $BaselineDir = Join-Path $SourceRoot 'temp\release_v2.1\clean-release-v2.1-20260903-132028'
}

$sourceFull = (Resolve-Path -LiteralPath $SourceRoot).Path
$baselineFull = (Resolve-Path -LiteralPath $BaselineDir).Path
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$outputFull = (Resolve-Path -LiteralPath $OutputDir).Path

$versionPath = Join-Path $sourceFull 'VERSION.txt'
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) { throw 'VERSION.txt is missing.' }
$version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
if (-not $version) { throw 'VERSION.txt is empty.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stage = Join-Path $outputFull ("clean-release-v{0}-{1}" -f $version, $stamp)
New-Item -ItemType Directory -Path $stage -Force | Out-Null

# 以已经验收过的干净发布骨架提供安装/启动脚本，再覆盖当前源码与资源。
Copy-Item -Path (Join-Path $baselineFull '*') -Destination $stage -Recurse -Force
foreach ($rootName in @('src', 'assets', 'lib')) {
    $sourcePath = Join-Path $sourceFull $rootName
    $targetPath = Join-Path $stage $rootName
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
        throw "Required release directory is missing: $rootName"
    }
    New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
    Copy-Item -Path (Join-Path $sourcePath '*') -Destination $targetPath -Recurse -Force
}

foreach ($fileName in @(
    'README.md', 'VERSION.txt', 'BUILD_ID.txt', 'requirements.txt', 'requirements-v2.txt',
    'monitor_gui.ps1', 'update.ps1', 'config\update.json'
)) {
    $sourcePath = Join-Path $sourceFull $fileName
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $targetFile = Join-Path $stage $fileName
        New-Item -ItemType Directory -Path (Split-Path $targetFile -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $targetFile -Force
    }
}

# Locate the versioned notes by an ASCII wildcard. This keeps the build script
# usable on Windows PowerShell machines that do not decode UTF-8 filenames.
$notesDir = Join-Path $sourceFull 'docs'
$notesSource = Get-ChildItem -LiteralPath $notesDir -File -Filter ('*-v' + $version + '.md') |
    Select-Object -First 1
if ($notesSource) {
    $notesTargetDir = Join-Path $stage 'docs'
    New-Item -ItemType Directory -Path $notesTargetDir -Force | Out-Null
    Copy-Item -LiteralPath $notesSource.FullName -Destination (Join-Path $notesTargetDir $notesSource.Name) -Force
}

foreach ($fileName in @('apply_code_update.ps1', 'apply_code_update.bat')) {
    $sourcePath = Join-Path $PSScriptRoot $fileName
    Copy-Item -LiteralPath $sourcePath -Destination $stage -Force
}

# The portable updater also ships with the formal package. Identify its folder
# by its update script instead of embedding a locale-dependent folder name.
$portableUpdaterDir = Get-ChildItem -LiteralPath $sourceFull -Directory |
    Where-Object {
        $_.Name -notin @('src', 'assets', 'lib', 'config', 'data', 'logs', 'tests', 'temp', 'tools', 'need', 'outputs') -and
        -not (Test-Path -LiteralPath (Join-Path $_.FullName 'src') -PathType Container) -and
        -not (Test-Path -LiteralPath (Join-Path $_.FullName 'assets') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $_.FullName 'update.ps1') -PathType Leaf) -and
        ((Get-ChildItem -LiteralPath $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum -lt 1000000)
    } | Select-Object -First 1
if ($portableUpdaterDir) {
    Copy-Item -LiteralPath $portableUpdaterDir.FullName -Destination (Join-Path $stage $portableUpdaterDir.Name) -Recurse -Force
}

# 发布包只允许包含程序与文档；再次清理源码复制带入的缓存和本地数据。
$excludedDirs = @('data', 'tests', 'exports', 'logs', '.venv', '.git', '__pycache__', 'backups')
foreach ($dirName in $excludedDirs) {
    $excludedPaths = @(Get-ChildItem -LiteralPath $stage -Recurse -Force -Directory |
        Where-Object { $_.Name -eq $dirName } |
        Sort-Object FullName -Descending)
    foreach ($excludedPath in $excludedPaths) {
        [System.IO.Directory]::Delete($excludedPath.FullName, $true)
    }
}
Get-ChildItem -LiteralPath $stage -Recurse -Force -File |
    Where-Object { $_.Extension -in @('.db', '.sqlite', '.sqlite3', '.jsonl', '.pyc', '.pyo') } |
    Remove-Item -Force

# 骨架自带的旧清单不能参与新清单哈希计算，否则会留下旧哈希。
$manifestTarget = [string]$stage + '\manifest.json'
if ($manifestTarget -and (Test-Path -LiteralPath $manifestTarget -PathType Leaf)) {
    Remove-Item -LiteralPath $manifestTarget -Force
}

$fileRecords = @()
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -Force -File) {
    $relative = $file.FullName.Substring($stage.Length).TrimStart('\', '/').Replace('\', '/')
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

$zipPath = Join-Path $outputFull ("collector-workbench-v{0}-formal-{1}.zip" -f $version, $stamp)
# .NET 压缩 API 可正确处理中文输出路径；Windows PowerShell 5.1 的
# Compress-Archive 在部分机器上会把中文目标路径误报为非法字符。
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Host ("PACKAGE=" + $zipPath)
Write-Host ("STAGE=" + $stage)
