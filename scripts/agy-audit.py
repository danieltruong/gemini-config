#!/usr/bin/env python3
"""Antigravity CLI usage audit: volume, model mix, tool failures, size, quality, speed.

Reads only. Every SQLite handle is opened with mode=ro so a live agy never blocks
and nothing under the data root is touched.

Sources and what each is trusted for:
  brain/<cid>/.system_generated/logs/transcript_full.jsonl  steps, text, tool calls,
      per-tool wall clock (the "Created At:/Completed At:" header on tool-result steps)
  conversations/<cid>.db  steps.status (int), steps.error_details (protobuf, strings
      extractable), gen_metadata.data (protobuf: real token counts + model string)
  conversation_summaries.db  title, workspace, killed, agent_name, parent conversation
  ~/.gemini/tmp/stop_gate.log  hook stop decisions

Self-check: --self-check exercises the protobuf walker, the transcript parser, the
active-time calculation, the correction heuristic, the gate-file and stop-log parsers,
the subagent overlap count and the snapshot delta against fixtures, no data root.
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "hooks"))
# one transcript parser for the hooks and this script
from transcript import EDIT_TOOLS, RUN_TOOLS, read_transcript  # noqa: E402

# Command-text matching lives here, not in the hooks: the stop gate judges a workspace by
# its git fingerprint, and only this report still asks what a past command line looked like.
VERIFIER_COMMANDS = (
    (".agents/verify", re.compile(r"(?i)verify\.(?:cmd|ps1|sh)\b")),
    ("renpy lint", re.compile(r"(?i)renpy(?:\.exe)?[^\n]{0,60}\blint\b|renpy_run_lint")),
    ("npm", re.compile(r"(?i)\bnpm\s+(?:run\s+)?(?:lint|test)\b")),
    ("python -m pytest", re.compile(r"(?i)\bpytest\b")),
    ("cargo test", re.compile(r"(?i)\bcargo\s+test\b")),
    ("go ", re.compile(r"(?i)\bgo\s+(?:vet|test)\b")),
    ("dotnet test", re.compile(r"(?i)\bdotnet\s+test\b")),
)
# a command that only prints or searches for the verifier has not run it
QUOTING = re.compile(r"(?i)^\s*(?:echo|printf|cat|type|less|more|head|tail|grep|rg|findstr"
                     r"|git\s+(?:log|grep|show|diff))\b")
INSPECT_ONLY = re.compile(r"(?i)--collect-only|--co\b|--help\b|--version\b|--dry-run\b|\s-h\b")
EXIT_CODE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
# run_command reports its status as prose at the head of the result, not as JSON;
# anchored so the same sentence quoted inside a file being read is not a failure
COMMAND_EXIT = re.compile(r"(?i)^\s*the command exited with code (-?\d+)")
SUCCESS_FLAG = re.compile(r'"success"\s*:\s*(true|false)')


def executed_text(name, args):
    """What a run-style tool actually executed, or '' for tools that run nothing."""
    if name == "run_command":
        return str(args.get("CommandLine") or "")
    if name == "call_mcp_tool":
        return f"{args.get('ServerName') or ''} {args.get('ToolName') or ''}"
    return ""


def result_failed(body):
    """Did a tool result say it failed? Only self-reported signals count."""
    head = body[:4000]
    exited = COMMAND_EXIT.match(body)
    if exited and int(exited.group(1)) != 0:
        return f"exit code {exited.group(1)}"
    code = EXIT_CODE.search(head)
    if code and int(code.group(1)) != 0:
        return f"exit_code {code.group(1)}"
    flag = SUCCESS_FLAG.search(head)
    if flag and flag.group(1) == "false":
        return "success false"
    return ""


def verifier_of(name, args):
    """The verifier label this call looks like it ran, or ''. Report only, never a gate."""
    if name not in RUN_TOOLS:
        return ""
    text = executed_text(name, args)
    if not text or QUOTING.match(text) or INSPECT_ONLY.search(text):
        return ""
    return next((lab for lab, pattern in VERIFIER_COMMANDS if pattern.search(text)), "")

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
STOP_GATE_LOG = os.path.join(os.path.expanduser("~"), ".gemini", "tmp", "stop_gate.log")
DEFAULT_SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".claude", "cache", "agy-audit")
IDLE_GAP_SECONDS = 600  # gaps longer than this are the user away, not the agent working

# gen_metadata protobuf paths, confirmed by correlation against transcript text:
#   f3 total output == f9 thinking + f10 response, and f5 appears only once the
#   prompt prefix is cached, so f2 is the uncached remainder.
TOK_UNCACHED_IN = ".1.17.2.2"
TOK_OUT_TOTAL = ".1.17.2.3"
TOK_CACHED_IN = ".1.17.2.5"
TOK_THINKING = ".1.17.2.9"
TOK_RESPONSE = ".1.17.2.10"
CTX_USED = ".1.9.10.1"
CTX_WINDOW = ".1.9.10.4"
LATENCY_NS = ".1.11.2"
MODEL_STRING = ".1.19"
KV_KEY, KV_VALUE = ".1.20.1", ".1.20.2"

# A larger model is an escalation away from the cheap flash default.
BIG_MODEL = re.compile(r"(?i)(pro|opus|sonnet|thinking|-high\b)")

# Files that decide whether work passed. Editing one while the verifier is red moves
# the goalposts, so every such edit is listed by conversation and step.
GATE_FILE = re.compile(
    r"(?i)(?:(?:^|[\\/])(?:test_[^\\/]*\.py|[^\\/]*_test\.py|audit_project\.py"
    r"|verify\.cmd|verify\.ps1|verify\.sh)$"
    r"|(?:^|[\\/])hooks[\\/])")
GIT_DIFF = re.compile(r"(?i)\bgit\b[^\n|;&]*\bdiff\b")
COMPACTION_STEP = "CHECKPOINT"
VIEW_TOOLS = {"view_file", "view_code_item", "view_file_outline"}
# the model rewrites this prose every call, so it must not enter a retry signature
NOISE_ARGS = {"toolAction", "toolSummary", "Blocking", "WaitMsBeforeAsync"}

CORRECTION_WORDS = re.compile(
    r"(?i)\b(still|again|wrong|incorrect|not what|revert|undo|broke|broken|why did you|"
    r"no[,.]|stop|don'?t|didn'?t work|doesn'?t work|that'?s not|instead|"
    r"you (missed|forgot|ignored)|fix it|regress)\b"
)
# Tool-result bodies are raw file and command output, so a keyword search over them
# is dominated by the content being read, not by failures (288 loose hits against 0
# real ones on this data root). Only these are trusted: steps.error_details,
# transcript status ERROR, db step status 7, and what transcript.result_failed reads.
DB_STATUS_ERROR = 7
# every kind stop_gate.py logs, plus the kinds older logs still hold
STOP_GATE_KINDS = ("stop", "skip", "release", "pending", "review", "verifier", "docs",
                   "leftovers", "no-review", "no-visual", "untrusted", "no-workspace",
                   "no-transcript", "no-verifier", "no-reviewer", "trust", "unresolved",
                   "audit")
# a gate decision that returned before any check ran, whatever kind it logged
UNCHECKED_KINDS = {"skip", "pending", "unresolved", "no-workspace", "no-transcript"}
# lines the gate logs beside a decision: a judge's record, a dropped path, a trust note
BOOKKEEPING_KINDS = {"review", "unresolved", "trust"}
# detail of a legacy line usually opens with one of these, so the word before it is the kind
LEGACY_DETAIL = re.compile(r"(\S+)\s+((?:execution|reason)=.*)$")
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{10,}")
TOOL_HEADER = re.compile(
    r"^Created At: (\S+)\nCompleted At: (\S+)\n?", re.MULTILINE)
DIGITS = re.compile(r"\d+")
HEXISH = re.compile(r"(?i)\b[0-9a-f]{8,}\b")


def die(msg):
    print(f"agy-audit.py: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- pb
def read_varint(buf, i):
    result = shift = 0
    while i < len(buf):
        byte = buf[i]
        result |= (byte & 0x7F) << shift
        i += 1
        shift += 7
        if not byte & 0x80:
            return result, i
    raise ValueError("truncated varint")


def walk_protobuf(buf, path="", depth=0, out=None, maxdepth=6):
    """Flatten an unknown protobuf into (dotted field path, kind, value) triples."""
    if out is None:
        out = []
    i = 0
    while i < len(buf):
        try:
            tag, i = read_varint(buf, i)
        except ValueError:
            return out
        field, wire = tag >> 3, tag & 7
        if field == 0:
            return out
        here = f"{path}.{field}"
        if wire == 0:
            try:
                value, i = read_varint(buf, i)
            except ValueError:
                return out
            out.append((here, "int", value))
        elif wire == 2:
            try:
                length, i = read_varint(buf, i)
            except ValueError:
                return out
            if length > len(buf) - i:
                return out
            sub, i = buf[i:i + length], i + length
            if length and all(32 <= b < 127 or b in (9, 10, 13) for b in sub):
                out.append((here, "str", sub.decode("utf-8", "replace")))
            elif depth < maxdepth:
                before = len(out)
                walk_protobuf(sub, here, depth + 1, out, maxdepth)
                if len(out) == before:
                    out.append((here, "bytes", length))
            else:
                out.append((here, "bytes", length))
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            return out
    return out


def protobuf_strings(blob):
    if blob is None:
        return []
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    return [m.group().decode("ascii") for m in PRINTABLE_RUN.finditer(blob)]


# ------------------------------------------------------------------------ parse
def parse_time(text):
    if not text:
        return None
    text = text.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    # SQLite writes 7 fractional digits; fromisoformat wants at most 6
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def active_seconds(stamps, idle_gap=IDLE_GAP_SECONDS):
    """Wall clock minus every gap longer than idle_gap."""
    stamps = sorted(t for t in stamps if t)
    if len(stamps) < 2:
        return 0.0
    total = 0.0
    for earlier, later in zip(stamps, stamps[1:]):
        gap = (later - earlier).total_seconds()
        if 0 <= gap <= idle_gap:
            total += gap
    return total


def tool_span(content):
    """Seconds a tool result took, from its Created At/Completed At header."""
    match = TOOL_HEADER.match(content or "")
    if not match:
        return None, content or ""
    start, end = parse_time(match.group(1)), parse_time(match.group(2))
    body = (content or "")[match.end():]
    if not start or not end:
        return None, body
    return max(0.0, (end - start).total_seconds()), body


def retry_signature(name, args):
    """Tool identity with the model's per-call prose stripped out."""
    stable = {k: v for k, v in args.items() if k not in NOISE_ARGS}
    return name + "|" + json.dumps(stable, sort_keys=True)


