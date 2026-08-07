# Bootstrap for juniors on Windows: create venv, install deps, ensure gh, run setup.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "============================================================"
Write-Host "x-mrr-banner bootstrap (Windows)"
Write-Host "============================================================"
Write-Host "Repo: $Root"
Write-Host ""

function Find-Python {
  foreach ($name in @("python3.13", "python3.12", "python3.11", "python")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) { return $cmd.Source }
  }
  return $null
}

$Python = Find-Python
if (-not $Python) {
  Write-Host "Python 3.11+ is required but was not found."
  Write-Host "Install with:  winget install Python.Python.3.12"
  Write-Host "Or download:   https://www.python.org/downloads/"
  Write-Host "Important: tick 'Add python.exe to PATH', then open a NEW terminal."
  Write-Host "See README.md → Prerequisites."
  exit 1
}
Write-Host "✓ Python: $(& $Python --version)"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "✗ Git not found. Install with:  winget install Git.Git"
  Write-Host "Docs: https://git-scm.com/downloads"
  exit 1
}
Write-Host "✓ Git: $(git --version)"

if (-not (Test-Path ".venv")) {
  Write-Host "→ Creating virtual environment (.venv)"
  & $Python -m venv .venv
}

$Activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
. $Activate
Write-Host "✓ Virtualenv active"

Write-Host "→ Upgrading pip"
python -m pip install --upgrade pip

Write-Host "→ Installing x-mrr-banner and dependencies"
python -m pip install -e .

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Write-Host "GitHub CLI (gh) not found — needed to upload Actions secrets."
  $ans = Read-Host "Install gh with winget now? [Y/n]"
  if ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^[Yy]') {
    winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
    Write-Host "If 'gh' is not found yet, close this window, open a new PowerShell, and re-run bootstrap."
  } else {
    Write-Host "Install manually: https://cli.github.com/  or  winget install GitHub.cli"
  }
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
  Write-Host "✓ GitHub CLI found"
  gh auth status 2>$null
  if ($LASTEXITCODE -ne 0) {
    $ans = Read-Host "Run gh auth login now? [Y/n]"
    if ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^[Yy]') {
      gh auth login
    }
  } else {
    Write-Host "✓ gh authenticated"
  }
}

Write-Host ""
Write-Host "→ Starting interactive setup wizard"
python -m x_mrr_banner setup @args
