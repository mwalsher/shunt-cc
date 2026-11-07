# shunt-claude-code

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
lines. It lets through reads with `offset`, `limit` or `pages`, files at or under
the threshold, missing paths, directories, and binary files (images and PDFs,
which Read handles natively). The deny reason gives the exact `bulk-read`
command, and suggests Read with offset/limit when exact text is needed for an
edit.

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
  --output-format json          # file contents go in on stdin
```

The child runs in an empty temp directory, so it discovers no `CLAUDE.md`, and
`--setting-sources ""` skips settings, hooks and plugins. Its fixed overhead is
about 1k tokens. `--bare` would be leaner, but it rejects subscription (OAuth)
logins, so it isn't used. `bulk-read` fails with a nonzero exit and an error on
stderr for a missing or binary file, an empty answer, a nonzero exit, an
`is_error` result, unparseable output, or a timeout (exit 124).

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
claude plugin marketplace add mwalsher/shunt-claude-code
```

```bash
claude plugin install shunt-claude-code@shunt-claude-code
```

From a local checkout, in one step:

```bash
claude plugin install shunt-claude-code --marketplace /path/to/shunt-claude-code
```

To try it for one session without installing:

```bash
claude --plugin-dir /path/to/shunt-claude-code
```

Plugins installed at user scope load in both the CLI and the Desktop app's Code
tab. Start a new session after installing. To stop `bulk-read` from asking for
Bash permission every time, add an allow rule such as
`Bash(*/shunt-claude-code/scripts/bulk-read:*)` to your settings.

## Configuration

Set these in your shell or in `settings.json` under `"env"`.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `SHUNT_MIN_LINES` | `600` | hooks | Files with more lines than this are denied when read whole. Non-numeric values fall back to the default. |
| `SHUNT_MODEL` | `claude-haiku-5-5` | bulk-read | Model for the child: an alias (`haiku`, `sonnet`) or a full model ID. |
| `SHUNT_EFFORT` | `medium` | bulk-read | Child effort: `low`, `medium`, `high`, `xhigh` or `max`. |
| `SHUNT_TIMEOUT` | `300` | bulk-read | Seconds before the child is killed (exit 124). |
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

```bash
python3 evals/run.py
```

This runs 116 cases with no network access or login needed:

- **Read hook (24)**: adapted from shunt's `hook-evals.json` with a threshold of
  600, plus binary files, directories, `pages`, a file with no trailing newline,
  malformed input and the recursion guard.
- **Bash hook (74)**: adapted from shunt's `bash-hook-evals.json`, plus
  compound commands, `cd`, globs, wrappers, rtk (`rtk read`, `--head-lines`,
  `rtk bypass`, `rtk cat`), real `rtk hook claude` rewrites, and the recursion
  guard.
- **Transport (18)**: `bulk-read` against a stub `claude`. It checks that file
  contents go on stdin and never in argv, the child flags, `SHUNT_CHILD=1`,
  model and effort overrides, and every loud-failure path.

Where shunt's expected results differ here, the case's `reason` starts with
CHANGED or FIXED. Bounded `head` and `tail` now pass (they work like Read with a
limit), `2>/dev/null` no longer exempts a read, and `head -n 5` is parsed as a
count.

Add `-v` to print each deny reason.

## Not included

Shunt's `code-write` (delegated code generation) is out of scope for now.

## License

Apache-2.0. The eval cases are adapted from spotify/portal-ai-plugins
(Apache-2.0).
