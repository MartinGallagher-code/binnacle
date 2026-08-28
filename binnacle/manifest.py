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
      --sort          sort the output (default: layout order)
      --quiet         no summary on stderr
      --sample        print a layout to start from, and nothing else

What it does
  A fleet list is a datacenter fact, but it is usually kept as a text file
  somebody maintains by hand -- which is why it is always slightly wrong.
  The building already knows: rooms hold rows, rows hold racks, racks hold
  machines.  This reads that description and prints the machines a
  question picks out, one per line, which is the shape every other tool
  here already takes.

      manifest dc.dc 'rack[1-3]' | muster add -
      manifest dc.dc 'row[A,C]' > hosts.txt && agree --hosts hosts.txt
      manifest dc.dc 'room[1]' | netmesh gen --servers -

  It reads and prints.  Nothing is contacted, nothing is written, and the
  layout file is never modified.

The layout file
  The `.dc` format from the layout_visualizer project: indentation nests,
  ranges expand, and one line can describe a whole floor.

      dc IAD1
        room DH1
          row A..D
            rack R[01..06] u=42
              node tor at=42 role=tor +switch
              node u[01..20] role=server +x86 model=r7625

  Five lines, 504 elements.  `name={room}{rack}{id}` gives every machine a flat
  hostname like `wr12r06u15`; where a name is set, that is what this
  prints.  Attributes and tags inherit downward, so a `+prod` on the room
  is carried by every machine under it.

  `--sample` prints a commented layout using every construct understood
  here, which is the fastest way to see the format and a reasonable file
  to edit into your own floor.

  That project is the format's reference implementation -- see its README
  for the full grammar.  What is understood here is the element half:
  `net` and `link` lines describe cabling and are read past.

Selectors
  Each argument is one selector, and they are ANDed: an element has to
  satisfy all of them.  Inside one selector, top-level commas are OR.

  A selector names elements; the answer is the machines at or under
  them.  That one rule is what makes `rack[1-3]` mean what you expect: a
  rack is not a server, so matching the rack elements and then filtering
  for role=server would leave nothing at all.

      rack[1-3]        kind and id: racks 1, 2 and 3
      row[A,C]         rows A and C
      room[1]          room 1
      r1u1,r1u7        either of those two, by name
      +gpu             carries the tag, including inherited
      role=tor         an attribute, globs allowed: model=r76*
      DH1/A/R01/u05    a path, or any suffix of one -- which
                       may name more than one element
      wr01r03*         a glob against name, id or path
      !+decom          negated

  An id is matched forgivingly, because a rack called `R01` is the rack
  you mean when you type `rack[1]`: exact first, then numerically with
  padding and any leading letters ignored, then as a glob.  Both range
  spellings work -- `rack[1-3]` as the rest of this package writes them,
  and `rack[1..3]` as the layout file does.

  A plural kind is accepted where the singular exists, so `racks[1-3]`
  and `rack[1-3]` are the same question.  A kind this layout does not
  have is named -- "nothing matched" would send you off checking rack
  numbers when the word in front of the bracket was the problem.

Exit status
  0   at least one server matched
  1   the selectors matched nothing, or matched no servers
  2   usage error, or the layout could not be read
"""

import argparse
import csv
import fnmatch
import io
import os
import re
import sys

VERSION = "0.6.0"
PROG = os.path.basename(sys.argv[0]) or "manifest.py"

DEFAULT_ROLE = "server"

# Attributes that describe one element and must not cascade to its children.
# Same set as the reference parser: a rack's `u=42` is its own height, not
# every machine's.
NON_INHERITED = frozenset((
    "id", "name", "at", "u", "cols", "dir", "gap", "label", "size"))

# Lines that are not elements.
DIRECTIVES = frozenset(("net", "link"))

BRACKET_RE = re.compile(r"\[([^\]]*)\]")
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
INT_RE = re.compile(r"^-?\d+$")
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

    `01..20`, `1..40x2`, `A..H`, `web|db|cache`, or a literal.  Kept
    separate from expand_range above: that one is how the rest of this
    package writes a range on a command line, this one is how the layout
    file writes it, and they are not the same spelling.
    """
    if "|" in body:
        return body.split("|")
    m = re.match(r"^(.+?)\.\.(.+?)(?:x(\d+))?$", body)
    if not m:
        return [body]
    lo, hi, raw_step = m.group(1), m.group(2), m.group(3)
    step = int(raw_step) if raw_step else 1
    if step < 1:
        die("bad step in range %r" % body)
    if INT_RE.match(lo) and INT_RE.match(hi):
        a, b = int(lo), int(hi)
        # zfill keeps the sign where there is one, which abs() did not:
        # 05 pads to 05, -5 pads to -05.  Same spelling as expand_range.
        width = len(lo) if lo.startswith("0") else 0
        direction = 1 if b >= a else -1
        out = []
        v = a
        while (v <= b) if direction > 0 else (v >= b):
            out.append(str(v).zfill(width) if width else str(v))
            v += direction * step
        return out
    if len(lo) == 1 and len(hi) == 1:
        direction = 1 if ord(hi) >= ord(lo) else -1
        out = []
        c = ord(lo)
        while (c <= ord(hi)) if direction > 0 else (c >= ord(hi)):
            out.append(chr(c))
            c += direction * step
        return out
    die("cannot expand range %r" % body)


