#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""manifest.py -- which servers are those, by where they are in the building.

Usage: manifest dc.dc 'rack[1-3]'          every server in racks 1 to 3
       manifest dc.dc 'row[A,C]' +gpu      gpu servers in rows A and C
       manifest dc.dc r1u1,r1u7,r5u27      those three by name
       manifest dc.dc 'room[1]' --csv      the same, with where each one is
       manifest --sample > floor.dc        a layout to start from

Options:
      --layout FILE   the layout, if not the first argument   (MANIFEST_LAYOUT)
      --role ROLE     what counts as a server                 (MANIFEST_ROLE,
                      default server; --role any for everything)
      --path          print the full path instead of the name
      --csv           name,path,kind,role,room,row,rack,slot
      --count         print how many matched, and nothing else
      --explain       say what each selector named, on stderr
      --sort          sort the output (default: layout order)
      --quiet         no summary on stderr
      --sample        print a layout to start from, and nothing else

Exit status
  0   at least one server matched
  1   the selectors matched nothing, or matched no servers
  2   usage error, or the layout could not be read

Full manual: https://binnacle.readthedocs.io/en/latest/tools/manifest.html
"""

import argparse
import csv
import fnmatch
import io
import os
import re
import sys

VERSION = "0.8.0"
PROG = os.path.basename(sys.argv[0]) or "manifest.py"

DEFAULT_ROLE = "server"

# Attributes that describe one element and must not cascade to its children.
# Same set as the reference parser: a rack's `u=42` is its own height, not
# every machine's, and `seq=` and `if=` are about the line that carries them.
NON_INHERITED = frozenset((
    "id", "name", "at", "u", "cols", "dir", "gap", "label", "size", "seq",
    "align", "if"))

# Lines that are not elements, where the viewer reads them as such: at the
# top of the file, or under another of them.  Anywhere else the viewer
# makes an element of that kind, and so does this.
DIRECTIVES = frozenset(("net", "link", "title"))

# The placeholders every line has, which a kind of the same name never hides.
CONTEXT_NAMES = frozenset(("id", "i", "i0", "n", "seq", "kind", "parent",
                           "path"))

# The viewer's patterns, character for character: JavaScript's \d and \w
# are ASCII, its . stops at any line terminator, its $ only at the end, and
# its \s takes the Unicode spaces a Python \s leaves out.
_JS_DOT = "[^\n\r  ]"
_JS_SPACE = "\t\n\v\f\r    -     　﻿"
BRACKET_RE = re.compile(r"\[([^\]]*)\]")
PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::([0-9]+))?\}", re.ASCII)
SEQ_USE_RE = re.compile(r"\{seq(?::[0-9]+)?\}")
INT_RE = re.compile(r"-?[0-9]+\Z")
RANGE_SPEC_RE = re.compile(r"(%s+?)\.\.(%s+?)(?:x([0-9]+))?\Z" % (_JS_DOT, _JS_DOT))
BARE_RANGE_RE = re.compile(r"[^%s]+\.\.[^%s]+\Z" % (_JS_SPACE, _JS_SPACE))
ALPHABETS = (re.compile(r"[A-Z]\Z"), re.compile(r"[a-z]\Z"),
             re.compile(r"[0-9]\Z"))
LEADING_INT_RE = re.compile(r"(\d+)\s*$")

# canonical copy: binnacle/agree.py RANGE_RE.
RANGE_RE = re.compile(r"\[([^\]]+)\]")


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    Hostnames and rack labels come out of somebody's layout file, which
    is as likely to hold a non-ASCII name as any other document.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "buffer"):
            continue
        enc = (getattr(stream, "encoding", None) or "").lower()
        if enc.replace("-", "_") in ("ascii", "ansi_x3.4_1968", "us_ascii"):
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding=stream.encoding,
                errors="backslashreplace", line_buffering=True))


def die(msg, code=2):
    sys.stderr.write("%s: %s\n" % (PROG, msg))
    raise SystemExit(code)


def note(msg, quiet=False):
    if not quiet:
        sys.stderr.write("[%s] %s\n" % (PROG, msg))


def _env(name, default=None):
    v = os.environ.get("MANIFEST_" + name)
    return v if v not in (None, "") else default


# canonical copy: binnacle/agree.py.
def split_commas(spec):
    """Split on commas that are outside [brackets].

    'a,b' is two hosts, but 'node[1,3]' is one spec whose comma belongs to
    the range -- splitting it first would produce 'node[1' and '3]'.
    """
    out, depth, cur = [], 0, []
    for ch in spec:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s for s in (x.strip() for x in out) if s]


# canonical copy: binnacle/agree.py.
def expand_range(spec):
    """node[01-24] / node[1,3,5-8] / rack[a-c]-node[01-04] (cartesian).

    Every real fleet is named this way, and typing the list out by hand is
    exactly how a host gets left off the check.
    """
    m = RANGE_RE.search(spec)
    if not m:
        return [spec]
    # [::1] and [fe80::1]:22 are addresses, not ranges.  Expanding one
    # strips the brackets and glues the port back on as more colons --
    # "[::1]:2222" became the valid-looking and completely wrong
    # address "::1:2222".
    if ":" in m.group(1):
        return [spec]
    before, body, after = spec[:m.start()], m.group(1), spec[m.end():]
    items = []
    for part in body.split(","):
        part = part.strip()
        if "-" in part and not part.startswith("-"):
            a, b = part.split("-", 1)
            a, b = a.strip(), b.strip()
            if a.isdigit() and b.isdigit():
                width = len(a) if a.startswith("0") else 0
                for v in range(int(a), int(b) + 1):
                    items.append(str(v).zfill(width) if width else str(v))
                continue
            if len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha():
                for c in range(ord(a), ord(b) + 1):
                    items.append(chr(c))
                continue
            die("cannot expand range %r in %r" % (part, spec))
        else:
            items.append(part)
    out = []
    for it in items:
        out.extend(expand_range(before + it + after))
    return out


# ---------------------------------------------------------------------------
# The layout file
# ---------------------------------------------------------------------------

def dc_expand_one(body):
    """One bracket body, the layout file's own range grammar.

    `01..20`, `1..40x2`, `A..H`, `web|db|cache`, `1..4,7..10`, or a
    literal.  The viewer's expandRangeSpec, step for step: a layout it draws
    is a layout this reads, into the same names.  A body it refuses raises
    ValueError, with its words, and the line is left out as it leaves it.
    """
    # Comma segments first, so numbering with holes stays one range:
    # [1..4,7..10] is racks 1-4 and 7-10; an empty segment is nothing.
    if "," in body:
        out = []
        for part in body.split(","):
            if part != "":
                out.extend(dc_expand_one(part))
        return out
    if "|" in body:
        return body.split("|")
    m = RANGE_SPEC_RE.match(body)
    if not m:
        return [body]
    lo, hi, raw_step = m.group(1), m.group(2), m.group(3)
    # A step longer than Python will read as a number is longer than any
    # range: the viewer's parseInt makes it Infinity, and the walk stops
    # after the first value.
    step = (1 if not raw_step else int(raw_step)
            if len(raw_step.lstrip("0")) <= 4000 else float("inf"))
    if step < 1:
        raise ValueError('bad step in range "%s"' % body)
    if INT_RE.match(lo) and INT_RE.match(hi):
        a, b = int(lo), int(hi)
        # 01 pads every value to that many digits; the digits are what is
        # padded, so -05..-01 is -05 -04 ..., not five ids counting the
        # wrong way and not -5.
        digits = lo[1:] if lo.startswith("-") else lo
        width = len(digits) if re.match(r"0[0-9]", digits) else 0
        direction = 1 if b >= a else -1
        out = []
        v = a
        while (v <= b) if direction > 0 else (v >= b):
            if width:
                out.append(("-" if v < 0 else "") + str(abs(v)).zfill(width))
            else:
                out.append(str(v))
            v += direction * step
        return out
    if len(lo) == 1 and len(hi) == 1:
        # Both ends in one alphabet: A..z would otherwise walk through the
        # punctuation between Z and a, and make an element named "]".
        if not any(r.match(lo) and r.match(hi) for r in ALPHABETS):
            raise ValueError('cannot expand range "%s": %s and %s are not both '
                             'A-Z, both a-z or both digits' % (body, lo, hi))
        direction = 1 if ord(hi) >= ord(lo) else -1
        out = []
        c = ord(lo)
        while (c <= ord(hi)) if direction > 0 else (c >= ord(hi)):
            out.append(chr(c))
            c += direction * step
        return out
    raise ValueError('cannot expand range "%s"' % body)


def dc_expand(token):
    """Every [..] group in a token, cartesian across the groups."""
    if not token:
        return [""]
    if not BRACKET_RE.search(token):
        if BARE_RANGE_RE.match(token):
            return dc_expand_one(token)
        return [token]
    results = [token]
    while results and BRACKET_RE.search(results[0]):
        nxt = []
        for cur in results:
            m = BRACKET_RE.search(cur)
            if not m:
                nxt.append(cur)
                continue
            for piece in dc_expand_one(m.group(1)):
                nxt.append(cur[:m.start()] + piece + cur[m.end():])
        results = nxt
    return results


def subst(text, ctx):
    """{room}, {rack}, {id}, {i} ... resolved from enclosing elements.

    {name:W} pads a whole-number value to W digits, as a range written
    R[01..20] pads its own.  An unknown key is left as it stands, so a
    literal brace in a label survives.
    """
    if not isinstance(text, str) or "{" not in text:
        return text
    def one(m):
        key, width = m.group(1), m.group(2)
        if key not in ctx:
            return m.group(0)
        value = str(ctx[key])
        if not width or not INT_RE.match(value):
            return value
        digits = value.replace("-", "", 1)
        return ("-" if value.startswith("-") else "") + digits.zfill(int(width))
    return PLACEHOLDER_RE.sub(one, text)


def tokenize(line):
    """Words, honouring quotes and a # comment that is not inside one."""
    out, cur, quote, started = [], [], None, False
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in ('"', "'"):
            quote, started = ch, True
            continue
        if ch == "#" and not started:
            break
        if ch in (" ", "\t"):
            if started:
                out.append("".join(cur))
                cur, started = [], False
            continue
        cur.append(ch)
        started = True
    if started:
        out.append("".join(cur))
    return out


