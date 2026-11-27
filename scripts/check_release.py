#!/usr/bin/env python3
"""Release gate: fail when a tree that is about to be published carries anything it must not.

Scans a directory tree (the repository, an unpacked Docker image filesystem, or an
unzipped extension build) for:

  * secrets        - provider keys, Stripe keys, private keys, generic high-entropy assignments
  * personal data  - machine paths, and the releaser's own names from a private, untracked file
  * old branding   - retired names from the same private file
  * forbidden files- databases, key/token files, env files, logs, dumps, backups, oversized files

Every finding is printed with file and line. Exit status is 1 when anything is found,
0 when the tree is clean. Allowlisted findings (known fake test strings, documented
exceptions) live in scripts/release_allowlist.txt as `path-glob :: regex` lines.

Usage:
    python scripts/check_release.py [ROOT] [--git-tracked-only] [--max-mb N] [--private-patterns PATH]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".wxt",
    ".mypy_cache", ".ruff_cache", "test-results", "playwright-report",
}
SKIP_DIR_PREFIXES = (".output",)
TEXT_SUFFIX_BLOCKLIST = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".onnx", ".woff", ".woff2",
    ".ttf", ".otf", ".zip", ".gz", ".pdf", ".mp4", ".webm",
}

SECRET_PATTERNS = [
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{24,}")),
    ("stripe-secret", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("stripe-webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{16,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("starling-device-token", re.compile(r"\bstl_dev_[A-Za-z0-9_-]{20,}")),
    ("generic-secret-assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|password|passwd|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]")),
]

# Names that must not appear in a release are PRIVATE to whoever cuts it, so they are not written
# here: this file is published with everything else, and a scanner that spells out somebody's
# email address and machine user, or a retired internal name, hands the reader exactly what it
# exists to keep out. They live in `scripts/release_private_patterns.json`, which Git ignores:
#
#     {"personal": {"my-email": "(?i)jane@doe\\.example", ...}, "brand": {"old-name": "(?i)acme"}}
#
# Without that file the gate still looks for secrets, machine paths and forbidden files, and says
# that it looked for no names. `--private-patterns PATH` (or STARLING_RELEASE_PRIVATE_PATTERNS)
# names another file, for scanning a tree that does not hold the private one (an exported copy, an
# unpacked image). `scripts/check_site.py` bans the same names from the website's pages.
PRIVATE_PATTERNS_FILE = "release_private_patterns.json"

# Generic, and safe to ship: a home directory of any user on any machine.
PERSONAL_PATTERNS = [
    ("home-directory-path", re.compile(r"(?i)\b[a-z]:\\users\\[a-z0-9._ -]+\\|(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+/")),
    ("synced-folder-path", re.compile(r"(?i)onedrive[\\/]|dropbox[\\/]|google ?drive[\\/]")),
]
BRAND_PATTERNS: list[tuple[str, re.Pattern[str]]] = []


def load_private_patterns(explicit: str | None) -> tuple[int, str | None]:
    """Add the builder's private names to the two lists. Returns (how many, where from)."""
    candidate = explicit or os.environ.get("STARLING_RELEASE_PRIVATE_PATTERNS") or str(REPO_ROOT / "scripts" / PRIVATE_PATTERNS_FILE)
    source = Path(candidate)
    if not source.is_file():
        if explicit:
            raise SystemExit(f"private patterns file not found: {source}")
        return 0, None
    data = json.loads(source.read_text(encoding="utf-8"))
    added = 0
    for group, target in (("personal", PERSONAL_PATTERNS), ("brand", BRAND_PATTERNS)):
        for name, pattern in (data.get(group) or {}).items():
            target.append((str(name), re.compile(str(pattern))))
            added += 1
    return added, str(source)


