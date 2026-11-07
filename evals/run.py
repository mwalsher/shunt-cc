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

EVALS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVALS)
HOOK = os.path.join(ROOT, "hooks", "shunt-hook")
BULK_READ = os.path.join(ROOT, "scripts", "bulk-read")
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
        if opts.get("binary"):
            open(path, "wb").write(b"\x89PNG\r\n\x1a\n\0\0" + b"\n" * 5000)
            continue
        body = "".join("line {}\n".format(n) for n in range(1, opts["lines"] + 1))
        if not opts.get("trailing_newline", True):
            body = body.rstrip("\n")
        open(path, "w").write(body)


def hook_env(extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHUNT_")}
    env.update(extra or {})
    return env


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
result = {"ok": "- `parse()` at big.txt:12", "empty": "  ", "error": "API Error: overloaded",
          "ttl": "API Error: 400 messages.0.content.0.cache_control.ttl: bad order"}[mode]
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": mode in ("error", "ttl"),
                  "result": result, "usage": {"cache_read_input_tokens": 0}}))
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
            ("ok-exit-and-answer", p.returncode == 0 and p.stdout.strip() == "- `parse()` at big.txt:12", "answer printed on stdout"),
            ("ok-token-note-stderr", "input tokens" in p.stderr and "SECRET" not in p.stdout + p.stderr, "rough token note on stderr, no file content echoed"),
            ("contents-on-stdin", '<file path="{}">'.format(big) in files_block.get("text", "") and "1\tSECRET_LINE_1\n" in files_block.get("text", "") and "Where is parse?" in question_block.get("text", ""), "files in <file> tags, numbered, on stdin; question in its own block"),
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


def main():
    run_hook_suite("hook-evals.json", "Read hook (shunt-hook read)")
    run_hook_suite("bash-hook-evals.json", "Bash hook (shunt-hook bash)")
    run_transport_suite()
    if LIVE:
        run_live_suite()
    print("\n" + "═" * 72)
    print("Total: {}{} passed{}, {}{} failed{}, {} skipped".format(
        GREEN, results["pass"], RESET, RED, results["fail"], RESET, results["skip"]))
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
