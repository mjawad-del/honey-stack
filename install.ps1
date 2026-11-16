# honey-stack one-line installer for Windows (PowerShell).
#   irm https://raw.githubusercontent.com/umararshad4/honey-stack/main/install.ps1 | iex
# Clones (or updates) the repo to ~/.honey-stack/repo and runs the reversible installer with a dry run first.
$ErrorActionPreference = 'Stop'

$RepoUrl = 'https://github.com/umararshad4/honey-stack.git'
$Dest = if ($env:HONEY_STACK_DIR) { $env:HONEY_STACK_DIR } else { Join-Path $HOME '.honey-stack\repo' }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host 'honey-stack needs git on PATH (Git for Windows).'; exit 1 }
# Hooks run `python3`, so that exact name must resolve (python.org install manager or Microsoft Store Python provide it).
if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    Write-Host 'honey-stack needs python3 (3.9+) on PATH. Install Python from python.org (install manager) or the Microsoft Store.'
    exit 1
}

if (Test-Path (Join-Path $Dest '.git')) {
    Write-Host "Updating honey-stack in $Dest ..."
    git -C $Dest pull --ff-only --tags -q
} else {
    Write-Host "Cloning honey-stack into $Dest ..."
    New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
    git clone -q $RepoUrl $Dest
}

$Honey = Join-Path $Dest 'bin\honey'
Write-Host ''
Write-Host 'Dry run (nothing is changed yet):'
python3 $Honey install --dry-run
Write-Host ''
Write-Host 'Review the plan above. To apply it, run:'
Write-Host "    python3 `"$Honey`" install"
Write-Host 'Then: start a new Claude Code session; in Codex run /hooks and trust the honey entries.'
