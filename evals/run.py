#!/usr/bin/env python3
"""Shunt eval runner: hook routing decisions + bulk-read transport (stubbed claude).

Usage:
  python3 evals/run.py            # all suites
  python3 evals/run.py -v         # also print deny reasons
  python3 evals/run.py --live     # also run real bulk-read calls (needs login)
Without --live, no network or Claude login is needed: the transport suite uses
a stub claude.
"""
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time

EVALS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVALS)
HOOK = os.path.join(ROOT, "hooks", "shunt-hook")
BULK_READ = os.path.join(ROOT, "scripts", "bulk-read")
TOGGLE = os.path.join(ROOT, "scripts", "shunt-toggle")
REPORT = os.path.join(ROOT, "scripts", "shunt-report")
# Usage log and session switches go here, never to the real state directory.
STATE = tempfile.mkdtemp(prefix="shunt-state-")
VERBOSE = "-v" in sys.argv
LIVE = "--live" in sys.argv
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
results = {"pass": 0, "fail": 0, "skip": 0}


def report(ok, name, detail):
    key = "pass" if ok is True else "skip" if ok is None else "fail"
    results[key] += 1
    label = {"pass": GREEN + "PASS", "fail": RED + "FAIL", "skip": DIM + "SKIP"}[key]
    print("  {}{}  {:<30} {}".format(label, RESET, name, detail))


def make_fixtures(root):
    spec = json.load(open(os.path.join(EVALS, "fixtures.json")))["fixtures"]
    for rel, opts in spec.items():
        path = os.path.join(root, rel)
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        opts = {"lines": opts} if isinstance(opts, int) else opts
        if opts.get("generate"):
            subprocess.run([sys.executable, os.path.join(EVALS, "fixtures", opts["generate"]), path],
                           check=True)
            continue
        if opts.get("binary"):
            open(path, "wb").write(b"\x89PNG\r\n\x1a\n\0\0" + b"\n" * 5000)
            continue
        body = "".join("line {}\n".format(n) for n in range(1, opts["lines"] + 1))
        if not opts.get("trailing_newline", True):
            body = body.rstrip("\n")
        open(path, "w").write(body)


