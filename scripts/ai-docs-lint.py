#!/usr/bin/env python3
"""Lint AI instruction docs (GEMINI.md, AGENTS.md, skills, agents, rules) for size, dead
paths, rule frontmatter and duplication.

Usage: ai-docs-lint.py [--all] [--quiet] [--self-check] [FILE ...]
  --all         lint the whole set, and check the always-on budget: GEMINI.md and every
                rules/*.md with trigger always_on share one 20,000-token budget
  --quiet       print nothing when clean
  --self-check  run the rule frontmatter and budget checks against fixtures, then exit
Exit 1 on any finding, 0 when clean. A "warn:" line (budget near full) does not fail.
"""
import glob
import json
import os
import re
import sys
import tempfile

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), "..")).replace("\\", "/")
USER_HOME = os.path.expanduser("~").replace("\\", "/")
# rule: the built-in agy-customizations docs/rules.md caps a rules file at 24 KB and all
# always-on text at 20,000 tokens; the older mirror said 12,000 chars per file
CAPS = {"global": 13000, "skill": 40000, "agent": 3000, "rule": 24000}
DESC_CAP = 200
TRIGGERS = ("always_on", "model_decision", "glob", "manual")
ALWAYS_ON_BUDGET = 20000
ALWAYS_ON_WARN = 0.8
CHARS_PER_TOKEN = 4  # rough estimate; Antigravity does not expose its tokenizer

findings = []
warnings = []
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
    if re.search(r"(^|/)rules/[^/]+\.md$", p) and "/skills/" not in p:
        return "rule", None
    return None, None

def all_files():
    out = [f"{REPO}/GEMINI.md", f"{REPO}/AGENTS.md"]
    out += glob.glob(f"{REPO}/skills/*/SKILL.md")
    out += glob.glob(f"{REPO}/agents/*.md")
    out += glob.glob(f"{REPO}/rules/*.md")
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
        for n, line in enumerate(read(p).split("\n"), 1):
            k = re.sub(r"\s+", " ", line.strip().lower())
            if len(k) < 60 or k.startswith(("|--", "```")):
                continue
            seen.setdefault(k, []).append((p, n))
    for k, locs in seen.items():
        ps = {l[0] for l in locs}
        if len(ps) > 1:
            first = locs[0]
            f(first[0], first[1], "duplicate", f"same line in {', '.join(sorted(os.path.relpath(x, REPO) for x in ps - {first[0]}))}")

