#!/usr/bin/env python3
"""A/B eval: real tasks with shunt-cc on and off, scored on outcomes and cost.

Usage:
  python3 evals/ab/run.py [--tasks overview,debug] [--runs 3] [--arms on,off,delegate]
                          [--model claude-sonnet-5-5] [--max-turns 30] [-j 2]
                          [--max-cost-usd 10] [--out DIR] [--dry-run]

Each run is a headless `claude -p` session in a fresh copy of the workspace
(workspace.py): the `on` arm loads this plugin with --plugin-dir, the `off`
arm loads nothing. The `delegate` arm (not run by default) loads the plugin
and tells the main model to start with bulk-read, so Haiku's answers are
tested on every task rather than only when the model chooses to delegate.
Settings, hooks and MCP servers from your own setup are left out of every arm
(--setting-sources ""), so the plugin is the only difference. Runs use your Claude Code login and cost plan usage or API spend:
about $0.05-0.30 a run on Sonnet. --dry-run prints the plan and exits.

Per run it records: task success (a deterministic check), turns, main-session
cost and tokens, the largest context the main model saw, tool output that
entered the context, Edits that failed, bulk-read calls and Haiku's cost,
hook denies, and flagged citations. The summary compares the arms per task;
the "risky" tasks are where delegation is expected to hurt, so a drop there
means the "Haiku locates, you judge" guidance isn't holding. The harder ones
(rootcause, security-precise, negative-stock, sign-audit) grade an exact
answer or run hidden tests, so a wrong lead taken on trust fails.
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import workspace  # noqa: E402

CLASSES = ["WarehouseRegistry", "StockLedger", "ReservationManager", "ReplenishmentPlanner",
           "SupplierGateway", "BillingEngine", "CaseOrders", "AuditTrail"]


def line_of(ws, path, needle):
    for n, line in enumerate(open(os.path.join(ws, path)), 1):
        if needle in line:
            return n
    return None


def git_diff_files(ws):
    out = subprocess.run(["git", "-C", ws, "diff", "--name-only"], capture_output=True, text=True).stdout
    return sorted(out.split())


# ── Tasks: (prompt, kind, check(ws, answer) -> (passed, detail)) ─────────────

def check_overview(ws, answer):
    """Each class is named, with its start line (within 2) on the same line of
    the answer: a bullet or a table row, number before or after the name."""
    missing, off = [], []
    for cls in CLASSES:
        want = line_of(ws, "inventory_service.py", "class {}:".format(cls))
        rows = [l for l in answer.splitlines() if re.search(r"\b{}\b".format(cls), l)]
        if not rows:
            missing.append(cls)
        elif not any(abs(int(n) - want) <= 2 for l in rows for n in re.findall(r"\b\d{1,4}\b", l)):
            off.append("{}!={}".format(cls, want))
    return not missing and not off, "missing={} off={}".format(missing, off)


def check_locate(ws, answer):
    ok = "call_supplier" in answer and re.search(r"\b3\b", answer) and \
        re.search(r"2\s*\*\*\s*attempt|exponential|doubl|1,? 2,? (and )?4", answer, re.I)
    return bool(ok), "needs call_supplier, 3, and exponential backoff"


def check_edit(ws, answer):
    text = open(os.path.join(ws, "inventory_service.py")).read()
    diff = subprocess.run(["git", "-C", ws, "diff", "-U0"], capture_output=True, text=True).stdout
    changed = [l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    ok = re.search(r"^LATE_FEE_RATE = 0\.02\s*$", text, re.M) and len(changed) == 2
    return bool(ok), "changed lines: {}".format(changed[:4])


def check_debug(ws, answer):
    p = subprocess.run([sys.executable, "-m", "unittest", "test_inventory"], cwd=ws,
                       capture_output=True, text=True)
    files = git_diff_files(ws)
    ok = p.returncode == 0 and files == ["inventory_service.py"]
    return ok, "tests exit {}; changed {}".format(p.returncode, files)


def check_review(ws, answer):
    release = re.search(r"release[\s\S]{0,400}?(twice|double|idempot|again|more than once|second|open)", answer, re.I)
    force = re.search(r"force[\s\S]{0,300}?negative|negative[\s\S]{0,300}?force", answer, re.I)
    return bool(release), "double release {}, force/negative {}".format(bool(release), bool(force))


def check_security(ws, answer):
    ok = "export_archive" in answer and re.search(r"inject|os\.system|shell", answer, re.I)
    return bool(ok), "needs export_archive and command injection"


def check_crossfile(ws, answer):
    line = line_of(ws, "order_api.py", "def {}(".format(workspace.SWAPPED_HANDLER))
    call = line_of(ws, "order_api.py", "reservations.hold(ledger, sku, order_id, qty)")
    ok = workspace.SWAPPED_HANDLER in answer or re.search(r"\b({}|{})\b".format(line, call), answer)
    return bool(ok), "needs {} (line {}/{})".format(workspace.SWAPPED_HANDLER, line, call)


def final_names(answer, label):
    """The names on the answer's last `LABEL: a, b` line, or None if there's none."""
    rows = [l for l in answer.splitlines() if re.match(r"^[\s*`>-]*{}\s*:".format(label), l)]
    if not rows:
        return None
    names = rows[-1].split(":", 1)[1]
    names = [re.sub(r"[`*()\s]", "", n).split(".")[-1] for n in names.split(",")]
    return {n for n in names if n and n.lower() != "none"}


def check_exact(label, want):
    def check(ws, answer):
        got = final_names(answer, label)
        if got is None:
            return False, "no {}: line".format(label)
        return got == want, "missing {} extra {}".format(sorted(want - got), sorted(got - want))
    return check


def check_rootcause(ws, answer):
    """The visible tests pass, and so do hidden ones that a fix of the symptom
    (converting in cancel) fails: the cause is that a booking stores cases."""
    files = git_diff_files(ws)
    with open(os.path.join(ws, "test_cases_hidden.py"), "w") as f:
        f.write(workspace.HIDDEN_TESTS)
    p = subprocess.run([sys.executable, "-m", "unittest", "test_cases", "test_cases_hidden"], cwd=ws,
                       capture_output=True, text=True)
    last = (p.stderr.strip().splitlines() or [""])[-1]
    return p.returncode == 0 and files == ["inventory_service.py"], "tests: {}; changed {}".format(last, files)


FINAL = ("Finish with one line exactly `{}: <comma-separated function names>`, or `{}: none`.")

TASKS = {
    "overview": ("Give me an overview of inventory_service.py: its main classes, what each one is "
                 "responsible for, and the line where each starts.", "helps", check_overview),
    "locate": ("In inventory_service.py, how many attempts does a supplier call get, which method "
               "does that, and how long does it wait between attempts?", "helps", check_locate),
    "edit": ("In inventory_service.py, change LATE_FEE_RATE to 0.02.", "neutral", check_edit),
    "debug": ("`python3 -m unittest test_inventory` fails. Find the cause in inventory_service.py "
              "and fix it with a minimal change. Don't edit the tests.", "risky", check_debug),
    "review": ("Review inventory_service.py for bugs in how stock levels change. List each bug "
               "with the method and line.", "risky", check_review),
    "security": ("Do a security review of inventory_service.py. List each issue with the method "
                 "and line.", "risky", check_security),
    "crossfile": ("Check that every call order_api.py makes into inventory_service.py passes its "
                  "arguments in the right order. List each wrong call with its function and line.",
                  "risky", check_crossfile),
    # Harder: each has a trap that taking Haiku's word, or patching where the
    # symptom shows, falls into; and an exact answer, so a wrong extra fails.
    "rootcause": ("`python3 -m unittest test_cases` fails. Find the root cause in inventory_service.py "
                  "and fix it there, so the rest of the class stays consistent with the fix. Don't "
                  "edit the tests.", "risky", check_rootcause),
    "security-precise": ("Do a security review of the AuditTrail class in inventory_service.py. Assume "
                         "an attacker controls every method argument. " + FINAL.format("VULNERABLE", "VULNERABLE")
                         + " Name only the methods that attacker can exploit.", "risky",
                         check_exact("VULNERABLE", workspace.VULNERABLE)),
    "negative-stock": ("Which functions in order_api.py can leave a sku's on-hand stock below zero? Check "
                       "what each call does in inventory_service.py. " + FINAL.format("ANSWER", "ANSWER"),
                       "risky", check_exact("ANSWER", workspace.CAN_GO_NEGATIVE)),
    "sign-audit": ("In order_api.py, a restock handler should add stock and a writeoff handler should "
                   "remove it. Find every handler that does the opposite. " + FINAL.format("ANSWER", "ANSWER"),
                   "recall", check_exact("ANSWER", set(workspace.FLIPPED))),
}


# ── Running one session ──────────────────────────────────────────────────────

def claude_bin():
    for c in (os.environ.get("SHUNT_CLAUDE_BIN"), shutil.which("claude"), os.environ.get("CLAUDE_CODE_EXECPATH")):
        if c and os.access(c, os.X_OK):
            return c
    raise SystemExit("claude CLI not found; set SHUNT_CLAUDE_BIN")


# Appended to the delegate arm's system prompt. It forces the delegation but
# leaves the judging to the skill's guidance, which is what's under test.
DELEGATE = ("Before anything else, ask about the files this task concerns with the bulk-reader "
            "skill's bulk-read script, and work from its answer. Follow the skill's guidance on "
            "checking that answer.")
ARMS = ("on", "off", "delegate")


def command(arm, prompt, args):
    tools = ["Read", "Edit", "Write", "Grep", "Glob",
             "Bash(python3 -m unittest:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
             "Bash(grep:*)", "Bash(sed -n:*)", "Bash(wc:*)", "Bash(git diff:*)",
             "Bash({}/scripts/bulk-read:*)".format(ROOT), "Bash(*/scripts/bulk-read:*)"]
    cmd = [claude_bin(), "-p", prompt, "--model", args.model, "--max-turns", str(args.max_turns),
           "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--allowedTools"] + tools
    if arm in ("on", "delegate"):
        cmd += ["--plugin-dir", ROOT]
    if arm == "delegate":
        cmd += ["--append-system-prompt", DELEGATE]
    return cmd


def parse_stream(stdout):
    """Metrics from a stream-json transcript."""
    m = {"tool_calls": {}, "failed_edits": 0, "denies": 0, "bulk_reads": 0, "tool_output_tokens": 0,
         "peak_context": 0, "answer": "", "turns": 0, "main_usd": 0.0, "main_tokens": 0, "error": None}
    names = {}
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind = e.get("type")
        msg = e.get("message") or {}
        if kind == "assistant":
            u = msg.get("usage") or {}
            ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
            m["peak_context"] = max(m["peak_context"], ctx)
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    names[block.get("id")] = block.get("name")
                    m["tool_calls"][block["name"]] = m["tool_calls"].get(block["name"], 0) + 1
                    if block["name"] == "Bash" and "bulk-read" in (block.get("input") or {}).get("command", ""):
                        m["bulk_reads"] += 1
        elif kind == "user":
            for block in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict))
                m["tool_output_tokens"] += len(text) // 4
                if block.get("is_error") and "shunt:" in text:
                    m["denies"] += 1
                if block.get("is_error") and names.get(block.get("tool_use_id")) in ("Edit", "Write"):
                    m["failed_edits"] += 1
        elif kind == "result":
            u = e.get("usage") or {}
            m["turns"] = e.get("num_turns", 0)
            m["main_usd"] = float(e.get("total_cost_usd") or 0)
            m["main_tokens"] = sum(int(u.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
            m["answer"] = e.get("result") or ""
            if e.get("is_error") or e.get("subtype") != "success":
                m["error"] = e.get("subtype")
    return m


def run_one(task, arm, i, args, out_dir):
    prompt, kind, check = TASKS[task]
    run_dir = tempfile.mkdtemp(prefix="shunt-ab-")
    try:
        ws = os.path.join(run_dir, "ws")
        workspace.main(ws)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SHUNT_") and k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE")}
        env.update(SHUNT_STATE_DIR=os.path.join(run_dir, "state"), TMPDIR=run_dir)
        started = time.time()
        try:
            p = subprocess.run(command(arm, prompt, args), cwd=ws, env=env, capture_output=True,
                               text=True, timeout=args.timeout)
            stdout = p.stdout
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        m = parse_stream(stdout)
        m["seconds"] = round(time.time() - started, 1)
        passed, detail = check(ws, m["answer"])
        child_usd, flagged, cited, unchecked = 0.0, 0, 0, 0
        try:
            for line in open(os.path.join(run_dir, "state", "usage.jsonl")):
                e = json.loads(line)
                if e.get("event") == "delegate":
                    child_usd += (e.get("child") or {}).get("cost_usd", 0)
                    cited += (e.get("verify") or {}).get("cited", 0)
                    flagged += (e.get("verify") or {}).get("flagged", 0)
                    unchecked += (e.get("verify") or {}).get("unchecked", 0)
        except OSError:
            pass
        record = dict(m, task=task, kind=kind, arm=arm, run=i, passed=bool(passed), detail=detail,
                      haiku_usd=round(child_usd, 6), total_usd=round(m["main_usd"] + child_usd, 6),
                      cited=cited, flagged=flagged, unchecked=unchecked)
        with open(os.path.join(out_dir, "{}-{}-{}.jsonl".format(task, arm, i)), "w") as f:
            f.write(stdout)
        return record
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ── Summary ──────────────────────────────────────────────────────────────────

COLUMNS = [("passed", "pass", "{:.0%}"), ("turns", "turns", "{:.1f}"), ("total_usd", "cost $", "{:.3f}"),
           ("haiku_usd", "haiku $", "{:.4f}"), ("main_tokens", "main tok", "{:,.0f}"),
           ("peak_context", "peak ctx", "{:,.0f}"), ("tool_output_tokens", "tool out", "{:,.0f}"),
           ("failed_edits", "bad edits", "{:.1f}"), ("bulk_reads", "bulk-read", "{:.1f}"),
           ("denies", "denies", "{:.1f}"), ("flagged", "flagged", "{:.1f}"), ("seconds", "secs", "{:.0f}")]


def mean(rows, key):
    vals = [float(r[key]) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def summary(records):
    lines = ["| task | kind | arm | n | " + " | ".join(c[1] for c in COLUMNS) + " |",
             "|" + "---|" * (4 + len(COLUMNS))]
    for task in TASKS:
        for arm in ARMS:
            rows = [r for r in records if r["task"] == task and r["arm"] == arm]
            if not rows:
                continue
            cells = [fmt.format(mean(rows, key)) for key, _, fmt in COLUMNS]
            lines.append("| {} | {} | {} | {} | {} |".format(task, rows[0]["kind"], arm, len(rows), " | ".join(cells)))
    lines.append("")
    for arm in ARMS:
        rows = [r for r in records if r["arm"] == arm]
        if rows:
            lines.append("Totals {}: pass {:.0%}, ${:.3f} a run, peak context {:,.0f}, {:.1f} turns, "
                         "{:.1f} bulk-reads.".format(arm, mean(rows, "passed"), mean(rows, "total_usd"),
                                                     mean(rows, "peak_context"), mean(rows, "turns"),
                                                     mean(rows, "bulk_reads")))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--arms", default="on,off")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("-j", "--concurrency", type=int, default=2)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--out")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = [t for t in args.tasks.split(",") if t]
    unknown = [t for t in tasks if t not in TASKS]
    if unknown:
        raise SystemExit("unknown tasks {}; choose from {}".format(unknown, ", ".join(TASKS)))
    arms = [a for a in args.arms.split(",") if a]
    if set(arms) - set(ARMS):
        raise SystemExit("unknown arms {}; choose from {}".format(sorted(set(arms) - set(ARMS)), ", ".join(ARMS)))
    plan = [(t, a, i) for i in range(args.runs) for t in tasks for a in arms]
    print("{} runs: tasks {} x arms {} x {} runs, model {}".format(
        len(plan), ",".join(tasks), ",".join(arms), args.runs, args.model))
    if args.dry_run:
        return
    out_dir = args.out or os.path.join(ROOT, "evals", "results", time.strftime("ab-%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    records, spent, lock = [], [0.0], threading.Lock()

    def job(item):
        with lock:
            if args.max_cost_usd is not None and spent[0] >= args.max_cost_usd:
                return None
        r = run_one(*item, args=args, out_dir=out_dir)
        with lock:
            spent[0] += r["total_usd"]
            records.append(r)
            print("  {:<16} {:<8} #{}  {}  {:>2} turns  ${:.3f}  {}".format(
                r["task"], r["arm"], r["run"], "PASS" if r["passed"] else "FAIL", r["turns"],
                r["total_usd"], r["error"] or r["detail"][:70]), flush=True)
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        list(pool.map(job, plan))
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump([{k: v for k, v in r.items() if k != "answer"} for r in records], f, indent=1)
    text = summary(records)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(text + "\n")
    print("\n" + text + "\n\nSpent ${:.2f}. Transcripts and results: {}".format(spent[0], out_dir))


if __name__ == "__main__":
    main()
