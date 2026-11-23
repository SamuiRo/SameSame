<#
.SYNOPSIS
  Publishes the current version as a release on GitHub and on Forgejo (starbase),
  with the same files on both.

.DESCRIPTION
  1. Checks the working tree is clean and the tag v<version> does not exist yet.
  2. Runs tools\build_windows.ps1 (unless -SkipBuild).
  3. Creates the annotated tag and pushes it (origin pushes to GitHub and starbase).
  4. Creates the GitHub release with gh and uploads the files.
  5. Creates the Forgejo release through its API and uploads the same files.

  Release notes come from -NotesFile, or default to the tagged commit's message.

  Forgejo needs:
    $env:SAMESAME_FORGEJO_URL   optional, defaults to http://starbase:1200
    $env:FORGEJO_TOKEN          access token with write:repository scope
  Without them the Forgejo step is skipped with a warning; rerun with
  -ForgejoOnly once they are set.

.EXAMPLE
  .\tools\release.ps1
.EXAMPLE
  .\tools\release.ps1 -ForgejoOnly -SkipBuild
#>
[CmdletBinding()]
param(
    [string]$NotesFile,
    [string]$ForgejoUrl = $(if ($env:SAMESAME_FORGEJO_URL) { $env:SAMESAME_FORGEJO_URL } else { "http://starbase:1200" }),
    [string]$ForgejoRepo = "StarLith/SameSame",
    [string]$GitHubRepo = "SamuiRo/SameSame",
    [switch]$SkipBuild,
    [switch]$ForgejoOnly
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Invoke-Checked([string]$What, [scriptblock]$Block) {
    Write-Host "==> $What" -ForegroundColor Cyan
    # Native tools log to stderr; under Windows PowerShell 5.1 with "Stop" that
    # becomes a terminating error once output is redirected. Judge by exit code.
    $ErrorActionPreference = "Continue"
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

$Version = (Select-String -Path "dedupe\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$ProjectVersion = (Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
if ($Version -ne $ProjectVersion) { throw "dedupe/__init__.py says $Version but pyproject.toml says $ProjectVersion" }
$Tag = "v$Version"
$OutDir = Join-Path $Root "dist"
$Files = @(
    (Join-Path $OutDir "SameSame-$Version-win64-setup.exe"),
    (Join-Path $OutDir "SameSame-$Version-win64.zip"),
    (Join-Path $OutDir "SHA256SUMS-$Version.txt")
)

if (-not $ForgejoOnly) {
    if (git status --porcelain) { throw "Working tree is not clean; commit first." }
    git rev-parse -q --verify "refs/tags/$Tag" | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "Tag $Tag already exists. Bump the version, or use -ForgejoOnly to finish a partial release." }

    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot "build_windows.ps1")
    }
}
foreach ($file in $Files) { if (-not (Test-Path $file)) { throw "Missing release file: $file" } }

$Notes = if ($NotesFile) { Get-Content -Raw $NotesFile } else { (git log -1 --format=%B HEAD) -join "`n" }
$Notes += @"

## Install (Windows 10/11, x64)

- **SameSame-$Version-win64-setup.exe**: installer. No administrator prompt; adds a Start menu entry and an uninstaller in Settings > Apps.
- **SameSame-$Version-win64.zip**: the same program without installing; unzip and run SameSame.exe.

Video and audio matching need FFmpeg on PATH: ``winget install Gyan.FFmpeg``.
Windows SmartScreen may warn about an unknown publisher because the build is not code-signed; choose *More info > Run anyway*.
Checksums: SHA256SUMS-$Version.txt.
"@
$NotesPath = Join-Path $env:TEMP "samesame-release-notes-$Version.md"
Set-Content -Encoding utf8 -Path $NotesPath -Value $Notes

if (-not $ForgejoOnly) {
    Invoke-Checked "Tag $Tag" { git tag -a $Tag -m "SameSame $Version" }
    Invoke-Checked "Push $Tag (GitHub and starbase)" { git push origin $Tag }
    Invoke-Checked "GitHub release" {
        gh release create $Tag @Files --repo $GitHubRepo --title "SameSame $Version" --notes-file $NotesPath
    }
}

if (-not $ForgejoUrl -or -not $env:FORGEJO_TOKEN) {
    Write-Warning "Forgejo skipped: set FORGEJO_TOKEN, then run: .\tools\release.ps1 -ForgejoOnly"
    return
}

Write-Host "==> Forgejo release" -ForegroundColor Cyan
$Api = "$($ForgejoUrl.TrimEnd('/'))/api/v1/repos/$ForgejoRepo"
$Headers = @{ Authorization = "token $env:FORGEJO_TOKEN" }
$Body = @{ tag_name = $Tag; name = "SameSame $Version"; body = $Notes; draft = $false; prerelease = $false } | ConvertTo-Json
$Release = Invoke-RestMethod -Method Post -Uri "$Api/releases" -Headers $Headers -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($Body))
foreach ($file in $Files) {
    $Name = Split-Path $file -Leaf
    Write-Host "    upload $Name"
    # curl.exe because Windows PowerShell 5.1 has no multipart -Form support.
    & curl.exe --fail --silent --show-error -X POST `
        -H "Authorization: token $env:FORGEJO_TOKEN" `
        -F "attachment=@$file" "$Api/releases/$($Release.id)/assets?name=$Name" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Forgejo upload of $Name failed" }
}
Write-Host "Released $Tag on GitHub and Forgejo." -ForegroundColor Green