def indent_of(line):
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


class Element(object):
    __slots__ = ("kind", "id", "name", "path", "tags", "attrs", "parent",
                 "depth", "ancestors", "children")

    def __init__(self, kind, ident, name, path, tags, attrs, parent, depth):
        self.kind = kind
        self.id = ident
        self.name = name
        self.path = path
        self.tags = tags
        self.attrs = attrs
        self.parent = parent
        self.depth = depth
        self.ancestors = {}
        self.children = []

    def role(self):
        return self.attrs.get("role", "")

    def where(self, kind):
        return self.ancestors.get(kind, "")


def _js_units(text):
    """A string as JavaScript holds it: UTF-16 code units, one per char."""
    out = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            out.append(chr(0xD800 + (code >> 10)))
            out.append(chr(0xDC00 + (code & 0x3FF)))
        else:
            out.append(ch)
    return "".join(out)


def _js_fold(unit):
    """One code unit as a JavaScript /i regex without the u flag sees it.

    Upper case, unless that is not one code unit or takes a non-ASCII
    letter to an ASCII one: the long s is not s, the Kelvin sign is not k,
    and Turkish dotted I is not i -- Python's re.IGNORECASE says otherwise.
    """
    up = unit.upper()
    if len(_js_units(up)) != 1 or (ord(unit) >= 128 and ord(up) < 128):
        return unit
    return up