def file_target(args):
    """Path a file tool read or wrote."""
    return str(args.get("TargetFile") or args.get("AbsolutePath") or
               args.get("Path") or args.get("File") or "")


def peak_overlap(spans):
    """Most intervals live at once, from (start, end) pairs."""
    marks = []
    for start, end in spans:
        if start and end:
            marks.append((start, 1))
            marks.append((end, -1))
    marks.sort(key=lambda m: (m[0], m[1]))  # an end at time T frees a slot before a start
    live = peak = 0
    for _when, delta in marks:
        live += delta
        peak = max(peak, live)
    return peak


def subagent_specs(args):
    """invoke_subagent packs a list of {TypeName, Model, Role, Prompt}."""
    raw = args.get("Subagents")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def normalise_error(text):
    text = " ".join(text.split())
    text = HEXISH.sub("<hex>", text)
    text = DIGITS.sub("N", text)
    return text[:160]


def workspace_of(uris):
    try:
        parsed = json.loads(uris or "[]")
    except ValueError:
        return ""
    names = []
    for uri in parsed:
        path = urllib.parse.unquote(uri)
        names.append(path[len("file:///"):] if path.startswith("file:///") else path)
    return ",".join(names)


# -------------------------------------------------------------------- summaries
def load_summaries(root):
    path = os.path.join(root, "conversation_summaries.db")
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    for row in conn.execute(
            "select conversation_id,title,preview,step_count,last_modified_time,"
            "workspace_uris,status,agent_name,killed,not_fully_idle,"
            "parent_conversation_id,nesting_depth from conversation_summaries"):
        out[row[0]] = {
            "title": (row[1] or row[2] or "").replace("\t", " ").replace("\n", " ")[:90],
            "step_count": row[3] or 0,
            "last_modified": parse_time(row[4]),
            "workspace": workspace_of(row[5]),
            "status": row[6] or "",
            "agent_name": row[7] or "",
            "killed": int(row[8] or 0),
            "not_fully_idle": int(row[9] or 0),
            "parent": row[10] or "",
            "depth": int(row[11] or 0),
        }
    conn.close()
    return out


