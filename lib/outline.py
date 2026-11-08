"""Model-free outline of a source file: symbols and their line numbers.

Regexes per file type, no dependencies. It aims to be cheap and roughly right,
not a parser: the outline goes into the hook's deny message so the main model
can Read just the ranges it needs, often without delegating at all.
"""
import os
import re

MAX_SCAN_LINES = 200000

# Each pattern yields named groups: n (name), optionally k (kind) and i (indent).
PY = [r"^(?P<i>\s*)(?P<k>class|def|async def)\s+(?P<n>\w+)",
      r"^(?P<n>[A-Z][A-Z0-9_]{2,})\s*(?::[^=]*)?=(?!=)"]
JS_MODS = r"(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
JS = [r"^(?P<i>\s*)" + JS_MODS + r"(?P<k>function\*?|class|interface|enum|namespace)\s+(?P<n>[\w$]+)",
      r"^(?P<i>)" + JS_MODS + r"(?P<k>type)\s+(?P<n>[\w$]+)\s*(?:<[^=]*>)?\s*=",
      r"^(?P<i>)" + JS_MODS + r"(?P<k>const|let|var)\s+(?P<n>[\w$]+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:function|\([^)]*\)\s*(?::[^=]+)?=>|[\w$]+\s*=>)",
      r"^(?P<i>\s+)(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*(?P<n>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\([^;]*\)\s*(?::\s*[^{;]+)?\{\s*$"]
GO = [r"^(?P<k>func)\s+(?:\((?P<recv>[^)]*)\)\s*)?(?P<n>\w+)",
      r"^(?P<k>type)\s+(?P<n>\w+)"]
RS = [r"^(?P<i>\s*)(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern\s+\"C\")\s+)*(?P<k>fn|struct|enum|trait|mod|macro_rules!|type)\s+(?P<n>\w+)",
      r"^(?P<i>\s*)(?P<k>impl)(?:<[^>]*>)?\s+(?P<n>[\w:<>, ]+?)\s*(?:where\b.*)?\{?\s*$"]
JVM_MODS = r"(?:(?:public|private|protected|internal|static|final|abstract|sealed|open|override|data|inline|partial|async|virtual|synchronized|suspend)\s+)*"
JVM = [r"^(?P<i>\s*)" + JVM_MODS + r"(?P<k>class|interface|enum|record|struct|object|trait|fun|func|def)\s+(?P<n>\w+)",
       r"^(?P<i>\s+)" + JVM_MODS.replace(")*", ")+") + r"[\w<>\[\],.? ]+?\s+(?P<n>\w+)\s*\([^;]*$"]
RB = [r"^(?P<i>\s*)(?P<k>class|module|def)\s+(?P<n>[\w:.?!=]+)"]
PHP = [r"^(?P<i>\s*)(?:(?:public|private|protected|static|abstract|final)\s+)*(?P<k>function|class|interface|trait|enum)\s+(?P<n>\w+)"]
SH = [r"^(?P<i>\s*)(?:function\s+)?(?P<n>[\w:.-]+)\s*\(\)\s*\{?"]
C = [r"^(?P<k>struct|enum|union|class|namespace)\s+(?P<n>\w+)[^;]*$",
     r"^(?!(?:if|for|while|switch|return|else)\b)[A-Za-z_][\w\s*&:<>,]*?[\s*&](?P<n>[A-Za-z_~][\w:~]*)\s*\([^;]*$"]