def _glob_match(pattern, text):
    """The viewer's globToRegExp: * and ? only, whole string, any case.

    Matched as JavaScript matches it: on UTF-16 code units, so ? is half an
    emoji, and neither * nor ? takes a line separator.
    """
    body = []
    for unit in _js_units(pattern):
        if unit == "*":
            body.append(_JS_DOT + "*")
        elif unit == "?":
            body.append(_JS_DOT)
        else:
            body.append(re.escape(_js_fold(unit)))
    folded = "".join(_js_fold(u) for u in _js_units(text))
    return re.match("(?:%s)\\Z" % "".join(body), folded) is not None


def _condition_holds(raw, ctx, warn, line):
    """Whether a line's if= holds for the element about to be made.

    `{placeholder}=value` or `!=`, joined by commas, all of which must
    hold; a value may list alternatives with | and use * and ?.  One that
    cannot be read is reported and ignored, as the viewer ignores it.
    """
    for clause in raw.split(","):
        neq = clause.find("!=")
        at = neq if neq >= 0 else clause.find("=")
        if at <= 0:
            warn(line, "if=%s: each condition is {placeholder}=value or "
                       "{placeholder}!=value, joined by commas -- ignored"
                 % raw)
            return True
        left = subst(clause[:at], ctx)
        wanted = [subst(v, ctx)
                  for v in clause[at + (2 if neq >= 0 else 1):].split("|")]
        hit = any(_glob_match(v, left) if re.search(r"[*?]", v)
                  else left.lower() == v.lower() for v in wanted)
        if hit == (neq >= 0):
            return False
    return True


def _next_seq(syn, series, parent, state):
    """The next {seq}: per block and kind, or per seq= name or scope.

    Lines of one kind side by side in one block share a count; seq=room
    gives one count per enclosing room, kept per kind of what is counted;
    any other seq= word is a count of that name, wherever it is used.
    Returns (counts, key, n) so an element not made can give n back.
    """
    if series.get("seq"):
        counts = state["named"]
        scope = parent
        while scope is not None and scope.kind != series["seq"]:
            scope = scope.parent
        key = (("scope", scope.path, syn["kind"]) if scope is not None
               else series["seq"])
    else:
        block = syn["up"] if syn["up"] is not None else syn
        counts = state["blocks"].setdefault(id(block), {})
        key = syn["kind"]
    n = counts.get(key, 0) + 1
    counts[key] = n
    return counts, key, n


