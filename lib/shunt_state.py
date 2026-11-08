"""State shared by the shunt-cc hooks and scripts: the usage log and the
per-session off switch.

Both live in SHUNT_STATE_DIR (default $XDG_STATE_HOME/shunt-cc, else
~/.local/state/shunt-cc), not in $TMPDIR: the hook and the Bash tool can see
different temp directories, and the log must outlive a reboot to report on.
"""
import json
import os
import pwd
import re
import time


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
        # One short line per write in append mode, so concurrent hooks don't interleave.
        with open(log_path(), "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def off_flag(session_id):
    return os.path.join(state_dir(), "off", re.sub(r"[^A-Za-z0-9_.-]", "_", session_id))


def disabled(session_id):
    """True when SHUNT_DISABLE is set, or this session was switched off."""
    if os.environ.get("SHUNT_DISABLE", "") not in ("", "0"):
        return True
    return bool(session_id) and os.path.exists(off_flag(session_id))
