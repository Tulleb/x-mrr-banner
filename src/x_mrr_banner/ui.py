from __future__ import annotations

import os
import sys


def colors_enabled() -> bool:
    """FORCE_COLOR wins; otherwise color when stdout is a real terminal."""
    if os.environ.get("FORCE_COLOR", "").strip() not in {"", "0", "false", "False"}:
        return True
    if os.environ.get("NO_COLOR", "").strip() != "":
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


class _Style:
    __slots__ = (
        "reset",
        "bold",
        "dim",
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "white",
    )

    def __init__(self) -> None:
        on = colors_enabled()
        self.reset = "\033[0m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.red = "\033[31m" if on else ""
        self.green = "\033[32m" if on else ""
        self.yellow = "\033[33m" if on else ""
        self.blue = "\033[34m" if on else ""
        self.magenta = "\033[35m" if on else ""
        self.cyan = "\033[36m" if on else ""
        self.white = "\033[37m" if on else ""


def _s() -> _Style:
    return _Style()


def _paint(text: str, *parts: str) -> str:
    if not parts or not colors_enabled():
        return text
    return f"{''.join(parts)}{text}{_s().reset}"


def header(title: str) -> None:
    s = _s()
    bar = _paint("=" * 64, s.bold, s.cyan)
    print()
    print(bar)
    print(_paint(title, s.bold, s.cyan))
    print(bar)


def field_heading(title_text: str, key: str) -> None:
    s = _s()
    print(f"{_paint('— ' + title_text, s.bold, s.magenta)} {_paint(f'({key})', s.dim)}")


def ok(text: str) -> None:
    s = _s()
    print(f"{_paint('✓', s.bold, s.green)} {text}")


def err(text: str) -> None:
    s = _s()
    print(f"{_paint('✗', s.bold, s.red)} {text}", file=sys.stderr)


def warn(text: str) -> None:
    s = _s()
    print(f"{_paint('!', s.bold, s.yellow)} {text}")


def step(text: str) -> None:
    s = _s()
    print(f"{_paint('→', s.bold, s.blue)} {text}")


def info(text: str) -> None:
    s = _s()
    print(_paint(text, s.dim))


def bullet(text: str) -> None:
    s = _s()
    print(f"  {_paint('•', s.dim)} {text}")


def url(text: str) -> str:
    s = _s()
    return _paint(text, s.blue)


def emphasize(text: str) -> str:
    s = _s()
    return _paint(text, s.bold, s.yellow)


def success_text(text: str) -> str:
    s = _s()
    return _paint(text, s.green)


def celebrate(message: str) -> None:
    s = _s()
    print()
    print(f"{_paint('🎉', s.bold)} {_paint(message, s.bold, s.green)} {_paint('✨', s.bold)}")


def pause(seconds: float = 2.0) -> None:
    import time

    time.sleep(seconds)


def prompt(text: str) -> str:
    s = _s()
    return _paint(text, s.bold, s.cyan)


def key_name(text: str) -> str:
    s = _s()
    return _paint(text, s.dim)


# Back-compat for any code that imported S / _enabled
def _enabled() -> bool:
    return colors_enabled()


S = _s()