def parse_layout(path, warn=None):
    """Every element the layout describes, in file order.

    The viewer's parseLayout, as far as which elements exist and what they
    are called: `net`, `link` and `title` lines at the top of the file are
    directives, several top-level lines share one `dc` root named after the
    title (`datacenter` when there is none), and a line the viewer cannot
    expand is left out with everything under it and reported, not fatal.
    `warn(line, message)` hears what was left out; by default it is
    written to stderr, once per message.
    """
    try:
        # Bytes, decoded in one go as the browser decodes them: a stray BOM
        # byte is U+FFFD, not held back, and no newline is translated --
        # only CR LF and LF end a line in the viewer, and a lone CR is a
        # character of the line it is in.
        with io.open(path, "rb") as fh:
            text = fh.read().decode("utf-8-sig", "replace")
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))
    if warn is None:
        said = set()

        def warn(line, msg):
            if (line, msg) not in said:
                said.add((line, msg))
                sys.stderr.write("%s: %s, line %d: %s\n"
                                 % (PROG, path, line, msg))

    root = {"kind": "@root", "indent": -1, "children": [], "up": None}
    stack = [root]
    deepest = [1]
    for lineno, raw in enumerate(re.split("\r?\n", text), 1):
        tokens = tokenize(raw)
        if not tokens:
            continue
        indent = indent_of(raw)
        while len(stack) > 1 and indent <= stack[-1]["indent"]:
            stack.pop()
        up = stack[-1]
        rest = tokens[1:]
        spec = None
        if rest and not rest[0].startswith("+") and "=" not in rest[0]:
            spec = rest.pop(0)
        attrs, tags = {}, []
        for tok in rest:
            if tok.startswith("+"):
                tags.extend(t for t in tok[1:].split(",") if t)
                continue
            at = tok.find("=")
            if at > 0:
                # The viewer keeps attributes in a JavaScript object, which
                # cannot hold the key __proto__: it is dropped there, so
                # here too.
                if tok[:at].lower() != "__proto__":
                    attrs[tok[:at].lower()] = tok[at + 1:]
            else:
                tags.append(tok)
        node = {"kind": tokens[0].lower(), "spec": spec, "attrs": attrs,
                "tags": tags, "children": [], "indent": indent,
                "line": lineno, "up": up}
        up["children"].append(node)
        stack.append(node)
        deepest[0] = max(deepest[0], len(stack))

    # Directives where the viewer finds them; everything else is an element.
    title = ["datacenter"]
    top = []

    def sift(node):
        for child in node["children"]:
            if child["kind"] == "title":
                title[0] = (child["spec"] or child["attrs"].get("name")
                            or title[0])
            elif child["kind"] not in DIRECTIVES:
                top.append(child)
                continue
            sift(child)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 2 * deepest[0] + 200))
    try:
        sift(root)
    finally:
        sys.setrecursionlimit(limit)
    if not top:
        return []
    # One top-level element that is one element is the root; anything else
    # is given one, as the viewer gives it.
    root_syn = {"kind": "dc", "spec": title[0], "attrs": {}, "tags": [],
                "children": top, "line": 0, "up": None}
    if len(top) == 1:
        try:
            n = len(dc_expand(top[0]["spec"])) if top[0]["spec"] else 1
        except ValueError:
            n = 1
        if n == 1:
            root_syn = top[0]
    out = []
    state = {"blocks": {}, "named": {}, "paths": set(), "warn": warn}
    # One frame per level of nesting: a layout the viewer reads 1000 deep
    # must not end here in a RecursionError.
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 2 * deepest[0] + 200))
    try:
        _materialize(root_syn, None, out, state)
    finally:
        sys.setrecursionlimit(limit)
    return out


def _materialize(syn, parent, out, state):
    warn = state["warn"]
    base = {}
    if parent is not None:
        chain = []
        p = parent
        while p is not None:
            chain.append(p)
            p = p.parent
        for anc in reversed(chain):
            base[anc.kind] = anc.id
        base["parent"] = parent.id
        base["path"] = parent.path
    kind = syn["kind"]
    spec = subst(syn["spec"], base) if syn["spec"] else None
    try:
        ids = (dc_expand(spec) if spec else
               ["%s%d" % (kind, (len(parent.children) if parent is not None
                                 else 0) + 1)])
    except ValueError as exc:
        warn(syn["line"], "%s -- nothing was created for this line or the "
                          "lines under it" % exc)
        return
    if not ids:
        warn(syn["line"], '%s "%s" expands to no ids -- nothing was created '
                          "for this line or the lines under it"
             % (kind, syn["spec"]))
        return
    uses_seq = (any(SEQ_USE_RE.search(v) for v in syn["attrs"].values())
                or any(SEQ_USE_RE.search(t) for t in syn["tags"]))
    dynamic = any("{" in v for v in syn["attrs"].values())
    tagged = any("{" in t for t in syn["tags"])

    for i, raw_id in enumerate(ids):
        # {rack} on a rack's own line is the rack itself; the built-in
        # names win over a kind that shares one.
        ctx = dict(base)
        # (A kind called __proto__ has no own placeholder in the viewer --
        # JavaScript will not set that key -- unless an ancestor gave it
        # one.)
        if kind not in CONTEXT_NAMES and (kind != "__proto__"
                                          or "__proto__" in base):
            ctx[kind] = raw_id
        ctx.update({"id": raw_id, "i": i + 1, "i0": i, "n": len(ids),
                    "seq": 0, "kind": kind})
        numbered = None
        if uses_seq:
            series = ({"seq": subst(syn["attrs"]["seq"], ctx)}
                      if "seq" in syn["attrs"] else {})
            numbered = _next_seq(syn, series, parent, state)
            ctx["seq"] = numbered[2]
        own = syn["attrs"]
        if dynamic:
            own = dict((k, subst(v, ctx)) for k, v in own.items())
        # if= decides whether the element is made; it is not one of its
        # attributes, and an element not made gives its {seq} back.
        if "if" in syn["attrs"]:
            if not _condition_holds(syn["attrs"]["if"], ctx, warn,
                                    syn["line"]):
                if numbered:
                    counts, key, n = numbered
                    counts[key] = n - 1
                continue
            own = dict(own)
            own.pop("if", None)
        tags = [subst(t, ctx) for t in syn["tags"]] if tagged else syn["tags"]
        ident = own.get("id") or raw_id

        path = "%s/%s" % (parent.path, ident) if parent is not None else ident
        if path in state["paths"]:
            n = 2
            while "%s#%d" % (path, n) in state["paths"]:
                n += 1
            warn(syn["line"], 'duplicate path "%s" renamed to "%s#%d"'
                 % (path, path, n))
            path = "%s#%d" % (path, n)
        state["paths"].add(path)

        attrs = {}
        if parent is not None:
            # The child sees the parent's attributes, minus the ones that
            # describe the parent alone.
            attrs.update(parent.attrs)
            for key in NON_INHERITED:
                attrs.pop(key, None)
        attrs.update(own)
        all_tags = set(parent.tags) if parent is not None else set()
        for tag in tags:
            all_tags.add(tag.lower())
        el = Element(kind, ident, own.get("name") or ident, path, all_tags,
                     attrs, parent, 0 if parent is None else parent.depth + 1)
        if parent is not None:
            el.ancestors = dict(parent.ancestors)
            el.ancestors[parent.kind] = parent.id
            parent.children.append(el)
        out.append(el)
        for child in syn["children"]:
            _materialize(child, el, out, state)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

