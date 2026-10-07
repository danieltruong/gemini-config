"""Share one agy settings.json patch between parallel OpenRouter launchers.

agy reads modelProvider only from its settings.json; no flag or env var sets it per run.
The first launcher backs the file up and patches it, the last one to leave restores it.
Holders are launcher process ids, so a crashed launcher is dropped the next time any
launcher acquires or releases.

usage: settings_lease.py acquire|release <settings.json> <launcher pid>
"""

import contextlib
import json
import os
import sys
import time

PROVIDER = "gemini"  # the only value agy accepts for a custom endpoint
LOCK_WAIT = 30
STALE_LOCK = 30  # the critical section takes milliseconds; an older lock is left by a crash


def is_alive(pid):
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


@contextlib.contextmanager
def locked(lock_path):
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > STALE_LOCK:
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise SystemExit(f"settings_lease: {lock_path} held for over {LOCK_WAIT}s")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.remove(lock_path)


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
    with open(path, "w", encoding="utf-8", newline="") as f:  # no BOM: agy rejects one
        f.write(text)


def _load_state(path):
    text = _read(path)
    state = json.loads(text) if text else {}
    return {"holders": state.get("holders", []), "patched": state.get("patched", False),
            "backup": state.get("backup")}


def acquire(settings, pid, alive=is_alive):
    lock, state_path = _paths(settings)
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with locked(lock):
        state = _load_state(state_path)
        state["holders"] = [p for p in state["holders"] if p != pid and alive(p)]
        current = _read(settings)
        if not state["patched"]:
            state["backup"] = current  # None: the file did not exist, so delete it on restore
            state["patched"] = True
        cfg = json.loads(current) if current and current.strip() else {}
        cfg["modelProvider"] = PROVIDER
        _write(settings, json.dumps(cfg, indent=2))
        state["holders"].append(pid)
        _write(state_path, json.dumps(state))


def release(settings, pid, alive=is_alive):
    lock, state_path = _paths(settings)
    with locked(lock):
        state = _load_state(state_path)
        state["holders"] = [p for p in state["holders"] if p != pid and alive(p)]
        if not state["holders"] and state["patched"]:
            if state["backup"] is None:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(settings)
            else:
                _write(settings, state["backup"])
            state = {"holders": [], "patched": False, "backup": None}
        _write(state_path, json.dumps(state))


def main(argv):
    # The pid comes from the caller: a venv python.exe on Windows is a shim, so getppid() would be it.
    if len(argv) != 4 or argv[1] not in ("acquire", "release") or not argv[3].isdigit():
        sys.stderr.write(__doc__)
        return 2
    (acquire if argv[1] == "acquire" else release)(argv[2], int(argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
