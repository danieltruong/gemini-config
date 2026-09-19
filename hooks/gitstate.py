#!/usr/bin/env python3
"""What a workspace holds right now, and what the gate already proved about it.

A fingerprint is taken from git, not from the transcript: HEAD, the working tree's diff
against it, and the untracked files git does not ignore. Whatever changed the tree, and
however the command line spelled it, the fingerprint moves.
"""
import collections
import hashlib
import os
import subprocess
import time

import hookpaths

GIT_TIMEOUT = 30
# a file this big is read at its two ends only, so a stop never hashes a whole asset
SIZE_CAP = 2_000_000
EDGE_BYTES = 65_536
VERIFIED = os.path.join(hookpaths.TMP, "verified")
REVIEWED = os.path.join(hookpaths.TMP, "reviewed")
SEEN = os.path.join(hookpaths.TMP, "seen")
# every git call is scoped to the workspace, so a workspace inside a bigger repo ignores
# whatever changed elsewhere in that repo
HERE = ("--", ".")
Fingerprint = collections.namedtuple("Fingerprint", "tracked untracked names")


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


def same_dir(one, other):
    return os.path.normcase(os.path.realpath(one)) == os.path.normcase(os.path.realpath(other))


def repo_state(ws):
    """(repository root, HEAD) in one git call, or None when ws is not in a repository."""
    out = text(ws, "rev-parse", "--show-toplevel", "HEAD")
    found = (out or "").splitlines()  # one per line: a repository path can hold spaces
    if len(found) >= 2:
        return found[0].strip(), found[1].strip()
    root = text(ws, "rev-parse", "--show-toplevel")
    return None if root is None else (root.strip(), "no-head")


def hash_map(value):
    """A record's untracked hashes. A record that stored names only counts as unknown."""
    return value if isinstance(value, dict) else {}


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
    try:
        with open(path, "rb") as fh:
            if stat.st_size <= SIZE_CAP:
                return hashlib.sha256(fh.read()).hexdigest()
            # size and mtime alone can be restored by hand, so the two ends are read too
            digest = hashlib.sha256(fh.read(EDGE_BYTES))
            fh.seek(max(0, stat.st_size - EDGE_BYTES))
            digest.update(fh.read(EDGE_BYTES))
            return f"stat:{stat.st_size}:{stat.st_mtime_ns}:{digest.hexdigest()}"
    except OSError:
        return "unreadable"


def tracked_hash(ws):
    """HEAD plus the diff against it, hashed. None when ws is not a git repository."""
    state = repo_state(ws)
    if state is None:
        return None
    root, head = state
    # stashing work away leaves a clean tree that otherwise looks exactly like the one the
    # conversation started from. The stash is repo-wide, so it only counts at the repo root.
    stash = ""
    if same_dir(root, ws):
        stash = (text(ws, "rev-parse", "--verify", "-q", "refs/stash") or "no-stash").strip()
    # --binary so a changed image or archive moves the hash like any other file
    diff = git(ws, "diff", "--binary", "HEAD", *HERE)
    if diff is None:
        diff = git(ws, "diff", "--binary", "--cached", *HERE) or b""
    digest = hashlib.sha256(f"{head}\0{stash}".encode())
    digest.update(b"\0")
    digest.update(diff)
    return digest.hexdigest()


def dir_hashes(ws, rel):
    """Every file under an untracked directory git refused to look inside, such as a nested repo."""
    out = {}
    for base, dirs, files in os.walk(os.path.join(ws, rel)):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = os.path.join(base, name)
            out[os.path.relpath(full, ws).replace("\\", "/")] = file_hash(full)
    return out


def untracked_hashes(ws):
    """{workspace-relative path: content hash} for files git neither tracks nor ignores."""
    out = {}
    for rel in lines(ws, "ls-files", "--others", "--exclude-standard", *HERE):
        # git reports a directory it will not descend into with a trailing slash
        if rel.endswith("/"):
            out.update(dir_hashes(ws, rel.rstrip("/")))
        else:
            out[rel] = file_hash(os.path.join(ws, rel))
    return out


def fingerprint(ws):
    """Tracked hash, untracked hashes and changed names. None when ws is not a git repository."""
    part = tracked_hash(ws)
    if part is None:
        return None
    others = untracked_hashes(ws)
    changed = set(lines(ws, "diff", "--name-only", "--relative", "HEAD", *HERE))
    return Fingerprint(part, others, changed | set(others))


def digest(fp):
    """One string standing for a whole fingerprint."""
    if fp is None:
        return ""
    out = hashlib.sha256(fp.tracked.encode())
    for rel in sorted(fp.untracked):
        out.update(f"\0{rel}\0{fp.untracked[rel]}".encode())
    return out.hexdigest()


