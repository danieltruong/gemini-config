#!/usr/bin/env python3
"""Shorten markdown instruction files (GEMINI.md, SKILL.md, agent files) with Gemini.

usage: compress-docs.py <file.md> [more.md ...]

Rewrites each file in place in plain, short English. Code blocks, inline code,
URLs, headings and paths are kept and checked afterwards; a file that fails the
check twice is put back as it was.

Model: gemini-3.8-flash at thinking level high through the Gemini API when GEMINI_API_KEY is set and
the google-genai package is installed, otherwise gemini-3.8-flash-high through
one headless `agy` turn. Set COMPRESS_MODEL to use another model; its value is
passed to whichever of the two runs, so use that side's model name.

Each run keeps the originals under its own dated folder (printed at the end),
so the same file can be shortened again later.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from compress.compress import data_home  # noqa: E402


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if not argv:
        print("usage: compress-docs.py <file.md> [more.md ...]", file=sys.stderr)
        return 2

    targets = [Path(arg).resolve() for arg in argv]
    missing = [arg for arg, t in zip(argv, targets) if not t.is_file()]
    if missing:
        print("not a file: " + ", ".join(missing), file=sys.stderr)
        return 1

    # The engine refuses to overwrite a backup, so every run gets a fresh base.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = data_home() / "compress-runs" / f"{stamp}-{os.getpid()}"
    env = dict(os.environ, COMPRESS_BACKUP_DIR=str(run_dir))
    try:
        for target in targets:
            before = target.stat().st_size
            print(f"== {target} ({before} bytes)", flush=True)
            code = subprocess.run([sys.executable, "-m", "compress", str(target)], cwd=HERE, env=env).returncode
            if code:
                return code
            print(f"   {before} -> {target.stat().st_size} bytes")
        return 0
    finally:
        print(f"originals kept under: {run_dir}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