def load_generations(root, cid):
    """Per-generation token counts and model, from gen_metadata's protobuf."""
    path = os.path.join(root, "conversations", f"{cid}.db")
    if not os.path.exists(path):
        return [], "missing db"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = list(conn.execute("select idx,data from gen_metadata order by idx"))
        step_rows = list(conn.execute("select idx,status,error_details from steps"))
        conn.close()
    except sqlite3.DatabaseError as exc:
        return [], str(exc)
    gens = []
    for idx, blob in rows:
        fields, kv, last_key = {}, {}, None
        for where, _kind, value in walk_protobuf(blob or b""):
            if where == KV_KEY:
                last_key = value
            elif where == KV_VALUE and last_key is not None:
                kv[last_key] = value
                last_key = None
            elif where not in fields:
                fields[where] = value
        gens.append({
            "idx": idx,
            "in_uncached": int(fields.get(TOK_UNCACHED_IN, 0) or 0),
            "in_cached": int(fields.get(TOK_CACHED_IN, 0) or 0),
            "out_total": int(fields.get(TOK_OUT_TOTAL, 0) or 0),
            "thinking": int(fields.get(TOK_THINKING, 0) or 0),
            "response": int(fields.get(TOK_RESPONSE, 0) or 0),
            "ctx_used": int(fields.get(CTX_USED, 0) or 0),
            "ctx_window": int(fields.get(CTX_WINDOW, 0) or 0),
            "latency_s": int(fields.get(LATENCY_NS, 0) or 0) / 1e9,
            "model": fields.get(MODEL_STRING, "") or "",
            "kv": kv,
        })
    errors = {}
    bad_status = set()
    statuses = collections.Counter()
    for idx, status, detail in step_rows:
        statuses[status] += 1
        if status == DB_STATUS_ERROR:
            bad_status.add(idx)
        if detail:
            strings = protobuf_strings(detail)
            if strings:
                errors[idx] = max(strings, key=len)
    return gens, {"errors": errors, "bad_status": bad_status, "statuses": statuses}


# ----------------------------------------------------------------- conversation
def audit_conversation(root, cid, summary, since):
    tpath = os.path.join(root, "brain", cid, ".system_generated",
                         "logs", "transcript_full.jsonl")
    if not os.path.exists(tpath):
        tpath = os.path.join(root, "brain", cid, ".system_generated",
                             "logs", "transcript.jsonl")
    if not os.path.exists(tpath):
        return None
    steps, bad_lines = read_transcript(tpath)
    if not steps:
        return None
    stamps = [parse_time(s.get("created_at")) for s in steps]
    known = [t for t in stamps if t]
    if not known or max(known) < since:
        return None

    gens, db_info = load_generations(root, cid)
    structured = isinstance(db_info, dict)
    err_by_idx = db_info["errors"] if structured else {}
    bad_status = db_info["bad_status"] if structured else set()
    db_note = "" if structured else db_info

    # a conversation with a parent is a subagent run: its "user" turns are briefs the
    # parent wrote, so they must not dilute the human correction rate
    is_subagent = bool(summary.get("parent")) or int(summary.get("depth") or 0) > 0

    rec = {
        "conversation_id": cid,
        "title": summary.get("title", ""),
        "workspace": summary.get("workspace", ""),
        "agent_name": summary.get("agent_name", ""),
        "parent": summary.get("parent", ""),
        "killed": summary.get("killed", 0),
        "not_fully_idle": summary.get("not_fully_idle", 0),
        "db_note": db_note,
        "bad_transcript_lines": bad_lines,
        "is_subagent": int(is_subagent),
        "steps": len(steps),
        "user_turns": 0, "model_turns": 0,
        "human_turns": 0, "brief_turns": 0,
        "compactions": 0,
        "red_gate_edits": 0, "red_gate_edit_rows": [],
        "reviewer_diff_lines": 0, "max_subagent_batch": 0,
        "start": min(known), "end": max(known),
        "wall_s": (max(known) - min(known)).total_seconds(),
        "active_s": active_seconds(stamps),
        "tool_calls": 0, "tool_failures": 0,
        "chars_user": 0, "chars_model": 0, "chars_thinking": 0,
        "chars_tool_args": 0, "chars_tool_results": 0, "chars_view_results": 0,
        "edits": 0, "views": 0, "verifier_runs": 0, "retry_loop_calls": 0,
        "corrections": 0,
        "tokens_in_uncached": 0, "tokens_in_cached": 0, "tokens_out": 0,
        "tokens_thinking": 0, "tokens_response": 0,
        "ctx_peak": 0, "ctx_window": 0,
        "escalations": 0,
        "subagents": collections.Counter(),
        "subagent_models": collections.Counter(),
        "models": collections.Counter(),
        "by_tool": collections.Counter(),
        "fail_by_tool": collections.Counter(),
        "secs_by_tool": collections.defaultdict(list),
        "mcp_calls": collections.Counter(),
        "mcp_failures": collections.Counter(),
        "errors": [],
        "big_results": [],
        "file_reads": collections.Counter(),
        "user_turn_rows": [],
        "verifier_runs_detail": [],
        "failed_call_seconds": 0.0,
        "hot_files": 0,
        "last_verifier_ok": "never",
        "edits_after_last_verifier": 0,
        "plans": 0,
    }

    for gen in gens:
        rec["tokens_in_uncached"] += gen["in_uncached"]
        rec["tokens_in_cached"] += gen["in_cached"]
        rec["tokens_out"] += gen["out_total"]
        rec["tokens_thinking"] += gen["thinking"]
        rec["tokens_response"] += gen["response"]
        rec["ctx_peak"] = max(rec["ctx_peak"], gen["ctx_used"])
        rec["ctx_window"] = max(rec["ctx_window"], gen["ctx_window"])
        if gen["model"]:
            rec["models"][gen["model"]] += 1
    order = [g["model"] for g in gens if g["model"]]
    for earlier, later in zip(order, order[1:]):
        if later != earlier and BIG_MODEL.search(later) and not BIG_MODEL.search(earlier):
            rec["escalations"] += 1

    last_signature = None
    repeat_run = 0
    pending = []          # tool calls awaiting their result step
    user_stamps = []
    last_verifier_at = None
    edits_timeline = []
    verifier_red = False  # did the most recent verifier run in this conversation fail
    red_label = ""

    for step, stamp in zip(steps, stamps):
        stype = step.get("type")
        content = step.get("content") or ""
        thinking = step.get("thinking") or ""
        calls = step.get("tool_calls") or []
        idx = step.get("step_index", 0)
        step_failed = (step.get("status") == "ERROR" or idx in err_by_idx
                       or idx in bad_status)

        if stype == "USER_INPUT":
            rec["user_turns"] += 1
            rec["brief_turns" if is_subagent else "human_turns"] += 1
            rec["chars_user"] += len(content)
            user_stamps.append(stamp)
            label = "brief" if is_subagent else ""
            if rec["user_turns"] > 1 and not is_subagent:
                label = "correction" if CORRECTION_WORDS.search(content) else "followup"
                if label == "correction":
                    rec["corrections"] += 1
            rec["user_turn_rows"].append({
                "conversation_id": cid, "step_index": idx,
                "timestamp": step.get("created_at"), "label": label,
                "workspace": rec["workspace"], "text": content,
            })
            continue

        if stype == COMPACTION_STEP:
            rec["compactions"] += 1

        if stype == "PLANNER_RESPONSE":
            rec["model_turns"] += 1
            rec["chars_model"] += len(content)
            rec["chars_thinking"] += len(thinking)
        elif stype == "GENERIC":
            seconds, body = tool_span(content)
            call_info = pending.pop(0) if pending else {}
            name = call_info.get("name", "?")
            last_mcp = call_info.get("mcp", "")
            rec["chars_tool_results"] += len(body)
            if seconds is not None:
                rec["secs_by_tool"][name].append(seconds)
            if name in VIEW_TOOLS:
                rec["chars_view_results"] += len(body)
            self_reported = result_failed(body)
            if step_failed or self_reported:
                rec["tool_failures"] += 1
                rec["fail_by_tool"][name] += 1
                if name == "call_mcp_tool":
                    rec["mcp_failures"][last_mcp or "?"] += 1
                if seconds:
                    rec["failed_call_seconds"] += seconds
                rec["errors"].append((name, normalise_error(
                    err_by_idx.get(idx) or self_reported or "status ERROR")))
            if len(body) > 20000:
                rec["big_results"].append((len(body), name, idx))
            if call_info.get("verifier"):
                verifier_red = bool(step_failed or self_reported)
                red_label = call_info["verifier"]
            if call_info.get("git_diff"):
                rec["reviewer_diff_lines"] = max(
                    rec["reviewer_diff_lines"], body.count("\n") + bool(body))
        elif step_failed:
            rec["tool_failures"] += 1
            rec["errors"].append(("<step>", normalise_error(
                err_by_idx.get(idx, "db step status 7"))))

        for call in calls:
            name = call.get("name") or "?"
            args = call.get("args") or {}
            args_text = json.dumps(args, sort_keys=True)
            rec["tool_calls"] += 1
            rec["by_tool"][name] += 1
            rec["chars_tool_args"] += len(args_text)
            mcp_key = ""
            if name == "call_mcp_tool":
                mcp_key = (f"{args.get('ServerName') or '?'}/"
                           f"{args.get('ToolName') or '?'}")
                rec["mcp_calls"][mcp_key] += 1
            ran = executed_text(name, args) if name in RUN_TOOLS else ""
            verifier_label = verifier_of(name, args)
            pending.append({"name": name, "mcp": mcp_key, "verifier": verifier_label,
                            "git_diff": bool(ran and GIT_DIFF.search(ran))})
            signature = retry_signature(name, args)
            if signature == last_signature:
                repeat_run += 1
                rec["retry_loop_calls"] += 1
            else:
                repeat_run = 0
            last_signature = signature

            specs = subagent_specs(args)
            rec["max_subagent_batch"] = max(rec["max_subagent_batch"], len(specs))
            for spec in specs:
                rec["subagents"][str(spec.get("TypeName") or spec.get("Role") or "?")] += 1
                rec["subagent_models"][str(spec.get("Model") or "?")] += 1
                if BIG_MODEL.search(str(spec.get("Model") or "")):
                    rec["escalations"] += 1
            if name in EDIT_TOOLS:
                rec["edits"] += 1
                edits_timeline.append(stamp)
                target = file_target(args)
                if verifier_red and target and GATE_FILE.search(target):
                    rec["red_gate_edits"] += 1
                    rec["red_gate_edit_rows"].append({
                        "conversation_id": cid, "step_index": idx, "file": target,
                        "failing_verifier": red_label, "workspace": rec["workspace"],
                        "timestamp": step.get("created_at"),
                    })
            if name in VIEW_TOOLS:
                rec["views"] += 1
                target = file_target(args)
                if target:
                    rec["file_reads"][target] += 1
            # a verifier run means something ran, not that a test file was read
            if verifier_label:
                rec["verifier_runs"] += 1
                rec["verifier_runs_detail"].append((idx, verifier_label))
                last_verifier_at = stamp
            if name in EDIT_TOOLS and re.search(
                    r"(?i)(implementation[_-]?plan|\bplan\.md\b|_PLAN\.md)", args_text):
                rec["plans"] += 1

    # did the last verifier pass, and how much was edited after it
    if last_verifier_at is None:
        rec["last_verifier_ok"] = "never" if rec["edits"] == 0 else "never-but-edited"
    else:
        tail = [i for i, _l in rec["verifier_runs_detail"]]
        last_idx = tail[-1]
        verdict = "unknown"
        for step in steps:
            if step.get("step_index", 0) > last_idx and step.get("type") == "GENERIC":
                _s, body = tool_span(step.get("content") or "")
                bad = (result_failed(body) or step.get("status") == "ERROR"
                       or step.get("step_index") in bad_status)
                verdict = "fail" if bad else "pass"
                break
        rec["last_verifier_ok"] = verdict
        rec["edits_after_last_verifier"] = sum(
            1 for t in edits_timeline if t and t > last_verifier_at)

    rec["hot_files"] = sum(1 for _f, n in rec["file_reads"].items() if n > 3)
    gaps = []
    marks = user_stamps + [rec["end"]]
    for earlier, later in zip(marks, marks[1:]):
        if earlier and later:
            gaps.append((later - earlier).total_seconds())
    rec["turn_gaps"] = gaps
    rec["corrections_per_100_model_turns"] = (
        round(100.0 * rec["corrections"] / rec["model_turns"], 1)
        if rec["model_turns"] else 0.0)
    rec["fail_rate"] = (round(rec["tool_failures"] / rec["tool_calls"], 3)
                        if rec["tool_calls"] else 0.0)
    return rec


