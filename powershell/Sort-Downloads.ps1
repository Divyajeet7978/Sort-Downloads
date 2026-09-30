<#
.SYNOPSIS
    Sorts files into broad category folders, each holding one folder per file extension
    and one folder per arrival month (Compressed\ZIP\2026-09\...), and sends exact
    duplicates to the Recycle Bin.

.DESCRIPTION
    Moves only the loose files sitting directly in the target folder. Existing subfolders
    (extracted archives, portable apps, games) are left untouched.

    The category comes from the table below; extensions not listed go to 'Other'.
    Extensions with a single file sit loose in their category folder. Once a second file
    of that extension shows up, both move to their own extension folder. An extension
    that already has a folder always uses it.

    Inside an extension folder, files are grouped by the month they arrived in Downloads
    (creation time, yyyy-MM). Files left loose in an extension folder are moved into
    their month folder; files already in a month folder are never moved again.

    Duplicates are files with identical content (same size and MD5) in the same extension
    folder, across all its month folders (or loose in the same category folder). A copy
    already filed is kept, otherwise the shortest name (then the oldest). The rest are sent to the Recycle Bin together as one folder named
    'Duplicates <date time>', so they can be restored in one go. Empty files are never
    treated as duplicates.

    Files are never overwritten: a name clash becomes 'name (2).ext'.

.EXAMPLE
    .\Sort-Downloads.ps1 -WhatIf          # preview, nothing is moved or deleted
    .\Sort-Downloads.ps1                  # sort loose files
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Path = (Join-Path $env:USERPROFILE 'Downloads')
)

$ErrorActionPreference = 'Stop'
$Path = (Resolve-Path -LiteralPath $Path).ProviderPath
Add-Type -AssemblyName Microsoft.VisualBasic

# Browser/download-manager files that are still being written.
$partialExtensions = '.crdownload', '.part', '.partial', '.download', '.opdownload', '.tmp'

$categories = [ordered]@{
    Documents  = 'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'xls', 'xlsx', 'xlsm', 'ods', 'ppt', 'pptx', 'odp', 'dwg', 'dxf'
    Images     = 'png', 'jpg', 'jpeg', 'gif', 'bmp', 'svg', 'webp', 'avif', 'heic', 'ico', 'tif', 'tiff'
    Videos     = 'mp4', 'mkv', 'avi', 'mov', 'wmv', 'webm', 'm4v'
    Audio      = 'mp3', 'wav', 'flac', 'aac', 'm4a', 'ogg', 'wma'
    Compressed = 'zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'xz', 'bz2', 'iso'
    Programs   = 'exe', 'msi', 'dll', 'sys', 'inf', 'apk', 'msix', 'appx'
    Code       = 'js', 'ts', 'css', 'html', 'htm', 'py', 'java', 'c', 'cpp', 'cs', 'sh', 'ps1', 'bat', 'cmd', 'ipynb'
    Data       = 'json', 'csv', 'xml', 'yaml', 'yml', 'sql', 'cypher', 'log', 'dat', 'bak', 'evtx', 'bag', 'prp', 'ini'
}
$categoryOf = @{}
foreach ($category in $categories.Keys) {
    foreach ($extension in $categories[$category]) { $categoryOf[$extension] = $category }
}

function Get-CategoryFolder([System.IO.FileInfo]$File) {
    $category = $categoryOf[$File.Extension.TrimStart('.')]
    Join-Path $Path $(if ($category) { $category } else { 'Other' })
}

function Get-ExtensionFolder([System.IO.FileInfo]$File) {
    $name = if ($File.Extension) { $File.Extension.TrimStart('.').ToUpper() } else { 'NoExtension' }
    Join-Path (Get-CategoryFolder $File) $name
}

# Get-FileHash returns nothing under -WhatIf in Windows PowerShell 5.1, so hash with .NET.
function Get-Md5([string]$File) {
    $stream = [System.IO.File]::OpenRead($File)
    try { [System.BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash($stream)) }
    finally { $stream.Dispose() }
}

function Get-UniquePath([string]$Folder, [System.IO.FileInfo]$File) {
    $target = Join-Path $Folder $File.Name
    $n = 2
    while (Test-Path -LiteralPath $target) {
        $target = Join-Path $Folder ('{0} ({1}){2}' -f $File.BaseName, $n++, $File.Extension)
    }
    $target
}

# Hidden/system files (e.g. desktop.ini) are excluded because -Force is not used.
# The script and its .cmd launcher are skipped so they can live in the folder they sort.
$self = [System.IO.Path]::GetFileNameWithoutExtension($PSCommandPath)
$files = @(Get-ChildItem -LiteralPath $Path -File |
    Where-Object { $_.Extension.ToLower() -notin $partialExtensions -and $_.BaseName -ne $self })