def hook_env(extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SHUNT_") and k != "CLAUDE_CODE_SESSION_ID"}
    env["SHUNT_STATE_DIR"] = STATE
    env.update(extra or {})
    return env


def read_log(state=STATE):
    try:
        return [json.loads(line) for line in open(os.path.join(state, "usage.jsonl"))]
    except OSError:
        return []


def rtk_rewrite(payload):
    """Apply the real rtk hook's updatedInput, as Claude Code would."""
    out = subprocess.run(["rtk", "hook", "claude"], input=json.dumps(payload),
                         capture_output=True, text=True).stdout.strip()
    if out:
        updated = json.loads(out).get("hookSpecificOutput", {}).get("updatedInput")
        if updated:
            payload = dict(payload, tool_input=updated)
    return payload


def decision_of(stdout):
    """'allow' for no output, 'deny' for a well-formed deny, else a description."""
    if not stdout.strip():
        return "allow", None
    try:
        out = json.loads(stdout)
    except ValueError:
        return "invalid-json", None
    if "decision" in out:
        return "deprecated-format", None
    hso = out.get("hookSpecificOutput", {})
    if hso.get("hookEventName") != "PreToolUse" or not hso.get("permissionDecisionReason"):
        return "malformed", None
    return hso.get("permissionDecision"), hso["permissionDecisionReason"]


def run_hook_suite(evals_file, label):
    spec = json.load(open(os.path.join(EVALS, evals_file)))
    mode = spec["hook_mode"]
    print("\n{}\n{}".format(label, "─" * 72))
    fixtures = tempfile.mkdtemp(prefix="shunt-fixtures-")
    try:
        make_fixtures(fixtures)
        ledger_tmp = os.path.join(fixtures, ".tmp")  # isolates the read-budget ledger
        os.makedirs(ledger_tmp)
        for ev in spec["evals"]:
            name = ev["name"]
            if ev.get("via_rtk") and not shutil.which("rtk"):
                report(None, name, "rtk not installed")
                continue
            sub = lambda s: s.replace("{{FIXTURES}}", fixtures)
            env = hook_env(dict({"TMPDIR": ledger_tmp}, **ev.get("env", {})))
            if ev.get("session_off"):  # as `shunt-toggle off` would leave it
                off = ev["session_off"] if isinstance(ev["session_off"], str) else "eval-" + name
                subprocess.run([TOGGLE, "off"], capture_output=True,
                               env=dict(env, CLAUDE_CODE_SESSION_ID=off), check=True)

            def build(step):
                payload = json.loads(sub(json.dumps(step.get("input", {}))))
                payload.setdefault("hook_event_name", "PreToolUse")
                payload.setdefault("tool_name", "Read" if mode == "read" else "Bash")
                payload.setdefault("cwd", sub(ev.get("cwd", "")) or fixtures + "/subdir")
                # Each eval gets its own session unless it names one (null = none).
                session = step.get("session_id", ev.get("session_id", "eval-" + name))
                if session:
                    payload["session_id"] = session
                if "command" in step:
                    payload.setdefault("tool_input", {})["command"] = sub(step["command"])
                if "tool_input" in step:
                    payload["tool_input"] = json.loads(sub(json.dumps(step["tool_input"])))
                if ev.get("via_rtk"):
                    payload = rtk_rewrite(payload)
                return json.dumps(payload)

            # Earlier reads in the same session; each must pass through.
            prior_denied = None
            for step in ev.get("prior", []):
                proc = subprocess.run([HOOK, mode], input=build(step), capture_output=True,
                                      text=True, env=env)
                if decision_of(proc.stdout)[0] != "allow":
                    prior_denied = step
                    break
            if prior_denied:
                report(False, name, "prior step was denied: {}".format(prior_denied))
                continue
            stdin = ev["raw_input"] if "raw_input" in ev else build(ev)
            proc = subprocess.run([HOOK, mode], input=stdin, capture_output=True,
                                  text=True, env=env)
            actual, reason = decision_of(proc.stdout)
            ok = proc.returncode == 0 and actual == ev["expected_decision"]
            missing = [s for s in ev.get("reason_contains", []) if s not in (reason or "")]
            if ok and missing:
                ok, actual = False, "reason lacks {}".format(missing)
            detail = ev["reason"] if ok else "expected={} got={} (exit {})".format(
                ev["expected_decision"], actual, proc.returncode)
            report(ok, name, detail)
            if VERBOSE and reason:
                print("        " + DIM + reason.replace("\n", "\n        ") + RESET)
    finally:
        shutil.rmtree(fixtures)


STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
rec = os.environ["STUB_RECORD"]
stdin = sys.stdin.read()
calls = json.load(open(rec)) if os.path.exists(rec) else []
calls.append({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
              "env": {k: os.environ.get(k) for k in ("SHUNT_CHILD", "CLAUDE_EFFORT")}})
json.dump(calls, open(rec, "w"))
mode = os.environ.get("STUB_MODE", "ok")
if mode == "sleep":
    time.sleep(10)
if mode == "fail":
    sys.stderr.write("Not logged in. Run claude /login\n"); sys.exit(1)
if mode == "garbage":
    print("not json"); sys.exit(0)
if mode == "ttl-error":  # reject 1h breakpoints, like a 5m session would
    block = json.loads(stdin)["message"]["content"][0]
    mode = "ttl" if block.get("cache_control", {}).get("ttl") == "1h" else "ok"
result = {"ok": "- big.txt:12 `parse()`", "cited": "- big.txt:12 `SECRET_LINE_12` and big.txt:2-3 `SECRET_LINE_3`",
          "prose": "- big.txt:12 `SECRET_LINE_12`, unlike `qty < 0`\n- big.txt:20\n  - `SECRET_LINE_20`\n- big.txt:30 `SECRET_LINE_99`\n- big.txt:40 and line 41 then `SECRET_LINE_99`",
          "empty": "  ", "error": "API Error: overloaded",
          "ttl": "API Error: 400 messages.0.content.0.cache_control.ttl: bad order"}[mode]
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": mode in ("error", "ttl"),
                  "result": result, "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 5, "cache_creation_input_tokens": 3000,
                            "cache_read_input_tokens": 0, "output_tokens": 40}}))