def quality_score(rec):
    """Higher means worse. Only signals the brief names; no judgement of content."""
    score = 0.0
    score += 3.0 * rec["corrections"]
    score += 0.05 * rec["corrections_per_100_model_turns"]
    score += 8.0 if rec["last_verifier_ok"] == "never-but-edited" else 0.0
    score += 6.0 if rec["last_verifier_ok"] == "fail" else 0.0
    score += 0.5 * min(rec["edits_after_last_verifier"], 20)
    score += 0.3 * min(rec["retry_loop_calls"], 30)
    score += 20.0 * rec["fail_rate"]
    score += 5.0 * rec["killed"] + 5.0 * rec["not_fully_idle"]
    return round(score, 1)


# ---------------------------------------------------------------------- reports
def parse_stop_line(line):
    """(timestamp, kind, detail) for one stop_gate.log line, or None."""
    fields = line.rstrip("\n").split("\t")
    if len(fields) >= 4:
        when = parse_time(fields[0])
        return (when, fields[1], fields[3]) if when else None
    # gate format before 2026-09-18: "<stamp> <workspace> <kind> <detail>", and a
    # workspace path can hold spaces ("Birth Battle"), so the kind is the word before
    # the detail, which always started with execution= or reason=
    stamp, _, rest = line.strip().partition(" ")
    when = parse_time(stamp)
    if not when:
        return None
    match = LEGACY_DETAIL.search(rest)
    if match and match.group(1) in STOP_GATE_KINDS:
        return when, match.group(1), match.group(2)
    # a legacy line whose detail is not execution=/reason=, such as "verifier .agents/verify.cmd":
    # the kind is the first kind word that is not part of a path, because the workspace comes
    # first and the detail behind it can hold a kind word of its own
    words = rest.split(" ")
    for i, word in enumerate(words):
        if word in STOP_GATE_KINDS and not any(c in word for c in "/\\:"):
            return when, word, " ".join(words[i + 1:])
    return when, "", rest


