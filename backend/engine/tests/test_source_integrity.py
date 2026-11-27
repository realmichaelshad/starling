"""No source file may contain a control character where an escape belongs.

This has now happened twice. Writing a pattern containing `\\b` through nested
shell heredocs produces a literal backspace byte (0x08) instead of the escape,
and the result is silent: the module imports, the regex compiles, and it simply
never matches. The first time it disabled an AI-tell detector. The second time
it disabled the signal that decides whether his own posts have a reason to be
read — and the only visible symptom was a number that did not move.

An existing check covered one module. A whole-tree check costs a millisecond
and covers every file anyone adds later, which is the point: the bug is not
about a particular module, it is about how the file was written.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Control characters that have no business in source. Tab, newline and carriage
# return are the legitimate ones.
FORBIDDEN = {code: name for code, name in (
    (0x07, "bell (probably meant \\a)"),
    (0x08, "backspace (almost certainly meant \\b in a regex)"),
    (0x0B, "vertical tab (probably meant \\v)"),
    (0x0C, "form feed (probably meant \\f)"),
    (0x1B, "escape (probably meant \\e or an ANSI sequence)"),
)}


def python_files() -> list[Path]:
    return sorted(path for folder in ("app", "tools", "tests")
                  for path in (ROOT / folder).rglob("*.py"))


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_stray_control_characters(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = sorted({code for code in FORBIDDEN if chr(code) in text})
    assert not found, (
        f"{path.relative_to(ROOT)} contains "
        + ", ".join(f"0x{code:02X} — {FORBIDDEN[code]}" for code in found)
        + ". A regex escape was written as a raw control byte, so the pattern "
          "compiles and never matches."
    )