MD = [r"^(?P<k>#{1,4})\s+(?P<n>.+?)\s*#*\s*$"]
YAML = [r"^(?P<n>[\w.-]+)\s*:"]
TOML = [r"^\[\[?(?P<n>[^\]]+)\]\]?"]
SQL = [r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?P<k>table|view|function|procedure|index|trigger|type)\s+(?:if\s+not\s+exists\s+)?(?P<n>[\w.\"`]+)"]

BY_EXT = {
    "py": PY, "pyi": PY,
    "js": JS, "jsx": JS, "mjs": JS, "cjs": JS, "ts": JS, "tsx": JS, "mts": JS, "cts": JS,
    "go": GO, "rs": RS,
    "java": JVM, "kt": JVM, "kts": JVM, "scala": JVM, "swift": JVM, "cs": JVM, "groovy": JVM,
    "rb": RB, "php": PHP,
    "sh": SH, "bash": SH, "zsh": SH,
    "c": C, "h": C, "cc": C, "cpp": C, "cxx": C, "hpp": C, "hh": C, "m": C, "mm": C,
    "md": MD, "mdx": MD, "markdown": MD,
    "yaml": YAML, "yml": YAML, "toml": TOML, "sql": SQL,
}
SKIP_NAMES = {"if", "for", "while", "switch", "catch", "return", "function", "else", "do",
              "try", "with", "new", "typeof", "await", "super", "constructor"}


def symbols(path):
    """[(line, depth, label)] for the file's symbols, or None for an unknown type."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    patterns = BY_EXT.get(ext)
    if not patterns:
        return None
    compiled = [re.compile(p) for p in patterns]
    found = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if n > MAX_SCAN_LINES:
                break
            if not line.strip():
                continue
            for rx in compiled:
                m = rx.match(line)
                if not m:
                    continue
                g = m.groupdict()
                name = g["n"].strip()
                if name in SKIP_NAMES:
                    break
                kind = (g.get("k") or "").strip()
                if patterns is MD:
                    depth, label = len(kind) - 1, name
                else:
                    depth = len((g.get("i") or "").expandtabs(4))
                    if g.get("recv"):  # Go method: show the receiver type
                        name = "({}) {}".format(g["recv"].split()[-1], name)
                    label = "{} {}".format(kind, name) if kind else name
                found.append((n, depth, label[:80]))
                break
    # Indents become levels: the distinct indent widths, in order.
    if patterns is not MD:
        widths = sorted({d for _, d, _ in found})
        level = {w: i for i, w in enumerate(widths)}
        found = [(n, level[d], label) for n, d, label in found]
    return found


def outline(path, total, max_entries=40, max_children=30, max_chars=3200):
    """Outline text for the deny message, or "" when there is nothing useful.

    Shows the deepest levels that fit in max_entries, one per row. Each
    top-level row gets its line range so the model can Read it with
    offset/limit, and the next level down, when hidden, is listed inline.
    """
    try:
        found = symbols(path)
    except OSError:
        return ""
    if not found or max_entries <= 0:
        return ""
    levels = sorted({d for _, d, _ in found})
    shown_level = levels[0]
    for lv in levels:
        if sum(1 for _, d, _ in found if d <= lv) <= max_entries:
            shown_level = lv
    shown = [s for s in found if s[1] <= shown_level]
    rows, extra = [], len(shown) - max_entries
    tops = [n for n, d, _ in shown if d == levels[0]]
    child_level = next((lv for lv in levels if lv > shown_level), None)
    for i, (n, d, label) in enumerate(shown[:max_entries]):
        if sum(len(r) + 1 for r in rows) > max_chars:
            extra = len(shown) - i
            break
        loc = str(n)
        children = []
        if d == levels[0]:
            end = next((m for m in tops if m > n), total + 1) - 1
            if end > n:
                loc = "{}-{}".format(n, end)
            # When only the top level fits, its children are listed inline by
            # name and line: cheap, and enough to Read the one method you need.
            if shown_level == levels[0]:
                children = ["{} {}".format(lbl.split()[-1], m) for m, dd, lbl in found
                            if dd == child_level and n < m <= end]
        indent = "  " * levels.index(d)
        rows.append("  {:>11}  {}{}".format(loc, indent, label))
        if children:
            more = len(children) - max_children
            names = ", ".join(children[:max_children]) + (", +{} more".format(more) if more > 0 else "")
            rows.append("  {:>11}    {}".format("", names))
    if extra > 0:
        rows.append("  ... +{} more; grep -n finds them".format(extra))
    return "\n".join(rows)
