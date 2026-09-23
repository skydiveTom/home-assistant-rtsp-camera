# Copies the Home Assistant integration from the repository root into the add-on
# folder. The add-on folder is the Docker build context, so the files have to be
# present inside it for the image to ship them.
#
# Usage:
#   pwsh -File scripts/sync_integration.ps1          # update the copy in the add-on
#   pwsh -File scripts/sync_integration.ps1 -Check   # fail when both copies differ

[CmdletBinding()]
param(
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'custom_components\rtsp_cameras'
$target = Join-Path $root 'rtsp_cameras\custom_components\rtsp_cameras'

if (-not (Test-Path -LiteralPath $source)) {
    throw "Integration source folder not found: $source"
}

function Get-SourceFiles {
    param([string]$BasePath)

    Get-ChildItem -LiteralPath $BasePath -Recurse -File |
        Where-Object { $_.FullName -notmatch '__pycache__' }
}

if ($Check) {
    $problems = @()
    $sourceFiles = Get-SourceFiles -BasePath $source
    foreach ($file in $sourceFiles) {
        $relative = $file.FullName.Substring($source.Length).TrimStart('\', '/')
        $mirror = Join-Path $target $relative
        if (-not (Test-Path -LiteralPath $mirror)) {
            $problems += "missing in the add-on copy: $relative"
            continue
        }
        if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne (Get-FileHash -LiteralPath $mirror).Hash) {
            $problems += "differs from the add-on copy: $relative"
        }
    }

    if ($problems.Count -gt 0) {
        $problems | ForEach-Object { Write-Host " - $_" }
        throw 'The two integration copies are out of sync, run scripts/sync_integration.ps1'
    }
    Write-Host 'Both integration copies are identical.'
    exit 0
}

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force
Get-ChildItem -LiteralPath $target -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force

$count = (Get-SourceFiles -BasePath $target).Count
Write-Host "Synced $count file(s) from $source to $target"
