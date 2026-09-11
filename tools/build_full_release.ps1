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
    'monitor_gui.ps1', 'update.ps1', 'config\update.json', 'docs\更新说明-v2.2.2.md'
)) {
    $sourcePath = Join-Path $sourceFull $fileName
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $targetFile = Join-Path $stage $fileName
        New-Item -ItemType Directory -Path (Split-Path $targetFile -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $targetFile -Force
    }
}

foreach ($fileName in @('apply_code_update.ps1', 'apply_code_update.bat')) {
    $sourcePath = Join-Path $PSScriptRoot $fileName
    Copy-Item -LiteralPath $sourcePath -Destination $stage -Force
}

# 便携更新器也随正式包发布，免安装 EXE 包可将该文件夹复制到任意安装目录后更新。
$portableUpdater = Join-Path $sourceFull '更新程序'
if (Test-Path -LiteralPath $portableUpdater -PathType Container) {
    Copy-Item -LiteralPath $portableUpdater -Destination (Join-Path $stage '更新程序') -Recurse -Force
}

# 发布包只允许包含程序与文档；再次清理源码复制带入的缓存和本地数据。
$excludedDirs = @('data', 'tests', 'exports', 'logs', '.venv', '.git', '__pycache__', 'backups')
foreach ($dirName in $excludedDirs) {
    Get-ChildItem -LiteralPath $stage -Recurse -Force -Directory -Filter $dirName |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
}
Get-ChildItem -LiteralPath $stage -Recurse -Force -File |
    Where-Object { $_.Extension -in @('.db', '.sqlite', '.sqlite3', '.jsonl', '.pyc', '.pyo') } |
    Remove-Item -Force

# 骨架自带的旧清单不能参与新清单哈希计算，否则会留下旧哈希。
$oldManifestPath = Join-Path $stage 'manifest.json'
if (Test-Path -LiteralPath $oldManifestPath -PathType Leaf) {
    Remove-Item -LiteralPath $oldManifestPath -Force
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
