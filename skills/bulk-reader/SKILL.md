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
`offset`/`limit` for that section instead.

## How to call

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bulk-read" --question "<specific question>" --paths <file1> [<file2> ...]
```

If `${CLAUDE_PLUGIN_ROOT}` isn't set in your shell, use the full path printed in
the hook's deny message.

- Ask a specific question and say what you need back, e.g. "List every exported
  function with its line number and a one-line purpose."
- Each call is independent. For a follow-up, call again with the same `--paths`;
  re-sending files costs you nothing because they go to Haiku, not to you.
- The answer is on stdout; a rough input-token note is on stderr. A nonzero exit
  means it failed (missing file, timeout, empty answer): read the error and fix
  the call rather than trusting partial output.

## Before you edit

Haiku's line numbers and quotes are usually right but not guaranteed. Before any
Edit, Read the cited range with `offset`/`limit` and confirm the exact text.
