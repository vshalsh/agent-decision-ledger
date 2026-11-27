"""TTY-aware ANSI styling per DESIGN_TOKENS.md (colour is a secondary encoding only)."""

from __future__ import annotations

import os
from typing import IO

RED, YELLOW, GREEN, DIM = "31", "33", "32", "2"


def use_color(stream: IO) -> bool:
    """Colour only for an interactive TTY, and never when NO_COLOR is set (non-empty)."""
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def fail(self, text: str) -> str:
        return self._wrap(RED, text)

    def warn(self, text: str) -> str:
        return self._wrap(YELLOW, text)

    def ok(self, text: str) -> str:
        return self._wrap(GREEN, text)

    def dim(self, text: str) -> str:
        return self._wrap(DIM, text)