def _num_of(text):
    """The trailing number in an id, or None.  R01 -> 1, wr12 -> 12."""
    m = LEADING_INT_RE.search(text or "")
    return int(m.group(1)) if m else None


TRAILING_DIGITS_RE = re.compile(r"\d+\s*$")
GLOB_CHARS_RE = re.compile(r"[*?\[]")

# A range in a selector is a filter, not the list of things to create, so
# it is bounded by the size of the building rather than by patience. The
# largest published layout is a quarter of a million elements; a range an
# order of magnitude past that is a typed zero too many, and expanding it
# would eat the memory before saying so.
MAX_RANGE = 2000000


def _range_size(body):
    """Roughly what `body` will expand to, worked out before expanding it."""
    total = 0
    for part in body.split(","):
        m = re.match(r"^(\d+)-(\d+)$", part.strip())
        total += max(1, int(m.group(2)) - int(m.group(1)) + 1) if m else 1
    return total


class LooseIds(object):
    """Ids that answered to a bare number, kept so the answer can say so.

    `rack[1]` is `r01`, and it is also `g01` in the GPU room -- a bare
    number ignores the letters in front of it.  That is deliberate and
    usually what you want; it is only a trap when it reaches into a
    container you were not thinking about, which is exactly the case
    where more than one prefix answers.
    """

    __slots__ = ("kind", "term", "by_prefix")

    def __init__(self, term, kind):
        self.term = term
        self.kind = kind
        self.by_prefix = {}

    def record(self, ident):
        prefix = TRAILING_DIGITS_RE.sub("", ident or "")
        self.by_prefix.setdefault(prefix, ident)

    def crossed(self):
        """Did one number answer to ids spelled more than one way?"""
        return len(self.by_prefix) > 1

    def examples(self):
        return [self.by_prefix[k] for k in sorted(self.by_prefix)]


def id_index(wanted, loose=None):
    """The wanted ids, arranged so an element is a lookup rather than a scan.

    Matching is forgiving, because a rack called `R01` is the one you
    mean when you type `rack[1]` and having to remember the zero padding
    would make the tool useless from memory.  An id answers to a want if
    it is the same string ignoring case, or the same number once padding
    and any leading letters are ignored -- but only where the non-numeric
    part does not disagree, so `R01` answers to `1` and to `R1` and not
    to `U1` -- or if the want is a glob that matches it.

    Forgiveness has one cost, and it used to be silent: a bare number
    ignores the letters altogether, so `rack[1]` names `r01` and `g01`
    both.  Those matches are recorded in `loose` so the caller can say
    which spellings answered -- quietly returning a second room's worth
    of machines is what makes this tool feel unpredictable.

    Written as an index rather than as a comparison because the range is
    a filter over the whole building: `rack[1-40]` against a quarter of
    a million machines is ten million comparisons done one at a time,
    each of them through two regexes.
    """
    exact, by_prefix, unprefixed, globs = set(), {}, set(), []
    for want in wanted:
        low = want.lower()
        exact.add(low)
        num = _num_of(want)
        if num is not None:
            prefix = TRAILING_DIGITS_RE.sub("", low)
            if prefix:
                by_prefix.setdefault(prefix, set()).add(num)
            else:
                # A bare number answers to any prefix: 1 finds R01 and u01.
                unprefixed.add(num)
        # A want with no glob character matches exactly what `exact`
        # already holds, so only the real patterns need the slow path.
        if GLOB_CHARS_RE.search(low):
            globs.append(low)

    def hit(ident):
        ident = (ident or "").lower()
        if ident in exact:
            return True
        num = _num_of(ident)
        if num is not None:
            if num in unprefixed:
                if loose is not None:
                    loose.record(ident)
                return True
            if num in by_prefix.get(TRAILING_DIGITS_RE.sub("", ident), ()):
                return True
        for pattern in globs:
            if fnmatch.fnmatch(ident, pattern):
                return True
        return False
    return hit


