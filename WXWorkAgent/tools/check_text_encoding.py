"""Validate repository text files are UTF-8 and not accidental mojibake."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TEXT_SUFFIXES = {
    ".bat",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".ts",
    ".vue",
    ".yaml",
    ".yml",
}

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "exports",
    "logs",
    "node_modules",
    "shots",
}

_MOJIBAKE_MARKERS = [
    "".join(map(chr, [0x6D7C, 0x4F74, 0x7B1F, 0x5BF0])),
    "".join(map(chr, [0x5BF0, 0xE1BB, 0x4FCA])),
    "".join(map(chr, [0x6FB6, 0x682D, 0x5134])),
    "".join(map(chr, [0x9A9E, 0x78B6])),
    "".join(map(chr, [0x93C8, 0x573D])),
    "".join(map(chr, [0x93C3, 0xE662])),
    chr(0x9239),
    chr(0x9225),
    chr(0x951B),
    chr(0x9286),
    chr(0x20AC) + "?",
]

MOJIBAKE_PATTERNS = [
    re.compile(r"[\uFFFD]"),
    re.compile("|".join(re.escape(marker) for marker in _MOJIBAKE_MARKERS)),
]

# These are intentional OCR fallback variants kept for matching old OCR output.
ALLOWLIST = {
    ("wxwork_export_recent.py", "璇煶"),
    ("wxwork_export_recent.py", "鍥剧墖宸茶繃鏈"),
    ("wxwork_export_recent.py", "@" + "".join(map(chr, [0x5BF0, 0xE1BB, 0x4FCA]))),
    ("wxwork_export_recent.py", "".join(map(chr, [0x6FB6, 0x682D, 0x5134]))),
}


def iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return sorted(files)


def is_allowlisted(rel: str, line: str) -> bool:
    return any(rel.endswith(file) and marker in line for file, marker in ALLOWLIST)


def check_file(root: Path, path: Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    errors: list[str] = []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [f"{rel}: invalid UTF-8: {exc}"]

    for idx, line in enumerate(text.splitlines(), start=1):
        if is_allowlisted(rel, line):
            continue
        for pattern in MOJIBAKE_PATTERNS:
            if pattern.search(line):
                errors.append(f"{rel}:{idx}: possible mojibake: {line.strip()[:160]}")
                break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    root = args.root.resolve()
    errors: list[str] = []
    for path in iter_text_files(root):
        errors.extend(check_file(root, path))

    if errors:
        print("Encoding check failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    print("Encoding check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
