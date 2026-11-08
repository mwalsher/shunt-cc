"""Check a bulk-read answer's citations against the files, without a model.

Every path:line (or path:start-end) the answer cites is checked: the file must
be one that was sent and the lines must exist. A bare "line N" after a path
citation is a citation of that path too, when code is quoted right after it
("line 12 `x = 1`").

A citation's quote is the code span right after it ("a.py:3 `x = 1`", "a.py:3
is `x = 1`"): on its line, else opening the indented line under it, else the
first line of a fenced block after it. The quote must appear at the cited
lines, give or take a couple ("..." in it stands for any text). Code further
on is prose ("a.py:3 says closed holds are skipped, but nothing reads
`hold['open']`"), and so is code past the ")" of a citation in parentheses;
neither is checked. A quote that is just the name of a function or class in
the file means the cited lines are inside it. `Class.method` right next to a
citation ("`A.b` at a.py:3", "a.py:3 (`A.b`)"), before its quote, labels it:
when the file has that class, the cited lines must sit inside the method, per
the outline (`ledger.adjust`, an instance's call, isn't a label). A citation
with neither a quote nor a label is counted as unchecked, not as a match.
Nothing here calls a model, so it costs nothing to run.
"""
import os
import re

import outline

CITE = re.compile(r"(?P<path>[^\s`'\"()\[\]{}<>,;:]+):(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?")
BARE = re.compile(r"\blines? (?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?")
ADJACENT = re.compile(r"^[\s:,(]*$")
GAP = re.compile(r"^[\s`:,()\-\u2013\u2014]*(?:at|in|on)?[\s`:,(]*$")
TICK = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"^\s*```")
NUMBER = re.compile(r"^\d+(?:\s*[-\u2013:]\s*\d+)?$")
OPENS = re.compile(r"^\s*(?:[-*+>]|\d+[.)])?\s*$")  # before a quote that opens its line
IDENT = re.compile(r"^[A-Za-z_]\w*$")
QUALIFIED = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|#)[A-Za-z_$][\w$]*)+(?:\(\))?$")
SLACK = 2  # lines of tolerance either side of a cited range
MIN_QUOTE = 3
MAX_FLAGS = 8


def norm(text):
    return " ".join(text.split())


class FileIndex:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.lines = f.read().decode("utf-8", errors="replace").splitlines()
        self.normed = [norm(line) for line in self.lines]
        self.path = path
        self._symbols = None

    def find(self, quote, start, end):
        """'ok' if quote is at start..end (with slack), else the 1-based line
        where it first appears, else None."""
        lo, hi = max(start - 1 - SLACK, 0), min(end + SLACK, len(self.lines))
        if elided_in(quote, " ".join(self.normed[lo:hi])):
            return "ok"
        for i, line in enumerate(self.normed):
            if elided_in(quote, line):
                return i + 1
        return None

    def has(self, name):
        """True if the outline has a symbol called name."""
        if self._symbols is None:
            self._symbols = outline.symbols(self.path) or []
        return any(label.split()[-1] == name for _, _, label in self._symbols)

    def encloses(self, name, start):
        """True if start is inside a symbol called name, per the outline."""
        syms = self._symbols if self.has(name) else []
        for i, (line, depth, label) in enumerate(syms):
            if line > start:
                break
            if label.split()[-1] != name:
                continue
            after = [n for n, d, _ in syms[i + 1:] if d <= depth]
            if not after or after[0] > start:
                return True
        return False


def elided_in(quote, text):
    """quote is in text, where "..." in the quote stands for any run of text."""
    pos = 0
    for part in re.split(r"\s*(?:\.\.\.|\u2026)\s*", quote):
        if not part:
            continue
        pos = text.find(part, pos)
        if pos < 0:
            return False
        pos += len(part)
    return True


def resolve(token, paths):
    """The sent path a citation names: exact, by suffix, or by basename."""
    token = token.lstrip("./") if not os.path.isabs(token) else token
    for p in paths:
        if p == token or os.path.abspath(p) == os.path.abspath(token):
            return p
    hits = [p for p in paths if p.endswith("/" + token)] or \
           [p for p in paths if os.path.basename(p) == os.path.basename(token)]
    return hits[0] if len(hits) == 1 else None


def span_kind(raw):
    """('cite', None), ('label', (owner, name)), ('code', text) or (None, None)
    for a backtick span. A bare line number (`135`) is none of them."""
    text = norm(re.sub(r"^\s*\d+\t", "", raw))
    if CITE.fullmatch(text):
        return "cite", None
    if QUALIFIED.match(text):
        return "label", tuple(re.split(r"\.|::|#", text.rstrip("()"))[-2:])
    if NUMBER.match(text) or len(text) < MIN_QUOTE:
        return None, None
    return "code", text


class Citation:
    def __init__(self, path, start, end, label):
        self.path, self.start, self.end, self.label = path, start, end, label
        self.quote = None  # the code the citation says is at its lines
        self.names = []    # (Class, method) labels next to it
        self.anchor = None     # where its quote may start: after it or its label
        self.in_parens = False  # "(a.py:3)": its quote can't be past the ")"

    def takes(self, line, start):
        """True if a code span at start on line is this citation's quote or
        label."""
        gap = line[self.anchor:start]
        return bool(GAP.match(gap)) and not (self.in_parens and ")" in gap)


