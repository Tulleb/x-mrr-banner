#!/usr/bin/env bash
# Start: create venv, install Python deps, ensure gh, run setup wizard, generate first banner.
set -euo pipefail

# Prefer color in interactive start even if the environment sets NO_COLOR.
export FORCE_COLOR="${FORCE_COLOR:-1}"
# Clear NO_COLOR for this process tree so the Python wizard can colorize too.
unset NO_COLOR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- colors (disabled when not a TTY, or NO_COLOR is set) ---
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
  C_CYAN=$'\033[36m'
else
  C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN=""
fi

header()  { printf '\n%s%s%s\n' "$C_BOLD$C_CYAN" "============================================================" "$C_RESET"
            printf '%s%s%s\n' "$C_BOLD$C_CYAN" "$*" "$C_RESET"
            printf '%s%s%s\n\n' "$C_BOLD$C_CYAN" "============================================================" "$C_RESET"; }
ok()      { printf '%s✓%s %s\n' "$C_GREEN$C_BOLD" "$C_RESET" "$*"; }
err()     { printf '%s✗%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; }
warn()    { printf '%s!%s %s\n' "$C_YELLOW$C_BOLD" "$C_RESET" "$*"; }
step()    { printf '%s→%s %s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$*"; }
info()    { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
bullet()  { printf '  %s•%s %s\n' "$C_DIM" "$C_RESET" "$*"; }

header "x-mrr-banner start"
info "Repo: $ROOT"

need_python() {
  err "Python 3.11+ is required but was not found."
  info "See README.md → Prerequisites."
  bullet "brew install python@3.12"
  bullet "https://www.python.org/downloads/"
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

ok "Python: $($PYTHON --version)"

if ! command -v git >/dev/null 2>&1; then
  err "Git not found."
  bullet "xcode-select --install   OR   brew install git"
  bullet "https://git-scm.com/downloads"
  exit 1
fi
ok "Git: $(git --version)"

if [[ ! -d .venv ]]; then
  step "Creating virtual environment (.venv)"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
ok "Virtualenv active: $VIRTUAL_ENV"

step "Upgrading pip"
python -m pip install --upgrade pip

step "Installing x-mrr-banner and dependencies"
python -m pip install -e .

ensure_gh() {
  if command -v gh >/dev/null 2>&1; then
    ok "GitHub CLI: $(gh --version | head -n 1)"
    return 0
  fi
  warn "GitHub CLI (gh) not found — needed to upload Actions secrets to your fork."
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    read -r -p "$(printf '%sInstall gh with Homebrew now? [Y/n]%s ' "$C_BOLD" "$C_RESET")" ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      step "brew install gh"
      brew install gh
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    read -r -p "$(printf '%sInstall gh with apt now (may ask for sudo)? [Y/n]%s ' "$C_BOLD" "$C_RESET")" ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      step "apt-get install gh"
      sudo apt-get update
      sudo apt-get install -y gh || {
        err "apt install failed. See https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
      }
    fi
  else
    warn "Install manually: https://cli.github.com/"
    bullet "brew install gh"
  fi

  if command -v gh >/dev/null 2>&1; then
    ok "GitHub CLI installed"
  else
    warn "gh still missing — you can finish credentials locally and sync later:"
    bullet "python -m x_mrr_banner setup --local-only"
    bullet "python -m x_mrr_banner setup --github-only"
  fi
}

ensure_gh

if command -v gh >/dev/null 2>&1; then
  if ! gh auth status >/dev/null 2>&1; then
    echo
    warn "You are not logged into GitHub CLI."
    read -r -p "$(printf '%sRun gh auth login now? [Y/n]%s ' "$C_BOLD" "$C_RESET")" ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      step "gh auth login"
      gh auth login
    fi
  else
    ok "gh authenticated"
  fi
fi

run_banner() {
  echo
  step "Banner generation"
  info "Fetches revenues, renders inputs/BANNER.md.j2, calls OpenAI → output/YYYYMM/"
  info "Revenue APIs + OpenAI can take a minute — progress logs appear next."
  step "Running update --dry-run…"
  python -m x_mrr_banner update --dry-run
  echo
  ok "Done. Outputs under output/YYYYMM/ (banner.png + BANNER.md)"
}

finish_tips() {
  bullet "Commit config.yaml and push when ready"
  bullet "GitHub → Actions → Update X banner → Run workflow"
}

echo
# When credentials + config are already complete, offer to skip the wizard.
if python -c 'from x_mrr_banner.setup_wizard import offer_skip_setup_to_banner; raise SystemExit(0 if offer_skip_setup_to_banner() else 1)'; then
  run_banner
  finish_tips
  exit 0
fi

echo
step "Starting interactive setup wizard"
info "API keys → .env + GitHub Actions secrets"
info "Banner preferences → config.yaml"
echo
python -m x_mrr_banner setup "$@"

echo
step "First banner generation"
info "Fetches revenues, renders inputs/BANNER.md.j2, calls OpenAI → output/YYYYMM/"
info "Revenue APIs + OpenAI can take a minute — progress logs appear after you confirm."
read -r -p "$(printf '%sGenerate the first banner now? [Y/n]%s ' "$C_BOLD" "$C_RESET")" ans
ans=${ans:-Y}
if [[ "$ans" =~ ^[Yy]$ ]]; then
  step "Running update --dry-run…"
  python -m x_mrr_banner update --dry-run
  echo
  ok "Done. Outputs under output/YYYYMM/ (banner.png + BANNER.md)"
else
  echo
  ok "Skipped banner generation."
  bullet "Generate later:  python -m x_mrr_banner update --dry-run"
fi
finish_tips