def unchecked_stop(kind, detail):
    """A stop the gate returned from before running any check."""
    return kind in UNCHECKED_KINDS or "idle=False" in detail


def stop_gate_log(since, path=None):
    """(decision counts, stops, stops that skipped every check) since a cutoff."""
    path = path or STOP_GATE_LOG
    counts = collections.Counter()
    stops = unchecked = 0
    if not os.path.exists(path):
        return counts, stops, unchecked
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parsed = parse_stop_line(line)
            if not parsed:
                continue
            when, kind, detail = parsed
            if when < since:
                continue
            if not kind:
                counts["<unparsed>"] += 1
                continue
            first = detail.split(" ")[0] if detail else ""
            counts[f"{kind} {first}" if first else kind] += 1
            if kind in BOOKKEEPING_KINDS:
                continue  # lines the gate writes in passing, not decisions it took
            # every gate line is one stop decision, so an unchecked one counts wherever it landed
            stops += 1
            unchecked += unchecked_stop(kind, detail)
    return counts, stops, unchecked


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return 0.0
    position = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
    return values[position]


def line(label, value):
    print(f"  {label:<40} {value}")


def top(counter, limit=5, indent=6):
    if not counter:
        print(" " * indent + "none")
        return
    for name, count in counter.most_common(limit):
        print(f"{' ' * indent}{count:>7}  {name}")


def write_tsv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def snapshot_delta(previous, current):
    """One line per headline metric that has a comparable number in both runs."""
    lines = []
    for key, now in sorted(current.items()):
        before = previous.get(key)
        if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
            continue
        lines.append(f"{key}: {before:g} -> {now:g} ({now - before:+g})")
    return lines


def save_snapshot(metrics, snapshot_dir):
    """Write today's headline metrics. Returns (path, previous file name, delta lines)."""
    os.makedirs(snapshot_dir, exist_ok=True)
    name = f"{dt.date.today().isoformat()}.json"
    path = os.path.join(snapshot_dir, name)
    earlier = sorted(f for f in os.listdir(snapshot_dir)
                     if f.endswith(".json") and f < name)
    previous = {}
    if earlier:
        try:
            with open(os.path.join(snapshot_dir, earlier[-1]), encoding="utf-8") as handle:
                previous = json.load(handle) or {}
        except (OSError, ValueError):
            previous = {}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2, sort_keys=True)
    return path, (earlier[-1] if earlier else ""), snapshot_delta(previous, metrics)