def _plural_kinds(kind, kinds):
    """`racks[1-3]` is the same question as `rack[1-3]`."""
    if kind in kinds:
        return kind
    if kind.endswith("s") and kind[:-1] in kinds:
        return kind[:-1]
    if kind + "s" in kinds:
        return kind + "s"
    return kind


def compile_term(term, kinds, unknown, loose=None):
    """One OR-branch of a selector, as a predicate over elements.

    A kind this layout has never heard of is recorded in `unknown` rather
    than refused: `rack[1],cage[2]` is a reasonable thing to type against
    a floor plan that might have either, and the caller says so only if
    the whole question came back empty.

    `loose`, where given, collects the bare-number matches this term made,
    so an answer that reached further than it looked can say which ids
    answered.
    """
    if term.startswith("+"):
        tag = term[1:].lower()
        return lambda el: tag in el.tags

    m = re.match(r"^([A-Za-z_][\w-]*)\[(.+)\]$", term)
    if m:
        kind = _plural_kinds(m.group(1).lower(), kinds)
        if kind not in kinds:
            unknown.add(m.group(1))
        # Both spellings: rack[1-3] as this package writes ranges, and
        # rack[1..3] as the layout file does.
        body = m.group(2).replace("..", "-")
        if _range_size(body) > MAX_RANGE:
            die("%s expands to more ids than any building holds -- a zero "
                "too many?" % term)
        seen = LooseIds(term, m.group(1))
        if loose is not None:
            loose.append(seen)
        hit = id_index(expand_range("[" + body + "]"), seen)
        return lambda el: el.kind.lower() == kind and hit(el.id)

    at = term.find("=")
    if at > 0:
        key, want = term[:at].lower(), term[at + 1:]
        if key == "kind":
            return lambda el: fnmatch.fnmatch(el.kind.lower(), want.lower())
        return lambda el: fnmatch.fnmatch(
            str(el.attrs.get(key, "")).lower(), want.lower())

    low = term.lower()
    if "/" in term:
        return lambda el: (el.path.lower() == low
                           or el.path.lower().endswith("/" + low)
                           or fnmatch.fnmatch(el.path.lower(), low))

    def bare(el):
        for field in (el.name, el.id, el.path):
            f = (field or "").lower()
            if f == low or fnmatch.fnmatch(f, low):
                return True
        return el.path.lower().endswith("/" + low)
    return bare


class Selector(object):
    """One command-line argument, as a question you can ask an element.

    Calling it answers "is this machine in the answer"; `names` answers
    the narrower "did this element itself satisfy a term", which is what
    `--explain` needs in order to show what a selector actually picked
    out rather than only how many machines came back.
    """

    __slots__ = ("text", "negate", "preds", "loose", "cache")

    def __init__(self, text, kinds, unknown):
        self.text = text
        self.negate = text.startswith("!")
        body = text[1:] if self.negate else text
        terms = split_commas(body)
        if not terms:
            die("empty selector")
        self.loose = []
        self.preds = [compile_term(t, kinds, unknown, self.loose)
                      for t in terms]
        # One answer per element, kept because the walk below asks the
        # same question of the same rack once for every machine in it.
        self.cache = {}

    def names(self, el):
        """Does this element itself satisfy a term, ignoring its ancestors?"""
        return any(p(el) for p in self.preds)

    def __call__(self, el):
        # A selector that names a container names everything under it:
        # `rack[1-3]` is the servers in those racks, not the three rack
        # elements, and filtering those by role=server would leave
        # nothing at all. So a term is tried against the element and then
        # against each of its ancestors.
        cache = self.cache
        node, chain = el, []
        while node is not None:
            if node in cache:
                hit = cache[node]
                break
            chain.append(node)
            if self.names(node):
                hit = True
                break
            node = node.parent
        else:
            hit = False
        for node in chain:
            cache[node] = hit
        return (not hit) if self.negate else hit

    def ambiguous(self):
        """The bare-number matches that answered to more than one spelling."""
        return [lo for lo in self.loose if lo.crossed()]


def compile_selector(text, kinds, unknown):
    """One argument: `!` negates the whole thing, top-level commas are OR."""
    return Selector(text, kinds, unknown)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CSV_FIELDS = ["name", "path", "kind", "role", "room", "row", "rack", "slot"]