def without(fp, paths):
    """The same fingerprint with some untracked paths left out, such as verifier output."""
    if fp is None or not paths:
        return fp
    drop = set(paths)
    return Fingerprint(fp.tracked,
                       {k: v for k, v in fp.untracked.items() if k not in drop},
                       set(fp.names) - drop)


def record_path(directory, ws, scope=""):
    """One state file per workspace, or per (scope, workspace) when the record needs one."""
    name = hookpaths.ws_key(ws)
    if scope:
        name = f"{hookpaths.slug_of(scope)}-{name}"
    return os.path.join(directory, name + ".json")


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


def verified(ws):
    """The last passing verifier run here: the fingerprint it passed on, and what it wrote."""
    return read_record(record_path(VERIFIED, ws)) or {}


def artifacts(ws):
    """Untracked paths the verifier itself creates, so its own output is not a fresh change."""
    return set(verified(ws).get("artifacts") or [])


def save_verified(ws, fp, made):
    """Record a pass. The digest leaves out the files that run wrote, which it writes again."""
    hookpaths.write_json_file(record_path(VERIFIED, ws),
                              {"at": time.time(), "workspace": ws,
                               "digest": digest(without(fp, made)),
                               "artifacts": sorted(made)})


def guard_hashes(ws):
    """Content hash per file the gate trusts: its own code, and this workspace's verifier."""
    return {hookpaths.norm(path): file_hash(path) for path in hookpaths.guard_paths(ws)}


def guard_moved(cid, ws):
    """Protected files that differ from this conversation's baseline, added ones included.

    Detection, not prevention: a shell command can still write these, and this state file
    with them. It names what moved so the next run does not trust the gate's own verdict.
    """
    before = (seen(cid, ws) or {}).get("guard")
    if not isinstance(before, dict) or not before:
        return []
    now = guard_hashes(ws)
    return sorted([rel for rel, digest in before.items() if now.get(rel) != digest]
                  + [rel for rel in now if rel not in before])


def note_seen(cid, ws, fp, fallback=False):
    """Record what this workspace looked like when the conversation first reached a hook.

    fallback marks a baseline taken at a Stop, where the turn's work has already happened,
    so the record cannot say what the tree looked like before it. With no conversation id the
    record is keyed on the workspace alone, so the next event still has a floor to compare with.
    """
    if fp is None:
        return
    path = record_path(SEEN, ws, cid)
    old = read_record(path)
    # a real baseline replaces a fallback: the next PreInvocation knows what the tree holds
    # before its turn runs, which is exactly what the fallback could not say
    if old and not (old.get("fallback") and not fallback):
        return
    hookpaths.write_json_file(path, {"at": time.time(), "workspace": ws, "fallback": fallback,
                                     "tracked": fp.tracked, "untracked": fp.untracked,
                                     "guard": guard_hashes(ws)})


def seen(cid, ws):
    """The oldest record of this workspace in this conversation, real baseline or fallback."""
    return read_record(record_path(SEEN, ws, cid))


def baseline(cid, ws):
    """The conversation's own starting fingerprint, or None when only a fallback exists."""
    start = seen(cid, ws)
    return None if not start or start.get("fallback") else start


def moved(cid, ws, fp):
    """Has the workspace changed since the gate first saw it in this conversation?

    A fallback record is a floor rather than a baseline: it cannot date work that happened
    before it, but anything after it still shows up here.
    """
    start = seen(cid, ws)
    if not start or fp is None:
        return False
    return (start.get("tracked") != fp.tracked
            or hash_map(start.get("untracked")) != fp.untracked)


def new_untracked(cid, ws, fp):
    """Untracked, unignored paths that appeared since the gate first saw this workspace."""
    start = seen(cid, ws)
    if not start or fp is None:
        return []
    return sorted(set(fp.untracked) - set(hash_map(start.get("untracked"))))


def save_review(cid, ws, kind, fp):
    """Record that a judging subagent saw this tree. False when it cannot stand for one.

    The judge's own baseline says what it started from: a judge that changed tracked content,
    or that never got a real baseline, proves nothing about what is in the tree now. Only the
    untracked files present at both ends count as reviewed, and by content, so a file the
    judge created, or one rewritten after it stopped, is still new work for its parent.
    """
    start = baseline(cid, ws)
    if fp is None or not start or start.get("tracked") != fp.tracked:
        return False
    both = set(hash_map(start.get("untracked"))) & set(fp.untracked)
    hookpaths.write_json_file(
        record_path(REVIEWED, ws, kind),
        {"at": time.time(), "workspace": ws, "by": cid, "tracked": fp.tracked,
         "untracked": {rel: fp.untracked[rel] for rel in both}})
    return True


def review(ws, kind):
    return read_record(record_path(REVIEWED, ws, kind))
