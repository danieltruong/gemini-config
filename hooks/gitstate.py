#!/usr/bin/env python3
"""What a workspace holds right now, and what the gate already proved about it.

A fingerprint is taken from git, not from the transcript: HEAD, the working tree's diff
against it, and the untracked files git does not ignore. Whatever changed the tree, and
however the command line spelled it, the fingerprint moves.
"""
import hashlib
import os
import subprocess
import time

import hookpaths

GIT_TIMEOUT = 30
# a file this big is fingerprinted by size and mtime, so a stop never reads a whole asset
SIZE_CAP = 2_000_000
VERIFIED = os.path.join(hookpaths.TMP, "verified")
REVIEWED = os.path.join(hookpaths.TMP, "reviewed")
SEEN = os.path.join(hookpaths.TMP, "seen")


def git(ws, *args):
    """stdout bytes, or None when git fails: not a repo, no commits, git missing."""
    try:
        proc = subprocess.run(["git", *args], cwd=ws, capture_output=True, timeout=GIT_TIMEOUT)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def text(ws, *args):
    out = git(ws, *args)
    return None if out is None else out.decode("utf-8", "replace")


def lines(ws, *args):
    return [ln.strip().replace("\\", "/") for ln in (text(ws, *args) or "").splitlines()
            if ln.strip()]


def is_repo(ws):
    return text(ws, "rev-parse", "--git-dir") is not None


def toplevel(path):
    """The repository root above a path, or '' when it is not in a repo."""
    if not os.path.isdir(path):
        return ""
    out = text(path, "rev-parse", "--show-toplevel")
    return hookpaths.real_path((out or "").strip())


def file_hash(path):
    try:
        stat = os.stat(path)
    except OSError:
        return "gone"
    if stat.st_size > SIZE_CAP:
        return f"stat:{stat.st_size}:{int(stat.st_mtime)}"
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "unreadable"


def tracked_hash(ws):
    """HEAD plus the diff against it, hashed. None when ws is not a git repository."""
    if not is_repo(ws):
        return None
    head = (text(ws, "rev-parse", "HEAD") or "no-head").strip()
    # --binary so a changed image or archive moves the hash like any other file
    diff = git(ws, "diff", "--binary", "HEAD")
    if diff is None:
        diff = git(ws, "diff", "--binary", "--cached") or b""
    digest = hashlib.sha256(head.encode())
    digest.update(b"\0")
    digest.update(diff)
    return digest.hexdigest()


def untracked_hashes(ws):
    """{workspace-relative path: content hash} for files git neither tracks nor ignores."""
    return {rel: file_hash(os.path.join(ws, rel))
            for rel in lines(ws, "ls-files", "--others", "--exclude-standard")}


def fingerprint(ws):
    """(tracked hash, {untracked path: hash}), or None when ws is not a git repository."""
    part = tracked_hash(ws)
    return None if part is None else (part, untracked_hashes(ws))


def digest(fp):
    """One string standing for a whole fingerprint."""
    if fp is None:
        return ""
    part, others = fp
    out = hashlib.sha256(part.encode())
    for rel in sorted(others):
        out.update(f"\0{rel}\0{others[rel]}".encode())
    return out.hexdigest()


def clean(ws):
    """Does git report nothing to commit and nothing untracked?"""
    return not lines(ws, "status", "--porcelain")


def changed_names(ws):
    """Workspace-relative paths git reports as changed or untracked. None outside a repo."""
    if not is_repo(ws):
        return None
    names = set(lines(ws, "diff", "--name-only", "--relative", "HEAD"))
    return names | set(lines(ws, "ls-files", "--others", "--exclude-standard"))


def record_path(kind, ws, extra=""):
    """One state file per workspace, or per (extra, workspace) when the record needs a scope."""
    name = hookpaths.ws_key(ws)
    if extra:
        name = f"{hookpaths.slug_of(extra)}-{name}"
    return os.path.join(kind, name + ".json")


def read_record(path):
    """A state file the gate wrote, or None when it is missing, unreadable or a day old."""
    data = hookpaths.read_json_file(path)
    if not isinstance(data, dict):
        return None
    if hookpaths.stale(data):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return data


def verified_digest(ws):
    """The fingerprint the gate last ran a passing verifier on, or ''."""
    rec = read_record(record_path(VERIFIED, ws)) or {}
    return str(rec.get("digest") or "")


def save_verified(ws, fp):
    hookpaths.write_json_file(record_path(VERIFIED, ws),
                              {"at": time.time(), "workspace": ws, "digest": digest(fp)})


def note_seen(cid, ws, fp):
    """Record what this workspace looked like when the conversation first reached a hook."""
    if not cid or fp is None:
        return
    path = record_path(SEEN, ws, cid)
    if read_record(path):
        return
    hookpaths.write_json_file(path, {"at": time.time(), "workspace": ws,
                                     "tracked": fp[0], "untracked": sorted(fp[1])})


def seen(cid, ws):
    return read_record(record_path(SEEN, ws, cid))


def moved(cid, ws, fp):
    """Has the workspace changed since this conversation first saw it?"""
    start = seen(cid, ws)
    if not start or fp is None:
        return False
    return start.get("tracked") != fp[0] or sorted(start.get("untracked") or []) != sorted(fp[1])


def new_untracked(cid, ws, fp):
    """Untracked, unignored paths this conversation added since its first seen event."""
    start = seen(cid, ws)
    if not start or fp is None:
        return []
    return sorted(set(fp[1]) - set(start.get("untracked") or []))


def save_review(cid, ws, kind, fp):
    """Record that a judging subagent saw this tree. False when it changed tracked content.

    The reviewer's start fingerprint comes from its own first seen event, so a reviewer that
    edited a tracked file proves nothing about what is in the tree now.
    """
    start = seen(cid, ws)
    if fp is None or not start or start.get("tracked") != fp[0]:
        return False
    hookpaths.write_json_file(record_path(REVIEWED, ws, kind),
                              {"at": time.time(), "workspace": ws, "by": cid,
                               "tracked": fp[0], "untracked": sorted(fp[1])})
    return True


def review(ws, kind):
    return read_record(record_path(REVIEWED, ws, kind))
