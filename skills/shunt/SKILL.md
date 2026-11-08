---
name: shunt
description: Turn shunt-cc off or on for this session, show whether it's on, or report what it saved. Run by the user, as /shunt-cc:shunt off, on, status, or report [7d|30d|YYYY-MM-DD].
argument-hint: off | on | status | report [period] [--by day|session|project]
disable-model-invocation: true
---

# shunt

The arguments are: `$ARGUMENTS`

The scripts are in this plugin's `scripts/` directory, two levels above this
skill's base directory. Run exactly one command, then show the user its output
as is.

- `off`, `on` or `status`, or no arguments: run `scripts/shunt-toggle` with that
  word (`status` when there's none).
- `report`, with anything after it: run `scripts/shunt-report`, passing a period
  as `--since` (for example `report 30d` becomes `--since 30d`) and any other
  options unchanged. With no period, it covers the last 7 days.

If the script fails, show its error. Don't work around it.
