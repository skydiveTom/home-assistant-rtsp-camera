# Publishes the GitHub release of every tagged version of the add-on that has no
# release yet. The body of a release is the section of the changelog that carries
# the version and the title is the subject of the tagged commit, so a release can
# never say something the repository does not say.
#
# Usage:
#   pwsh -File scripts/publish_releases.ps1 -DryRun             # print the plan, no writes
#   pwsh -File scripts/publish_releases.ps1 -DryRun -Since 0.5.3
#   $env:GITHUB_TOKEN = '...'                                   # needs the "repo" scope
#   pwsh -File scripts/publish_releases.ps1 -Tag 0.7.2           # one release
#   pwsh -File scripts/publish_releases.ps1                      # the whole backlog
#
# A version of the changelog without a tag is skipped: a release needs a tag, so
# the changelog may run ahead of the tags without this script inventing one.

[CmdletBinding()]
param(
    [string[]]$Tag,
    [string]$Since,
    [string]$Repository,
    [string]$Token,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$changelog = Join-Path $root 'rtsp_cameras\CHANGELOG.md'
$api = 'https://api.github.com'
$versionTagPattern = '^v?\d+\.\d+(\.\d+)?$'

function Invoke-Git {
    param([string[]]$Arguments)

    $output = & git @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed: $output"
    }
    return ($output | ForEach-Object { $_.ToString() })
}

function Get-RepositoryName {
    $url = @(Invoke-Git -Arguments @('config', '--get', 'remote.origin.url'))[0]
    $url = "$url".Trim()
    if ($url -notmatch '[:/](?<owner>[^/:]+)/(?<name>[^/]+?)(\.git)?/?$') {
        throw "Cannot read owner and name from the remote: $url"
    }
    return "$($Matches.owner)/$($Matches.name)"
}

function Get-Tags {
    # In version order, oldest first: a release is published in that order, so the
    # newest tag is known before the first write.
    $tags = Invoke-Git -Arguments @('for-each-ref', 'refs/tags', '--format=%(refname:short)')
    $tags | Where-Object { $_ -match $versionTagPattern } |
        Sort-Object { [version]($_ -replace '^v', '') }
}

function Get-TagField {
    param([string]$Name, [string]$Format)

    return @(Invoke-Git -Arguments @('log', '-1', "--format=$Format", "$Name^{commit}"))[0]
}

function Get-ChangelogSections {
    # "## [0.7.2] - 2026-10-19" opens a section, the next "## " closes it.
    $text = Get-Content -LiteralPath $changelog -Raw
    $sections = @{}
    $found = [regex]::Matches(
        $text,
        '(?ms)^## \[(?<version>[^\]]+)\][^\n]*\n(?<body>.*?)(?=^## |\z)'
    )
    foreach ($match in $found) {
        $sections[$match.Groups['version'].Value] = $match.Groups['body'].Value.Trim()
    }
    return $sections
}

function Get-PublishedTags {
    param([string]$RepositoryName, [string]$ApiToken)

    $headers = @{ Accept = 'application/vnd.github+json'; 'X-GitHub-Api-Version' = '2022-11-28' }
    if ($ApiToken) {
        $headers['Authorization'] = "Bearer $ApiToken"
    }

    $published = @()
    for ($page = 1; $page -le 20; $page++) {
        $uri = "$api/repos/$RepositoryName/releases?per_page=100&page=$page"
        $answer = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get
        if (-not $answer) {
            break
        }
        $published += $answer | ForEach-Object { $_.tag_name }
        if (@($answer).Count -lt 100) {
            break
        }
    }
    return $published
}

if (-not (Test-Path -LiteralPath $changelog)) {
    throw "Changelog not found: $changelog"
}
if (-not $Repository) {
    $Repository = Get-RepositoryName
}
if (-not $Token) {
    $Token = $env:GITHUB_TOKEN
}
if (-not $Token) {
    $Token = $env:GH_TOKEN
}
if (-not $DryRun -and -not $Token) {
    throw 'No token: set GITHUB_TOKEN (scope "repo") or pass -Token. A dry run needs none (-DryRun).'
}

$repositoryUrl = "https://github.com/$Repository"
$sections = Get-ChangelogSections
$allTags = @(Get-Tags)
$published = @(Get-PublishedTags -RepositoryName $Repository -ApiToken $Token)
$tagVersions = @($allTags | ForEach-Object { $_ -replace '^v', '' })

$candidates = $allTags
if ($Tag) {
    $wanted = @($Tag | ForEach-Object { $_.Trim() })
    $missing = @($wanted | Where-Object { $allTags -notcontains $_ })
    if ($missing) {
        throw "No such tag: $($missing -join ', ')"
    }
    $candidates = @($allTags | Where-Object { $wanted -contains $_ })
}
if ($Since) {
    $from = [version]($Since -replace '^v', '')
    $candidates = @($candidates | Where-Object { [version]($_ -replace '^v', '') -ge $from })
}

$skipped = @()
$plan = @()
foreach ($name in $candidates) {
    if ($published -contains $name) {
        continue
    }
    $version = $name -replace '^v', ''
    if (-not $sections.ContainsKey($version)) {
        $skipped += "$name (no section in the changelog)"
        continue
    }
    $plan += [pscustomobject]@{
        Tag     = $name
        Version = $version
        Title   = Get-TagField -Name $name -Format '%s'
        Commit  = Get-TagField -Name $name -Format '%H'
        Body    = $sections[$version]
    }
}

$untagged = @($sections.Keys | Where-Object { $tagVersions -notcontains $_ } |
        Sort-Object { [version]$_ })

Write-Host "Repository: $Repository"
Write-Host "Tags: $($allTags.Count), published releases: $($published.Count)"
if ($untagged) {
    Write-Host "In the changelog without a tag (skipped): $($untagged -join ', ')"
}
if ($skipped) {
    Write-Host 'Skipped:'
    $skipped | ForEach-Object { Write-Host " - $_" }
}

if (-not $plan) {
    Write-Host 'Nothing to publish - every tagged version has a release.'
    exit 0
}

$headers = @{
    Accept                 = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
}
if ($Token) {
    $headers['Authorization'] = "Bearer $Token"
}

$newestTag = $allTags[-1]
$total = $plan.Count
for ($index = 0; $index -lt $total; $index++) {
    $release = $plan[$index]
    $latest = $release.Tag -eq $newestTag
    $footer = "`n`n_Full changelog: [$($release.Version)]($repositoryUrl/tree/$($release.Tag)/rtsp_cameras/CHANGELOG.md)_"
    $body = $release.Body + $footer
    $payload = @{
        tag_name         = $release.Tag
        target_commitish = $release.Commit
        name             = $release.Title
        body             = $body
        draft            = $false
        prerelease       = $false
        make_latest      = if ($latest) { 'true' } else { 'false' }
    }

    if ($DryRun) {
        Write-Host ("would create {0}: {1} ({2} characters of notes, latest={3})" -f `
                $release.Tag, $release.Title, $body.Length, $latest)
        continue
    }

    $json = $payload | ConvertTo-Json -Depth 5
    try {
        $created = Invoke-RestMethod -Uri "$api/repos/$Repository/releases" -Headers $headers `
            -Method Post -Body $json -ContentType 'application/json'
        Write-Host "created $($release.Tag): $($created.html_url)"
    }
    catch {
        $detail = $_.ErrorDetails.Message
        throw "Publishing $($release.Tag) failed: $($_.Exception.Message)`n$detail"
    }
}

if ($DryRun) {
    Write-Host "$total release(s) would be created - run again without -DryRun and with a token to publish them."
}
