"""State shared by the shunt-cc hooks and scripts: the usage log, the
per-session off switch, the read-budget ledgers, and their retention.

Both live in SHUNT_STATE_DIR (default $XDG_STATE_HOME/shunt-cc, else
~/.local/state/shunt-cc), not in $TMPDIR: the hook and the Bash tool can see
different temp directories, and the log must outlive a reboot to report on.
"""
import fcntl
import json
import os
import pwd
import re
import tempfile
import time

DEFAULT_LOG_DAYS = 365
PRUNE_EVERY = 86400  # seconds between retention passes
LEDGER_DAYS = 7  # a session's read-budget ledger, after its last read
OFF_FLAG_DAYS = 30  # a session's off switch, after it was set


def state_dir():
    if os.environ.get("SHUNT_STATE_DIR"):
        return os.environ["SHUNT_STATE_DIR"]
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        # HOME can be stripped (sandboxes, GUI apps): fall back to the real home.
        home = os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
        base = os.path.join(home, ".local", "state")
    return os.path.join(base, "shunt-cc")


def log_path():
    return os.path.join(state_dir(), "usage.jsonl")


def ledger_dir():
    """Per-session read-budget ledgers. In $TMPDIR: they only matter while a
    session runs."""
    return os.path.join(tempfile.gettempdir(), "shunt-cc")


def log_days():
    """Days of usage log to keep; 0 keeps it all. SHUNT_LOG_DAYS overrides."""
    raw = os.environ.get("SHUNT_LOG_DAYS", "")
    return int(raw) if raw.isdigit() else DEFAULT_LOG_DAYS


def tokens(chars):
    """Rough token count for text of this many characters."""
    return int(chars) // 4


def log_event(event, **fields):
    """Append one event to the usage log. Never raises: logging must not break
    a read. SHUNT_LOG=0 turns logging off."""
    if os.environ.get("SHUNT_LOG") == "0":
        return
    try:
        os.makedirs(state_dir(), exist_ok=True)
        record = dict({"ts": round(time.time(), 3), "event": event}, **fields)
        # Locked so an append never lands while prune() rewrites the file.
        with open(log_path(), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass
    maybe_prune()


def remove_older(directory, max_age, now):
    """Delete plain files in `directory` not modified for `max_age` seconds."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                os.remove(path)
        except OSError:
            pass


def prune(now=None):
    """One retention pass: drop usage-log events older than log_days(), and
    ledgers and off switches of sessions long over. Never raises."""
    now = now or time.time()
    remove_older(ledger_dir(), LEDGER_DAYS * 86400, now)
    remove_older(os.path.join(state_dir(), "off"), OFF_FLAG_DAYS * 86400, now)
    days = log_days()
    if not days:
        return
    cutoff = now - days * 86400
    try:
        with open(log_path(), "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.readlines()
            kept = []
            for line in lines:
                try:
                    if json.loads(line).get("ts", now) < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass  # keep what can't be read; the report skips it
                kept.append(line)
            if len(kept) < len(lines):
                # Rewritten in place, under the lock, so appenders keep the same file.
                f.seek(0)
                f.writelines(kept)
                f.truncate()
    except OSError:
        pass


def maybe_prune():
    """Run prune() at most once every PRUNE_EVERY seconds, keyed on a marker
    file's mtime. Called on every logged event; costs one stat otherwise."""
    marker = os.path.join(state_dir(), ".pruned")
    try:
        if time.time() - os.path.getmtime(marker) < PRUNE_EVERY:
            return
    except OSError:
        pass
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(marker, "a"):
            os.utime(marker, None)
    except OSError:
        return
    prune()


def off_flag(session_id):
    return os.path.join(state_dir(), "off", re.sub(r"[^A-Za-z0-9_.-]", "_", session_id))


def disabled(session_id):
    """True when SHUNT_DISABLE is set, or this session was switched off."""
    if os.environ.get("SHUNT_DISABLE", "") not in ("", "0"):
        return True
    return bool(session_id) and os.path.exists(off_flag(session_id))
