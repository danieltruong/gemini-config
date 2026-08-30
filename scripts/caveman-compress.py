#!/usr/bin/env python3
"""Portable caveman compressor for instruction documents.

Usage: caveman-compress.py <file.md> [more.md ...]
Creates a backup with timestamp and applies compression rules.
"""
import os
import re
import sys
import time

def compress_text(text):
    # Preserve frontmatter if present
    fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if fm_match:
        frontmatter = fm_match.group(0)
        body = text[len(frontmatter):]
    else:
        frontmatter = ""
        body = text

    # Remove markdown filler phrases while keeping code, paths, tables intact
    fillers = [
        r"\b(?:it is important to note that|please note that|it should be noted that)\b\s*",
        r"\b(?:in order to|as a matter of fact|at the end of the day)\b\s*",
        r"\b(?:additionally|furthermore|moreover|consequently)\b,?\s*",
        r"\b(?:basically|essentially|actually|literally)\b\s*",
    ]
    for pattern in fillers:
        body = re.sub(pattern, "", body, flags=re.IGNORECASE)

    # Normalize multiple blank lines
    body = re.sub(r"\n{3,}", "\n\n", body)
    return frontmatter + body

def main():
    if len(sys.argv) < 2:
        print("Usage: caveman-compress.py <file.md> [...]", file=sys.stderr)
        sys.exit(1)

    backup_dir = os.path.expanduser(f"~/.gemini/tmp/backups/{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(backup_dir, exist_ok=True)

    for target in sys.argv[1:]:
        if not os.path.isfile(target):
            print(f"Skipping non-file: {target}", file=sys.stderr)
            continue

        raw = open(target, encoding="utf-8").read()
        before_len = len(raw.encode("utf-8"))

        # Save backup
        backup_path = os.path.join(backup_dir, os.path.basename(target))
        with open(backup_path, "w", encoding="utf-8") as bf:
            bf.write(raw)

        compressed = compress_text(raw)
        after_len = len(compressed.encode("utf-8"))

        with open(target, "w", encoding="utf-8") as out:
            out.write(compressed)

        print(f"Compressed {target}: {before_len} -> {after_len} bytes (backup at {backup_path})")

if __name__ == "__main__":
    main()