def report(records, days, since, out_dir, snapshot_dir, skipped):
    os.makedirs(out_dir, exist_ok=True)
    total_tool_calls = sum(r["tool_calls"] for r in records)
    all_tools, all_fails, all_errors = (collections.Counter() for _ in range(3))
    mcp_calls, mcp_fails, models, by_day, by_ws = (collections.Counter() for _ in range(5))
    secs_by_tool = collections.defaultdict(list)
    gaps = []
    big = []
    for rec in records:
        all_tools.update(rec["by_tool"])
        all_fails.update(rec["fail_by_tool"])
        mcp_calls.update(rec["mcp_calls"])
        models.update(rec["models"])
        gaps.extend(rec["turn_gaps"])
        by_day[rec["start"].date().isoformat()] += 1
        by_ws[rec["workspace"] or "<none>"] += 1
        mcp_fails.update(rec["mcp_failures"])
        for _name, text in rec["errors"]:
            all_errors[text] += 1
        for name, spans in rec["secs_by_tool"].items():
            secs_by_tool[name].extend(spans)
        for size, name, idx in rec["big_results"]:
            big.append((size, name, rec["conversation_id"], idx, rec["title"]))

    human_turns = sum(r["human_turns"] for r in records)
    brief_turns = sum(r["brief_turns"] for r in records)
    corrections = sum(r["corrections"] for r in records)
    correction_rate = round(100.0 * corrections / human_turns, 1) if human_turns else 0.0
    longest = max(records, key=lambda r: r["steps"], default=None)
    red_rows = [row for r in records for row in r["red_gate_edit_rows"]]
    stop_counts, stops, unchecked = stop_gate_log(since)
    unchecked_share = round(100.0 * unchecked / stops, 1) if stops else 0.0

    children = collections.defaultdict(list)
    for rec in records:
        if rec["parent"]:
            children[rec["parent"]].append((rec["start"], rec["end"]))
    peaks = collections.Counter({parent: peak_overlap(spans)
                                 for parent, spans in children.items()})
    reviewer_diffs = sorted(r["reviewer_diff_lines"] for r in records
                            if r["agent_name"] == "reviewer" and r["reviewer_diff_lines"])

    tok_in = sum(r["tokens_in_uncached"] for r in records)
    tok_cached = sum(r["tokens_in_cached"] for r in records)
    tok_out = sum(r["tokens_out"] for r in records)
    tok_think = sum(r["tokens_thinking"] for r in records)

    print(f"agy audit - last {days}d (since {since:%Y-%m-%d}), data root read-only")
    if skipped:
        print(f"  note: {len(skipped)} conversation(s) unreadable: "
              + ", ".join(f"{c[:8]} ({why[:40]})" for c, why in skipped[:4]))
    print("\nVOLUME")
    line("conversations", len(records))
    line("  of which subagent runs", sum(1 for r in records if r["parent"]))
    line("steps", sum(r["steps"] for r in records))
    line("user turns / model turns",
         f"{sum(r['user_turns'] for r in records)} / {sum(r['model_turns'] for r in records)}")
    line("  human turns / subagent briefs", f"{human_turns} / {brief_turns}")
    line("compactions (CHECKPOINT steps)", sum(r["compactions"] for r in records))
    line("longest conversation (steps)",
         f"{longest['steps']} {longest['conversation_id'][:8]} "
         f"{(longest['title'] or '<untitled>')[:40]}" if longest else 0)
    line("wall clock hours", round(sum(r["wall_s"] for r in records) / 3600, 1))
    line(f"active hours (gaps >{IDLE_GAP_SECONDS // 60}min excluded)",
         round(sum(r["active_s"] for r in records) / 3600, 1))
    line("killed / not_fully_idle",
         f"{sum(r['killed'] for r in records)} / {sum(r['not_fully_idle'] for r in records)}")
    print("    busiest days:")
    top(by_day, 3)
    print("    busiest workspaces:")
    top(by_ws, 3)

    print("\nTOKENS (real counts from gen_metadata, not a proxy)")
    line("input uncached / cached", f"{tok_in:,} / {tok_cached:,}")
    line("cache read share of input",
         f"{(tok_cached / (tok_in + tok_cached) * 100 if tok_in + tok_cached else 0):.0f}%")
    line("output total / of that thinking", f"{tok_out:,} / {tok_think:,}")
    line("thinking share of output",
         f"{(tok_think / tok_out * 100 if tok_out else 0):.0f}%")
    line("peak context used / window",
         f"{max([r['ctx_peak'] for r in records] or [0]):,} / "
         f"{max([r['ctx_window'] for r in records] or [0]):,}")
    print("    tokens by conversation (top 5, in+out):")
    top(collections.Counter({
        f"{r['conversation_id'][:8]} {r['title'][:40]}":
        r["tokens_in_uncached"] + r["tokens_in_cached"] + r["tokens_out"]
        for r in records}), 5)

    print("\nMODEL MIX")
    top(models, 8)
    line("generations on a larger model",
         sum(n for m, n in models.items() if BIG_MODEL.search(m)))
    line("conversations using more than one model",
         sum(1 for r in records if len(r["models"]) > 1))
    line("escalations (subagent asked for the big model)",
         sum(r["escalations"] for r in records))
    print("    subagent model requests:")
    submodels = collections.Counter()
    for rec in records:
        submodels.update(rec["subagent_models"])
    top(submodels, 5)

    print("\nTOOL USE")
    line("tool calls", total_tool_calls)
    line("failures / rate",
         f"{sum(r['tool_failures'] for r in records)} "
         f"({(sum(r['tool_failures'] for r in records) / total_tool_calls * 100 if total_tool_calls else 0):.0f}%)")
    line("repeated identical calls in a row",
         sum(r["retry_loop_calls"] for r in records))
    print("    calls by tool:")
    top(all_tools, 8)
    print("    failure rate by tool (calls >=5):")
    rates = collections.Counter({
        f"{n} {all_fails[n]}/{c}": round(all_fails[n] / c, 2)
        for n, c in all_tools.items() if c >= 5 and all_fails[n]})
    top(rates, 5)
    print("    MCP calls by server/tool (failures in brackets):")
    top(collections.Counter({f"{k} [{mcp_fails[k]} failed]": v
                             for k, v in mcp_calls.items()}), 5)

    print("\nCONTEXT AND SIZE")
    for label, key in (("user input", "chars_user"), ("model content", "chars_model"),
                       ("thinking", "chars_thinking"), ("tool args", "chars_tool_args"),
                       ("tool results", "chars_tool_results")):
        line(f"chars {label}", f"{sum(r[key] for r in records):,}")
    line("file-view result chars vs edits made",
         f"{sum(r['chars_view_results'] for r in records):,} chars / "
         f"{sum(r['edits'] for r in records)} edits")
    line("conversations re-reading a file >3x",
         sum(1 for r in records if r["hot_files"]))
    print("    10 largest single tool results:")
    for size, name, cid, idx, title in sorted(big, reverse=True)[:10]:
        print(f"      {size:>8,}  {name:<22} {cid[:8]} step {idx}  {title[:34]}")

    print("\nQUALITY SIGNALS")
    line("verifier runs", sum(r["verifier_runs"] for r in records))
    line("conversations that edited but never verified",
         sum(1 for r in records if r["last_verifier_ok"] == "never-but-edited"))
    line("conversations whose last verifier failed",
         sum(1 for r in records if r["last_verifier_ok"] == "fail"))
    line("edits after the last verifier run",
         sum(r["edits_after_last_verifier"] for r in records))
    line("plan files written", sum(r["plans"] for r in records))
    print("    invoke_subagent calls by requested agent:")
    asked = collections.Counter()
    for rec in records:
        asked.update(rec["subagents"])
    top(asked, 6)
    print("    subagent conversations, and how each ended:")
    ends = collections.Counter(
        f"{r['agent_name'] or '<root>'} -> "
        f"{'killed' if r['killed'] else r['last_verifier_ok']}"
        for r in records if r["parent"])
    top(ends, 6)
    print("    hook stop-gate decisions:")
    top(stop_counts, 6)

    print("\nGATE AND REVIEW")
    line("stops / of those, skipped every check",
         f"{stops} / {unchecked} ({unchecked_share}%)")
    line("test or verifier edits while verifier red", len(red_rows))
    print("    files edited while red (top 5):")
    top(collections.Counter(os.path.basename(row["file"]) for row in red_rows), 5)
    line("peak live subagents (worst parent)",
         f"{max(peaks.values(), default=0)} live, "
         f"{max((r['max_subagent_batch'] for r in records), default=0)} asked for at once")
    line("parents that ran more than 3 at once",
         sum(1 for n in peaks.values() if n > 3))
    line("reviewer diff lines, median / max",
         f"{percentile(reviewer_diffs, 0.5):,} / {max(reviewer_diffs, default=0):,}"
         f" over {len(reviewer_diffs)} reviewer runs")

    print("\nUSER CORRECTIONS (keyword heuristic, human turns only)")
    line("human turns after the first",
         sum(max(0, r["human_turns"] - 1) for r in records if not r["is_subagent"]))
    line("of those, correction-flagged", corrections)
    line("rate per 100 human turns", correction_rate)

    print("\nSPEED")
    line("user-turn to next-turn seconds, median / p90",
         f"{percentile(gaps, 0.5):.0f} / {percentile(gaps, 0.9):.0f}")
    line("seconds lost to failed calls",
         round(sum(r["failed_call_seconds"] for r in records)))
    # the Created At/Completed At header is whole seconds, so anything faster reads 0
    print("    median seconds per tool call (1s resolution, calls >=5):")
    med = collections.Counter({
        n: round(statistics.median(v), 1)
        for n, v in secs_by_tool.items() if len(v) >= 5})
    top(med, 6)

    print("\nTOP OFFENDERS ON QUALITY SIGNALS")
    ranked = sorted(records, key=quality_score, reverse=True)[:10]
    for rec in ranked:
        why = []
        if rec["corrections"]:
            why.append(f"{rec['corrections']} corrections "
                       f"({rec['corrections_per_100_model_turns']}/100 model turns)")
        if rec["last_verifier_ok"] == "never-but-edited":
            why.append(f"{rec['edits']} edits, no verifier")
        elif rec["last_verifier_ok"] == "fail":
            why.append("last verifier failed")
        if rec["edits_after_last_verifier"]:
            why.append(f"{rec['edits_after_last_verifier']} edits after last verifier")
        if rec["retry_loop_calls"]:
            why.append(f"{rec['retry_loop_calls']} repeat calls")
        if rec["fail_rate"] >= 0.1:
            why.append(f"{rec['fail_rate'] * 100:.0f}% tool failure")
        if rec["killed"]:
            why.append("killed")
        print(f"  {quality_score(rec):>6}  {rec['conversation_id'][:8]}  "
              f"{(rec['title'] or '<untitled>')[:34]:<34} {rec['workspace'][:26]:<26} "
              f"{'; '.join(why) or 'clean'}")

    print("\nTOP 15 DISTINCT ERRORS (normalised)")
    top(all_errors, 15)

    # -------------------------------------------------------------- detail files
    conv_cols = ["conversation_id", "title", "workspace", "agent_name", "parent",
                 "is_subagent", "start", "end", "steps", "user_turns", "model_turns",
                 "human_turns", "brief_turns", "compactions", "red_gate_edits",
                 "reviewer_diff_lines",
                 "wall_s", "active_s", "killed", "not_fully_idle",
                 "tokens_in_uncached", "tokens_in_cached", "tokens_out",
                 "tokens_thinking", "ctx_peak", "ctx_window", "models", "escalations",
                 "tool_calls", "tool_failures", "fail_rate", "retry_loop_calls",
                 "chars_user", "chars_model", "chars_thinking", "chars_tool_args",
                 "chars_tool_results", "chars_view_results", "views", "edits",
                 "hot_files", "verifier_runs", "last_verifier_ok",
                 "edits_after_last_verifier", "plans", "corrections",
                 "corrections_per_100_model_turns", "failed_call_seconds",
                 "quality_score", "db_note", "bad_transcript_lines"]
    rows = []
    for rec in sorted(records, key=quality_score, reverse=True):
        row = []
        for col in conv_cols:
            if col == "quality_score":
                row.append(quality_score(rec))
            elif col == "models":
                row.append(",".join(f"{m}:{n}" for m, n in rec["models"].most_common()))
            elif col in ("start", "end"):
                row.append(rec[col].isoformat())
            elif col in ("wall_s", "active_s", "failed_call_seconds"):
                row.append(round(rec[col]))
            else:
                row.append(rec[col])
        rows.append(row)
    write_tsv(os.path.join(out_dir, "conversations.tsv"), conv_cols, rows)

    write_tsv(os.path.join(out_dir, "errors.tsv"),
              ["count", "tool", "normalised_error"],
              sorted(((n, t, e) for (t, e), n in collections.Counter(
                  (t, e) for r in records for t, e in r["errors"]).items()),
                  reverse=True))

    write_tsv(os.path.join(out_dir, "big_results.tsv"),
              ["chars", "tool", "conversation_id", "step_index", "title", "workspace"],
              [(s, n, c, i, t, next((r["workspace"] for r in records
                                     if r["conversation_id"] == c), ""))
               for s, n, c, i, t in sorted(big, reverse=True)[:200]])

    write_jsonl(os.path.join(out_dir, "user_turns.jsonl"),
                [row for rec in records for row in rec["user_turn_rows"]])
    write_jsonl(os.path.join(out_dir, "red_gate_edits.jsonl"), red_rows)
    print(f"\ndetail: {out_dir}")

    metrics = {
        "date": dt.date.today().isoformat(),
        "days": days,
        "conversations": len(records),
        "steps": sum(r["steps"] for r in records),
        "human_turns": human_turns,
        "subagent_briefs": brief_turns,
        "corrections": corrections,
        "corrections_per_100_human_turns": correction_rate,
        "red_gate_edits": len(red_rows),
        "compactions": sum(r["compactions"] for r in records),
        "longest_conversation_steps": longest["steps"] if longest else 0,
        "stops": stops,
        "unchecked_stops": unchecked,
        "unchecked_stop_share_pct": unchecked_share,
        "peak_live_subagents": max(peaks.values(), default=0),
        "reviewer_diff_lines_median": percentile(reviewer_diffs, 0.5),
        "reviewer_diff_lines_max": max(reviewer_diffs, default=0),
        "tokens_total": tok_in + tok_cached + tok_out,
    }
    path, previous, delta = save_snapshot(metrics, snapshot_dir)
    print(f"snapshot: {path}")
    if delta:
        print(f"    delta against {previous}:")
        for text in delta:
            print(f"      {text}")


