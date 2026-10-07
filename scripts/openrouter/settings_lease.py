"""Share one agy settings.json patch between parallel OpenRouter launchers.

agy reads modelProvider only from its settings.json; no flag or env var sets it per run.
The first launcher records the old value and patches the file, the last one to leave puts
the old value back. Holders are launcher process ids, so a crashed launcher is dropped the
next time any launcher acquires or releases, or on `recover`.

usage: settings_lease.py acquire|release <settings.json> <launcher pid>
       settings_lease.py recover [settings.json]   (default: agy's settings.json)
"""

import contextlib
import json
import os
import sys
import time

PROVIDER = "gemini"  # the only value agy accepts for a custom endpoint
KEY = "modelProvider"
DEFAULT_SETTINGS = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "settings.json")
LOCK_WAIT = 30


def is_alive(pid):
    # ponytail: pid only, so a reused pid keeps a lease open; store process start time if that bites
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _try_lock(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def locked(lock_path):
    """OS file lock: a crashed holder's lock is freed with its process, so none goes stale."""
    deadline = time.monotonic() + LOCK_WAIT
    with open(lock_path, "a+b") as f:
        while True:
            try:
                _try_lock(f)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise SystemExit(f"settings_lease: {lock_path} held for over {LOCK_WAIT}s")
                time.sleep(0.1)
        yield  # closing the file releases the lock


def _paths(settings):
    base = os.path.join(os.path.dirname(settings), ".openrouter")
    return base + ".lock", base + ".state.json"


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:  # no BOM: agy rejects one
        f.write(text)
    os.replace(tmp, path)  # a crash mid-write never leaves agy a truncated file


def _dump(cfg):
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def _load_json(text):
    return json.loads(text) if text and text.strip() else {}


def _load_state(path):
    state = _load_json(_read(path))
    return {"holders": state.get("holders", []), "patched": state.get("patched", False),
            "existed": state.get("existed", True), "old": state.get("old")}


def _alive_holders(state, pid, alive):
    return [p for p in state["holders"] if p != pid and alive(p)]


def acquire(settings, pid, alive=is_alive):
    lock, state_path = _paths(settings)
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with locked(lock):
        state = _load_state(state_path)
        state["holders"] = _alive_holders(state, pid, alive) + [pid]
        current = _read(settings)
        cfg = _load_json(current)
        if not state["patched"]:
            # old is {"value": v} or None when the key was absent
            state.update(patched=True, existed=current is not None,
                         old={"value": cfg[KEY]} if KEY in cfg else None)
        _write(state_path, json.dumps(state))  # recorded first, so a crash below is recoverable
        cfg[KEY] = PROVIDER
        _write(settings, _dump(cfg))


def release(settings, pid=None, alive=is_alive):
    """Drop pid (None: only dead holders); the last holder out restores modelProvider alone."""
    lock, state_path = _paths(settings)
    if not os.path.exists(state_path):
        return
    with locked(lock):
        state = _load_state(state_path)
        state["holders"] = _alive_holders(state, pid, alive)
        if not state["holders"] and state["patched"]:
            current = _read(settings)
            cfg = _load_json(current)
            if state["old"] is None:
                cfg.pop(KEY, None)
            else:
                cfg[KEY] = state["old"]["value"]
            if cfg or state["existed"]:
                _write(settings, _dump(cfg))
            elif current is not None:
                os.remove(settings)
            state = {"holders": [], "patched": False}
        _write(state_path, json.dumps(state))


def main(argv):
    # The pid comes from the caller: a venv python.exe on Windows is a shim, so getppid() would be it.
    if len(argv) in (2, 3) and argv[1] == "recover":
        release(argv[2] if len(argv) == 3 else DEFAULT_SETTINGS)
        return 0
    if len(argv) != 4 or argv[1] not in ("acquire", "release") or not argv[3].isdigit():
        sys.stderr.write(__doc__)
        return 2
    (acquire if argv[1] == "acquire" else release)(argv[2], int(argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