def _has_children(el, elements):
    # Cheap enough: only reached for layouts with no roles at all, which
    # are the small hand-written ones.
    return any(x.parent is el for x in elements)


def row_of(el):
    return {
        "name": el.name,
        "path": el.path,
        "kind": el.kind,
        "role": el.role(),
        "room": el.where("room"),
        "row": el.where("row"),
        "rack": el.where("rack"),
        # Where in the rack.  A layout that pins a node writes at=42;
        # one that names its nodes u01..u40 has already said it in the
        # id, and an empty column for every machine in the fleet would
        # be the wrong answer to give.
        "slot": el.attrs.get("at") or el.id or "",
    }


# ---------------------------------------------------------------------------
# Showing the work
# ---------------------------------------------------------------------------

# Enough to see what a selector picked out; a rack list is the point, not
# a second copy of the output.
EXPLAIN_MAX = 8


def explain_steps(layout, elements, steps, servers, want):
    """What each selector named, and what it left -- on stderr.

    A count that surprises you is the normal way this tool goes wrong,
    and working out why used to mean re-running it with --csv and an awk
    over the room column.  This says it directly: which elements each
    selector actually named, and how many were still standing after it.
    """
    w = sys.stderr.write
    w("[%s] explain: %s, %d element(s), role=%s\n"
      % (PROG, layout, len(elements), want or "any"))
    if not steps:
        w("  no selectors -- every machine in the layout\n")
    for i, (sel, before, named, after) in enumerate(steps, 1):
        w("  %d. %s\n" % (i, sel.text))
        w("       %s %d element(s); %d -> %d element(s)\n"
          % ("excludes" if sel.negate else "names",
             len(named), before, after))
        if not named:
            w("       nothing carries that -- the answer is empty here\n")
        # Only the ids that reached across spellings are flagged: a bare
        # number matching r01 is the forgiveness working, not a surprise.
        loose = set()
        for lo in sel.ambiguous():
            loose.update(v.lower() for v in lo.by_prefix.values())
        for el in named[:EXPLAIN_MAX]:
            w("       %-5s %-8s %s%s\n"
              % (el.kind, el.id, el.path,
                 "   <- by number, not by name" if el.id.lower() in loose
                 else ""))
        if len(named) > EXPLAIN_MAX:
            w("       ... and %d more\n" % (len(named) - EXPLAIN_MAX))
    w("  = %d server(s)\n" % len(servers))


def ambiguity_notes(steps, quiet):
    """Say when one bare number answered to ids spelled more than one way.

    `room[1]` is room wr01, and it is also room gpu1, because a bare
    number ignores the letters.  Answering with both and saying nothing
    is how a fan-out reaches a room nobody meant to touch.
    """
    for sel, _, _, _ in steps:
        for lo in sel.ambiguous():
            ex = lo.examples()[:4]
            spelled = " or ".join("%s[%s]" % (lo.kind, e) for e in ex)
            note("%s matched more than one spelling of that id (%s) -- a "
                 "bare number ignores the letters in front of it; %s, or a "
                 "path selector, picks one out"
                 % (lo.term, ", ".join(ex), spelled), quiet)


# ---------------------------------------------------------------------------
# A layout to start from
# ---------------------------------------------------------------------------

