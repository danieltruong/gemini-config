#!/usr/bin/env bash
# One page on how the Antigravity hooks behaved: decisions, denials, verifiers, log noise.
set -uo pipefail

usage() {
    cat <<'EOF'
usage: agy-audit.sh [--days N]

  --days N   how far back to look (default: 7)
  --help     show this help

Reads ~/.gemini/tmp/denials and the agy cli.log.
Stop-gate decisions belong to agy-audit.py, the only reader of stop_gate.log.
Always exits 0; it reports, it does not judge.
EOF
}

DAYS=7
while [ $# -gt 0 ]; do
    case "$1" in
        --days) DAYS="${2:-}"; shift 2 || true ;;
        --days=*) DAYS="${1#*=}"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "agy-audit.sh: unknown argument $1" >&2; usage >&2; exit 2 ;;
    esac
done
case "$DAYS" in ''|*[!0-9]*) echo "agy-audit.sh: --days needs a number" >&2; exit 2 ;; esac

GEMINI_DIR="${GEMINI_DIR:-$HOME/.gemini}"

report=$(cat <<'EOF'
import collections, datetime, json, os, re, sys

days = int(sys.argv[1])
root = sys.argv[2]
cutoff = datetime.datetime.now() - datetime.timedelta(days=days)


def head(title):
    print(f"\n{title}")


def counted(rows, limit=None):
    if not rows:
        print("  none")
        return
    for name, n in rows[:limit]:
        print(f"  {n:>4}  {name}")


print(f"agy audit - last {days} days (since {cutoff:%Y-%m-%d})")

head("denied tool calls still counted against a session")
denials = collections.Counter()
folder = os.path.join(root, "tmp", "denials")
for name in os.listdir(folder) if os.path.isdir(folder) else []:
    path = os.path.join(folder, name)
    try:
        if datetime.datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            continue
        for key, n in (json.load(open(path, encoding="utf-8")) or {}).items():
            denials[key[:80]] += n
    except Exception:
        continue
counted(denials.most_common(), 10)

head("cli.log warnings and errors (top 10)")
GLOG = re.compile(r"\b([WEF])(\d{2})(\d{2})\s")
NUMBERS = re.compile(r"\b\d+\b")
STAMP = re.compile(r"^[\d:.]+\s+\d+\s+")
problems = collections.Counter()
try:
    cli = open(os.path.join(root, "antigravity-cli", "cli.log"),
               encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    cli = []
for line in cli:
    m = GLOG.search(line)
    if not m:
        continue
    when = datetime.datetime(cutoff.year, int(m.group(2)), int(m.group(3)))
    if when < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
        continue
    # numbers are pids, ports and line numbers; the message is what repeats
    problems[NUMBERS.sub("N", STAMP.sub("", line[m.end():].strip()))[:100]] += 1
counted(problems.most_common(), 10)
EOF
)
python -c "$report" "$DAYS" "$GEMINI_DIR"
