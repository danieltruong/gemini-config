#!/usr/bin/env python3
"""Lint AI instruction docs (GEMINI.md, AGENTS.md, skills, agents) for size, dead paths, and duplication.

Usage: ai-docs-lint.py [--all] [--quiet] [FILE ...]
  --all    lint the whole set
  --quiet  print nothing when clean
Exit 1 on any finding, 0 when clean.
"""
import glob
import json
import os
import re
import sys

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), "..")).replace("\\", "/")
USER_HOME = os.path.expanduser("~").replace("\\", "/")
CAPS = {"global": 13000, "skill": 40000, "agent": 3000, "rule": 10000}
DESC_CAP = 200

findings = []
def f(path, line, rule, msg):
    findings.append(f"{path}:{line}: {rule}: {msg}")

def classify(p):
    p = os.path.abspath(p).replace("\\", "/")
    if any(p.endswith(name) for name in ("/GEMINI.md", "/AGENTS.md", "GEMINI.md", "AGENTS.md")):
        return "global", REPO
    if p.endswith("/SKILL.md") or p.endswith("SKILL.md"):
        return "skill", os.path.dirname(p)
    if "/agents/" in p and p.endswith(".md"):
        return "agent", None
    if "/rules/" in p and p.endswith(".md"):
        return "rule", None
    return None, None

def all_files():
    out = [f"{REPO}/GEMINI.md", f"{REPO}/AGENTS.md"]
    out += glob.glob(f"{REPO}/skills/*/SKILL.md")
    out += glob.glob(f"{REPO}/agents/*.md")
    seen = set()
    uniq = []
    for p in out:
        r = os.path.realpath(p).replace("\\", "/")
        if os.path.isfile(p) and r not in seen:
            seen.add(r)
            uniq.append(p.replace("\\", "/"))
    return uniq

PATH_RE = re.compile(r"`([^`\s]+)`")
def check_paths(p, text, kind, base):
    for n, line in enumerate(text.split("\n"), 1):
        for tok in PATH_RE.findall(line):
            if any(c in tok for c in "<>*{}$?|") or "://" in tok or tok.startswith("mcp__"):
                continue
            if tok.startswith((USER_HOME + "/Downloads/", "~/Downloads/", USER_HOME + "/Desktop/", "~/Desktop/", USER_HOME + "/.config/", "~/.config/")):
                continue
            if tok.startswith((USER_HOME + "/", "~/")):
                full = os.path.expanduser(tok).rstrip("/")
                first = tok.split("/")[1]
                if os.path.isdir(f"{USER_HOME}/{first}") and not os.path.exists(full):
                    f(p, n, "dead-path", tok)
            elif kind in ("repo", "skill") and base and "/" in tok and not tok.startswith((".", "-", "/", "~")):
                first = tok.split("/")[0]
                if os.path.isdir(os.path.join(base, first)) and not os.path.exists(os.path.join(base, tok.rstrip("/"))):
                    f(p, n, "dead-path", tok)

def check_strays():
    for pat in ("*.bak", "*.orig", "*.original.md", "*~", ".bak*"):
        for s in glob.glob(f"{REPO}/**/{pat}", recursive=True):
            s_norm = s.replace("\\", "/")
            if "/.git/" in s_norm or "/hooks/tests/" in s_norm:
                continue
            f(s, 0, "stray-backup", "delete; git holds history")

def check_dry(files):
    seen = {}
    for p in files:
        for n, line in enumerate(open(p, encoding="utf-8", errors="replace").read().split("\n"), 1):
            k = re.sub(r"\s+", " ", line.strip().lower())
            if len(k) < 60 or k.startswith(("|--", "```")):
                continue
            seen.setdefault(k, []).append((p, n))
    for k, locs in seen.items():
        ps = {l[0] for l in locs}
        if len(ps) > 1:
            first = locs[0]
            f(first[0], first[1], "duplicate", f"same line in {', '.join(sorted(os.path.relpath(x, REPO) for x in ps - {first[0]}))}")

def lint(p):
    kind, base = classify(p)
    if not kind:
        return
    text = open(p, encoding="utf-8", errors="replace").read()
    size = len(text.encode("utf-8"))
    if size > CAPS.get(kind, 100000):
        f(p, 0, "too-big", f"{size} bytes > {CAPS[kind]} cap for {kind}")
    if kind == "skill":
        m = re.search(r"^description:\s*(.*)$", text, re.M)
        if m and len(m.group(1)) > DESC_CAP:
            f(p, 0, "long-description", f"{len(m.group(1))} chars > {DESC_CAP}")
    check_paths(p, text, kind, base)

def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    quiet = "--quiet" in argv
    files = [a for a in argv if not a.startswith("--")]
    if "--all" in argv:
        files = all_files()
        check_strays()
        check_dry([p for p in files if classify(p)[0] in ("global", "agent", "rule")])
    for p in files:
        if os.path.isfile(p):
            lint(p)
    for x in sorted(set(findings)):
        print(x)
    if not findings and not quiet:
        print("ai-docs-lint: clean")
    return 1 if findings else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