def frontmatter(text):
    """Frontmatter fields, or None when the file has none. A YAML block list under a key
    (`globs:` then `- "*.py"` lines) reads as its items joined by commas."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fields, key = {}, None
    for line in m.group(1).split("\n"):
        pair = re.match(r"([A-Za-z_]+):[ \t]*(.*?)[ \t]*$", line)
        item = re.match(r"[ \t]*-[ \t]+(.*?)[ \t]*$", line)
        if pair:
            key = pair.group(1)
            fields[key] = pair.group(2).strip("\"'")
        elif item and key:
            fields[key] = ",".join(filter(None, (fields[key], item.group(1).strip("\"'"))))
    return fields

def check_rule(p, text):
    fields = frontmatter(text)
    if fields is None:
        f(p, 1, "rule-frontmatter", f"missing; Antigravity needs trigger: {'|'.join(TRIGGERS)}")
        return
    trigger = fields.get("trigger", "")
    if trigger not in TRIGGERS:
        f(p, 1, "rule-trigger", f"{trigger or '<none>'} is not one of {'|'.join(TRIGGERS)}")
    elif trigger == "model_decision" and not fields.get("description"):
        f(p, 1, "rule-frontmatter", "model_decision needs a description")
    elif trigger == "glob" and not (fields.get("glob") or fields.get("globs")):
        f(p, 1, "rule-frontmatter", "trigger glob needs a glob field")

def check_always_on(files):
    # guards what install links globally and Antigravity loads every turn; AGENTS.md is not linked
    total = 0
    for p in files:
        kind, _ = classify(p)
        text = read(p)
        if os.path.basename(p) == "GEMINI.md" or (
                kind == "rule" and (frontmatter(text) or {}).get("trigger") == "always_on"):
            total += len(text)
    tokens = total // CHARS_PER_TOKEN
    msg = f"~{tokens} tokens (chars/{CHARS_PER_TOKEN}) of always-on text vs {ALWAYS_ON_BUDGET} budget"
    if tokens > ALWAYS_ON_BUDGET:
        f(REPO, 0, "always-on-budget", msg + "; move rules to model_decision")
    elif tokens >= ALWAYS_ON_BUDGET * ALWAYS_ON_WARN:
        warnings.append(f"warn: {REPO}:0: always-on-budget: {msg}")

def read(p):
    # utf-8-sig drops a BOM, which would otherwise hide the frontmatter
    with open(p, encoding="utf-8-sig", errors="replace") as handle:
        return handle.read().replace("\r\n", "\n")

def lint(p):
    kind, base = classify(p)
    if not kind:
        return
    text = read(p)
    size = len(text.encode("utf-8"))
    if size > CAPS.get(kind, 100000):
        f(p, 0, "too-big", f"{size} bytes > {CAPS[kind]} cap for {kind}")
    if kind == "skill":
        m = re.search(r"^description:\s*(.*)$", text, re.M)
        if m and len(m.group(1)) > DESC_CAP:
            f(p, 0, "long-description", f"{len(m.group(1))} chars > {DESC_CAP}")
    if kind == "rule":
        check_rule(p, text)
    check_paths(p, text, kind, base)

def self_check():
    def lint_fixture(name, text, encoding="utf-8"):
        findings.clear()
        path = f"{tmp}/rules/{name}"
        with open(path, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
        lint(path)
        return [x.split(": ", 2)[1] for x in findings]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = tmp.replace("\\", "/")
        os.makedirs(f"{tmp}/rules")
        assert lint_fixture("bad.md", "---\ntrigger: sometimes\n---\nx\n") == ["rule-trigger"]
        assert lint_fixture("g.md", "---\ntrigger: glob\n---\nx\n") == ["rule-frontmatter"]
        assert lint_fixture("md.md", "---\ntrigger: model_decision\n---\nx\n") == \
            ["rule-frontmatter"]
        assert lint_fixture("bom.md", "---\ntrigger: always_on\n---\nx\n", "utf-8-sig") == []
        listed = "---\ntrigger: glob\nglobs:\n  - \"*.py\"\n  - '*.rpy'\n---\nx\n"
        assert lint_fixture("list.md", listed) == []
        assert frontmatter(listed)["globs"] == "*.py,*.rpy"

        # AGENTS.md is not installed globally, so it stays out of the budget; GEMINI.md does not
        big = "x" * (ALWAYS_ON_BUDGET + 1) * CHARS_PER_TOKEN
        for name, text in (("GEMINI.md", "small"), ("AGENTS.md", big),
                           ("rules/on.md", "---\ntrigger: always_on\n---\nsmall\n"),
                           ("rules/off.md",
                            "---\ntrigger: model_decision\ndescription: d\n---\n" + big)):
            with open(f"{tmp}/{name}", "w", encoding="utf-8") as handle:
                handle.write(text)
        files = [f"{tmp}/{n}" for n in ("GEMINI.md", "AGENTS.md", "rules/on.md", "rules/off.md")]
        findings.clear()
        check_always_on(files)
        assert findings == [], findings
        with open(f"{tmp}/GEMINI.md", "w", encoding="utf-8") as handle:
            handle.write(big)
        check_always_on(files)
        assert len(findings) == 1 and "always-on-budget" in findings[0], findings
    findings.clear()
    warnings.clear()
    print("ai-docs-lint self-check ok")

def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--self-check" in argv:
        self_check()
        return 0
    quiet = "--quiet" in argv
    files = [a for a in argv if not a.startswith("--")]
    if "--all" in argv:
        files = all_files()
        check_strays()
        check_dry([p for p in files if classify(p)[0] in ("global", "agent", "rule")])
        check_always_on(files)
    for p in files:
        if os.path.isfile(p):
            lint(p)
    for x in warnings + sorted(set(findings)):
        print(x)
    if not findings and not quiet:
        print("ai-docs-lint: clean")
    return 1 if findings else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
