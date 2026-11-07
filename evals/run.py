#!/usr/bin/env python3
"""Shunt eval runner: hook routing decisions + bulk-read transport (stubbed claude).

Usage:
  python3 evals/run.py            # all suites
  python3 evals/run.py -v         # also print deny reasons
No network or Claude login needed: the transport suite uses a stub claude.
"""
import json
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
        for ev in spec["evals"]:
            name = ev["name"]
            if ev.get("via_rtk") and not shutil.which("rtk"):
                report(None, name, "rtk not installed")
                continue
            sub = lambda s: s.replace("{{FIXTURES}}", fixtures)
            if "raw_input" in ev:
                stdin = ev["raw_input"]
            else:
                payload = json.loads(sub(json.dumps(ev.get("input", {}))))
                payload.setdefault("hook_event_name", "PreToolUse")
                payload.setdefault("tool_name", "Read" if mode == "read" else "Bash")
                payload.setdefault("cwd", sub(ev.get("cwd", "")) or fixtures + "/subdir")
                if "command" in ev:
                    payload.setdefault("tool_input", {})["command"] = sub(ev["command"])
                if ev.get("via_rtk"):
                    payload = rtk_rewrite(payload)
                stdin = json.dumps(payload)
            proc = subprocess.run([HOOK, mode], input=stdin, capture_output=True,
                                  text=True, env=hook_env(ev.get("env")))
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
json.dump({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
           "env": {k: os.environ.get(k) for k in ("SHUNT_CHILD", "CLAUDE_EFFORT")}},
          open(rec, "w"))
mode = os.environ.get("STUB_MODE", "ok")
if mode == "sleep":
    time.sleep(10)
if mode == "fail":
    sys.stderr.write("Not logged in. Run claude /login\n"); sys.exit(1)
if mode == "garbage":
    print("not json"); sys.exit(0)
result = {"ok": "- `parse()` at big.txt:12", "empty": "  ", "error": "API Error: overloaded"}[mode]
print(json.dumps({"type": "result", "subtype": "success", "is_error": mode == "error", "result": result}))
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
            rec = json.load(open(record)) if os.path.exists(record) else None
            return p, rec

        def flag(argv, name):
            return argv[argv.index(name) + 1] if name in argv else None

        p, rec = call()
        argv = rec["argv"] if rec else []
        checks = [
            ("ok-exit-and-answer", p.returncode == 0 and p.stdout.strip() == "- `parse()` at big.txt:12", "answer printed on stdout"),
            ("ok-token-note-stderr", "input tokens" in p.stderr and "SECRET" not in p.stdout + p.stderr, "rough token note on stderr, no file content echoed"),
            ("contents-on-stdin", rec and '<file path="{}">'.format(big) in rec["stdin"] and "1\tSECRET_LINE_1\n" in rec["stdin"] and "Where is parse?" in rec["stdin"], "files wrapped in <file> tags, numbered, on stdin"),
            ("contents-not-in-argv", rec and not any("SECRET_LINE" in a for a in argv), "file content never in argv"),
            ("default-model-effort", flag(argv, "--model") == "claude-haiku-5-5" and flag(argv, "--effort") == "medium", "claude-haiku-5-5 at medium effort"),
            ("tools-and-mcp-off", flag(argv, "--tools") == "" and "--strict-mcp-config" in argv and flag(argv, "--mcp-config") == '{"mcpServers":{}}', "--tools '' and empty strict MCP config"),
            ("minimal-startup", flag(argv, "--setting-sources") == "" and "--disable-slash-commands" in argv and "--no-session-persistence" in argv and "-p" in argv, "no settings/hooks/plugins, skills, persistence"),
            ("child-env-guard", rec and rec["env"]["SHUNT_CHILD"] == "1" and rec["env"]["CLAUDE_EFFORT"] is None, "SHUNT_CHILD=1 set; parent CLAUDE_EFFORT stripped"),
            ("child-empty-cwd", rec and rec["cwd"] != os.getcwd() and "shunt-" in rec["cwd"], "child runs in an empty temp dir"),
        ]
        for name, ok, detail in checks:
            report(bool(ok), name, detail)

        p, rec = call({"SHUNT_MODEL": "sonnet", "SHUNT_EFFORT": "low"})
        report(rec is not None and flag(rec["argv"], "--model") == "sonnet" and flag(rec["argv"], "--effort") == "low",
               "model-effort-configurable", "SHUNT_MODEL / SHUNT_EFFORT honored")

        failures = [
            ("missing-file", {}, ["--question", "q", "--paths", big, os.path.join(tmp, "nope.txt")], 1, "not found", False),
            ("no-question", {}, ["--paths", big], 2, "--question", False),
            ("empty-answer", {"STUB_MODE": "empty"}, None, 1, "empty answer", True),
            ("nonzero-exit", {"STUB_MODE": "fail"}, None, 1, "Not logged in", True),
            ("is-error-result", {"STUB_MODE": "error"}, None, 1, "overloaded", True),
            ("unparseable-output", {"STUB_MODE": "garbage"}, None, 1, "empty answer", True),
            ("timeout", {"STUB_MODE": "sleep", "SHUNT_TIMEOUT": "1"}, None, 124, "timed out", True),
            ("recursion-guard", {"SHUNT_CHILD": "1"}, None, 1, "SHUNT_CHILD", False),
        ]
        for name, env, args, code, needle, should_call in failures:
            p, rec = call(env, args)
            ok = p.returncode == code and needle in p.stderr and not p.stdout.strip() and (rec is not None) == should_call
            report(ok, name, "fails loudly: exit {}, '{}'".format(code, needle) if ok
                   else "exit={} stderr={!r}".format(p.returncode, p.stderr[-200:]))
    finally:
        shutil.rmtree(tmp)


def main():
    run_hook_suite("hook-evals.json", "Read hook (shunt-hook read)")
    run_hook_suite("bash-hook-evals.json", "Bash hook (shunt-hook bash)")
    run_transport_suite()
    print("\n" + "═" * 72)
    print("Total: {}{} passed{}, {}{} failed{}, {} skipped".format(
        GREEN, results["pass"], RESET, RED, results["fail"], RESET, results["skip"]))
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