def dc_expand(token):
    """Every [..] group in a token, cartesian across the groups."""
    if not token:
        return [""]
    if not BRACKET_RE.search(token):
        if re.match(r"^\S+\.\.\S+$", token):
            return dc_expand_one(token)
        return [token]
    results = [token]
    while BRACKET_RE.search(results[0]):
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

    An unknown key is left as it stands, so a literal brace in a label
    survives.
    """
    if not isinstance(text, str) or "{" not in text:
        return text
    def one(m):
        key = m.group(1)
        return str(ctx[key]) if key in ctx else m.group(0)
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
                 "depth", "ancestors")

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

    def role(self):
        return self.attrs.get("role", "")

    def where(self, kind):
        return self.ancestors.get(kind, "")


def parse_layout(path):
    """Every element the layout describes, in file order.

    `net` and `link` lines are cabling rules rather than elements and are
    read past -- this tool answers "which machines", not "which cables".
    """
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))

    # (indent, kind, id_spec, attrs, tags, children)
    root = {"indent": -1, "children": []}
    stack = [root]
    for lineno, raw in enumerate(lines, 1):
        tokens = tokenize(raw)
        if not tokens:
            continue
        kind = tokens[0]
        if kind in DIRECTIVES:
            continue
        indent = indent_of(raw)
        rest = tokens[1:]
        id_spec = None
        if rest and not rest[0].startswith("+") and "=" not in rest[0]:
            id_spec = rest.pop(0)
        attrs, tags = {}, []
        for tok in rest:
            if tok.startswith("+"):
                tags.extend(t for t in tok[1:].split(",") if t)
                continue
            at = tok.find("=")
            if at > 0:
                attrs[tok[:at].lower()] = tok[at + 1:]
            else:
                tags.append(tok)
        node = {"indent": indent, "kind": kind, "spec": id_spec,
                "attrs": attrs, "tags": tags, "children": [], "line": lineno}
        while len(stack) > 1 and stack[-1]["indent"] >= indent:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)

    out = []
    seen = {}
    for child in root["children"]:
        _materialize(child, None, out, seen)
    return out


def _materialize(node, parent, out, seen):
    ctx = {}
    if parent is not None:
        chain = []
        p = parent
        while p is not None:
            chain.append(p)
            p = p.parent
        for anc in reversed(chain):
            ctx[anc.kind] = anc.id
        ctx["parent"] = parent.id

    spec = subst(node["spec"], ctx) if node["spec"] else None
    ids = dc_expand(spec) if spec else ["%s%d" % (node["kind"], 1)]

    for i, ident in enumerate(ids):
        local = dict(ctx)
        local.update({"id": ident, "i": i + 1, "i0": i, "n": len(ids),
                      "kind": node["kind"]})
        attrs = {}
        if parent is not None:
            # The child sees the parent's attributes, minus the ones that
            # describe the parent alone: a rack's u=42 is its own height,
            # not every machine's.
            attrs.update(parent.attrs)
            for key in NON_INHERITED:
                attrs.pop(key, None)
        for key, value in node["attrs"].items():
            attrs[key] = subst(value, local)

        tags = set(parent.tags) if parent is not None else set()
        for tag in node["tags"]:
            tags.add(subst(tag, local).lower())

        path = "%s/%s" % (parent.path, ident) if parent is not None else ident
        if path in seen:
            seen[path] += 1
            path = "%s#%d" % (path, seen[path])
        else:
            seen[path] = 1

        name = attrs.get("name") or ident
        el = Element(node["kind"], ident, name, path, tags, attrs,
                     parent, 0 if parent is None else parent.depth + 1)
        if parent is not None:
            el.ancestors = dict(parent.ancestors)
            el.ancestors[parent.kind] = parent.id
        out.append(el)
        for child in node["children"]:
            _materialize(child, el, out, seen)


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


def id_index(wanted):
    """The wanted ids, arranged so an element is a lookup rather than a scan.

    Matching is forgiving, because a rack called `R01` is the one you
    mean when you type `rack[1]` and having to remember the zero padding
    would make the tool useless from memory.  An id answers to a want if
    it is the same string ignoring case, or the same number once padding
    and any leading letters are ignored -- but only where the non-numeric
    part does not disagree, so `R01` answers to `1` and to `R1` and not
    to `U1` -- or if the want is a glob that matches it.

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


def compile_term(term, kinds, unknown):
    """One OR-branch of a selector, as a predicate over elements.

    A kind this layout has never heard of is recorded in `unknown` rather
    than refused: `rack[1],cage[2]` is a reasonable thing to type against
    a floor plan that might have either, and the caller says so only if
    the whole question came back empty.
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
        hit = id_index(expand_range("[" + body + "]"))
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


def compile_selector(text, kinds, unknown):
    """One argument: `!` negates the whole thing, top-level commas are OR."""
    negate = text.startswith("!")
    if negate:
        text = text[1:]
    terms = split_commas(text)
    if not terms:
        die("empty selector")
    preds = [compile_term(t, kinds, unknown) for t in terms]

    # One answer per element, kept because the walk below asks the same
    # question of the same rack once for every machine in it.
    cache = {}

    def match(el):
        # A selector that names a container names everything under it:
        # `rack[1-3]` is the servers in those racks, not the three rack
        # elements, and filtering those by role=server would leave
        # nothing at all. So a term is tried against the element and then
        # against each of its ancestors.
        node, chain = el, []
        while node is not None:
            if node in cache:
                hit = cache[node]
                break
            chain.append(node)
            if any(p(node) for p in preds):
                hit = True
                break
            node = node.parent
        else:
            hit = False
        for node in chain:
            cache[node] = hit
        return (not hit) if negate else hit
    return match


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
    for text in rest:
        match = compile_selector(text, kinds, unknown)
        picked = [el for el in picked if match(el)]

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
