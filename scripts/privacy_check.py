#!/usr/bin/env python3
"""Block commits that would publish personal data.

Checks the files staged for commit (or, with --all, every tracked file) against:
  * regex patterns in the git-ignored `.privacy-patterns` file (one per line, `#` comments;
    the patterns themselves are personal, so they never enter git), and
  * every secret value in `.env` (8+ characters).

Installed as a pre-commit hook by `scripts/install-hooks.sh`. Exit code 1 lists each hit.
Run by hand: python3 scripts/privacy_check.py [--all]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_FILE = ROOT / ".privacy-patterns"
ENV_FILE = ROOT / ".env"


def load_patterns() -> list[re.Pattern]:
    if not PATTERNS_FILE.exists():
        return []
    out = []
    for n, line in enumerate(PATTERNS_FILE.read_text().splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line))
            except re.error as e:
                sys.exit(f"{PATTERNS_FILE.name}:{n}: bad regex {line!r}: {e}")
    return out


def load_secrets() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    pairs = (l.strip().partition("=") for l in ENV_FILE.read_text().splitlines() if "=" in l and not l.startswith("#"))
    return {k: v for k, _, v in pairs if len(v) >= 8}


def files_to_check(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    return [f for f in subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
            if (ROOT / f).is_file()]


def staged_text(path: str, all_files: bool) -> str:
    if all_files:
        return (ROOT / path).read_text(errors="ignore")
    # what will actually be committed, not the working-tree copy
    return subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True, text=True, errors="ignore").stdout


def main() -> int:
    all_files = "--all" in sys.argv
    patterns, secrets = load_patterns(), load_secrets()
    if not patterns:
        print(f"privacy_check: no {PATTERNS_FILE.name} — only .env secrets are checked", file=sys.stderr)
    hits = []
    for f in files_to_check(all_files):
        text = staged_text(f, all_files)
        for pat in patterns:
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{f}:{line}: matches {pat.pattern!r} ({m.group(0)[:40]!r})")
                break
        for name, value in secrets.items():
            if value in text:
                hits.append(f"{f}: contains the value of .env {name}")
    if hits:
        print("privacy_check: personal data in files about to be committed:\n  " + "\n  ".join(hits), file=sys.stderr)
        print("Fix it, or (if it's a false positive) refine .privacy-patterns.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