# Written here rather than shipped as a data file, because a tool that is
# routinely scp'd to a bare machine on its own has to carry everything it
# needs inside the one file.  It is also original rather than copied from
# the reference implementation's examples: that project ships no licence,
# and this one is GPL and REUSE-compliant.
#
# Every construct this parser understands appears at least once, and the
# comments say what each line is doing -- so `manifest --sample > floor.dc`
# is both a worked example and a file you can edit into your own floor.
SAMPLE_LAYOUT = """\
# A small site, in the .dc format manifest reads.
#
#   <kind> <id-spec> [key=value ...] [+tag ...]
#
# Indentation nests: a rack indented under a row is in that row.  An
# id-spec in brackets expands, so one line describes a whole floor.

dc IAD1 name="Ashburn 1" region=us-east +prod
                                  # attrs and tags inherit downward, so
                                  # every machine below is +prod, us-east

  room wr[01..02] +hall           # 01..02 is a range: two rooms

    row A                         # rack numbers run on across the rows so
      rack r[01..04] u=42         # the flat name below stays unique
        node tor at=42 role=tor +switch name={room}{rack}tor
        node u[01..20] role=server name={room}{rack}{id} +x86 model=r7625

    row B                         # ...continuing r05..r08 here
      rack r[05..08] u=42
        node tor at=42 role=tor +switch name={room}{rack}tor
        node u[01..20] role=server name={room}{rack}{id} +x86 model=r7625

  room gpu1 +hall +gpu            # a room named outright, not from a range
    row A
      rack g[01..02] u=48
        node tor at=48 role=tor +switch name={room}{rack}tor
        node u[01..08x2] role=server name={room}{rack}{id} model=hgx-h100
                                  # 01..08x2 steps by two: u01 u03 u05 u07,
                                  # for chassis that take two U each

# net and link describe cabling rather than machines.  manifest reads past
# them; the viewer draws them.
net data label="Data" color=#4fa3ff
net mgmt label="Mgmt" color=#8b93a7 style=dashed

link data role=server role=tor scope=rack
link mgmt role=server role=tor scope=rack

# 328 servers and 18 switches.  Things to try against it:
#
#   manifest floor.dc 'rack[1-4]'        row A of both halls
#   manifest floor.dc 'room[gpu1]'       the gpu room
#   manifest floor.dc +gpu               the same, by inherited tag
#   manifest floor.dc 'model=hgx*'       by attribute, globbed
#   manifest floor.dc 'room[1]' --csv    with where each machine is
#   manifest floor.dc --role tor         the switches instead
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("args", nargs="*", metavar="LAYOUT SELECTOR",
                   help="the layout file, then one or more selectors")
    p.add_argument("--layout", default=_env("LAYOUT"))
    p.add_argument("--role", default=_env("ROLE", DEFAULT_ROLE))
    p.add_argument("--path", action="store_true")
    p.add_argument("--csv", action="store_true")
    p.add_argument("--count", action="store_true")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--sort", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--sample", action="store_true")
    return p


def main(argv=None):
    _stdio_safe()
    args = build_parser().parse_args(argv)

    if args.sample:
        # Before the layout is looked for: the whole point is to have one
        # when you do not yet.
        sys.stdout.write(SAMPLE_LAYOUT)
        return 0

    rest = list(args.args)
    layout = args.layout
    if layout is None:
        if not rest:
            die("which layout? give a .dc file, or set MANIFEST_LAYOUT")
        layout = rest.pop(0)
    if os.path.isdir(layout):
        # A directory reads as zero bytes rather than as an error, and
        # "describes no elements" would send someone looking for the bug
        # in their layout.
        die("%s is a directory, not a layout file" % layout)
    if not os.path.isfile(layout):
        die("no such layout file: %s" % layout)

    elements = parse_layout(layout)
    if not elements:
        die("%s describes no elements" % layout, 1)
    kinds = set(el.kind.lower() for el in elements)

    picked, unknown = elements, set()
    steps = []
    for text in rest:
        sel = compile_selector(text, kinds, unknown)
        before = len(picked)
        # What the selector named in its own right, kept only when it is
        # going to be shown: it is a second pass over the elements.
        named = [el for el in picked if sel.names(el)] if args.explain else []
        picked = [el for el in picked if sel(el)]
        steps.append((sel, before, named, len(picked)))

    want = (args.role or "").lower()
    if want in ("any", "all", ""):
        servers = list(picked)
    elif any(el.role() for el in elements):
        servers = [el for el in picked if el.role().lower() == want]
    else:
        # The layout sets no roles at all, so filtering by one would
        # answer nothing however the question was asked. Fall back to the
        # machines: leaf elements of kind `node`.
        servers = [el for el in picked
                   if el.kind.lower() == "node" and not _has_children(el, elements)]
        note("%s sets no role= anywhere; treating leaf nodes as the "
             "machines" % os.path.basename(layout), args.quiet)

    if args.sort:
        servers.sort(key=lambda el: (el.name, el.path))

    if args.explain:
        # Asked for outright, so --quiet does not silence it.
        explain_steps(layout, elements, steps, servers, want)
    ambiguity_notes(steps, args.quiet)

    if args.count:
        sys.stdout.write("%d\n" % len(servers))
        return 0 if servers else 1

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS,
                                lineterminator="\n")
        writer.writeheader()
        for el in servers:
            writer.writerow(row_of(el))
    else:
        out = []
        for el in servers:
            out.append(el.path if args.path else el.name)
        if out:
            sys.stdout.write("\n".join(out) + "\n")

    if not servers:
        if picked:
            note("%d element(s) matched, none of them role=%s -- --role any "
                 "prints them all" % (len(picked), want), args.quiet)
        elif unknown:
            # The layout is the authority on what kinds exist, so say
            # which ones rather than leaving someone to guess at the
            # spelling of a word this file has never contained.
            note("no %s in %s -- it has %s"
                 % (", ".join(sorted(unknown)), layout,
                    ", ".join(sorted(kinds))), args.quiet)
        else:
            note("nothing matched in %s" % layout, args.quiet)
        return 1
    note("%d of %d element(s)" % (len(servers), len(elements)), args.quiet)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
        # `manifest ... | head` is a normal way to use this.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)

# ---------------------------------------------------------------------------
# Why this file has no imports from its siblings
# ---------------------------------------------------------------------------
#
# Every module in binnacle is a complete, standalone program: standard
# library only, and nothing imported from the rest of the package. That is
# not tidiness, it is a requirement. These files get copied to machines that
# have never heard of binnacle -- `netmesh` scp's itself to every host in the
# mesh, and `agree script ./why_slow.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
