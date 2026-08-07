#!/usr/bin/env bash
# Bootstrap for juniors: create venv, install Python deps, ensure gh, run setup wizard.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================================"
echo "x-mrr-banner bootstrap"
echo "============================================================"
echo "Repo: $ROOT"
echo

need_python() {
  echo "Python 3.11+ is required but was not found."
  echo "Read README.md → Prerequisites for install steps."
  echo "  macOS:   brew install python@3.12"
  echo "  Ubuntu:  sudo apt install -y python3 python3-venv python3-pip"
  echo "  Windows: use scripts/bootstrap.ps1 or winget install Python.Python.3.12"
  echo "Download: https://www.python.org/downloads/"
  exit 1
}

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  need_python
fi

echo "✓ Python: $($PYTHON --version)"

if ! command -v git >/dev/null 2>&1; then
  echo "✗ Git not found."
  echo "  macOS:  xcode-select --install   OR   brew install git"
  echo "  Ubuntu: sudo apt install -y git"
  echo "  Docs:   https://git-scm.com/downloads"
  exit 1
fi
echo "✓ Git: $(git --version)"

if [[ ! -d .venv ]]; then
  echo "→ Creating virtual environment (.venv)"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
echo "✓ Virtualenv active: $VIRTUAL_ENV"

echo "→ Upgrading pip"
python -m pip install --upgrade pip

echo "→ Installing x-mrr-banner and dependencies"
python -m pip install -e .

ensure_gh() {
  if command -v gh >/dev/null 2>&1; then
    echo "✓ GitHub CLI: $(gh --version | head -n 1)"
    return 0
  fi
  echo "GitHub CLI (gh) not found — needed to upload Actions secrets to your fork."
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    read -r -p "Install gh with Homebrew now? [Y/n] " ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      brew install gh
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    read -r -p "Install gh with apt now (may ask for sudo)? [Y/n] " ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      sudo apt-get update
      sudo apt-get install -y gh || {
        echo "apt install failed. See https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
      }
    fi
  else
    echo "Install manually: https://cli.github.com/"
    echo "  macOS:   brew install gh"
    echo "  Windows: winget install GitHub.cli"
  fi

  if command -v gh >/dev/null 2>&1; then
    echo "✓ GitHub CLI installed"
  else
    echo "! gh still missing — you can finish credentials locally and sync later:"
    echo "    python -m x_mrr_banner setup --local-only"
    echo "    python -m x_mrr_banner setup --github-only"
  fi
}

ensure_gh

if command -v gh >/dev/null 2>&1; then
  if ! gh auth status >/dev/null 2>&1; then
    echo
    echo "You are not logged into GitHub CLI."
    read -r -p "Run gh auth login now? [Y/n] " ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      gh auth login
    fi
  else
    echo "✓ gh authenticated"
  fi
fi

echo
echo "→ Starting interactive setup wizard"
echo "   (API keys → .env + optional GitHub Actions secrets)"
echo
exec python -m x_mrr_banner setup "$@"