FORBIDDEN_NAME_PATTERNS = [
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.key", "*.pem", "*.pfx",
    "*.p12", "*.token", ".env", ".env.*", "*.log", "*.stdout", "*.stderr", "*.bak",
    "*.bak-*", "*crash-report*", "*.dmp", "*backup*",
]
FORBIDDEN_NAME_EXCEPTIONS = {".env.example", ".env.e2e", "check_release.py"}


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_allowlist(root: Path) -> list[tuple[str, re.Pattern[str]]]:
    """Rules from this repository's allowlist, plus the scanned tree's own copy if it has one."""
    rules: list[tuple[str, re.Pattern[str]]] = []
    seen: set[Path] = set()
    for candidate in (REPO_ROOT / "scripts" / "release_allowlist.txt", root / "scripts" / "release_allowlist.txt"):
        path = candidate.resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "::" not in line:
                continue
            glob, _, regex = line.partition("::")
            rules.append((glob.strip(), re.compile(regex.strip())))
    return rules


def relative_names(root: Path, path: Path) -> tuple[str, ...]:
    """The path relative to the scan root and, for files inside this repository, to the repository,
    so allowlist globs (written from the repository root) match whichever folder is scanned."""
    names = [path.relative_to(root).as_posix()]
    try:
        names.append(path.resolve().relative_to(REPO_ROOT).as_posix())
    except ValueError:
        pass
    return tuple(dict.fromkeys(names))


def allowed(rules, rels: tuple[str, ...], category: str, text: str) -> bool:
    for glob, regex in rules:
        if any(fnmatch.fnmatch(rel, glob) for rel in rels) and (regex.search(text) or regex.search(category)):
            return True
    return False


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [root / p for p in out.split("\0") if p]


def walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(SKIP_DIR_PREFIXES)
        ]
        files.extend(Path(dirpath) / name for name in filenames)
    return files


def scan(root: Path, files: list[Path], max_bytes: int) -> list[str]:
    rules = load_allowlist(root)
    findings: list[str] = []
    for path in sorted(files):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rels = relative_names(root, path)
        name = path.name
        if name not in FORBIDDEN_NAME_EXCEPTIONS and any(
            fnmatch.fnmatch(name.lower(), pat) for pat in FORBIDDEN_NAME_PATTERNS
        ):
            if not allowed(rules, rels, "forbidden-file", name):
                findings.append(f"forbidden-file  {rel}")
        size = path.stat().st_size
        if size > max_bytes and not allowed(rules, rels, "oversized-file", name):
            findings.append(f"oversized-file  {rel}  ({size / 1_048_576:.1f} MB)")
        if path.suffix.lower() in TEXT_SUFFIX_BLOCKLIST or size > 8 * 1_048_576:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for group, patterns in (("secret", SECRET_PATTERNS),
                                    ("personal", PERSONAL_PATTERNS),
                                    ("brand", BRAND_PATTERNS)):
                for category, pattern in patterns:
                    match = pattern.search(line)
                    if not match:
                        continue
                    if allowed(rules, rels, category, line):
                        continue
                    snippet = line.strip()
                    if group == "secret":
                        token = match.group(0)
                        snippet = snippet.replace(token, token[:6] + "…[redacted]")
                    findings.append(f"{group}:{category}  {rel}:{lineno}  {snippet[:160]}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--git-tracked-only", action="store_true",
                        help="scan only files git would commit (tracked plus unignored untracked)")
    parser.add_argument("--max-mb", type=float, default=5.0)
    parser.add_argument("--private-patterns", metavar="PATH",
                        help=f"names this release must not contain (default: scripts/{PRIVATE_PATTERNS_FILE}, if it exists)")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    private_count, private_source = load_private_patterns(args.private_patterns)
    files = tracked_files(root) if args.git_tracked_only else walk_files(root)
    findings = scan(root, files, int(args.max_mb * 1_048_576))
    names = (f"{private_count} private name patterns" if private_count
             else "NO private name patterns: only secrets, machine paths and forbidden files were looked for")
    if findings:
        print(f"RELEASE GATE: FAIL ({len(findings)} findings in {len(files)} files; {names})")
        for finding in findings:
            print("  " + finding)
        return 1
    print(f"RELEASE GATE: PASS ({len(files)} files scanned under {root}; {names})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
