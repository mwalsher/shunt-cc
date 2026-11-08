---
name: bulk-reader
description: Delegate reading large files to Haiku so only the answer enters your context. Use it instead of reading whole, or guessing from grep, when you need to understand, summarize or survey a file over ~600 lines, locate code across several files, or skim a large log or diff. Also use it whenever a shunt hook denies a Read or cat of a large file and the outline in the deny message isn't enough.
---

# bulk-reader

Sends whole files to Haiku (headless Claude Code, no tools) with your question
and prints Haiku's answer. The file contents never enter your context.

## Pick the cheapest route

1. **The outline.** A shunt deny message lists the file's symbols with line
   numbers. If you know which part you need, Read just that range with
   `offset`/`limit`. This costs no model call.
2. **`grep -n`**, then a narrow Read, for a name or string you can search for.
3. **bulk-read**, when you need the whole file understood: what it does, where
   something is handled, which parts relate to a question, a survey of a log.

Each large file has a per-session budget of distinct lines (about 600 by
default). Narrow reads fit; paging through the file in chunks doesn't, and the
hook denies reads past the budget. Re-reading lines is free.

## Haiku locates; you judge

Haiku is good at finding names, signatures, structure and where things are. It
is weaker at deciding whether code is correct, so it is told not to: it says
what the code does and where, and gives bugs or risks only as leads to check.
For debugging, code review, security review or a design decision:

- Ask for locations, not verdicts: "list every place that builds a shell
  command or a file path from a parameter, with the line" rather than "find
  the security issues".
- Read those ranges yourself and reach the conclusion yourself, including
  which leads are not problems.
- Don't report a bug or a vulnerability, or rule one out, on Haiku's word.

Haiku only answers what you ask, so it can miss what you didn't think to ask
about. Its answer ends with a "Related code:" list (other callers or writers of
the same state, comments on the cited code) for that reason; read any that
bear on your task.

## How to call

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read" --question "<specific question>" --paths <file1> [<file2> ...]
```

If `${CLAUDE_PLUGIN_ROOT}` isn't set in your shell, use the full path printed in
the hook's deny message.

- Ask a specific question and say what you need back, e.g. "List every exported
  function with its line number and a one-line purpose."
- Put related questions in one call. Haiku keeps no memory between calls, so
  separate calls can contradict each other.
- For a follow-up, call again with the same `--paths` in the same order: the
  files are prompt-cached for an hour, so it costs about a tenth of the first
  call.
- The answer is on stdout; a rough input-token note is on stderr. A nonzero exit
  means it failed (missing file, timeout, empty answer): read the error and fix
  the call rather than trusting partial output. If it can't run at all (for
  example `claude` isn't available), say so, then work from the outline,
  `grep -n` and narrow Reads instead.

## Checking the answer

The answer ends with a `[shunt-verify: ...]` line. It checks every `path:line`
Haiku cited against the files: the lines exist, and the code Haiku quoted right
after the citation is really at those lines. Flagged citations show where the
quote actually is, or that it isn't in the file at all. Treat a flagged
citation as wrong until you have read the lines yourself. Citations "with no
quote to check" are unverified.

A citation that passes is in the right place, but that doesn't make Haiku's
description of the code right. Before any Edit, and before you report a finding
as certain, Read the cited lines with `offset`/`limit` (the range plus a few
lines either side, usually under 50). Don't re-read large chunks to
double-check: that puts the file back in your context.
