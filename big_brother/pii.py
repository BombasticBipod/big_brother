"""Find personal data before it is published.

Detects email addresses (except placeholder and no-reply ones), phone numbers,
home directory paths that name a user, and any term from a private denylist
(names, hostnames, handles). The denylist lives outside the repository, by
default in ~/.config/big_brother/pii_terms.txt, because it is PII itself.

`scan_tree` checks files git would publish; `scan_history` checks every commit's
author, committer and message, and every blob reachable from any ref. Output
masks each match so the report does not repeat the data it found. A line that
contains the marker `pii: fake` is skipped, for made-up data in tests.

Usage: python -m big_brother.pii [REPO] [--terms FILE] [--history]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TERMS = Path.home() / ".config" / "big_brother" / "pii_terms.txt"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w.+])(?:\+1[ .-]?)?(?:\(\d{3}\) ?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w.])")
HOME = re.compile(r"/(?:home|Users)/([A-Za-z0-9._-]+)")
PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")
GENERIC_USERS = {"user"}
FAKE_MARKER = "pii: fake"  # a line carrying this marker holds made-up test data


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    match: str
    col: int = 0


def _safe_email(email: str) -> bool:
    local, domain = email.lower().rsplit("@", 1)
    if domain == "users.noreply.github.com" or local.startswith("noreply"):
        return True
    return any(domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS)


def scan_text(text: str, terms: Sequence[str] = (), path: str = "") -> list[Finding]:
    term_res = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I)
                for t in terms]
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        if FAKE_MARKER in line:
            continue
        for m in EMAIL.finditer(line):
            email, col = m.group(), m.start()
            if col and line[col - 1] == "\\":  # "\n@x" in source is an escape, not an address
                email, col = email[1:], col + 1
            if not email.startswith("@") and not _safe_email(email):
                found.append(Finding(path, n, "email", email, col))
        for m in PHONE.finditer(line):
            found.append(Finding(path, n, "phone", m.group(), m.start()))
        for m in HOME.finditer(line):
            if m.group(1) not in GENERIC_USERS:
                found.append(Finding(path, n, "home_path", m.group(), m.start()))
        for term_re in term_res:
            for m in term_re.finditer(line):
                found.append(Finding(path, n, "term", m.group(), m.start()))
    return sorted(found, key=lambda f: (f.line, f.col))


def mask(value: str) -> str:
    if "@" in value:
        local, domain = value.rsplit("@", 1)
        return f"{local[:1]}***@{domain}"
    return value[:1] + "***"


def load_terms(path: Path | str) -> list[str]:
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    return [s.strip() for s in lines if s.strip() and not s.strip().startswith("#")]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_tree(repo: Path | str, terms: Sequence[str] = ()) -> list[Finding]:
    """Scan tracked and untracked, non-ignored files in the working tree."""
    repo = Path(repo)
    found = []
    for rel in sorted(_git(repo, "ls-files", "-co", "--exclude-standard", "-z").split("\0")):
        file = repo / rel
        if not rel or not file.is_file():
            continue
        text = _decode(file.read_bytes())
        if text is not None:
            found += scan_text(text, terms, rel)
    return found


def scan_history(repo: Path | str, terms: Sequence[str] = ()) -> list[Finding]:
    """Scan commit metadata and every blob reachable from any ref."""
    repo = Path(repo)
    found = []
    log = _git(repo, "log", "--all", "--format=%H%x1f%an <%ae>%n%cn <%ce>%n%B%x1e")
    for record in log.split("\x1e"):
        if "\x1f" in record:
            sha, text = record.strip("\n").split("\x1f", 1)
            found += scan_text(text, terms, f"commit {sha[:7]}")
    seen = set()
    for row in _git(repo, "rev-list", "--objects", "--all").splitlines():
        sha, _, name = row.partition(" ")
        if not name or sha in seen:
            continue
        seen.add(sha)
        if _git(repo, "cat-file", "-t", sha).strip() != "blob":
            continue
        data = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", sha],
                              check=True, capture_output=True).stdout
        text = _decode(data)
        if text is not None:
            found += scan_text(text, terms, f"blob {sha[:7]} {name}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find PII in a git repository.")
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--terms", default=str(DEFAULT_TERMS), help="denylist file, one term per line")
    parser.add_argument("--history", action="store_true", help="also scan all commits and blobs")
    args = parser.parse_args(argv)
    terms = load_terms(args.terms)
    found = scan_tree(args.repo, terms)
    if args.history:
        found += scan_history(args.repo, terms)
    for f in found:
        print(f"{f.path}:{f.line}: {f.kind} {mask(f.match)}")
    print(f"{len(found)} finding(s), {len(terms)} denylist term(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
