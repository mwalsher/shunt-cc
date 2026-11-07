# shunt-cc

A minimal Haiku-backed large-file reader for Claude Code. When your session
tries to read a large file whole, a hook denies it and points the model at the
`bulk-reader` skill. The skill sends the file to Haiku through headless Claude
Code (`claude -p`) and returns only Haiku's answer, so the file contents never
enter your main context.

Inspired by Spotify's [shunt](https://github.com/spotify/portal-ai-plugins/tree/main/plugins/shunt)
plugin. Where shunt delegates to AiKA through the Portal CLI, this plugin uses
your own Claude Code login, so it runs on a Claude subscription. It needs no API
key, proxy or third-party service.

Example: a 2,575-line file is about 27k tokens. Through `bulk-read`, a
structured answer with line numbers costs the main model about 700 tokens and
takes about 11 seconds.

## What's inside

| Path | Purpose |
|---|---|
| `hooks/shunt-hook` | PreToolUse hook for `Read` and `Bash`. Denies full reads of files over `SHUNT_MIN_LINES`. |
| `scripts/bulk-read` | `--question "..." --paths f1 [f2 ...]`. Wraps the files in `<file path="...">` tags with line numbers and asks Haiku once. |
| `skills/bulk-reader/SKILL.md` | When and how the model should use `bulk-read`, and to verify line numbers before editing. |
| `evals/` | Hook and transport eval suite (`python3 evals/run.py`). |

### Hook rules

**Read**: denies a whole-file Read when the file has more than `SHUNT_MIN_LINES`
lines. It lets through files at or under the threshold, `pages` reads, missing
paths, directories, and binary files (images and PDFs, which Read handles
natively). The deny reason gives the exact `bulk-read` command, and suggests
Read with offset/limit when exact text is needed for an edit.

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

`bulk-read` fails with a nonzero exit and an error on stderr for a missing or
binary file, an empty answer, a nonzero exit, an `is_error` result, unparseable
output, or a timeout (exit 124).

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
| `SHUNT_CHILD` | (unset) | both | Set to `1` on the child. Hooks pass everything through and `bulk-read` refuses to run. Don't set it yourself. |

## Usage

The model normally finds the skill on its own, either from the skill
description or from the hook's deny message. To call the script directly:

```bash
scripts/bulk-read --question "Which functions handle retries? Give line numbers." --paths src/client.ts src/retry.ts
```

The answer goes to stdout. A note like
`[shunt: ~27456 input tokens (1 files) -> claude-haiku-5-5 (medium), 11s]` goes
to stderr.

## Evals

### Hooks and transport (deterministic)

```bash
python3 evals/run.py
```

This runs 139 cases with no network access or login needed:

- **Read hook (38)**: adapted from shunt's `hook-evals.json` with a threshold of
  600, plus binary files, directories, `pages`, a file with no trailing newline,
  malformed input, the recursion guard, and the read budget: paging, re-reads,
  overlaps, per-file and per-session scope, and many files in one session.
- **Bash hook (79)**: adapted from shunt's `bash-hook-evals.json`, plus
  compound commands, `cd`, globs, wrappers, rtk (`rtk read`, `--head-lines`,
  `rtk bypass`, `rtk cat`), real `rtk hook claude` rewrites, the recursion
  guard, and head/tail paging against the read budget.
- **Transport (22)**: `bulk-read` against a stub `claude`. It checks that file
  contents go on stdin and never in argv, the cached files block and its TTL
  fallback, the child flags, `SHUNT_CHILD=1`, model and effort overrides, and
  every loud-failure path.

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
| `bug-hunt-large-file` | reaches for `bulk-read` unprompted, no paging, finds the planted double-release bug | 89% |
| `full-read-denied` | an explicit full Read is denied, then answered correctly | 100% |
| `edit-large-file` | edits via grep or a small Read, never a big chunk | 100% |
| `small-file-direct` | a 3-line file is read directly, never delegated | 100% |

`claude plugin eval` runs Bash in an OS sandbox where the child `claude` can't
start, so `bulk-read` fails inside these runs. They measure routing (whether the
model reaches for the skill, avoids paging, and verifies narrowly) and the
fallback answer, not Haiku's answer. `--live` above covers the real call. A
full suite run costs about $1 on Sonnet.

## Not included

Shunt's `code-write` (delegated code generation) is out of scope for now.

## License

MIT, except `evals/hook-evals.json` and `evals/bash-hook-evals.json`, which
are adapted from spotify/portal-ai-plugins and stay under Apache-2.0
([LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)). [NOTICE](NOTICE) lists
the changes.