# ------------------------------------------------------------------- self-check
def self_check():
    assert walk_protobuf(b"\x08\xa6\n") == [(".1", "int", 1318)]
    assert walk_protobuf(b"\x12\x03abc") == [(".2", "str", "abc")]
    assert walk_protobuf(b"\x08") == [], "truncated varint must not raise"
    assert walk_protobuf(b"\x12\x7f") == [], "truncated length must not raise"
    assert protobuf_strings(b"\x12\x0chello world!") == ["hello world!"]

    base = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)
    step = dt.timedelta(seconds=30)
    # three 30s steps then a 40min break then two more 30s steps
    stamps = [base, base + step, base + 2 * step,
              base + 2 * step + dt.timedelta(minutes=40),
              base + 2 * step + dt.timedelta(minutes=40) + step]
    assert active_seconds(stamps) == 90.0, active_seconds(stamps)
    assert (stamps[-1] - stamps[0]).total_seconds() == 2490.0
    assert active_seconds([base]) == 0.0
    assert active_seconds([]) == 0.0

    span, body = tool_span("Created At: 2026-09-17T00:25:27-07:00\n"
                           "Completed At: 2026-09-17T00:25:31-07:00\nresult text")
    assert span == 4.0 and body == "result text", (span, body)
    assert tool_span("no header") == (None, "no header")

    assert parse_time("2026-09-18 08:34:36.5462698+00:00") is not None
    assert parse_time("2026-09-17T07:25:25Z").hour == 7
    assert parse_time("") is None and parse_time("junk") is None

    assert CORRECTION_WORDS.search("that's still wrong")
    assert CORRECTION_WORDS.search("why did you revert that")
    assert not CORRECTION_WORDS.search("now add the second screen please")
    assert normalise_error("failed at line 4021 in deadbeefcafe") == \
        "failed at line N in <hex>"
    assert workspace_of('["file:///F:/Factory/renpy/Birth%20Battle"]') == \
        "F:/Factory/renpy/Birth Battle"
    assert BIG_MODEL.search("gemini-3.1-pro-low") and not BIG_MODEL.search("gemini-3.8-flash")

    assert result_failed('{"success":true,"exit_code":0}') == ""
    assert result_failed('{"success":true,"exit_code":2}') == "exit_code 2"
    assert result_failed('{"success":false}') == "success false"
    # a file whose text merely mentions failure is not a failed call
    assert result_failed("# raise RuntimeError on permission denied") == ""
    assert result_failed("\nThe command exited with code 1.\nOutput:\nboom") == "exit code 1"
    assert result_failed("\nThe command exited with code 0.\nOutput:\nfine") == ""
    # the same sentence quoted inside a file that was read is not a failed call
    assert result_failed("docs say: The command exited with code 1.") == ""

    prose = {"toolAction": "Looking again", "Query": "X"}
    again = {"toolAction": "Different words", "Query": "X"}
    assert retry_signature("grep_search", prose) == retry_signature("grep_search", again)
    assert retry_signature("grep_search", prose) != retry_signature("grep_search", {"Query": "Y"})

    assert executed_text("run_command", {"CommandLine": "pytest -q"}) == "pytest -q"
    assert executed_text("view_file", {"AbsolutePath": "test_x.py"}) == ""
    assert verifier_of("run_command", {"CommandLine": "pytest -q"}) == "python -m pytest"
    # reading a test file, printing the command or collecting tests is not a verifier run
    assert executed_text("view_file", {"AbsolutePath": "tests/test_x.py"}) == ""
    assert verifier_of("view_file", {"AbsolutePath": "tests/test_x.py"}) == ""
    assert verifier_of("run_command", {"CommandLine": "echo pytest"}) == ""
    assert verifier_of("run_command", {"CommandLine": "pytest --collect-only"}) == ""

    assert [s["TypeName"] for s in subagent_specs(
        {"Subagents": [{"TypeName": "reviewer", "Model": "pro"}]})] == ["reviewer"]
    assert subagent_specs({"Subagents": "not a list"}) == []

    for path in ("F:/proj/tests/test_gameplay.py", "hooks/stop_gate.py",
                 "C:\\r\\hooks\\tests\\test_hooks.py", "x/audit_project.py",
                 "F:/p/.agents/verify.cmd", "F:/p/.agents/verify.sh",
                 "F:/p/.agents/verify.ps1", "pkg/parser_test.py"):
        assert GATE_FILE.search(path), path
    for path in ("game/bb/bb_birth_loop.rpy", "docs/testing.md", "latest.json",
                 "scripts/verify_output.py", "src/hooks_registry.py"):
        assert not GATE_FILE.search(path), path
    assert file_target({"TargetFile": "a.py", "AbsolutePath": "b.py"}) == "a.py"
    assert file_target({}) == ""

    assert GIT_DIFF.search("git --no-pager diff HEAD")
    assert GIT_DIFF.search("git diff HEAD -- a.rpy")
    assert not GIT_DIFF.search("git status -s")

    hour = dt.timedelta(hours=1)
    # two overlap, the third starts after the first two ended
    assert peak_overlap([(base, base + 2 * hour), (base + hour, base + 3 * hour),
                         (base + 4 * hour, base + 5 * hour)]) == 2
    assert peak_overlap([(base, base + hour), (base + hour, base + 2 * hour)]) == 1
    assert peak_overlap([(None, base)]) == 0 and peak_overlap([]) == 0

    when, kind, detail = parse_stop_line(
        "2026-09-18T00:33:41\tno-verifier\tF:/Factory/renpy/Birth Battle\ttop-level step 42")
    assert (kind, detail) == ("no-verifier", "top-level step 42") and when.day == 18
    assert unchecked_stop(*parse_stop_line(
        "2026-09-18T00:32:09\tskip\t-\treason='ERROR' idle=True")[1:])

    # older lines hold the workspace before the kind, and a path can hold spaces, so the
    # kind is the word in front of the execution=/reason= detail
    when, kind, detail = parse_stop_line(
        "2026-09-18T00:33:41 F:/Factory/renpy/Birth Battle stop execution=0")
    assert (kind, detail) == ("stop", "execution=0") and when.day == 18
    # an old line with no execution=/reason= detail still names its kind after the path
    assert parse_stop_line(
        "2026-09-18T00:33:41 F:/Factory/renpy/Birth Battle verifier .agents/verify.cmd"
    )[1:] == ("verifier", ".agents/verify.cmd")
    assert parse_stop_line("2026-09-18T00:33:41 F:/p leftovers 3 paths")[1:] == \
        ("leftovers", "3 paths")
    # a release line carries the gap kind inside its detail, so the kind is the one by position
    assert parse_stop_line("2026-09-18T00:33:41 F:/p release no-review top-level")[1:] == \
        ("release", "no-review top-level")
    assert parse_stop_line("2026-09-18T00:33:41 F:/p nothing here")[1] == ""
    assert parse_stop_line("not a log line") is None
    skipped_stop = parse_stop_line("2026-09-18T00:32:09 - stop reason='NO_TOOL_CALL' idle=False")
    assert unchecked_stop(*skipped_stop[1:])
    assert not unchecked_stop(*parse_stop_line(
        "2026-09-18T00:32:09 - stop reason='NO_TOOL_CALL' idle=True")[1:])
    assert not unchecked_stop("stop", "execution=0")

    # what a judge reviewed, a path that would not resolve and a trust note are all things the
    # gate writes down beside a decision, not stops it decided anything about
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                     encoding="utf-8") as handle:
        handle.write("2026-09-18T00:33:41\tstop\tF:/p\tverified\n"
                     "2026-09-18T00:33:42\treview\tF:/p\treviewer recorded 1/1\n"
                     "2026-09-18T00:33:43\tunresolved\t/no/such\tpath dropped\n"
                     "2026-09-18T00:33:44\ttrust\tF:/p\tuntrusted workspace\n")
    counts, stops, unchecked = stop_gate_log(base - dt.timedelta(days=1), handle.name)
    os.unlink(handle.name)
    assert (stops, unchecked) == (1, 0) and counts["review reviewer"] == 1
    assert counts["unresolved path"] == 1 and counts["trust untrusted"] == 1

    assert snapshot_delta({"stops": 10, "date": "2026-09-11"},
                          {"stops": 4, "date": "2026-09-18"}) == ["stops: 10 -> 4 (-6)"]
    assert snapshot_delta({}, {"stops": 4}) == []
    print("self-check ok")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit Google Antigravity CLI (agy) usage. Read-only.",
        epilog="Exits 1 on failure. Sources: brain transcripts, conversations/*.db "
               "(gen_metadata holds real token counts), conversation_summaries.db, "
               "~/.gemini/tmp/stop_gate.log.")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--data-root", default=DEFAULT_ROOT,
                        help=f"antigravity-cli data root (default {DEFAULT_ROOT})")
    parser.add_argument("--out", default=os.path.join(
        os.path.expanduser("~"), ".gemini", "cache", "agy-audit"),
        help="directory for the detail files")
    parser.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR,
                        help=f"dated headline-metric snapshots (default {DEFAULT_SNAPSHOT_DIR})")
    parser.add_argument("--self-check", action="store_true",
                        help="run the built-in parser and active-time checks, then exit")
    args = parser.parse_args(argv)

    if args.self_check:
        self_check()
        return 0
    if args.days < 1:
        die("--days must be 1 or more")
    root = args.data_root
    if not os.path.isdir(os.path.join(root, "brain")):
        die(f"no brain/ under {root}")

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    summaries = load_summaries(root)
    records, skipped = [], []
    for path in sorted(glob.glob(os.path.join(root, "brain", "*"))):
        cid = os.path.basename(path)
        try:
            rec = audit_conversation(root, cid, summaries.get(cid, {}), since)
        except Exception as exc:                       # one bad file must not stop the run
            skipped.append((cid, f"{type(exc).__name__}: {exc}"))
            continue
        if rec:
            if rec["db_note"]:
                skipped.append((cid, rec["db_note"]))
            records.append(rec)
    if not records:
        die(f"no conversations modified in the last {args.days} days under {root}")
    report(records, args.days, since, args.out, args.snapshot_dir, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
