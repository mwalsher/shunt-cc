# shunt-cc

Read routing for large files in Claude Code. Hooks stop the model from pulling
a large file into context whole and steer it to the lines it needs:

- **Deny with an outline.** A whole-file `Read` (or `cat`, `less`, `rtk read`)
  of a file over 600 lines is denied. The deny message carries a model-free
  outline of the file (symbols and line ranges), so the next step is a `grep -n`
  or a narrow `Read` with `offset`/`limit`.
- **Read budget.** Bounded reads of one large file draw on a per-session
  budget of distinct lines, so the file can't be paged into context in chunks.
  Re-reading lines is free.
- **Large files listed up front.** A SessionStart hook lists the repo's largest
  files, so the model picks a narrow route before a Read is denied.

In an A/B eval (11 tasks, 3 runs per arm, Sonnet 5.5, synthetic workspace)
these hooks cut cost per run by 21% (95% CI 17–26%) and peak context by 15%,
with every task still passing. The saving comes from routing: much less file
content entered the context. See [A/B](#ab-routing-on-off-and-forced-delegation-evalsab)
for the numbers and their limits.

The plugin also has an optional `bulk-reader` skill that asks Haiku, through
headless Claude Code (`claude -p`), a question about a whole large file or log
and returns only the answer. It's for surveying or summarizing a whole file or
skimming a log, not the default way to read or locate code; the A/B eval found
no saving from sending tasks through it. It uses your own Claude Code login, so
it runs on a Claude subscription with no API key, proxy or third-party service.

Inspired by Spotify's [shunt](https://github.com/spotify/portal-ai-plugins/tree/main/plugins/shunt)
plugin, which delegates large reads to a model behind its own platform's
tooling.

### Using bulk-read

Example: a 2,575-line file is about 27k tokens. Through `bulk-read`, a
structured overview with line numbers costs the main model about 700 tokens
and takes about 11 seconds. For a narrow question, `grep -n` and a 30-line Read
cost less than that and need no second model.

Delegating trades exact recall for a smaller context: the main model sees only
an answer shaped by its question, and the cheaper model's mistakes look like
facts. When you do use it, shunt-cc limits that trade:

- **Checked citations.** Haiku must cite `path:line` and quote the code, and
  `bulk-read` checks every citation against the files before printing the
  answer. Wrong lines and invented quotes are flagged.
- **Haiku locates, the main model judges.** Haiku says what the code does
  and where, and gives possible bugs or risks only as leads, with no verdicts
  or confidence ratings. The skill keeps debugging, review, security and
  design reasoning local: the main model reads the cited code and decides.
  Haiku also points to related code the question didn't ask about.

Also:

- **Easy off switch.** `SHUNT_DISABLE=1`, or `/shunt-cc:shunt off` for one
  session.
- **Logged.** Every deny, follow-up read and `bulk-read` call is logged, and
  `shunt-report` summarizes the log for any period. The A/B eval
  (`evals/ab/`) is what measures the plugin's effect.

## What's inside

| Path | Purpose |
|---|---|
| `hooks/shunt-hook` | The core. PreToolUse hook for `Read` and `Bash`: denies whole reads of files over `SHUNT_MIN_LINES` with an outline, and enforces the read budget. SessionStart hook: lists the largest tracked files. |
| `scripts/bulk-read` | Optional. `--question "..." --paths f1 [f2 ...]`. Wraps the files in `<file path="...">` tags with line numbers, asks Haiku once, and checks the answer's citations. |
| `scripts/shunt-report` | Tokens and dollars saved over a period, from the usage log. |
| `scripts/shunt-toggle` | `off`, `on` or `status` for the current session. |
| `skills/bulk-reader/SKILL.md` | The routes in order (outline, `grep -n`, narrow Read, then `bulk-read` for whole-file surveys), and how to check Haiku's answer. |
| `skills/shunt/SKILL.md` | `/shunt-cc:shunt off\|on\|status\|report [period]`, for you rather than the model. |
| `lib/` | The outline, the citation check, and the shared state (usage log, session switches). |
| `evals/` | Deterministic suite (`python3 evals/run.py`), behavior cases, and the A/B eval (`evals/ab/`). |

### Hook rules

**Read**: denies a whole-file Read when the file has more than `SHUNT_MIN_LINES`
lines. It lets through files at or under the threshold, `pages` reads, missing
paths, directories, and binary files (images and PDFs, which Read handles
natively). The deny reason has an outline, then the next steps, cheapest
first:

- an outline of the file: classes, functions, headings or keys with their line
  numbers, and the line range of each top-level symbol. It comes from regexes
  per file type (Python, JS/TS, Go, Rust, JVM languages, C/C++, Ruby, PHP,
  shell, Markdown, YAML, TOML, SQL), costs no model call, and is capped at about
  800 tokens. Other file types get no outline.
- find the lines with the outline or `grep -n`;
- Read just that range with `offset`/`limit`, with how many distinct lines of
  the file still fit in the read budget;
- last, and only to survey or summarize the whole file or skim a long log, the
  exact `bulk-read` command, with a reminder to read the cited lines before
  editing or reporting a finding.

**Read budget (large files only)**: reads with `offset`/`limit` pass until they
would page a large file into context piece by piece. Each large file has a
budget of `SHUNT_READ_BUDGET` distinct lines per session (default:
`SHUNT_MIN_LINES`).
- The budget is per file. Reads across different files never add up, and
  files at or under `SHUNT_MIN_LINES` are never counted.
- Re-reading lines you've already read is free, so narrow checks before edits
  never run out.
- Bounded Bash reads (`head`, `tail`, `rtk read --head-lines`) of the same file
  draw on the same budget.
- `offset` alone reads to Read's 2000-line default, and `offset: 0` or
  `limit: 0` with no real bound counts as a whole-file read. Those were open
  bypasses in shunt.
- The ledger is a JSON file per session under `$TMPDIR/shunt-cc/`. Hook input
  without a `session_id` gets only the per-read check.

**Bash**: applies the same rule to `cat`, `less`, `more`, `head`, `tail`,
`rtk read` and `rtk cat`, including:

- compound commands (`&&`, `;`, newlines), `cd dir && cat file`, and relative paths
- globs, multiple files, `cat < file`, `/bin/cat`, and `VAR=x`, `env`, `command` and `rtk bypass` prefixes
- `head` and `tail` by how many lines they print: `head -100 big` passes, but
  `head -n 5000 big`, `tail -n +2 big` and BSD `tail -r big` are denied
- `rtk read --head-lines/--tail-lines/--max-lines N`, which counts as N lines

Pipes (`cat big | grep x`) and stdout redirects (`> out`, `>> out`, `&>`) pass,
because the file doesn't land in context. `2>/dev/null` alone doesn't divert
stdout, so it is still checked.

**SessionStart**: in a git repo, lists up to `SHUNT_LIST_LARGEST` (default 8)
tracked files over `SHUNT_MIN_LINES`, largest first, and says to find lines
(outline or `grep -n`) and Read narrow ranges, so the model picks a narrow route
before a Read is denied. `bulk-reader` is mentioned last, for whole-file
surveys. It stats every tracked file but counts lines only in
the 200 largest, for at most 3 seconds. Outside git it adds nothing.

**Off switch**: with `SHUNT_DISABLE=1` every hook passes everything through.
`scripts/shunt-toggle off` (or `/shunt-cc:shunt off`) does the same for one
session, keyed on `CLAUDE_CODE_SESSION_ID`, until `on`.

To pass a command through, the hook prints nothing and the normal permission
flow decides. It never emits `permissionDecision: "allow"`, because that would
skip your permission prompts. A deny uses the current format:
`hookSpecificOutput.permissionDecision: "deny"` plus
`permissionDecisionReason`. Any internal error fails open.

### rtk coexistence

[rtk](https://github.com/rtk-ai/rtk)'s global PreToolUse Bash hook
(`rtk hook claude`) rewrites `cat big` to `rtk read big` with `updatedInput`.
Claude Code runs both hooks on the original command, and a deny takes
precedence over a rewrite, so `cat big` is denied. When the model types
`rtk read big` itself, the Bash hook catches that too. The eval suite runs the
real `rtk hook claude` on the `via_rtk` cases and checks the rewritten command.

### Recursion guard

`bulk-read` sets `SHUNT_CHILD=1` on the child. Every shunt hook passes
everything through when that variable is set, and `bulk-read` refuses to run
under it. The child also runs with no tools, so it cannot call back into shunt.

### The headless child

```
claude -p --model claude-haiku-5-5 --effort medium \
  --system-prompt "<terse: structured bullets, names, path:line>" \
  --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --setting-sources "" --disable-slash-commands --no-session-persistence \
  --input-format stream-json --output-format stream-json --verbose
```

The message goes in on stdin as one user turn with two content blocks: the
files, then the question. The child runs in an empty temp directory, so it
discovers no `CLAUDE.md`, and `--setting-sources ""` skips settings, hooks and
plugins.

**Token cost.** Headless Claude Code adds a fixed ~940 tokens per call, on
Haiku and never in your main context. No flag makes this smaller under a
subscription: `--disable-slash-commands` saves nothing, `--safe-mode` costs
more, and `--bare` would be leaner but rejects subscription (OAuth) logins. The
bigger cost is the files themselves, so the files block carries its own
`cache_control` breakpoint (1h TTL by default). A follow-up question about the
same paths, in the same order, within the TTL reads the files from cache:

| argparse.py, 2,575 lines | Haiku cost | Time |
|---|---|---|
| First question | $0.0083 | 5–11s |
| Follow-up, different question | $0.00056 | 4s |

The API rejects a 1h breakpoint after a 5m one, and Claude Code picks its own
TTLs. If the child reports a `cache_control` error, `bulk-read` retries with the
other TTL, then with no breakpoint.

**Short names.** Each file is tagged with its basename when that's unique
among the paths, else the path as given. Haiku repeats the name in every
citation, so long absolute paths would double the answer.

`bulk-read` fails with a nonzero exit and an error on stderr for a missing or
binary file, an empty answer, a nonzero exit, an `is_error` result, unparseable
output, or a timeout (exit 124).

### Checking the answer

The system prompt asks Haiku to cite `path:line` or `path:start-end` for every
point, with the key line from there quoted in backticks right after it. Haiku
says what code is and does, not whether it is correct or safe: a question
about bugs or risks gets a list of leads, each with the facts that make it one,
and no severity or confidence. It claims something is absent only for a named
call it looked for ("no call to eval"), never "no issues". Then a "Related
code:" list cites up to three places the question didn't ask about that touch
the same names or state (other callers or writers, a comment on the cited
code), without judging them.

Before printing, `bulk-read` checks each citation against the files
(`lib/verify.py`, no model call):

- the file must be one that was sent, and the lines must exist;
- the citation's quote, the code span right after it (on its line, opening
  the indented line under it, or a fenced block after it), must appear at
  the cited lines, give or take two. Code further on is prose ("a.py:3 says
  holds are skipped, but nothing reads `hold['open']`"), and so is code past
  the ")" of a citation in parentheses; neither is checked;
- a quote that is just a function or class name in the file must be the
  symbol the cited lines sit in, per the outline;
- a `Class.method` name right next to the citation ("`A.b` at a.py:3",
  "a.py:3 (`A.b`)"), before its quote, must be the method the cited lines sit
  in, when the file has that class (`ledger.adjust`, an instance's call, isn't
  a label);
- a bare "line N `code`" after a citation is checked as a citation of the
  same file;
- a citation with no quote or name is reported as unchecked, not as a match.

A line at the end of the answer reports the result, and lists up to eight
problems with the line where a misplaced quote really is:

```
[shunt-verify: 8 of 9 citations match the files; check these before relying on them:
  ! inventory_service.py:120: "def release(self, ledger, order_id):" is at line 376]
```

A passing citation means the quote is where Haiku says. It doesn't mean Haiku
read the code correctly, which is why the skill still asks for a narrow Read
before an edit or a reported finding.

## Measuring savings

Each event goes to `usage.jsonl` in `SHUNT_STATE_DIR` (default
`~/.local/state/shunt-cc/`) as one JSON line: a `delegate` per `bulk-read` call,
with Haiku's reported usage and cost and the citation check; a `deny` with the
tokens its message costs; a `list` per session start; and a `read` per bounded
read of a large file, counting only lines not read before. Logging never fails
a read, and `SHUNT_LOG=0` turns it off.

```bash
scripts/shunt-report                 # last 7 days
scripts/shunt-report --since 30d --by day
scripts/shunt-report --since 2026-11-01 --until 2026-11-07 --by project --main-model sonnet
scripts/shunt-report --since all --json
```

Or `/shunt-cc:shunt report 30d` in a session.

```
shunt-cc, 2026-10-31 14:02 to now
---------------------------------
Delegations      42 calls, 61 files, 18 sessions; 12 cache hits (29%); 1 failed
Kept out         1,234,000 tokens of files read by Haiku instead
Brought back     93,000 tokens: answers 31,000, deny messages 9,800, file lists 2,200, follow-up reads 50,000
Net kept out     1,141,000 tokens (92% of what was sent)
Haiku cost       $0.2100 (1,402,000 tokens, as reported by each call)
Main-model value $4.5640 at $4.00/MTok input (opus), if each file were otherwise read whole
Est. net saving  $4.3540
Citations        310 checked, 296 matched (95%), 14 flagged
Denied reads     57 (41 whole-file, 16 over budget) on 49 files; 20 handled without Haiku
```

(Illustrative numbers.) "Kept out" assumes the model would otherwise have read
each delegated file whole once. "Brought back" is what shunt itself put into
the main context, including narrow reads of a file after it was delegated in
the same session: the cost of checking Haiku's answer. "Handled without Haiku"
counts denied files that were never delegated, where the outline and narrow
reads were enough. The dollar value prices net tokens once at the main model's
input rate (`--main-model opus|sonnet|fable|haiku` or `--price-per-mtok`).

These delegation figures are estimates, not measurements. "Kept out" assumes
each delegated file would otherwise have been read whole, but with the hooks on
the model usually reads a few ranges instead, so the figures can overstate what
`bulk-read` adds. The report doesn't measure the routing's own saving; the A/B
eval below does.

## Requirements

- Claude Code 2.1+ (CLI or the Desktop app's Code tab)
- Signed in to Claude Code (subscription or API key); `claude -p "hi"` must work
- `python3` (3.9+; on macOS it comes with the Xcode Command Line Tools)

`bulk-read` looks for the `claude` binary in `SHUNT_CLAUDE_BIN`, then on `PATH`,
then in `CLAUDE_CODE_EXECPATH`. The Desktop app sets `CLAUDE_CODE_EXECPATH` to
its bundled binary, so it works there without the CLI installed.

## Install

From GitHub (replace the owner if you fork it):

```bash
claude plugin marketplace add mwalsher/shunt-cc
```

```bash
claude plugin install shunt-cc@shunt-cc
```

From a local checkout, in one step:

```bash
claude plugin install shunt-cc --marketplace /path/to/shunt-cc
```

To try it for one session without installing:

```bash
claude --plugin-dir /path/to/shunt-cc
```

Plugins installed at user scope load in both the CLI and the Desktop app's Code
tab. Start a new session after installing. To stop `bulk-read` from asking for
Bash permission every time, add an allow rule such as
`Bash(*/scripts/bulk-read:*)` to your settings.

## Configuration

Set these in your shell or in `settings.json` under `"env"`.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `SHUNT_MIN_LINES` | `600` | hooks | Files with more lines than this are denied when read whole. Non-numeric values fall back to the default. |
| `SHUNT_READ_BUDGET` | `SHUNT_MIN_LINES` | hooks | Distinct lines of one large file that bounded reads may bring into context per session. |
| `SHUNT_MODEL` | `claude-haiku-5-5` | bulk-read | Model for the child: an alias (`haiku`, `sonnet`) or a full model ID. |
| `SHUNT_EFFORT` | `medium` | bulk-read | Child effort: `low`, `medium`, `high`, `xhigh` or `max`. |
| `SHUNT_TIMEOUT` | `300` | bulk-read | Seconds before the child is killed (exit 124). |
| `SHUNT_CACHE_TTL` | `1h` | bulk-read | Prompt-cache TTL for the files block: `1h`, `5m` or `off`. |
| `SHUNT_CLAUDE_BIN` | (auto) | bulk-read | Path to the `claude` binary. |
| `SHUNT_DISABLE` | (unset) | hooks | `1` turns shunt-cc off: hooks pass everything through. |
| `SHUNT_LIST_LARGEST` | `8` | hooks | Files listed at session start; `0` turns the list off. |
| `SHUNT_STATE_DIR` | `~/.local/state/shunt-cc` | all | Usage log and per-session switches. `$XDG_STATE_HOME/shunt-cc` when that's set. |
| `SHUNT_LOG` | (unset) | all | `0` turns the usage log off. |
| `SHUNT_CHILD` | (unset) | both | Set to `1` on the child. Hooks pass everything through and `bulk-read` refuses to run. Don't set it yourself. |

## Usage

The hooks work on their own. The model reaches for `bulk-reader` only when a
whole-file survey is worth it, from the skill description or the last step in a
deny message. To call the script directly:

```bash
scripts/bulk-read --question "Which functions handle retries? Give line numbers." --paths src/client.ts src/retry.ts
```

The answer goes to stdout, followed by the citation check. A note like
`[shunt: ~27456 input tokens (1 files) -> claude-haiku-5-5 (medium), 11s]` goes
to stderr.

## Evals

### Hooks and transport (deterministic)

```bash
python3 evals/run.py
```

This runs 191 cases with no network access or login needed:

- **Read hook (45)**: adapted from shunt's `hook-evals.json` with a threshold of
  600, plus binary files, directories, `pages`, a file with no trailing newline,
  malformed input, the recursion guard, and the read budget: paging, re-reads,
  overlaps, per-file and per-session scope, and many files in one session. Also
  the outline in the deny message, the budget left, and both off switches.
- **Bash hook (80)**: adapted from shunt's `bash-hook-evals.json`, plus
  compound commands, `cd`, globs, wrappers, rtk (`rtk read`, `--head-lines`,
  `rtk bypass`, `rtk cat`), real `rtk hook claude` rewrites, the recursion
  guard, head/tail paging against the read budget, and `SHUNT_DISABLE`.
- **Transport (27)**: `bulk-read` against a stub `claude`. It checks that file
  contents go on stdin and never in argv, the cached files block and its TTL
  fallback, the child flags, `SHUNT_CHILD=1`, model and effort overrides, every
  loud-failure path, the citation check, and the usage log.
- **Citation check (24)**: `lib/verify.py` on hand-written answers: quotes
  after, under and below a citation, prose code, code past a parenthesis and
  line numbers left unchecked, wrong lines, invented and elided quotes,
  enclosing names and symbol labels inside and outside, instance calls and
  labels in prose ignored, bare line references, unquoted citations, unsent
  files.
- **Session start (6)**: the largest-files list, its limit, and when it stays
  silent.
- **Usage log, toggle and report (9)**: what gets logged, `shunt-toggle`, and
  `shunt-report` totals on a synthetic log.

Where shunt's expected results differ here, the case's `reason` starts with
CHANGED or FIXED. Bounded `head` and `tail` now pass (they work like Read with a
limit), `2>/dev/null` no longer exempts a read, and `head -n 5` is parsed as a
count. Offset-only and zero offset/limit Reads are now denied (the read
budget closes those bypasses). Add `-v` to print each deny reason.

Add `--live` for two real Haiku calls on the generated fixture: a bug-hunt
question, then a follow-up that must report a cache hit.

### Model behavior (`claude plugin eval`)

```bash
claude plugin eval . --scaffold --trust-plugin --allow-tools Bash Edit \
  --model claude-sonnet-5-5 --ablation none --threshold 0.8 --no-publish
```

Five cases in `evals/<case>/case.yaml` run a real session against a generated
1,261-line module (`evals/fixtures/make_inventory.py`):

| Case | Checks | Last run (3 runs each) |
|---|---|---|
| `overview-large-file` | accurate class overview, no Read of 200+ lines | 100% |
| `bug-hunt-large-file` | no paging, finds the planted double-release bug | 89% (before the outline) |
| `full-read-denied` | an explicit full Read is denied, then answered correctly | 100% |
| `edit-large-file` | edits via grep or a small Read, never a big chunk | 100% |
| `small-file-direct` | a 3-line file is read directly, never delegated | 100% |

`claude plugin eval` runs Bash in an OS sandbox where the child `claude` can't
start, so `bulk-read` fails inside these runs. They measure routing (whether the
model reaches for the skill, avoids paging, and verifies narrowly) and the
fallback answer, not Haiku's answer. `--live` above covers the real call. A
full suite run costs about $1 on Sonnet.

### A/B: routing on, off, and forced delegation (`evals/ab/`)

Token counts alone don't show whether the plugin helps. `evals/ab/run.py`
runs real tasks in headless `claude -p` sessions with the plugin on and off
(`--plugin-dir` versus nothing; your own settings, hooks and MCP servers are
left out of every arm) and scores the outcome, not the route. A third arm,
`delegate`, loads the plugin and tells the main model to start with
`bulk-read`, so Haiku's answers are tested on every task instead of only when
the model chooses to delegate:

| Task | Expected effect | Pass when |
|---|---|---|
| `overview` | helps | all 8 classes with their start lines (within 2) |
| `locate` | helps | the retry method, the limit and the backoff |
| `edit` | neutral | `LATE_FEE_RATE = 0.02` and no other line changed |
| `debug` | risky | the failing test passes, only `inventory_service.py` changed |
| `review` | risky | the double release is reported |
| `security` | risky | the shell injection in `export_archive` is reported |
| `crossfile` | risky | the swapped arguments in `order_api.py` are reported |
| `rootcause` | risky | the failing test passes, and so do hidden tests that a fix of the symptom fails |
| `security-precise` | risky | exactly the 2 exploitable `AuditTrail` methods, not the 2 decoys |
| `negative-stock` | risky | exactly the 2 handlers that can drive stock below zero, one passing `force` by position |
| `sign-audit` | recall | exactly the 2 of 67 handlers whose stock change has the wrong sign |

The last four are harder: each has a trap that taking Haiku's word, or
patching where the symptom shows, falls into, and the answer is graded exactly
(a final `ANSWER:` line, or hidden tests run after the session), so a wrong
extra fails as surely as a miss. `rootcause`'s class stores a booking in cases
but counts it in units, and a comment points the fix at the wrong method;
`security-precise` has a validated `subprocess.run` list and a `shell=True` on
a fixed command beside a real shell injection and a path traversal.

The workspace (`evals/ab/workspace.py`) also plants traps for the first risky tasks: a
comment in `release` that claims a second release is a no-op, a shell command
built from input, and a call in a second large file with two arguments
swapped. Per run it records task success, turns, cost (main session plus
Haiku), main-session tokens, the largest context the main model saw, tool
output that entered the context, failed Edits, `bulk-read` calls, denies and
flagged citations, then compares the arms per task.

```bash
python3 evals/ab/run.py --dry-run                  # the plan: 11 tasks x 2 arms (on, off) x 3 runs
python3 evals/ab/run.py --max-cost-usd 10 -j 3     # results in evals/results/ab-<time>/
python3 evals/ab/run.py --tasks debug,review --runs 5
python3 evals/ab/run.py --arms delegate            # Haiku on every task: 33 sessions, about $3
```

A full run is 66 sessions, about $6–10 on Sonnet. A pass-rate drop on a risky
task means the routing, or the "Haiku locates, you judge" guidance, isn't
holding there.

Last full run (2026-11-07 for `on` and `off`, $6.09; 2026-11-08 for
`delegate`, $2.75; Sonnet 5.5, 3 runs per arm), per run:

| Task | Pass on / off / delegate | Cost on / off / delegate | Peak context on / off / delegate | Turns on / off / delegate |
|---|---|---|---|---|
| `overview` | 3/3 / 3/3 / 3/3 | $0.085 / $0.126 / $0.072 | 39.2k / 51.1k / 37.2k | 5.7 / 4.0 / 4.0 |
| `locate` | 3/3 / 3/3 / 3/3 | $0.050 / $0.092 / $0.066 | 34.3k / 45.3k / 36.4k | 3.0 / 3.0 / 6.0 |
| `edit` | 3/3 / 3/3 / 3/3 | $0.047 / $0.045 / $0.066 | 34.0k / 33.7k / 36.0k | 3.0 / 3.0 / 6.0 |
| `debug` | 3/3 / 3/3 / 3/3 | $0.067 / $0.066 / $0.081 | 36.1k / 36.1k / 38.0k | 6.3 / 6.0 / 7.0 |
| `review` | 3/3 / 3/3 / 3/3 | $0.084 / $0.123 / $0.088 | 38.8k / 50.8k / 38.7k | 5.7 / 3.0 / 7.0 |
| `security` | 3/3 / 3/3 / 3/3 | $0.110 / $0.126 / $0.085 | 43.2k / 50.8k / 38.3k | 7.3 / 3.0 / 6.7 |
| `crossfile` | 3/3 / 3/3 / 3/3 | $0.087 / $0.164 / $0.096 | 39.5k / 62.1k / 41.0k | 7.0 / 4.0 / 6.3 |
| `rootcause` | 3/3 / 3/3 / 3/3 | $0.097 / $0.071 / $0.094 | 39.4k / 36.5k / 39.4k | 10.3 / 6.0 / 8.7 |
| `security-precise` | 3/3 / 3/3 / 3/3 | $0.083 / $0.106 / $0.087 | 39.0k / 42.8k / 38.6k | 5.0 / 6.3 / 6.7 |
| `negative-stock` | 3/3 / 3/3 / 3/3 | $0.104 / $0.130 / $0.099 | 42.6k / 48.1k / 40.9k | 8.7 / 7.7 / 8.0 |
| `sign-audit` | 3/3 / 3/3 / 3/3 | $0.079 / $0.087 / $0.082 | 39.4k / 40.6k / 39.2k | 4.3 / 4.7 / 5.7 |
| all | 100% / 100% / 100% | $0.081 / $0.103 / $0.083 | 38.7k / 45.3k / 38.5k | 6.0 / 4.6 / 6.5 |

What it shows:

- **Routing pays; quality held.** With the plugin on, every task passed, as
  with it off. Cost per run fell 21% (95% CI 17–26%) and peak context 15%, at
  the same wall-clock speed.
- **The saving is less file content, not fewer tokens.** Total main-session
  tokens were about the same (151k on, 150k off), because the model took more
  turns (6.0 against 4.6). But only 2.5k tokens of tool output entered the
  context per run, against 7.7k: the model read ranges instead of whole files,
  and a smaller context makes each later turn cheaper.
- **It came from the hooks, not Haiku.** In the `on` arm, `bulk-read` ran in 2
  of 33 sessions. The deny with an outline, the read budget and the
  SessionStart list did the work.
- **Where it helps, and where it doesn't.** Against `off`: `locate` and
  `crossfile` about 46% cheaper, `overview` and `review` about 33%, `edit` and
  `debug` about even, and `rootcause` 37% dearer, because tracing a cause
  across methods took many narrow reads that cost more than reading the file
  once.
- **Forced delegation gains nothing.** In `delegate`, Haiku answered first in
  all 33 sessions and every task still passed, but cost per run ($0.083) was
  no better than `on` ($0.081). It cost more on small tasks (`locate`, `edit`,
  `debug`), where a whole-file call is wasted, and added about 27 seconds per
  task. Haiku itself cost $0.10 across the 33 sessions. That is why
  `bulk-read` is optional and offered last.
- **The main model judged for itself.** On every `delegate` task that needed
  a judgment, it read or grepped the lines Haiku pointed to before answering;
  only `overview` went on Haiku's answer alone.
- **Citations.** Of the 1,173 citations in Haiku's answers across these runs,
  the citation check flags 1, and that one is real (a signature quoted without
  `self`); 114 had no quote to check. Before the rules above were tightened
  against these answers it raised about 30 false alarms, mostly code mentioned
  in passing.

Limits: every arm passed 100%, so pass rate is at its ceiling and can't show a
quality difference. The workspace is synthetic (a 1,300-line
`inventory_service.py` and a 700-line `order_api.py`), there is one model
(Sonnet 5.5), and sessions were short (under 50k peak context). The saving may
differ on real repos, other models and long sessions.

## Not included

Shunt's `code-write` (delegated code generation) is out of scope for now.

An agentic reader (a child that can grep and re-read) isn't included. One
call already puts the whole files in Haiku's context, so it can only miss
what it misreads, and the citation check catches misplaced quotes. An agentic
reader would help with files too big for one call or repo-wide searches, at
several times the cost and latency. The last A/B run, including the arm that
sends every task through Haiku, showed no recall failures; add it if a later
one does.

## License

MIT, except `evals/hook-evals.json` and `evals/bash-hook-evals.json`, which
are adapted from spotify/portal-ai-plugins and stay under Apache-2.0
([LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)). [NOTICE](NOTICE) lists
the changes.
