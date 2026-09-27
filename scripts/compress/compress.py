#!/usr/bin/env python3
"""
Memory Compression Orchestrator

Usage:
    python3 -m compress <filepath>
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

# Windows consoles default to cp1252, which cannot encode the emoji glyphs in
# our status lines; replace unencodable characters instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# A fence marker at the start of a line, at CommonMark's 0-3 space indent.
FENCE_LINE_REGEX = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# YAML frontmatter: starts at file start with --- on its own line, ends with --- on its own line.
# Captures the entire block (including delimiters and trailing newline) and the body after.
FRONTMATTER_REGEX = re.compile(
    r"\A(---\r?\n.*?\r?\n---\r?\n)(.*)", re.DOTALL
)


def split_frontmatter(text: str):
    """Split YAML frontmatter from body. Returns (frontmatter, body).

    Memory files (and many other markdown docs) start with a YAML frontmatter
    block delimited by `---` lines. The compression LLM has a habit of stripping
    or rewriting these despite preserve-structure rules in the prompt — so we
    surgically remove the frontmatter before compression and prepend it back
    verbatim to the output. Files without frontmatter pass through unchanged.
    """
    m = FRONTMATTER_REGEX.match(text)
    if m:
        return m.group(1), m.group(2)
    return "", text

# Filenames and paths that almost certainly hold secrets or PII. Compressing
# them ships raw bytes to the model provider — a third-party data boundary that
# developers on sensitive codebases cannot cross. detect.py already skips .env
# by extension, but credentials.md / secrets.txt / ~/.aws/credentials would
# slip through the natural-language filter. This is a hard refuse before read.
SENSITIVE_BASENAME_REGEX = re.compile(
    r"(?ix)^("
    r"\.env(\..+)?"
    r"|\.netrc"
    r"|credentials(\..+)?"
    r"|secrets?(\..+)?"
    r"|passwords?(\..+)?"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|authorized_keys"
    r"|known_hosts"
    r"|.*\.(pem|key|p12|pfx|crt|cer|jks|keystore|asc|gpg)"
    r")$"
)

SENSITIVE_PATH_COMPONENTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})

SENSITIVE_NAME_TOKENS = (
    "secret", "credential", "password", "passwd",
    "apikey", "accesskey", "apitoken", "authtoken", "accesstoken", "privatekey",
)

# "token" only as a whole name word: a plain substring refused notes like
# feedback_quality_tokens_speed_priority.md.
SENSITIVE_NAME_WORDS = frozenset({"token"})


def data_home() -> Path:
    """Per-user data dir: %LOCALAPPDATA% on Windows, else $XDG_DATA_HOME or ~/.local/share."""
    if os.name == "nt" or sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        return Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def backup_dir_for(filepath: Path) -> Path:
    """Resolve the out-of-tree backup directory for a given source file.

    Backups must live OUTSIDE the source directory so skill auto-loaders
    (Antigravity skills/, workspace rules, etc.) stop re-ingesting the
    `.original.md` copies as live files. The base is COMPRESS_BACKUP_DIR when
    set, else data_home()/compress/backups.

    The source file's parent-dir name is mirrored under the base to reduce
    cross-project collisions (e.g. two `task.md` files in different repos).
    """
    override = os.environ.get("COMPRESS_BACKUP_DIR")
    base = Path(override) if override else data_home() / "compress" / "backups"
    return base / filepath.parent.name


def is_sensitive_path(filepath: Path) -> bool:
    """Heuristic denylist for files that must never be shipped to a third-party API."""
    name = filepath.name
    if SENSITIVE_BASENAME_REGEX.match(name):
        return True
    lowered_parts = {p.lower() for p in filepath.parts}
    if lowered_parts & SENSITIVE_PATH_COMPONENTS:
        return True
    if SENSITIVE_NAME_WORDS & set(re.split(r"[_\-\s.]+", name.lower())):
        return True
    # Normalize separators so "api-key" and "api_key" both match "apikey".
    lower = re.sub(r"[_\-\s.]", "", name.lower())
    return any(tok in lower for tok in SENSITIVE_NAME_TOKENS)


def strip_llm_wrapper(text: str) -> str:
    r"""Strip an outer ```markdown ... ``` fence when it wraps the ENTIRE output.

    The wrapper is only real when the first and last fence lines are the SAME
    block. The old regex (``\A\s*(fence)[^\n]*\n(.*)\n\1\s*\Z`` with DOTALL and
    a greedy ``.*``) never checked that: it matched any document that merely
    STARTS and ENDS with a fence line. An ordinary README section —
    ```bash npm install``` , prose, ```bash npm test``` — came back with its
    first and last fence markers deleted and its two code blocks merged into
    prose, so validation failed on both the compress and the fix path and the
    section was permanently uncompressible after three paid API calls.
    """
    lines = text.split("\n")
    first, last = 0, len(lines) - 1
    while first < len(lines) and not lines[first].strip():
        first += 1
    while last > first and not lines[last].strip():
        last -= 1
    if first >= last:
        return text
    opener = FENCE_LINE_REGEX.match(lines[first])
    closer = FENCE_LINE_REGEX.match(lines[last])
    if not opener or not closer:
        return text
    marker = opener.group(1)
    # Closing fence: same character, at least as long, and nothing else on the line.
    if closer.group(1)[0] != marker[0] or len(closer.group(1)) < len(marker):
        return text
    if lines[last].strip() != closer.group(1):
        return text
    # Any fence of the same kind in between means these two are not one block.
    for line in lines[first + 1:last]:
        inner = FENCE_LINE_REGEX.match(line)
        if inner and inner.group(1)[0] == marker[0] and len(inner.group(1)) >= len(marker):
            return text
    return "\n".join(lines[first + 1:last])


def write_text_atomic(path: Path, text: str, newline: str = "\n") -> None:
    """Write ``text`` to ``path`` atomically as UTF-8.

    Path.write_text() truncates the destination before encoding the string —
    a UnicodeEncodeError (or any other failure) partway through leaves a
    0-byte file, destroying whatever was there before (issue #655). Encode
    first, write the bytes to a sibling temp file, fsync, then os.replace()
    so the destination only ever moves from one complete, valid file to
    another. Preserves the original file's permission bits across the swap.

    ``newline`` is the line terminator to emit. Callers pass the terminator
    read_source() found in the source file so a CRLF document stays CRLF —
    text-mode writes translating LF to the platform default rewrote every
    line ending in every file the tool touched (issue #762), and reading the
    bytes ourselves means nothing translates them back.
    """
    if newline != "\n":
        # Normalise first: model output can already carry CRLF, and a bare
        # "\n" -> "\r\n" replace would turn those into "\r\r\n".
        text = text.replace("\r\n", "\n").replace("\n", newline)
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, preserving permission bits."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def read_source(filepath: Path) -> tuple[str, str, bytes]:
    """Read a source file as UTF-8, returning (text, line_terminator, raw_bytes).

    Decodes strictly. The old errors="ignore" silently DROPPED every byte that
    was not valid UTF-8 — a cp1252-authored file holding `\xe9` for "e-acute"
    lost that byte, the mangled text was what got written to the backup, the
    backup readback compared mangled-to-mangled so verification passed, and
    then the original was overwritten. The bytes were unrecoverable and
    nothing reported a problem (the destructive form of issue #686). A file we
    cannot read exactly is a file we must not rewrite.

    Line endings are detected from the raw bytes and returned to the caller
    rather than being universal-newline'd away, so write_text_atomic can put
    back what was there (issue #762). A mixed-ending file takes the terminator
    the majority of its lines use — presence of one CRLF is not a mandate to
    rewrite every LF in the document. The raw bytes come back too, so the
    backup can be a byte-for-byte copy rather than a re-rendering.
    """
    raw = filepath.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"Refusing to compress {filepath}: not valid UTF-8 "
            f"(byte 0x{raw[e.start]:02x} at offset {e.start}). "
            "Compression rewrites the file in place, and any byte this tool "
            "cannot decode would be destroyed by the round trip. "
            "Convert the file to UTF-8 first."
        ) from None
    crlf = text.count("\r\n")
    newline = "\r\n" if crlf * 2 > text.count("\n") else "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n"), newline, raw


def first_nonblank_line(text: str) -> str:
    """Return the first non-blank line, stripped — used to detect a prose
    preamble smuggled in ahead of the real content (issue #588)."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _write_target(filepath: Path, text: str | bytes, backup_path: Path, newline: str = "\n") -> None:
    """Write to the target file, surfacing the backup location if the write
    itself fails. write_text_atomic already leaves the target untouched on
    failure, but the caller still needs to know where the pre-compression
    original lives instead of being left to guess (issue #652).

    ``bytes`` restore the source verbatim; ``str`` is model output that still
    has to be rendered with the document's line terminator."""
    try:
        if isinstance(text, bytes):
            write_bytes_atomic(filepath, text)
        else:
            write_text_atomic(filepath, text, newline)
    except Exception:
        print(f"❌ Write to {filepath} failed. Original preserved at backup: {backup_path}")
        raise


from .detect import should_compress
from .validate import validate

MAX_RETRIES = 2


# ---------- Model Calls ----------

# The Gemini API and the agy CLI name the same model differently.
SDK_DEFAULT_MODEL = "gemini-3.8-flash"
AGY_DEFAULT_MODEL = "gemini-3.8-flash-high"
AGY_TIMEOUT_S = 600


def call_model(prompt: str) -> str:
    """Send a prompt to Gemini and return the reply text.

    Uses the google-genai SDK when GEMINI_API_KEY is set and the package is
    installed; otherwise runs one headless ``agy`` turn, which uses the
    signed-in Antigravity account. COMPRESS_MODEL overrides the model on
    either path and is passed through unchanged.
    """
    model = os.environ.get("COMPRESS_MODEL")
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            genai = None  # google-genai not installed, fall back to the CLI
        if genai:
            resp = genai.Client(api_key=api_key).models.generate_content(
                model=model or SDK_DEFAULT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level="high")
                ),
            )
            return strip_llm_wrapper((resp.text or "").strip())
    return strip_llm_wrapper(call_agy(prompt, model or AGY_DEFAULT_MODEL).strip())


def call_agy(prompt: str, model: str) -> str:
    """One agy turn with the prompt on stdin.

    ``agy --print`` takes the prompt as an argument, and a Windows command line
    stops at 32K characters, so the prompt goes in as a stream-json ``user``
    event instead. The empty temp dir as cwd keeps the turn out of any repo.
    """
    agy_bin = shutil.which("agy") or "agy"
    event = json.dumps({"event": "user", "message": {"content": prompt}})
    with tempfile.TemporaryDirectory(prefix="compress-") as cwd:
        proc = subprocess.run(
            [agy_bin, "--model", model, "--disable-slash-commands",
             "--print-timeout", f"{AGY_TIMEOUT_S}s",
             "--input-format", "stream-json", "--output-format", "stream-json"],
            input=event + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=AGY_TIMEOUT_S + 60,  # agy's own limit fires first; this catches a hung process
        )
    for line in reversed(proc.stdout.splitlines()):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") != "result":
            continue
        result = msg.get("result") or {}
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"agy call failed: {result.get('error') or result.get('status')}")
        return result.get("response") or ""
    raise RuntimeError(f"agy call failed (exit {proc.returncode}):\n{proc.stderr.strip()[-800:]}")


def build_compress_prompt(original: str) -> str:
    return f"""Rewrite the markdown document below so it is shorter. The reader is an AI coding agent that works best with direct, clear instructions.

<document>
{original}
</document>

Based on the document above, rewrite it with these rules.

Style:
- Use plain, short English sentences with normal grammar. Do not drop articles, verbs, or connecting words to save space.
- Cut filler, hedging, pleasantries, repetition, and background that no rule needs.
- Keep every rule, number, limit, negation, and name. A "never", "not", "only", or "must" in the original stays in the output.
- One idea per sentence. Keep lists as lists.

Keep these exactly as they are:
- Everything inside ``` code blocks and 4-space-indented code blocks.
- Everything inside inline backticks.
- Every URL, heading, file path, and command.

Output:
- Do not use any tools. Do not open the URLs, files or commands in the document; they are text to keep.
- Return only the rewritten markdown body.
- Do not wrap the whole output in a ```markdown fence or any other fence. Code blocks from the original stay as they are.
"""


def build_fix_prompt(original: str, compressed: str, errors: List[str]) -> str:
    errors_str = "\n".join(f"- {e}" for e in errors)
    return f"""A shortened markdown file failed validation against its original. Fix only the listed errors.

<original>
{original}
</original>

<shortened>
{compressed}
</shortened>

<errors>
{errors_str}
</errors>

Based on the files above, fix the shortened file with these rules:
- Fix only the listed errors. Leave every other line exactly as it is. Do not shorten or reword anything else.
- Use the original only as the source for restoring missing content.
- Missing URL: copy it from the original into the matching place in the shortened file.
- Code block mismatch: copy the exact code block from the original.
- Heading mismatch: copy the exact heading text from the original.

Do not use any tools. Do not open the URLs, files or commands in the files; they are text to keep.
Return only the fixed shortened file, with no explanation.
"""


# ---------- Core Logic ----------


def compress_file(filepath: Path) -> bool:
    # Resolve and validate path
    filepath = filepath.resolve()
    MAX_FILE_SIZE = 500_000  # 500KB
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    if filepath.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large to compress safely (max 500KB): {filepath}")

    # Refuse files that look like they contain secrets or PII. Compressing ships
    # the raw bytes to the model provider — a third-party boundary — so we fail
    # loudly rather than silently exfiltrate credentials or keys. Override is
    # intentional: the user must rename the file if the heuristic is wrong.
    if is_sensitive_path(filepath):
        raise ValueError(
            f"Refusing to compress {filepath}: filename looks sensitive "
            "(credentials, keys, secrets, or known private paths). "
            "Compression sends file contents to the Gemini model. "
            "Rename the file if this is a false positive."
        )

    print(f"Processing: {filepath}")

    if not should_compress(filepath):
        print("Skipping (not natural language)")
        return False

    original_text, newline, original_raw = read_source(filepath)
    # Store backup outside the source directory so skill auto-loaders don't
    # re-ingest the `.original.md` copy as a live file. Mirror the source's
    # parent-dir name + stem under a platform-aware base to reduce collisions.
    backup_dir = backup_dir_for(filepath)
    backup_path = backup_dir / (filepath.stem + ".original.md")

    if not original_text.strip():
        print("❌ Refusing to compress: file is empty or whitespace-only.")
        return False

    # Check if backup already exists to prevent accidental overwriting
    if backup_path.exists():
        print(f"⚠️ Backup file already exists: {backup_path}")
        print("The original backup may contain important content.")
        print("Aborting to prevent data loss. Please remove or rename the backup file if you want to proceed.")
        return False

    # Split YAML frontmatter off before compression. The model tends to strip or
    # rewrite frontmatter despite preserve-structure rules; we keep it verbatim
    # by removing it from the input and re-prepending it to the output.
    frontmatter, body = split_frontmatter(original_text)
    if frontmatter:
        print(f"Detected YAML frontmatter ({len(frontmatter)} chars) — preserving verbatim")

    if not body.strip():
        print("❌ Refusing to compress: body is empty after frontmatter removal.")
        return False

    # Step 1: Compress (body only, frontmatter excluded)
    print("Compressing with Gemini...")
    compressed_body = call_model(build_compress_prompt(body))

    if compressed_body is None or not compressed_body.strip():
        print("❌ Compression aborted: the model returned an empty response.")
        print("   Original file is untouched (no backup created).")
        return False

    # Compare the BODY (not the whole file) — frontmatter is preserved verbatim
    # and would never change, so identity must be judged on the compressible part.
    if compressed_body.strip() == body.strip():
        print("❌ Compression aborted: output is identical to input.")
        print("   Likely causes: the model refused, returned the prompt verbatim, or the file is")
        print("   already compressed. Original file is untouched (no backup created).")
        return False

    # Reassemble: frontmatter (verbatim) + compressed body
    compressed = frontmatter + compressed_body

    # Save original as backup, then verify the backup readback before
    # touching the input file. If the filesystem dropped bytes (encoding,
    # antivirus, disk full), unlink the bad backup and abort instead of
    # leaving the user with a corrupt backup + compressed primary.
    backup_dir.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(backup_path, original_raw)
    if backup_path.read_bytes() != original_raw:
        print(f"❌ Backup write verification failed: {backup_path}")
        print("   In-memory original differs from on-disk backup. Aborting before touching the input file.")
        try:
            backup_path.unlink()
        except OSError:
            pass
        return False
    _write_target(filepath, compressed, backup_path, newline)

    def restore(reason: str) -> None:
        _write_target(filepath, original_raw, backup_path, newline)
        backup_path.unlink(missing_ok=True)
        print(f"❌ {reason} — original restored")

    try:
        ok = _validate_and_fix(filepath, backup_path, original_text, compressed, newline)
    except BaseException:
        restore("Error after the file was rewritten")
        raise
    if not ok:
        restore("Failed after retries")
    return ok


def _validate_and_fix(filepath: Path, backup_path: Path, original_text: str,
                      compressed: str, newline: str) -> bool:
    """Validate the rewritten target, asking the model for fixes; False if it never passes."""
    for attempt in range(MAX_RETRIES):
        print(f"\nValidation attempt {attempt + 1}")

        result = validate(backup_path, filepath)

        if result.is_valid:
            print("Validation passed")
            break

        print("❌ Validation failed:")
        for err in result.errors:
            print(f"   - {err}")

        if attempt == MAX_RETRIES - 1:
            return False

        print("Fixing with Gemini...")
        compressed = call_model(
            build_fix_prompt(original_text, compressed, result.errors)
        )

        if compressed is None or not compressed.strip():
            print("❌ Fix attempt aborted: the model returned an empty response.")
            print("   Skipping this attempt.")
            continue

        # Guard against a prose preamble smuggled in ahead of the real fixed
        # content (issue #588). Only enforced when the original starts with a
        # structural anchor (frontmatter `---` or a heading) — plain-prose
        # first lines get legitimately rewritten by compression, and requiring
        # them verbatim would reject every valid fix.
        anchor = first_nonblank_line(original_text)
        if anchor.startswith(("---", "#")) and first_nonblank_line(compressed) != anchor:
            print("❌ Fix attempt aborted: output does not start with the original's first line.")
            print("   Possible preamble leak. Skipping this attempt.")
            continue

        _write_target(filepath, compressed, backup_path, newline)

    return True
