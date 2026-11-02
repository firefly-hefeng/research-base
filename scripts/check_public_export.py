"""Check an explicit public file set without opening private local artifacts.

By default inspect only the allowed source/documentation roots. --git-index
instead checks the exact tracked/staged file names, rejecting private or unknown
roots. Findings never print matching secret text. This is a local review helper,
not a guarantee that arbitrary content is safe to publish.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


PUBLIC_FILES = {
    "README.md", "pyproject.toml", ".gitignore", ".env.example", "LICENSE",
    "CONTRIBUTING.md", "SECURITY.md", "CITATION.cff", ".gitattributes",
}
PUBLIC_ROOTS = {"src", "tests", "docs", "scripts", "examples", "templates", ".github"}
TEXT_SUFFIXES = {".py", ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".cff"}
PRIVATE_PARTS = {".research-base", ".research", ".venv", ".git", "__pycache__", ".pytest_cache", "node_modules", "objects", "derived"}
PRIVATE_SUFFIXES = {".pdf", ".sqlite", ".sqlite3", ".db", ".pem", ".p12", ".pfx", ".key", ".crdownload", ".pyc"}
SECRET_PATTERNS = {
    "service_token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "personal_path": re.compile(r"(?i)(?:[A-Z]:)?[/\\](?:" + "|".join(("Users", "home", "Documents and Settings")) + r")[/\\][^\s\"'<>`]+"),
    "url_credential": re.compile(r"https?://[^\s/\"'<>]+:[^\s/\"'<>]+@"),
}


def public_name(path: Path) -> bool:
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(part in PRIVATE_PARTS for part in path.parts) or path.suffix.lower() in PRIVATE_SUFFIXES:
        return False
    if path.name.startswith(".env") and path.as_posix() != ".env.example":
        return False
    return path.as_posix() in PUBLIC_FILES or bool(path.parts and path.parts[0] in PUBLIC_ROOTS)


def selected_paths(root: Path, git_index: bool) -> list[Path]:
    if git_index:
        result = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, check=True)
        return sorted({Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value})
    paths = [Path(name) for name in PUBLIC_FILES if (root / name).is_file()]
    for name in PUBLIC_ROOTS:
        directory = root / name
        if directory.is_dir():
            # Do not descend into bytecode/private folders when selecting sources.
            paths.extend(path.relative_to(root) for path in directory.rglob("*")
                         if path.is_file() and not any(part in PRIVATE_PARTS for part in path.relative_to(root).parts))
    return sorted(set(paths))


def check(root: Path, paths: list[Path], *, git_index=False) -> list[dict]:
    findings = []
    selected = {path.as_posix() for path in paths if public_name(path)}
    for relative in paths:
        name = relative.as_posix()
        if not public_name(relative):
            findings.append({"file": name, "kind": "unapproved_file", "severity": "error"})
            continue  # Never open files outside the public selection.
        path = root / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.append({"file": name, "kind": "missing_or_indirect_file", "severity": "error"})
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and name not in PUBLIC_FILES:
            findings.append({"file": name, "kind": "unreviewed_file_type", "severity": "error"})
            continue
        try:
            if git_index:
                # Inspect the bytes that would be committed, not an unstaged fix.
                blob = subprocess.run(["git", "show", ":" + name], cwd=root, capture_output=True, check=True).stdout
                content = blob.decode("utf-8-sig")
            else:
                content = path.read_text(encoding="utf-8-sig")
        except (UnicodeError, OSError, subprocess.CalledProcessError):
            findings.append({"file": name, "kind": "unreadable_text", "severity": "error"})
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(content):
                findings.append({"file": name, "line": content.count("\n", 0, match.start()) + 1,
                                 "kind": kind, "severity": "error"})
        if path.suffix.lower() != ".md":
            continue
        for match in re.finditer(r"\]\(([^)]+)\)", content):
            link = match[1].strip("<>").split("#", 1)[0]
            parsed = urlsplit(link)
            if not link or parsed.scheme or parsed.netloc:
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root):
                kind = "link_outside_public_root"
            elif target.relative_to(root).as_posix() not in selected:
                kind = "unpublished_local_link"
            else:
                continue
            findings.append({"file": name, "line": content.count("\n", 0, match.start()) + 1,
                             "kind": kind, "severity": "warning", "target": link})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--git-index", action="store_true", help="Inspect only the exact tracked/staged file names")
    parser.add_argument("--strict-links", action="store_true", help="Also fail on links to files absent from the public selection")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        paths = selected_paths(root, args.git_index)
    except (OSError, subprocess.CalledProcessError):
        print(json.dumps({"ok": False, "error": "Cannot inspect the Git index; initialize and stage the intended public files first."}))
        return 2
    findings = check(root, paths, git_index=args.git_index)
    counts = Counter(item["severity"] for item in findings)
    ok = not counts["error"] and not (args.strict_links and counts["warning"])
    print(json.dumps({"ok": ok, "mode": "git-index" if args.git_index else "allowlist", "file_count": len(paths),
                      "error_count": counts["error"], "warning_count": counts["warning"], "findings": findings}, ensure_ascii=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