def span_range(m):
    start = int(m.group("start"))
    return start, max(int(m.group("end") or start), start)


def citations(answer, paths):
    """[Citation] for the citations in the answer, in order."""
    found = []
    pending = None    # the last citation, while it has no quote yet
    last_path = None  # what a bare "line N" refers to
    in_fence = False
    for row, line in enumerate(answer.splitlines()):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            kind, text = span_kind(line)
            if pending is not None and kind == "code":
                pending.quote, pending = text, None
            continue
        events = [(m.start(), "cite", m) for m in CITE.finditer(line)
                  if resolve(m.group("path"), paths) or "." in os.path.basename(m.group("path"))]
        taken = [(m.start(), m.end()) for _, _, m in events]
        events += [(m.start(), "bare", m) for m in BARE.finditer(line)
                   if not any(a <= m.start() < b for a, b in taken)]
        events += [(m.start(), "span", m) for m in TICK.finditer(line)]
        events.sort(key=lambda e: e[0])
        if not any(kind in ("cite", "bare") for _, kind, _ in events):
            # The indented line under a citation can carry its quote, when the
            # quote opens it ("  - `x = 1`"); any other line ends the wait.
            if pending is not None and line[:1].isspace():
                spans = [m for _, _, m in events]
                if spans and OPENS.match(line[:spans[0].start()]):
                    kind, text = span_kind(spans[0].group(1))
                    if kind == "code":
                        pending.quote = text
                pending = None
            elif line.strip():
                pending = None
            continue
        pending = None
        here = []         # citations made on this line
        loose = []        # (label, end) not yet next to a citation
        tentative = None  # a bare "line N" waiting for its quote
        for _, kind, m in events:
            if kind == "cite":
                start, end = span_range(m)
                c = Citation(m.group("path"), start, end, m.group(0))
                c.names = [n for n, end in loose if GAP.match(line[end:m.start()])]
                c.anchor, loose = m.end(), []
                c.in_parens = line[:m.start()].rstrip("`").endswith("(")
                found.append(c)
                here.append(c)
                pending, tentative, last_path = c, None, m.group("path")
            elif kind == "bare" and last_path:
                start, end = span_range(m)
                tentative = Citation(last_path, start, end,
                                     "{}:{}".format(last_path, m.group(0).split(" ", 1)[1]))
                tentative_end, pending = m.end(), None
            elif kind == "span":
                what, text = span_kind(m.group(1))
                if what == "label":
                    # A label right next to a citation, before its quote;
                    # anywhere else it's prose.
                    if here and here[-1].quote is None and here[-1].takes(line, m.start()):
                        here[-1].names.append(text)
                        here[-1].anchor = m.end()
                    else:
                        loose.append((text, m.end()))
                elif what == "code" and tentative is not None:
                    # Only a quote right after it: "line 12 `x = 1`", not
                    # "(line 12) is never set to `False`".
                    if ADJACENT.match(line[tentative_end:m.start()]):
                        tentative.quote = text
                        found.append(tentative)
                    tentative = None
                elif what == "code" and pending is not None:
                    if pending.takes(line, m.start()):
                        pending.quote = text
                    pending = None
    return found


def check(answer, paths):
    """(summary dict, footer text) for the answer's citations."""
    index, cited, ok, unchecked, flags = {}, 0, 0, 0, []
    found = citations(answer, paths)
    for c in found:
        path = resolve(c.path, paths)
        cited += 1
        if path is None:
            flags.append("{}: names a file that wasn't sent".format(c.label))
            continue
        if path not in index:
            index[path] = FileIndex(path)
        f = index[path]
        if c.start < 1 or c.end > len(f.lines):
            flags.append("{}: past the end of the file ({} lines)".format(c.label, len(f.lines)))
            continue
        # A label counts when the file has its class: `ledger.adjust` is a
        # call, `StockLedger.adjust` a place.
        names = [n for owner, n in c.names if f.has(owner)]
        if c.quote and IDENT.match(c.quote) and f.has(c.quote):
            names.append(c.quote)
            c.quote = None
        outside = [n for n in names if not f.encloses(n, c.start)]
        if outside:
            flags.append("{}: line {} isn't inside {}".format(c.label, c.start, outside[0]))
            continue
        if c.quote is None:
            if names:
                ok += 1
            else:
                unchecked += 1
            continue
        where = f.find(c.quote, c.start, c.end)
        if where == "ok":
            ok += 1
            continue
        short = c.quote if len(c.quote) <= 60 else c.quote[:57] + "..."
        flags.append('{}: "{}" is at line {}'.format(c.label, short, where) if where
                     else '{}: "{}" is not in the file'.format(c.label, short))
    summary = {"cited": cited, "ok": ok, "unchecked": unchecked, "flagged": len(flags)}
    if not cited:
        return summary, ("[shunt-verify: the answer cites no path:line; its line numbers "
                         "and quotes are unchecked]")
    head = "[shunt-verify: {} of {} citations match the files".format(ok, cited)
    if unchecked:
        head += ", {} had no quote to check".format(unchecked)
    if not flags:
        return summary, head + "]"
    lines = [head + "; check these before relying on them:"]
    lines += ["  ! " + flag for flag in flags[:MAX_FLAGS]]
    if len(flags) > MAX_FLAGS:
        lines.append("  ! +{} more".format(len(flags) - MAX_FLAGS))
    return summary, "\n".join(lines) + "]"
