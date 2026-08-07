from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from x_mrr_banner.config import REPO_ROOT
from x_mrr_banner import ui

MIN_PYTHON = (3, 11)

GH_INSTALL_DOCS = "https://cli.github.com/"
PYTHON_INSTALL_DOCS = "https://www.python.org/downloads/"
GIT_INSTALL_DOCS = "https://git-scm.com/downloads"


def _run(cmd: list[str], *, check: bool = False, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kwargs)


def python_version_ok(version: tuple[int, int] | None = None) -> bool:
    current = version or sys.version_info[:2]
    return current >= MIN_PYTHON


def describe_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system


def print_python_install_help() -> None:
    ui.err(
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
        f"(you have {sys.version.split()[0]})."
    )
    ui.info(f"Install guide: {ui.url(PYTHON_INSTALL_DOCS)}")
    ui.bullet("brew install python@3.12")
    ui.info("Then open a new terminal and re-run setup.")


def print_git_install_help() -> None:
    ui.err("Git is required to clone/push this repository.")
    ui.info(f"Install guide: {ui.url(GIT_INSTALL_DOCS)}")
    ui.bullet("xcode-select --install   OR   brew install git")


def print_gh_install_help() -> None:
    ui.warn("GitHub CLI (`gh`) is used to upload Actions secrets to your fork.")
    ui.info(f"Install guide: {ui.url(GH_INSTALL_DOCS)}")
    ui.bullet("brew install gh")
    ui.info("After installing, authenticate once:")
    ui.bullet("gh auth login")


def try_install_gh(*, interactive: bool = True) -> bool:
    """Attempt to install GitHub CLI. Returns True if `gh` is available afterwards."""
    if shutil.which("gh"):
        return True

    os_name = describe_os()
    commands: list[list[str]] = []
    if os_name == "macos" and shutil.which("brew"):
        commands.append(["brew", "install", "gh"])
    elif os_name == "windows" and shutil.which("winget"):
        commands.append(
            ["winget", "install", "--id", "GitHub.cli", "-e", "--accept-source-agreements", "--accept-package-agreements"]
        )
    elif os_name == "linux" and shutil.which("apt-get"):
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if is_root:
            commands.append(["apt-get", "update"])
            commands.append(["apt-get", "install", "-y", "gh"])
        elif shutil.which("sudo"):
            commands.append(["sudo", "apt-get", "update"])
            commands.append(["sudo", "apt-get", "install", "-y", "gh"])

    if not commands:
        print_gh_install_help()
        return False

    if interactive:
        ui.warn("GitHub CLI (`gh`) is not installed.")
        ui.info(f"Suggested command: {' && '.join(' '.join(c) for c in commands)}")
        answer = input(ui.prompt("Try to install it now? [Y/n] ")).strip().lower()
        if answer in {"n", "no"}:
            print_gh_install_help()
            return False
    else:
        ui.step("Installing GitHub CLI (`gh`)…")

    for cmd in commands:
        ui.step(" ".join(cmd))
        result = _run(cmd)
        if result.returncode != 0:
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
            print_gh_install_help()
            return False

    if not shutil.which("gh"):
        ui.warn("`gh` was installed but is not on PATH in this shell yet.")
        ui.info("Close this terminal, open a new one, then re-run setup.")
        print_gh_install_help()
        return False
    ui.ok("GitHub CLI is installed.")
    return True


def ensure_gh_authenticated(*, interactive: bool = True) -> bool:
    if not shutil.which("gh"):
        return False
    status = _run(["gh", "auth", "status"])
    if status.returncode == 0:
        return True
    ui.warn("GitHub CLI is installed but you are not logged in.")
    ui.info("This is needed so setup can create Actions secrets on your fork.")
    if interactive:
        answer = input(ui.prompt("Run `gh auth login` now? [Y/n] ")).strip().lower()
        if answer in {"n", "no"}:
            ui.info("Later, run:  gh auth login")
            return False
        proc = subprocess.run(["gh", "auth", "login"], check=False)
        return proc.returncode == 0
    return False


def ensure_python_package_installed(*, interactive: bool = True) -> None:
    """Install this repo into the current interpreter if imports are missing."""
    try:
        import PIL  # noqa: F401
        import yaml  # noqa: F401
        import dotenv  # noqa: F401
    except ImportError:
        ui.step("Python dependencies are missing. Installing this project with pip…")
        cmd = [sys.executable, "-m", "pip", "install", "-e", str(REPO_ROOT)]
        ui.step(" ".join(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "pip install failed. From the repo root run:\n"
                "  python -m venv .venv\n"
                "  source .venv/bin/activate\n"
                "  pip install -e .\n"
                "See README.md → Prerequisites."
            )


def ensure_venv_hint() -> None:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv and not os.environ.get("VIRTUAL_ENV"):
        venv_path = REPO_ROOT / ".venv"
        if venv_path.is_dir():
            ui.warn("A .venv exists but is not active.")
            ui.bullet("source .venv/bin/activate")
        else:
            ui.info("Tip: use a virtual environment so project packages stay isolated.")
            ui.bullet("python -m venv .venv && source .venv/bin/activate")


def check_core_tools() -> list[str]:
    """Return human-readable problems for tools we cannot auto-install (Python/Git)."""
    problems: list[str] = []
    if not python_version_ok():
        problems.append(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {sys.version.split()[0]}). See README.md → Prerequisites."
        )
    if not shutil.which("git"):
        problems.append(f"Git not found. Install from {GIT_INSTALL_DOCS} (see README.md).")
    return problems


def prepare_environment(*, want_github: bool = True, interactive: bool = True) -> None:
    """Validate / install what we can before the credential wizard."""
    problems = check_core_tools()
    if problems:
        for problem in problems:
            ui.err(problem)
        if not python_version_ok():
            print_python_install_help()
        if not shutil.which("git"):
            print_git_install_help()
        raise RuntimeError("Fix the prerequisites above, then re-run setup.")

    ensure_venv_hint()
    ensure_python_package_installed(interactive=interactive)

    if want_github:
        if not try_install_gh(interactive=interactive):
            ui.warn("Continuing without GitHub secret sync. You can re-run later with:")
            ui.bullet("python -m x_mrr_banner setup --github-only")
            return
        ensure_gh_authenticated(interactive=interactive)