'''


def run_transport_suite():
    print("\nTransport (scripts/bulk-read, stubbed claude)\n" + "─" * 72)
    tmp = tempfile.mkdtemp(prefix="shunt-transport-")
    try:
        stub, record = os.path.join(tmp, "claude"), os.path.join(tmp, "record.json")
        open(stub, "w").write(STUB)
        os.chmod(stub, 0o755)
        big = os.path.join(tmp, "big.txt")
        open(big, "w").write("".join("SECRET_LINE_{}\n".format(n) for n in range(1, 801)))

        def call(env=None, args=None):
            if os.path.exists(record):
                os.remove(record)
            e = hook_env(dict({"SHUNT_CLAUDE_BIN": stub, "STUB_RECORD": record,
                               "CLAUDE_EFFORT": "high"}, **(env or {})))
            argv = args if args is not None else ["--question", "Where is parse?", "--paths", big]
            p = subprocess.run([BULK_READ] + argv, capture_output=True, text=True, env=e)
            calls = json.load(open(record)) if os.path.exists(record) else []
            return p, (calls[-1] if calls else None), calls

        def flag(argv, name):
            return argv[argv.index(name) + 1] if name in argv else None

        def blocks(rec):
            try:
                return json.loads(rec["stdin"])["message"]["content"]
            except (TypeError, ValueError, KeyError):
                return [{}, {}]

        p, rec, _ = call()
        argv = rec["argv"] if rec else []
        files_block, question_block = (blocks(rec) + [{}, {}])[:2]
        checks = [
            ("ok-exit-and-answer", p.returncode == 0 and p.stdout.startswith("- big.txt:12 `parse()`\n"), "answer printed on stdout"),
            ("verify-flags-bad-quote", '[shunt-verify: 0 of 1 citations' in p.stdout and 'big.txt:12: "parse()" is not in the file' in p.stdout, "a quote that isn't at the cited line is flagged after the answer"),
            ("ok-token-note-stderr", "input tokens" in p.stderr and "SECRET" not in p.stdout + p.stderr, "rough token note on stderr, no file content echoed"),
            ("contents-on-stdin", '<file path="big.txt">' in files_block.get("text", "") and "1\tSECRET_LINE_1\n" in files_block.get("text", "") and "Where is parse?" in question_block.get("text", ""), "files in <file> tags under short names, numbered, on stdin; question in its own block"),
            ("files-block-cached", files_block.get("cache_control") == {"type": "ephemeral", "ttl": "1h"} and "cache_control" not in question_block, "files block has a 1h cache breakpoint"),
            ("contents-not-in-argv", rec and not any("SECRET_LINE" in a for a in argv), "file content never in argv"),
            ("default-model-effort", flag(argv, "--model") == "claude-haiku-5-5" and flag(argv, "--effort") == "medium", "claude-haiku-5-5 at medium effort"),
            ("tools-and-mcp-off", flag(argv, "--tools") == "" and "--strict-mcp-config" in argv and flag(argv, "--mcp-config") == '{"mcpServers":{}}', "--tools '' and empty strict MCP config"),
            ("minimal-startup", flag(argv, "--setting-sources") == "" and "--disable-slash-commands" in argv and "--no-session-persistence" in argv and "-p" in argv and flag(argv, "--input-format") == "stream-json", "no settings/hooks/plugins, skills, persistence"),
            ("child-env-guard", rec and rec["env"]["SHUNT_CHILD"] == "1" and rec["env"]["CLAUDE_EFFORT"] is None, "SHUNT_CHILD=1 set; parent CLAUDE_EFFORT stripped"),
            ("child-empty-cwd", rec and rec["cwd"] != os.getcwd() and "shunt-" in rec["cwd"], "child runs in an empty temp dir"),
        ]
        for name, ok, detail in checks:
            report(bool(ok), name, detail)

        log = [e for e in read_log() if e.get("event") == "delegate"]
        last = log[-1] if log else {}
        report(bool(last) and last.get("ok") is True and last.get("child", {}).get("cost_usd") == 0.001
               and last.get("child", {}).get("cache_write") == 3000 and last.get("tokens", 0) > 1000
               and last.get("lines") == 800 and last.get("verify") == {"cited": 1, "ok": 0, "unchecked": 0, "flagged": 1}
               and last.get("paths") == [os.path.realpath(big)],
               "logs-delegation", "logged: child usage and cost, tokens kept out, lines, citation checks"
               if last else "no delegate event logged")

        p, rec, _ = call({"STUB_MODE": "cited"})
        report(p.returncode == 0 and p.stdout.rstrip().endswith("[shunt-verify: 2 of 2 citations match the files]"),
               "verify-passes-good-citations", "an exact quote at its line and a range in the file pass"
               if p.returncode == 0 else p.stderr[-200:])

        p, rec, _ = call({"STUB_MODE": "prose"})
        report(p.returncode == 0 and "[shunt-verify: 2 of 4 citations match the files, 1 had no quote to check;" in p.stdout
               and 'big.txt:30: "SECRET_LINE_99" is at line 99' in p.stdout and "qty" not in p.stdout.split("[shunt-verify")[1],
               "verify-tolerates-prose-code", "only the quote right after a citation is checked, an indented line can carry it, a wrong one is flagged, an unquoted one isn't counted as a match"
               if p.returncode == 0 else p.stderr[-200:])

        p, rec, _ = call({"STUB_MODE": "fail"})
        last = ([e for e in read_log() if e.get("event") == "delegate"] or [{}])[-1]
        report(last.get("ok") is False and "Not logged in" in last.get("error", ""), "logs-failure",
               "a failed call is logged with its error, so the report can count it")

        p, rec, calls = call({"STUB_MODE": "ttl-error"})
        ttls = [blocks(c)[0].get("cache_control", {}).get("ttl") for c in calls]
        report(p.returncode == 0 and ttls == ["1h", "5m"], "cache-ttl-fallback",
               "1h breakpoint rejected -> retried with 5m" if p.returncode == 0 else "ttls={} exit={}".format(ttls, p.returncode))

        p, rec, _ = call({"SHUNT_CACHE_TTL": "off"})
        report(p.returncode == 0 and "cache_control" not in blocks(rec)[0], "cache-off", "SHUNT_CACHE_TTL=off sends no breakpoint")

        p, rec, _ = call({"SHUNT_MODEL": "sonnet", "SHUNT_EFFORT": "low"})
        report(rec is not None and flag(rec["argv"], "--model") == "sonnet" and flag(rec["argv"], "--effort") == "low",
               "model-effort-configurable", "SHUNT_MODEL / SHUNT_EFFORT honored")

        failures = [
            ("missing-file", {}, ["--question", "q", "--paths", big, os.path.join(tmp, "nope.txt")], 1, "not found", False),
            ("no-question", {}, ["--paths", big], 2, "--question", False),
            ("empty-answer", {"STUB_MODE": "empty"}, None, 1, "empty answer", True),
            ("nonzero-exit", {"STUB_MODE": "fail"}, None, 1, "Not logged in", True),
            ("is-error-result", {"STUB_MODE": "error"}, None, 1, "overloaded", True),
            ("unparseable-output", {"STUB_MODE": "garbage"}, None, 1, "empty answer", True),
            ("bad-cache-ttl", {"SHUNT_CACHE_TTL": "1d"}, None, 1, "SHUNT_CACHE_TTL", False),
            ("timeout", {"STUB_MODE": "sleep", "SHUNT_TIMEOUT": "1"}, None, 124, "timed out", True),
            ("recursion-guard", {"SHUNT_CHILD": "1"}, None, 1, "SHUNT_CHILD", False),
        ]
        for name, env, args, code, needle, should_call in failures:
            p, rec, _ = call(env, args)
            ok = p.returncode == code and needle in p.stderr and not p.stdout.strip() and (rec is not None) == should_call
            report(ok, name, "fails loudly: exit {}, '{}'".format(code, needle) if ok
                   else "exit={} stderr={!r}".format(p.returncode, p.stderr[-200:]))
    finally:
        shutil.rmtree(tmp)


def run_live_suite():
    """Real Haiku calls on the generated fixture: a first question, then a
    follow-up that must read the files from the prompt cache."""
    print("\nLive (scripts/bulk-read, real claude -p)\n" + "─" * 72)
    tmp = tempfile.mkdtemp(prefix="shunt-live-")
    try:
        fixture = os.path.join(tmp, "inventory_service.py")
        subprocess.run([sys.executable, os.path.join(EVALS, "fixtures", "make_inventory.py"), fixture], check=True)
        env = hook_env({})
        questions = [
            ("live-bug-hunt", "Which method lets a reservation be released twice, and on what line?", ["release", "37[6-9]"]),
            ("live-cached-follow-up", "What is RETRY_LIMIT set to, and which method uses it?", ["3", "call_supplier"]),
        ]
        for name, question, needles in questions:
            p = subprocess.run([BULK_READ, "--question", question, "--paths", fixture],
                               capture_output=True, text=True, env=env)
            missing = [n for n in needles if not re.search(n, p.stdout)]
            ok = p.returncode == 0 and not missing
            if name == "live-cached-follow-up" and "cache hit" not in p.stderr:
                ok, missing = False, missing + ["cache hit"]
            report(ok, name, p.stderr.strip().splitlines()[-1][:110] if ok and p.stderr.strip()
                   else "exit={} missing={} {}".format(p.returncode, missing, p.stderr.strip()[-200:]))
    finally:
        shutil.rmtree(tmp)


def run_session_start_suite():
    """SessionStart: list the largest tracked files over the threshold."""
    print("\nSession start (shunt-hook session-start)\n" + "─" * 72)
    tmp = tempfile.mkdtemp(prefix="shunt-start-")
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "src"))
        for rel, n in (("src/big.py", 3000), ("src/mid.py", 700), ("small.py", 100), ("untracked.py", 5000)):
            open(os.path.join(repo, rel), "w").write("x = 1  # filler line\n" * n)
        git = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, check=True)
        git("init", "-q")
        git("add", "src", "small.py")

        def start(cwd, env=None):
            payload = {"hook_event_name": "SessionStart", "session_id": "eval-start", "cwd": cwd}
            p = subprocess.run([HOOK, "session-start"], input=json.dumps(payload), capture_output=True,
                               text=True, env=hook_env(env))
            if not p.stdout.strip():
                return p, None
            return p, json.loads(p.stdout).get("hookSpecificOutput", {}).get("additionalContext")

        p, ctx = start(repo)
        ok = ctx and "src/big.py (3,000 lines), src/mid.py (700 lines)" in ctx and "small.py" not in ctx \
            and "untracked" not in ctx and "bulk-reader" in ctx \
            and ctx.index("grep -n") < ctx.index("offset/limit") < ctx.index("bulk-reader")
        report(bool(ok), "lists-largest-tracked", "largest first, with line counts; small and untracked files left out; "
               "grep and narrow reads before bulk-reader"
               if ok else "context={!r} stderr={!r}".format(ctx, p.stderr[-200:]))
        p, ctx = start(repo, {"SHUNT_LIST_LARGEST": "1"})
        report(bool(ctx) and "src/big.py" in ctx and "mid.py" not in ctx, "list-limit", "SHUNT_LIST_LARGEST caps the list")
        for name, cwd, env, detail in (
                ("list-off", repo, {"SHUNT_LIST_LARGEST": "0"}, "SHUNT_LIST_LARGEST=0: nothing added"),
                ("list-disabled", repo, {"SHUNT_DISABLE": "1"}, "SHUNT_DISABLE=1: nothing added"),
                ("list-not-git", tmp, None, "outside a git repo: nothing added"),
                ("list-none-large", repo, {"SHUNT_MIN_LINES": "5000"}, "no tracked file over the threshold: nothing added")):
            p, ctx = start(cwd, env)
            report(p.returncode == 0 and ctx is None and not p.stdout.strip(), name, detail)
    finally:
        shutil.rmtree(tmp)


def run_ledger_suite():
    """The usage log, shunt-toggle and shunt-report, with no model calls."""
    print("\nUsage log, toggle and report\n" + "─" * 72)
    tmp = tempfile.mkdtemp(prefix="shunt-ledger-")
    try:
        state = os.path.join(tmp, "state")
        big = os.path.join(tmp, "big.py")
        subprocess.run([sys.executable, os.path.join(EVALS, "fixtures", "make_inventory.py"), big], check=True)
        env = hook_env({"SHUNT_STATE_DIR": state, "TMPDIR": tmp})

        def hook(tool_input, session="eval-ledger"):
            payload = {"hook_event_name": "PreToolUse", "tool_name": "Read", "session_id": session,
                       "cwd": tmp, "tool_input": tool_input}
            return subprocess.run([HOOK, "read"], input=json.dumps(payload), capture_output=True,
                                  text=True, env=env)

        hook({"file_path": big})
        hook({"file_path": big, "offset": 370, "limit": 20})
        hook({"file_path": big, "offset": 375, "limit": 20})  # 5 new lines
        events = read_log(state)
        deny = [e for e in events if e["event"] == "deny"]
        reads = [e for e in events if e["event"] == "read"]
        report(len(deny) == 1 and deny[0]["kind"] == "whole" and deny[0]["outline"] and deny[0]["tokens"] > 100,
               "logs-deny", "a deny is logged with its kind and the tokens its message costs")
        report([r["lines"] for r in reads] == [20, 5] and all(r["tokens"] > 0 for r in reads),
               "logs-new-lines-only", "bounded reads of a large file log only lines not read before")

        tog = lambda action, session="eval-ledger": subprocess.run(
            [TOGGLE, action], capture_output=True, text=True,
            env=dict(env, CLAUDE_CODE_SESSION_ID=session) if session else env)
        p = tog("off")
        denied_after = hook({"file_path": big}).stdout.strip()
        report("off for this session" in p.stdout and not denied_after, "toggle-off",
               "shunt-toggle off: this session's reads pass")
        p = tog("on")
        report("on for this session" in p.stdout and hook({"file_path": big}).stdout.strip() != "", "toggle-on",
               "shunt-toggle on: denied again")
        p = tog("off", session=None)
        report(p.returncode == 1 and "SHUNT_DISABLE" in p.stderr, "toggle-needs-session",
               "no session id: fails and points at SHUNT_DISABLE")

        # A synthetic log: two delegations in one session, a follow-up read,
        # a direct read elsewhere, an old event outside the period.
        now = time.time()
        rows = [
            {"ts": now - 40 * 86400, "event": "delegate", "ok": True, "session": "old", "paths": ["/x"],
             "tokens": 99999, "answer_tokens": 1, "child": {"cost_usd": 1.0}},
            {"ts": now - 3600, "event": "list", "session": "s1", "tokens": 100},
            {"ts": now - 3500, "event": "deny", "session": "s1", "path": "/a.py", "kind": "whole", "tokens": 500},
            {"ts": now - 3400, "event": "delegate", "ok": True, "session": "s1", "cwd": "/proj", "paths": ["/a.py"],
             "tokens": 20000, "answer_tokens": 400, "cache_hit": False,
             "child": {"input": 2, "cache_write": 20000, "cache_read": 0, "output": 500, "cost_usd": 0.004},
             "verify": {"cited": 5, "ok": 3, "unchecked": 1, "flagged": 1}},
            {"ts": now - 3300, "event": "read", "session": "s1", "path": "/a.py", "lines": 30, "tokens": 300},
            {"ts": now - 3200, "event": "delegate", "ok": True, "session": "s1", "cwd": "/proj", "paths": ["/a.py"],
             "tokens": 20000, "answer_tokens": 200, "cache_hit": True,
             "child": {"input": 2, "cache_write": 0, "cache_read": 20000, "output": 300, "cost_usd": 0.001},
             "verify": {"cited": 3, "ok": 3, "unchecked": 0, "flagged": 0}},
            {"ts": now - 3100, "event": "delegate", "ok": False, "session": "s1", "paths": ["/a.py"],
             "error": "timeout", "child": {"cost_usd": 0.0}},
            {"ts": now - 3000, "event": "deny", "session": "s2", "path": "/b.py", "kind": "budget", "tokens": 400},
            {"ts": now - 2900, "event": "read", "session": "s2", "path": "/b.py", "lines": 40, "tokens": 999},
        ]
        rstate = os.path.join(tmp, "report-state")
        os.makedirs(rstate)
        with open(os.path.join(rstate, "usage.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        renv = hook_env({"SHUNT_STATE_DIR": rstate})
        p = subprocess.run([REPORT, "--since", "7d", "--json", "--main-model", "sonnet"], capture_output=True,
                           text=True, env=renv)
        try:
            g = json.loads(p.stdout)["groups"]["all"]
        except (ValueError, KeyError):
            g = {}
        want = {"calls": 2, "failed": 1, "kept_out": 40000, "answers": 600, "deny_tokens": 900,
                "list_tokens": 100, "followup_tokens": 300, "brought_back": 1900, "net": 38100,
                "cache_hits": 1, "cited": 8, "cited_ok": 6, "cited_unchecked": 1, "denied_files": 2, "denied_without_haiku": 1}
        wrong = {k: g.get(k) for k, v in want.items() if g.get(k) != v}
        money = abs(g.get("haiku_usd", 0) - 0.005) < 1e-9 and abs(g.get("saved_usd", 0) - (38100 * 2.0 / 1e6 - 0.005)) < 1e-9
        report(not wrong and money, "report-totals", "period filter, net tokens, follow-up reads, Haiku cost, saving"
               if not wrong and money else "wrong: {} money_ok={} stderr={}".format(wrong, money, p.stderr[-200:]))
        p = subprocess.run([REPORT, "--since", "7d", "--by", "session"], capture_output=True, text=True, env=renv)
        report(p.returncode == 0 and "session: s1" in p.stdout and "session: s2" in p.stdout and "Est. net saving" in p.stdout,
               "report-by-session", "--by session prints a block per session after the total")
        p = subprocess.run([REPORT, "--since", "2026-13-01"], capture_output=True, text=True, env=renv)
        report(p.returncode != 0 and "YYYY-MM-DD" in p.stderr, "report-bad-date", "an unreadable date fails with a hint")
        p = subprocess.run([REPORT], capture_output=True, text=True, env=hook_env({"SHUNT_STATE_DIR": os.path.join(tmp, "none")}))
        report(p.returncode == 0 and "No shunt activity" in p.stdout, "report-empty", "no log yet: says so")
    finally:
        shutil.rmtree(tmp)


def run_verify_suite():
    """lib/verify.py on hand-written answers: what is checked and what isn't."""
    print("\nCitation check (lib/verify.py)\n" + "\u2500" * 72)
    sys.path.insert(0, os.path.join(ROOT, "lib"))
    import verify
    tmp = tempfile.mkdtemp(prefix="shunt-verify-")
    try:
        src = os.path.join(tmp, "f.py")
        with open(src, "w") as f:
            f.write("class Ledger:\n"
                    "    def adjust(self, sku, delta):\n"
                    "        level = self.levels.get(sku, 0) + delta\n"
                    "        self.levels[sku] = level\n"
                    "        return level\n"
                    "\n"
                    "    def release(self, order):\n"
                    "        qty = self.holds.pop(order)\n"
                    "        self.adjust(order, qty)\n")
        q3 = "`level = self.levels.get(sku, 0) + delta`"
        cases = [
            ("quote-after-citation", "- f.py:3 " + q3, (1, 1, 0), None),
            ("prose-code-unchecked", "- f.py:3 " + q3 + "; nothing checks `level < 0` or `KeyError`", (1, 1, 0), None),
            ("wrong-line-flagged", "- f.py:8 `return level`", (1, 0, 0), 'f.py:8: "return level" is at line 5'),
            ("invented-quote-flagged", "- f.py:3 `level = max(0, delta)`", (1, 0, 0), "is not in the file"),
            ("quote-on-indented-line", "- f.py:9 the call\n  - `self.adjust(order, qty)`", (1, 1, 0), None),
            ("indented-prose-unchecked", "- f.py:9 the call\n  - nothing matches `level < 0`", (1, 0, 1), None),
            ("quote-in-fence", "- f.py:8\n```\nqty = self.holds.pop(order)\n```", (1, 1, 0), None),
            ("label-outside-flagged", "- `Ledger.release` at f.py:3 " + q3, (1, 0, 0), "line 3 isn't inside release"),
            ("label-inside-passes", "- f.py:8 (`Ledger.release`) `qty = self.holds.pop(order)`", (1, 1, 0), None),
            ("label-in-prose-ignored", "- set at `f.py:3` and never read. `Ledger.release` instead pops it (`f.py:8`)",
             (2, 0, 2), "2 had no quote to check"),
            ("foreign-name-ignored", "- f.py:3 (`os.path.join`) " + q3 + ", unlike `self.adjust`", (1, 1, 0), None),
            ("bare-line-checked", "- f.py:3 " + q3 + ", returned at line 9 `return level`", (2, 1, 0), 'f.py:9: "return level" is at line 5'),
            ("bare-line-needs-adjacent-quote", "- f.py:3 " + q3 + " (line 5) is never `negative`", (1, 1, 0), None),
            ("elided-quote", "- f.py:3 `level = self.levels.get(...) + delta`", (1, 1, 0), None),
            ("elided-quote-wrong-flagged", "- f.py:3 `level = self.holds.get(...) + delta`", (1, 0, 0), "is not in the file"),
            ("quote-far-from-citation-unchecked", "- f.py:3 and f.py:9 compute `level` and call `self.adjust(order, qty)`",
             (2, 0, 2), "2 had no quote to check"),
            ("quote-past-parenthesis-unchecked", "- the north audit (`f.py:8`), `Ledger.adjust` and `return level`", (1, 0, 1), None),
            ("number-not-a-quote", "- f.py:3, `135` and `level = 1`", (1, 0, 1), None),
            ("instance-call-not-a-label", "- calls `self.adjust` (f.py:9 `self.adjust(order, qty)`)", (1, 1, 0), None),
            ("enclosing-name-passes", "- f.py:8 `release`, and f.py:3 in `adjust`", (2, 2, 0), None),
            ("enclosing-name-wrong-flagged", "- f.py:3 `release`", (1, 0, 0), "line 3 isn't inside release"),
            ("unquoted-not-a-match", "- f.py:5 returns it", (1, 0, 1), "1 had no quote to check"),
            ("unsent-file-flagged", "- other.py:3 `return level`", (1, 0, 0), "names a file that wasn't sent"),
            ("past-end-flagged", "- f.py:40 `return level`", (1, 0, 0), "past the end of the file"),
        ]
        for name, answer, (cited, ok, unchecked), needle in cases:
            summary, footer = verify.check(answer, [src])
            good = (summary["cited"], summary["ok"], summary["unchecked"]) == (cited, ok, unchecked) and \
                (needle is None or needle in footer)
            report(good, name, "{} cited, {} ok, {} unchecked".format(cited, ok, unchecked)
                   if good else "{} {}".format(summary, footer))
    finally:
        shutil.rmtree(tmp)


def main():
    run_hook_suite("hook-evals.json", "Read hook (shunt-hook read)")
    run_hook_suite("bash-hook-evals.json", "Bash hook (shunt-hook bash)")
    run_transport_suite()
    run_verify_suite()
    run_session_start_suite()
    run_ledger_suite()
    if LIVE:
        run_live_suite()
    shutil.rmtree(STATE, ignore_errors=True)
    print("\n" + "═" * 72)
    print("Total: {}{} passed{}, {}{} failed{}, {} skipped".format(
        GREEN, results["pass"], RESET, RED, results["fail"], RESET, results["skip"]))
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