# Files loose in a category folder (one-offs) or in an extension folder (not yet dated)
# are reconsidered, so they can move once their extension has company or get a month folder.
foreach ($category in @($categories.Keys) + 'Other') {
    $folder = Join-Path $Path $category
    if (Test-Path -LiteralPath $folder) {
        $files += @(Get-Item -LiteralPath $folder) + @(Get-ChildItem -LiteralPath $folder -Directory) |
            Get-ChildItem -File -Force | Where-Object Name -ne 'desktop.ini'
    }
}

$countByFolder = $files | Group-Object { Get-ExtensionFolder $_ } -AsHashTable -AsString

# Scope is the extension folder (or the category folder for one-offs); Folder adds the month.
$plan = @(foreach ($file in $files) {
    $scope = Get-ExtensionFolder $file
    if ((Test-Path -LiteralPath $scope) -or $countByFolder[$scope].Count -ge 2) {
        $folder = Join-Path $scope $file.CreationTime.ToString('yyyy-MM')
    } else {
        $scope = $folder = Get-CategoryFolder $file
    }
    if ($file.DirectoryName -ne $folder) { [pscustomobject]@{ File = $file; Folder = $folder; Scope = $scope; Moving = $true } }
})
$movingPaths = [System.Collections.Generic.HashSet[string]]::new([string[]]@($plan | ForEach-Object { $_.File.FullName }), [System.StringComparer]::OrdinalIgnoreCase)

# Compare incoming files with each other and with what is already filed in the same scope
# (every month folder of an extension folder, or the loose files of a category folder).
$existing = @(foreach ($scope in $plan.Scope | Sort-Object -Unique) {
    if (Test-Path -LiteralPath $scope) {
        Get-ChildItem -LiteralPath $scope -File -Force -Recurse:((Split-Path $scope) -ne $Path) |
            Where-Object { -not $movingPaths.Contains($_.FullName) } |
            ForEach-Object { [pscustomobject]@{ File = $_; Folder = $_.DirectoryName; Scope = $scope; Moving = $false } }
    }
})

# Only size groups containing an incoming file are hashed, so filed files aren't re-read every run.
$duplicates = @($plan + $existing |
    Where-Object { $_.File.Length -gt 0 } |
    Group-Object Scope, { $_.File.Length } | Where-Object { $_.Count -gt 1 -and $_.Group.Moving -contains $true } | ForEach-Object { $_.Group } |
    Group-Object Scope, { Get-Md5 $_.File.FullName } | Where-Object Count -gt 1 |
    ForEach-Object { $_.Group | Sort-Object { $_.Moving }, { $_.File.Name.Length }, { $_.File.LastWriteTime } | Select-Object -Skip 1 } |
    ForEach-Object { $_.File.FullName })

# Collected in one folder and recycled at the end: a single Recycle Bin operation,
# and everything can be restored in one go.
$duplicatesFolder = Join-Path $Path ('Duplicates {0:yyyy-MM-dd HHmmss}' -f (Get-Date))
if ($duplicates) {
    New-Item -ItemType Directory -Path $duplicatesFolder | Out-Null
    foreach ($duplicate in $duplicates) {
        Move-Item -LiteralPath $duplicate -Destination (Get-UniquePath $duplicatesFolder (Get-Item -LiteralPath $duplicate -Force))
    }
}

$moved = foreach ($item in $plan | Where-Object { $_.File.FullName -notin $duplicates }) {
    if (-not (Test-Path -LiteralPath $item.Folder)) {
        New-Item -ItemType Directory -Path $item.Folder -Force | Out-Null
    }
    try {
        Move-Item -LiteralPath $item.File.FullName -Destination (Get-UniquePath $item.Folder $item.File)
        $item.Scope.Substring($Path.Length).TrimStart('\')
    } catch {
        Write-Warning "Skipped '$($item.File.FullName)': $($_.Exception.Message)"
    }
}

if ($duplicates -and $PSCmdlet.ShouldProcess($duplicatesFolder, 'Send duplicates to Recycle Bin')) {
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($duplicatesFolder, 'OnlyErrorDialogs', 'SendToRecycleBin')
}

$moved | Group-Object | Sort-Object Name | Format-Table @{ n = 'Folder'; e = 'Name' }, Count -AutoSize
Write-Host "Files sorted: $(@($moved).Count)   Duplicates sent to Recycle Bin: $($duplicates.Count)"
