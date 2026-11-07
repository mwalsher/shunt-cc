---
name: bulk-reader
description: Delegate reading large files to Haiku so only the answer enters your context. Use it instead of reading whole, or guessing from grep, when you need to understand, summarize or survey a file over ~600 lines, answer a question across several files, or skim a large log or diff. Also use it whenever a shunt hook denies a Read or cat of a large file.
---

# bulk-reader

Sends whole files to Haiku (headless Claude Code, no tools) with your question
and prints Haiku's answer. The file contents never enter your context.

## When to use

- Understanding a large file: "what does this module export", "where is X handled".
- Questions that span several files: pass them all in one call.
- Large logs, generated files, lockfiles, diffs saved to disk.

Don't use it for small files, or when you need exact text to edit: Read with
`offset`/`limit` for that section instead. Don't page through a large file in
big chunks: each large file has a per-session budget of distinct lines (about
600 by default), and the hook denies reads past it. Narrow reads of the lines
you need are fine.

## How to call

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read" --question "<specific question>" --paths <file1> [<file2> ...]
```

If `${CLAUDE_PLUGIN_ROOT}` isn't set in your shell, use the full path printed in
the hook's deny message.

- Ask a specific question and say what you need back, e.g. "List every exported
  function with its line number and a one-line purpose."
- Haiku keeps no memory between calls. Ask follow-ups as new calls with the same
  `--paths` in the same order: the files are prompt-cached for an hour, so a
  follow-up costs about a tenth of the first call, and none of it lands in your
  context.
- The answer is on stdout; a rough input-token note is on stderr. A nonzero exit
  means it failed (missing file, timeout, empty answer): read the error and fix
  the call rather than trusting partial output. If it can't run at all (for
  example `claude` isn't available), say so, then work from `grep -n` and
  narrow Reads of the matching lines instead.

## Checking the answer

Haiku's line numbers and quotes are usually right but not guaranteed. Before any
Edit, or before you report a finding as certain, Read just the cited lines with
`offset`/`limit` (the cited range plus a few lines either side, typically under
50) and confirm the exact text. Don't re-read large chunks to double-check:
that puts the file back in your context and defeats the delegation. For a
follow-up question, call `bulk-read` again instead.
