#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""reckon.py -- what should this run have reached, and where did it fall short?

Usage: reckon floor.dc --mx reports/               an mx run against the hardware
       reckon floor.dc --iperf iperf_overlay.tsv   an iperf run against it
       reckon --nic-gbps 25 --mx reports/          no layout: the NICs alone
       reckon floor.dc --mx reports/ --idle idle/  add queueing, from netmesh
       reckon floor.dc --mx reports/ --overlay r.tsv   paint it on the floor
       reckon --rules                              every rule and its thresholds
       reckon --explain RULE_ID                    why one rule exists

The layout says what was built -- nic_gbps= on the servers, uplinks= and
uplink_gbps= on each rack -- and the run says what was achieved.  Every
flow is expected to reach its fair share of the links it crosses, or its
target if that is lower, and every gap between the two is diagnosed.

Inputs:
      --layout FILE      the .dc layout, if not the first argument
                                                       (RECKON_LAYOUT)
      --mx PATH...       matrix_orchestrator reports: reports/ or its CSVs
      --iperf PATH       an iperf-orchestrator export-overlay file, or a
                         run directory holding iperf_results.csv
      --iperf-mode MODE  parallel | sequential-host | sequential-pair, for
                         an iperf input that does not say (RECKON_IPERF_MODE)
      --idle PATH...     netmesh reports from an idle run: the latency floor
      --speeds FILE      link speeds as the hosts report them, host and
                         Mb/s per line -- a dredge --tsv table works
      --names FILE       measured-name layout-name per line, where they differ
      --nic-gbps G       NIC speed for hosts the layout gives none
                                                       (RECKON_NIC_GBPS)
      --group-kind KIND  the container whose uplinks its servers share
                                         (RECKON_GROUP_KIND, default rack)

Judgement:
      --window S         seconds of mx history, 0 for all  (RECKON_WINDOW, 60)
      --short PCT        below this % of expected is short  (RECKON_SHORT, 90)
      --fail PCT         below this % is a failure          (RECKON_FAIL, 50)
      --bloat F          loaded RTT this many times idle is a queue
                                                        (RECKON_BLOAT, 4)
      --mtu N            MTU where the layout and netmesh say nothing
                                                        (RECKON_MTU, 1500)

Output:
      --top N            worst hosts and flows to list     (RECKON_TOP, 10)
      --overlay [PATH]   results for the datacenter viewer
      --prefix P         overlay test-name prefix  (RECKON_PREFIX, reckon_)
      --run LABEL        tag every overlay sample run=LABEL   (RECKON_RUN)
      --flows [PATH]     one row per flow, as CSV
      --csv [PATH]       findings as CSV, same shape as why-slow's
      --json [PATH]      hardware, assumptions, flows, hosts and findings
      --min-severity L   info | warn | critical -- hide findings below L
      --all              also list the rules that were skipped, and why
      --quiet            the verdict and the findings, nothing else
      --exit-code        exit 0 ok / 10 warn / 20 critical
      --no-color         plain output (also honours NO_COLOR)

Exit status
  0   compared, whatever was found (see --exit-code)
  1   nothing to compare: no flow has both a measurement and an expectation
  2   usage error, or an input could not be read

Full manual: https://binnacle.readthedocs.io/en/latest/tools/reckon.html
"""

import argparse
import csv
import io
import json
import math
import os
import re
import shlex
import sys
from collections import namedtuple

VERSION = "0.8.0"
PROG = os.path.basename(sys.argv[0]) or "reckon.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

# Per-packet overhead on the wire for UDP over Ethernet: preamble+SFD 8,
# MAC header 14, FCS 4, inter-frame gap 12, IPv4 20, UDP 8.  The figure
# matrix_orchestrator itself converts packets to wire bits with, so an
# expectation here and a Gb/s there are the same arithmetic.
UDP_WIRE_OVERHEAD = 66

# TCP: the Ethernet framing around an IP packet (preamble+SFD 8, MAC 14,
# FCS 4, IFG 12), and the headers inside it that are not payload (IPv4 20,
# TCP 20, and the 12-byte timestamp option Linux sends by default).  At a
# 1500 MTU that makes goodput 1448/1538 of the wire, 94.1%: a 25 Gb/s NIC
# that iperf shows at 23.5 Gb/s is at line rate, not 6% short of it.
ETH_FRAMING = 38
TCP_HEADERS = 52

DEFAULT_WINDOW = 60
DEFAULT_SHORT = 90.0
DEFAULT_FAIL = 50.0
DEFAULT_BLOAT = 4.0          # netmesh's own --bloat-factor
DEFAULT_MTU = 1500
DEFAULT_TOP = 10
DEFAULT_PREFIX = "reckon_"
DEFAULT_GROUP_KIND = "rack"

# A flow this far above its expected rate carried more than the declared
# hardware can.  Five per cent rather than one: the achieved rate is a mean
# over intervals whose edges do not line up with the agents' counters, and
# a flow at its line rate can read a point or two over it in a window.
ABOVE_TOLERANCE = 1.05

# The lines matrix_orchestrator's own summary draws for a CPU-limited agent
# (busiest core 85%, busiest worker 75% of a core), and during's for a
# receive path pinned in softirq.  Reusing them keeps one tool from calling
# a host CPU-bound that the tool which measured it called fine.
CPU_CORE_BOUND = 85.0
AGENT_CPU_BOUND = 75.0
SOFTIRQ_BOUND = 90.0

# mx summarize's loss line: below it, loss is noise on a busy fabric.
LOSS_PCT = 1.0

# RTT growth smaller than this is not a queue worth naming, whatever the
# ratio: 20 us idle against 90 us loaded is 4.5x and is still nothing.
MIN_ADDED_RTT_US = 50.0

# How iperf-orchestrator ran its tests, which decides what ran at once.
IPERF_MODES = ("parallel", "sequential-host", "sequential-pair")

# What each kind of limit is called, in the report and in the overlay.
TARGET, NIC, NIC_PPS, UPLINK, UNBOUNDED, UNMODELLED = (
    "TARGET", "NIC", "NIC_PPS", "UPLINK", "UNBOUNDED", "UNMODELLED")


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    Host and rack names come out of somebody's layout file, which is as
    likely to hold a non-ASCII name as any other document.
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


# canonical copy: binnacle/why_slow.py.
class _WriteGuard(object):
    """Full disk, quota, missing directory: name the file and stop.

    The partial file is removed too -- a half-written overlay that parses
    paints a floor that is half true.
    """

    def __init__(self, path, appending=False):
        self.path = path
        self.appending = appending

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.path and exc_type is not None and issubclass(exc_type, OSError):
            if not self.appending:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            die("cannot write %s: %s" % (self.path, exc))
        return False


def _env(name, default=None):
    v = os.environ.get("RECKON_" + name)
    return v if v not in (None, "") else default


def _env_num(name, kind, default):
    """A numeric environment default, refused by name when it is not one.

    argparse checks what is typed on the command line; a value that came
    from the environment arrives as the default and is never checked, so
    RECKON_SHORT=ninety would otherwise be a traceback.
    """
    raw = _env(name)
    if raw is None:
        return default
    try:
        return kind(raw)
    except ValueError:
        die("RECKON_%s=%r is not a number" % (name, raw))


def positive(text):
    """A finite number above zero, or None.

    Every hardware figure passes through here, because each comes out of
    a hand-edited file: `nic_gbps=25g`, `uplinks=eight` and
    `nic_gbps=nan` are all typos, and a typo read as a number would be
    the expectation every flow on that host is graded against.  None
    means "not declared", and the caller says so.
    """
    if isinstance(text, bool):
        return None
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _float(cell):
    """A report cell as a float; blank, non-numeric or non-finite is None.

    Blank means not measured in every report this reads -- a zero would
    be a measurement, and averaging one in would invent it.
    """
    if cell in (None, ""):
        return None
    try:
        v = float(cell)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _read_text(path):
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))


def _csv_paths(paths, what):
    """Every CSV a list of files and directories names, in a stable order."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            names = sorted(n for n in os.listdir(p) if n.endswith(".csv"))
            if not names:
                die("%s: no .csv files in %s" % (what, p))
            out.extend(os.path.join(p, n) for n in names)
        elif os.path.exists(p):
            out.append(p)
        else:
            die("%s: no such file or directory: %s" % (what, p))
    return out


def _read_csv(path):
    """(header, rows) of one CSV, decoded the way every tool here decodes."""
    text = _read_text(path)
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as exc:
        die("cannot parse %s: %s" % (path, exc))
    return reader.fieldnames or [], rows


# ---------------------------------------------------------------------------
# The layout file
# ---------------------------------------------------------------------------
#
# canonical copy: binnacle/manifest.py -- dc_expand_one, dc_expand, subst,
# tokenize, indent_of, Element, parse_layout and _materialize, verbatim.
# tests/test_compose.sh fails if they stop matching.  The layout grammar is
# the datacenter viewer's; manifest reads it to answer "which machines" and
# this reads it to answer "what was built".

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
# What was built
# ---------------------------------------------------------------------------
#
# The layout is the one description of the hardware that the viewer, this
# tool and `manifest` all read, so the speeds live in it as ordinary
# attributes and inherit the way every attribute does: nic_gbps=25 on a row
# is every server in it.  A rack's uplink capacity is uplinks x uplink_gbps,
# read off the rack itself -- set on a row, it means "every rack in this
# row has these uplinks", which is what inheritance says it means.

class HostHW(object):
    """One host's hardware as the model uses it, and where each figure came from."""

    __slots__ = ("name", "nic_bps", "nic_source", "declared_bps", "flag_bps",
                 "measured_bps", "measured_raw", "nic_pps", "mtu", "group",
                 "placed")

    def __init__(self, name):
        self.name = name
        self.nic_bps = None
        self.nic_source = None
        self.declared_bps = None
        self.flag_bps = None
        self.measured_bps = None
        # What the speeds file said, kept even when it was not a speed: a
        # host that reported "-1" is a link that is down, which is itself
        # the finding.
        self.measured_raw = None
        self.nic_pps = None
        self.mtu = None
        self.group = None
        self.placed = False


class GroupHW(object):
    """One rack (or whatever --group-kind names) and its uplinks."""

    __slots__ = ("name", "label", "uplinks", "uplink_gbps", "uplink_bps",
                 "hosts")

    def __init__(self, name):
        self.name = name
        self.label = name
        self.uplinks = None
        self.uplink_gbps = None
        self.uplink_bps = None
        self.hosts = []


class Hardware(object):
    def __init__(self, group_kind):
        self.group_kind = group_kind
        self.hosts = {}
        self.groups = {}
        self.layout = None
        self.speeds = None
        # (host or element, text) for every attribute that was present and
        # was not a number.  Each is reported: a typo that silently became
        # "not declared" would grade a host against nothing and say so
        # nowhere.
        self.problems = []


def _attr_number(el, key, hw, scale=1.0):
    raw = el.attrs.get(key)
    if raw is None:
        return None
    v = positive(raw)
    if v is None:
        hw.problems.append((el.name, "%s=%s on %s is not a positive number"
                            % (key, raw, el.path)))
        return None
    return v * scale


def _group_of(el, kind):
    p = el.parent
    while p is not None and p.kind != kind:
        p = p.parent
    return p


def _make_group(el, hw):
    g = GroupHW(el.path)
    gbps = _attr_number(el, "uplink_gbps", hw)
    raw_n = el.attrs.get("uplinks")
    n = 1.0
    if raw_n is not None:
        n = positive(raw_n)
        if n is None:
            hw.problems.append((el.name, "uplinks=%s on %s is not a count"
                                % (raw_n, el.path)))
            return g
        if gbps is None:
            # A count with no speed is half a declaration; guessing the
            # other half would grade a rack against a number nobody wrote.
            hw.problems.append((el.name, "uplinks=%s on %s with no "
                                "uplink_gbps= -- the speed of each is needed"
                                % (raw_n, el.path)))
            return g
    if gbps is not None:
        g.uplinks, g.uplink_gbps = n, gbps
        g.uplink_bps = n * gbps * 1e9
    return g


def _short_labels(groups):
    """The shortest tail of each group's path that is still unique.

    `MX1/wr01/A/r03` is unambiguous and unreadable in a table; `r03` is
    readable and, in a two-hall building, ambiguous.  Each group gets the
    fewest trailing components that no other group shares.
    """
    parts = dict((name, name.split("/")) for name in groups)
    for name, comps in parts.items():
        for n in range(1, len(comps) + 1):
            tail = "/".join(comps[-n:])
            clash = any("/".join(o[-n:]) == tail
                        for other, o in parts.items() if other != name)
            if not clash:
                groups[name].label = tail
                break


def build_hardware(names, layout, speeds, args):
    hw = Hardware(args.group_kind)
    by_name = {}
    if layout:
        hw.layout = layout
        elements = parse_layout(layout)
        if not elements:
            die("%s describes nothing -- is it a .dc layout?" % layout)
        for el in elements:
            by_name.setdefault(el.name, []).append(el)
    if speeds is not None:
        hw.speeds = speeds
    for name in names:
        h = hw.hosts[name] = HostHW(name)
        els = by_name.get(name) or []
        if els:
            el = els[0]
            if len(els) > 1:
                hw.problems.append((name, "%d elements in the layout are "
                                    "named %s; using %s"
                                    % (len(els), name, el.path)))
            h.placed = True
            h.declared_bps = _attr_number(el, "nic_gbps", hw, 1e9)
            h.nic_pps = _attr_number(el, "nic_mpps", hw, 1e6)
            mtu = _attr_number(el, "mtu", hw)
            if mtu is not None and not 68 <= mtu <= 65535:
                hw.problems.append((name, "mtu=%g on %s is not an MTU"
                                    % (mtu, el.path)))
                mtu = None
            h.mtu = int(mtu) if mtu is not None else None
            gel = _group_of(el, args.group_kind)
            if gel is not None:
                g = hw.groups.get(gel.path)
                if g is None:
                    g = hw.groups[gel.path] = _make_group(gel, hw)
                g.hosts.append(name)
                h.group = gel.path
        if args.nic_gbps:
            h.flag_bps = args.nic_gbps * 1e9
        if speeds is not None and name in speeds:
            h.measured_bps, h.measured_raw = speeds[name]
        # What the link negotiated outranks what anybody wrote down: it is
        # the speed the packets actually met.  The disagreement is a
        # finding of its own, not a silent substitution.
        for source, value in (("measured", h.measured_bps),
                              ("layout", h.declared_bps),
                              ("flag", h.flag_bps)):
            if value:
                h.nic_bps, h.nic_source = value, source
                break
    _short_labels(hw.groups)
    return hw


# ---------------------------------------------------------------------------
# Reading what was measured
# ---------------------------------------------------------------------------

class Flow(object):
    """One directed flow: what it was asked for, what it got, what it could."""

    __slots__ = ("src", "dst", "layer", "group", "demand", "achieved",
                 "sent", "loss", "rtt_p50", "rtt_p99", "size", "rep_size",
                 "samples", "coef", "expected", "limit", "limit_on",
                 "efficiency", "idle_p50", "added_rtt", "mtu", "mtu_from",
                 "modelled")

    def __init__(self, src, dst):
        for key in self.__slots__:
            setattr(self, key, None)
        self.src, self.dst = src, dst
        self.coef = {}
        self.modelled = False


class Run(object):
    """What one test run measured, reduced to flows."""

    def __init__(self, source, unit):
        self.source = source            # "mx" or "iperf"
        self.unit = unit                # "pps" or "Mb/s"
        self.flows = []
        self.reported = set()           # hosts that wrote anything
        self.seen = set()               # hosts named anywhere in the run
        self.cpu = {}                   # host -> {"core", "agent", "peak", "softirq"}
        self.failed = []                # (src, dst, status, why) -- iperf
        self.notes = []                 # what was done to the data, said once
        self.first_ts = None
        self.last_ts = None
        self.window = None
        self.mode = None
        self.run_id = None
        self.files = 0
        self.layers = 0

    def describe(self):
        if self.source == "mx":
            sizes = sorted(set((int(f.size), int(f.rep_size))
                               for f in self.flows))
            if not sizes:
                shape = "no flows"
            elif len(sizes) == 1:
                shape = "%d B requests, %d B replies" % sizes[0]
            else:
                shape = "mixed packet sizes"
            if self.layers > 1:
                shape += ", %d layers" % self.layers
            when = ("last %ds" % self.window if self.window
                    else "the whole history")
            return "mx run, %s, %s" % (shape, when)
        return "iperf %s run%s, TCP" % (
            self.mode, (" " + self.run_id) if self.run_id else "")


# The columns that tell the reports apart.  mx and netmesh both write a
# report.csv per host into a reports/ directory, so a path is no evidence
# of which one a file is -- the header is.
MX_MARK = ("rep_size", "target_pps", "rep_pps")
NETMESH_MARK = ("probe", "rtt_p50_us", "jitter_us")


def load_mx(paths, window, rename):
    files = _csv_paths(paths, "--mx")
    rows = []
    for path in files:
        header, found = _read_csv(path)
        if not header:
            continue
        if all(k in header for k in NETMESH_MARK) and "rep_size" not in header:
            die("%s is a netmesh report, not an mx one -- pass it with --idle"
                % path)
        missing = [k for k in MX_MARK if k not in header]
        if missing:
            die("%s is not an mx report: it has no %s column"
                % (path, missing[0]))
        for r in found:
            ts = _float(r.get("ts"))
            if ts is None or not r.get("host"):
                continue
            r["_ts"] = ts
            rows.append(r)
    if not rows:
        die("the mx reports hold no rows -- give the agents an interval or "
            "two, then collect them (mx collect)")
    run = Run("mx", "pps")
    run.files = len(files)
    latest = max(r["_ts"] for r in rows)
    if window > 0:
        # The same cut `mx summarize --window` makes, from the newest row
        # in any report, so the two tools average the same intervals.
        rows = [r for r in rows if r["_ts"] >= latest - window]
    run.window = window
    run.first_ts = min(r["_ts"] for r in rows)
    run.last_ts = latest

    acc, hostacc = {}, {}
    columns = (("target", "target_pps"), ("sent", "pps"), ("back", "rep_pps"),
               ("loss", "loss_pct"), ("p50", "rtt_p50_us"),
               ("p99", "rtt_p99_us"))
    for r in rows:
        host = rename(r["host"])
        run.reported.add(host)
        run.seen.add(host)
        kind = r.get("dir")
        if kind == "tx":
            peer = r.get("peer")
            if not peer or peer == "*":
                continue
            peer = rename(peer)
            run.seen.add(peer)
            if _float(r.get("pps")) is None:
                # A drain row: the tail of a finished layer, replies that
                # landed after the switch, with the send side left blank.
                # It is not a flow running at a rate.
                continue
            layer = (r.get("layer") or "").strip() or None
            a = acc.get((host, peer, layer))
            if a is None:
                a = acc[(host, peer, layer)] = dict(
                    (k, []) for k, _c in columns)
                a["size"] = a["rep"] = None
            for key, col in columns:
                v = _float(r.get(col))
                # A negative rate is not a measurement of anything, so it
                # is skipped like a blank rather than averaged in.  Loss is
                # the exception: mx works it out as 100 - replies/requests,
                # and a reply landing an interval late reads slightly below
                # zero honestly.
                if v is not None and (v >= 0 or key == "loss"):
                    a[key].append(v)
            size, rep = _float(r.get("size")), _float(r.get("rep_size"))
            if size is not None and size > 0:
                a["size"] = size
            if rep is not None and rep >= 0:
                a["rep"] = rep
        elif kind == "host":
            h = hostacc.setdefault(host, {"core": [], "agent": []})
            for key, col in (("core", "cpu_max_pct"), ("agent", "agent_cpu_pct")):
                v = _float(r.get(col))
                if v is not None:
                    h[key].append(v)

    unsized = 0
    for key in sorted(acc, key=lambda k: (k[0], k[1], k[2] or "")):
        a = acc[key]
        if a["size"] is None or a["rep"] is None:
            unsized += 1
            continue
        f = Flow(key[0], key[1])
        f.layer = key[2]
        # In a layered run only one layer's flows are on the wire at once,
        # so they are the ones that share; otherwise every flow does.
        f.group = ("layer", key[2] or "")
        # Blank target cells are an unpaced flow: it asks for whatever it
        # can get, so its demand is unbounded rather than zero.
        target = _mean(a["target"])
        if target is not None and target <= 0:
            continue
        f.demand = target
        f.achieved = _mean(a["back"])
        f.sent = _mean(a["sent"])
        f.loss = _mean(a["loss"])
        f.rtt_p50 = _mean(a["p50"])
        f.rtt_p99 = _mean(a["p99"])
        f.size, f.rep_size = a["size"], a["rep"]
        f.samples = len(a["sent"])
        run.flows.append(f)
    if unsized:
        run.notes.append("%d flow(s) left out: their rows carry no packet "
                         "size, so their wire rate cannot be worked out"
                         % unsized)
    run.layers = len(set(f.layer for f in run.flows if f.layer is not None))
    for host, h in hostacc.items():
        run.cpu[host] = {"core": _mean(h["core"]), "agent": _mean(h["agent"])}
    return run


OVERLAY_RUN_RE = re.compile(r"^#\s*run\s+(\S+),\s*mode\s+([\w-]+)")
OVERLAY_TARGET_RE = re.compile(r"-b target .*?\(([0-9.]+)\s*Mb/s per flow\)")


def _overlay_fields(line):
    """One results line, split the way the viewer splits it."""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _iperf_group(mode, src, dst):
    """Which tests ran at the same time, by how the run was driven."""
    if mode == "parallel":
        return ("all",)
    if mode == "sequential-host":
        return ("src", src)
    return ("pair", src, dst)


def _iperf_mode(found, override, path):
    if found and override and found != override:
        # The file was written by the run; the flag was typed afterwards.
        die("%s says it ran in %s mode; --iperf-mode says %s"
            % (path, found, override))
    mode = found or override
    if mode is None:
        die("%s does not say how its tests ran -- pass --iperf-mode "
            "parallel, sequential-host or sequential-pair" % path)
    if mode == "rolling":
        die("%s is a rolling run: its probes overlap at random, so which "
            "tests shared the fabric is not recorded and no fair share can "
            "be worked out.  reckon reads parallel, sequential-host and "
            "sequential-pair runs." % path)
    if mode not in IPERF_MODES:
        die("%s: unknown iperf mode %r" % (path, mode))
    return mode


def _iperf_flows(run, per_flow, target):
    for (src, dst) in sorted(per_flow):
        vals = per_flow[(src, dst)]
        f = Flow(src, dst)
        f.group = _iperf_group(run.mode, src, dst)
        # In a parallel run, --host-flows puts several tests on one edge at
        # once and their rates add; in the sequential modes each sample is
        # one whole test of that edge.
        if run.mode == "parallel":
            f.achieved = sum(vals)
            f.demand = target * len(vals) if target else None
        else:
            f.achieved = _mean(vals)
            f.demand = target
        f.samples = len(vals)
        run.flows.append(f)


def load_iperf(path, override, rename):
    if os.path.isdir(path):
        results = os.path.join(path, "iperf_results.csv")
        if not os.path.exists(results):
            die("%s holds no iperf_results.csv -- point --iperf at a run "
                "directory (results/latest) or at an export-overlay file"
                % path)
        mode_file = os.path.join(path, ".run_mode")
        mode = (_read_text(mode_file).strip() or None
                if os.path.exists(mode_file) else None)
        return _iperf_from_results(results, mode, override, rename)
    text = _read_text(path)
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if first.lower().startswith("timestamp,"):
        return _iperf_from_results(path, None, override, rename)
    return _iperf_from_overlay(path, text, override, rename)


def _iperf_from_results(path, mode, override, rename):
    header, rows = _read_csv(path)
    for col in ("source", "target", "status", "mbps"):
        if col not in header:
            die("%s is not an iperf_results.csv: it has no %s column"
                % (path, col))
    run = Run("iperf", "Mb/s")
    run.files = 1
    run.mode = _iperf_mode(mode, override, path)
    per_flow = {}
    for r in rows:
        src, dst = rename(r.get("source") or ""), rename(r.get("target") or "")
        if not src or not dst:
            continue
        run.seen.update((src, dst))
        status = (r.get("status") or "").strip()
        mbps = _float(r.get("mbps"))
        if status.upper() != "OK" or mbps is None:
            run.failed.append((src, dst, status or "?",
                               (r.get("error") or "").strip()))
            continue
        run.reported.add(src)
        per_flow.setdefault((src, dst), []).append(mbps)
    if not per_flow:
        die("%s holds no successful test" % path)
    run.notes.append("the -b target is not in iperf_results.csv, so every "
                     "flow is taken as unpaced; the export-overlay file "
                     "carries it")
    _iperf_flows(run, per_flow, None)
    return run


def _iperf_from_overlay(path, text, override, rename):
    runs, modes, targets, samples = [], {}, {}, []
    header_run = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = OVERLAY_RUN_RE.match(line)
            if m:
                header_run = m.group(1)
                modes[header_run] = m.group(2)
                if header_run not in runs:
                    runs.append(header_run)
            m = OVERLAY_TARGET_RE.search(line)
            if m and header_run is not None:
                targets[header_run] = positive(m.group(1))
            continue
        if line.startswith("!test"):
            continue
        fields = _overlay_fields(line)
        if len(fields) < 3:
            continue
        meta = {}
        for tok in fields[3:]:
            key, sep, value = tok.partition("=")
            if sep:
                meta[key] = value
        samples.append((fields[0], fields[1], fields[2], meta))
        rid = meta.get("run")
        if rid and rid not in runs:
            runs.append(rid)
    outs = [s for s in samples if s[0] == "iperf_mbps_out"]
    if not outs:
        die("%s has no iperf_mbps_out samples -- is it an iperf-orchestrator "
            "export-overlay file?" % path)
    if not any("peer" in s[3] for s in outs):
        die("%s has no peer= on its samples: it was exported with "
            "--overlay-reduce, which keeps one median per host and drops "
            "which flow was which.  Export it again without." % path)

    run = Run("iperf", "Mb/s")
    run.files = 1
    # An appended overlay holds several runs, newest last; reading them as
    # one would share capacity between tests that never met.
    run.run_id = runs[-1] if runs else None
    if len(runs) > 1:
        run.notes.append("%s holds %d runs; only the last, %s, is read"
                         % (path, len(runs), run.run_id))
    run.mode = _iperf_mode(modes.get(run.run_id), override, path)

    def ours(meta):
        return run.run_id is None or meta.get("run") in (None, run.run_id)

    per_flow = {}
    for test, target, value, meta in samples:
        if not ours(meta):
            continue
        host = rename(target)
        if test == "iperf_mbps_out":
            peer = meta.get("peer")
            v = _float(value)
            if not peer or v is None:
                continue
            peer = rename(peer)
            run.reported.add(host)
            run.seen.update((host, peer))
            per_flow.setdefault((host, peer), []).append(v)
        elif test == "iperf_state":
            run.seen.add(host)
            if value.upper() != "NO-DATA":
                run.reported.add(host)
        elif test == "iperf_status" and value.upper() != "OK":
            peer = rename(meta.get("peer") or "?")
            run.seen.add(host)
            run.failed.append((host, peer, value,
                               meta.get("error") or meta.get("err") or ""))
        elif test in ("iperf_cpu_peak", "iperf_cpu_softirq"):
            v = _float(value)
            if v is not None:
                key = "peak" if test == "iperf_cpu_peak" else "softirq"
                run.cpu.setdefault(host, {})[key] = v
    _iperf_flows(run, per_flow, targets.get(run.run_id))
    return run


def load_idle(paths, rename, busy):
    """(host, peer) -> (idle p50 RTT in us, path MTU), from netmesh reports.

    `busy` is the measured run's own time span: netmesh rows inside it were
    taken while the fabric was loaded, which is the opposite of a floor,
    and are left out and counted rather than averaged in.
    """
    files = _csv_paths(paths, "--idle")
    acc = {}
    dropped = 0
    for path in files:
        header, rows = _read_csv(path)
        if not header:
            continue
        if all(k in header for k in MX_MARK) and "probe" not in header:
            die("%s is an mx report, not a netmesh one -- pass it with --mx"
                % path)
        missing = [k for k in NETMESH_MARK if k not in header]
        if missing:
            die("%s is not a netmesh report: it has no %s column"
                % (path, missing[0]))
        for r in rows:
            # The pair's own row: not a per-port bucket of it, not a hop.
            if r.get("dir") != "tx" or r.get("flow"):
                continue
            if (r.get("probe") or "udp") != "udp":
                continue
            ts = _float(r.get("ts"))
            if busy and ts is not None and busy[0] <= ts <= busy[1]:
                dropped += 1
                continue
            host, peer = rename(r.get("host") or ""), rename(r.get("peer") or "")
            if not host or not peer:
                continue
            a = acc.setdefault((host, peer), {"p50": [], "mtu": None})
            v = _float(r.get("rtt_p50_us"))
            if v is not None:
                a["p50"].append(v)
            if r.get("mtu_state") in ("confirmed", "cached"):
                m = positive(r.get("path_mtu"))
                if m:
                    a["mtu"] = int(m)
    idle = dict((k, (_median(v["p50"]), v["mtu"])) for k, v in acc.items())
    return idle, dropped


SPEED_COLUMNS = (("speed_mbps", 1e6), ("mbps", 1e6), ("speed", 1e6),
                 ("speed_gbps", 1e9), ("gbps", 1e9))


def _split_cells(line):
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "," in line:
        return [c.strip() for c in line.split(",")]
    return line.split()


def load_speeds(path, rename):
    """host -> (bits/s or None, what the file said), the last row winning.

    Mb/s unless a column says otherwise, because that is the unit
    /sys/class/net/IFACE/speed reports in, and the one `ethtool` prints.
    A headed table (dredge --tsv appends one row per host per run) is read
    by its host column and the first speed column; a bare file is `host
    value` per line.  The last row for a host wins, so an appended table
    answers with the newest reading.
    """
    rows = []
    for raw in _read_text(path).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows.append(_split_cells(raw.rstrip("\r\n")))
    if not rows:
        die("%s holds no link speeds" % path)
    header = [c.lower() for c in rows[0]]
    host_col, speed_col, factor = 0, None, 1e6
    body = rows
    if "host" in header:
        host_col = header.index("host")
        for name, scale in SPEED_COLUMNS:
            if name in header:
                speed_col, factor = header.index(name), scale
                break
        if speed_col is None:
            die("%s has a host column and no speed column -- name one "
                "speed, speed_mbps or speed_gbps" % path)
        body = rows[1:]
    out = {}
    for cells in body:
        if len(cells) < 2:
            continue
        col = speed_col if speed_col is not None else len(cells) - 1
        if col >= len(cells) or host_col >= len(cells):
            continue
        value = cells[col]
        v = positive(value)
        out[rename(cells[host_col])] = (v * factor if v else None, value)
    return out


def load_names(path):
    names = {}
    for raw in _read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            die("%s: want `measured-name layout-name` per line, not %r"
                % (path, raw.strip()))
        names[parts[0]] = parts[1]
    return names


# ---------------------------------------------------------------------------
# The model: what each flow could have had
# ---------------------------------------------------------------------------
#
# Every flow crosses a handful of links -- its sender's NIC out, its
# receiver's NIC in, and, when it leaves its rack, that rack's uplinks out
# and the far rack's uplinks in.  An mx flow is a request/response pair, so
# its replies cross the same links the other way.  Each flow's expected rate
# is its max-min fair share: every flow rises at the same rate until a link
# it crosses is full or it reaches its own target, and whichever stopped it
# is its limit.  That is the most any flow can be promised without taking
# from another, which is what "this fabric should have delivered X" means.

LIMIT_OF = {"nic": NIC, "pps": NIC_PPS, "uplink": UPLINK}


def _add(coef, key, value):
    coef[key] = coef.get(key, 0.0) + value


def _crossing(coef, hw, src, dst, fwd, rev):
    gs, gd = hw.hosts[src].group, hw.hosts[dst].group
    if gs is None or gd is None or gs == gd:
        return
    _add(coef, ("uplink", gs, "out"), fwd)
    _add(coef, ("uplink", gd, "in"), fwd)
    if rev:
        _add(coef, ("uplink", gd, "out"), rev)
        _add(coef, ("uplink", gs, "in"), rev)


def mx_coef(f, hw):
    """Wire bits and packets per request per second, on every link it uses."""
    req = (f.size + UDP_WIRE_OVERHEAD) * 8.0
    rep = (f.rep_size + UDP_WIRE_OVERHEAD) * 8.0
    coef = {}
    _add(coef, ("nic", f.src, "out"), req)
    _add(coef, ("nic", f.dst, "in"), req)
    _add(coef, ("nic", f.dst, "out"), rep)
    _add(coef, ("nic", f.src, "in"), rep)
    for host, way in ((f.src, "out"), (f.dst, "in"), (f.dst, "out"),
                      (f.src, "in")):
        _add(coef, ("pps", host, way), 1.0)
    _crossing(coef, hw, f.src, f.dst, req, rep)
    return coef


def iperf_coef(f, hw):
    """Wire bits and segments per Mb/s of goodput, on every link it uses.

    The ACKs coming back are not counted: at one per two segments of 64
    bytes they are about 2% of the reverse direction, and a full-mesh run
    fills that direction with its own data anyway.
    """
    payload = float(f.mtu - TCP_HEADERS)
    wire = 1e6 * (f.mtu + ETH_FRAMING) / payload
    segments = 1e6 / (payload * 8.0)
    coef = {}
    _add(coef, ("nic", f.src, "out"), wire)
    _add(coef, ("nic", f.dst, "in"), wire)
    _add(coef, ("pps", f.src, "out"), segments)
    _add(coef, ("pps", f.dst, "in"), segments)
    _crossing(coef, hw, f.src, f.dst, wire, 0)
    return coef


def capacities(hw):
    cap = {}
    for h in hw.hosts.values():
        if h.nic_bps:
            cap[("nic", h.name, "out")] = cap[("nic", h.name, "in")] = h.nic_bps
        if h.nic_pps:
            cap[("pps", h.name, "out")] = cap[("pps", h.name, "in")] = h.nic_pps
    for g in hw.groups.values():
        if g.uplink_bps:
            cap[("uplink", g.name, "out")] = g.uplink_bps
            cap[("uplink", g.name, "in")] = g.uplink_bps
    return cap


def fair_share(flows, cap):
    """Max-min fair rates for flows that share the fabric, by progressive filling.

    All unfrozen flows rise together; at each step the water level goes up
    to the next event -- a link filling, or a flow reaching its target --
    and the flows that event stops are frozen at that level.  Each step
    freezes at least one flow or fills at least one link, so it ends after
    at most (flows + links) steps, and each flow's links are touched once
    when it freezes.

    A flow that crosses no link with a known capacity and has no target
    rises forever; it is marked UNBOUNDED with no expected rate, rather
    than given an infinite one.
    """
    live, load, users = {}, {}, {}
    for i, f in enumerate(flows):
        live[i] = f
        for r, c in f.coef.items():
            if r in cap and c > 0:
                load[r] = load.get(r, 0.0) + c
                users.setdefault(r, []).append(i)
    slack = dict((r, cap[r]) for r in load)
    full = set()
    demands = sorted((f.demand, i) for i, f in live.items()
                     if f.demand is not None)
    di = 0
    level = 0.0

    def freeze(i, limit, on):
        f = live.pop(i)
        f.expected, f.limit, f.limit_on = level, limit, on
        for r, c in f.coef.items():
            if r in load:
                load[r] -= c

    while live:
        step = None
        for r, used in load.items():
            if r in full or used <= 1e-9:
                continue
            s = slack[r] / used
            if step is None or s < step:
                step = s
        while di < len(demands) and demands[di][1] not in live:
            di += 1
        if di < len(demands):
            s = demands[di][0] - level
            if step is None or s < step:
                step = s
        if step is None:
            for i in sorted(live):
                freeze(i, UNBOUNDED, None)
                flows[i].expected = None
            break
        step = max(step, 0.0)
        level += step
        for r, used in load.items():
            if r not in full:
                slack[r] -= step * used
        # A flow that reaches its target and fills a link in the same step
        # is at its target: that is the fact that bound it.
        while di < len(demands):
            d, i = demands[di]
            if i not in live:
                di += 1
            elif d - level <= 1e-9 * max(d, 1.0):
                freeze(i, TARGET, None)
                di += 1
            else:
                break
        # Every link that filled in this step, then each flow it stopped
        # charged to the one it loads most: its own request leg rather than
        # a reply crossing the same NIC.  Between the two ends of that leg
        # the sending side is named -- a flow out of a rack whose uplinks
        # are full is charged to that rack, not to the one it was going
        # to -- and a last tie goes to the lower name, so the answer never
        # depends on dictionary order.
        newly = sorted(r for r in load
                       if r not in full and slack[r] <= 1e-9 * cap[r])
        full.update(newly)
        stopped = set()
        for r in newly:
            stopped.update(i for i in users[r] if i in live)
        for i in sorted(stopped):
            f = live[i]
            r = sorted((r for r in newly if r in f.coef),
                       key=lambda r: (-f.coef[r], r[2] != "out", r))[0]
            freeze(i, LIMIT_OF[r[0]], r)


def _flow_mtu(f, hw, idle, default):
    """The MTU a TCP flow's segments were cut to, and where that came from."""
    a, b = hw.hosts[f.src].mtu, hw.hosts[f.dst].mtu
    if a or b:
        return min(m for m in (a, b) if m), "layout"
    found = idle.get((f.src, f.dst)) if idle else None
    if found and found[1]:
        return found[1], "netmesh"
    return default, "default"


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

class HostStat(object):
    __slots__ = ("name", "expected", "achieved", "efficiency", "compared",
                 "short", "limit", "limit_on", "verdict", "reported",
                 "added_rtt", "group")

    def __init__(self, name):
        for key in self.__slots__:
            setattr(self, key, None)
        self.name = name


class Analysis(object):
    """Everything the rules and the renderers read."""

    def __init__(self, run, hw, idle, idle_dropped, args):
        self.run = run
        self.hw = hw
        self.idle = idle
        self.idle_dropped = idle_dropped
        self.args = args
        self.hosts = {}
        self.by_host = {}
        self.explained = set()
        self.assumptions = []
        self.compared = []


def analyse(run, hw, idle, idle_dropped, args):
    c = Analysis(run, hw, idle, idle_dropped, args)
    cap = capacities(hw)
    shared = {}
    for f in run.flows:
        if run.source == "mx":
            f.coef = mx_coef(f, hw)
        else:
            f.mtu, f.mtu_from = _flow_mtu(f, hw, idle, args.mtu)
            f.coef = iperf_coef(f, hw)
        shared.setdefault(f.group, []).append(f)
    for key in sorted(shared, key=str):
        fair_share(shared[key], cap)

    for f in run.flows:
        # Both ends need a speed.  With one missing, the share worked out
        # is a bound on the flow, not an expectation of it, and grading a
        # measurement against a bound would call a healthy flow short.
        if hw.hosts[f.src].nic_bps and hw.hosts[f.dst].nic_bps:
            f.modelled = True
        else:
            f.modelled = False
            f.expected, f.limit, f.limit_on = None, UNMODELLED, None
        if f.expected and f.achieved is not None:
            f.efficiency = f.achieved / f.expected * 100.0
            c.compared.append(f)
        if idle and f.rtt_p50 is not None:
            found = idle.get((f.src, f.dst))
            if found and found[0] is not None:
                f.idle_p50 = found[0]
                f.added_rtt = f.rtt_p50 - found[0]
        c.by_host.setdefault(f.src, []).append(f)
        c.by_host.setdefault(f.dst, []).append(f)

    for name in sorted(run.seen | run.reported):
        hs = c.hosts[name] = HostStat(name)
        hs.reported = name in run.reported
        hs.group = hw.hosts[name].group if name in hw.hosts else None
        touching = c.by_host.get(name, [])
        out = [f for f in touching if f.src == name and f.expected is not None]
        # A layered run's flows were on the wire one layer at a time, so a
        # host's rate is its flows' sum divided across its layers, not the
        # sum: that would count every layer as if it ran the whole window.
        layers = len(set(f.layer for f in out)) or 1
        if out:
            hs.expected = sum(f.expected for f in out) / layers
            got = [f.achieved for f in out if f.achieved is not None]
            hs.achieved = sum(got) / layers if got else None
        # The median over every flow it is an end of, in either direction:
        # a host that is itself slow has all of its flows slow, and one
        # with a single slow peer has one.  That is "I am slow" against "I
        # have a slow peer" -- the same reading mx_rel_median makes.
        effs = [f.efficiency for f in touching if f.efficiency is not None]
        hs.efficiency = _median(effs)
        hs.compared = len(effs)
        hs.short = len([e for e in effs if e < args.short])
        # The worst of its own flows, as mx reports its RTT: a mean would
        # average the one queueing path away.
        grown = [f.added_rtt for f in touching
                 if f.src == name and f.added_rtt is not None]
        hs.added_rtt = max(grown) if grown else None
        hs.limit, hs.limit_on = _host_limit(out)
        if not hs.reported:
            hs.verdict = "NO-DATA"
        elif hs.efficiency is None:
            hs.verdict = UNMODELLED if touching else None
        elif hs.efficiency < args.fail:
            hs.verdict = "FAIL"
        elif (hs.efficiency < args.short
              or hs.efficiency > ABOVE_TOLERANCE * 100.0):
            hs.verdict = "WARN"
        else:
            hs.verdict = "OK"
    c.assumptions = assumptions(c)
    return c


def _host_limit(flows):
    """The limit most of a host's own flows run into, and on what.

    TARGET only when every flow met its target: one flow stopped by a link
    says more about the host than nineteen that got what they asked for.
    """
    if not flows:
        return None, None
    counts = {}
    for f in flows:
        if f.limit != TARGET:
            counts[(f.limit, f.limit_on)] = counts.get((f.limit, f.limit_on), 0) + 1
    if not counts:
        return TARGET, None
    best = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
    return best


def assumptions(c):
    run, hw, kind = c.run, c.hw, c.hw.group_kind
    out = ["Each flow's expected rate is its max-min fair share: flows rise "
           "together until a link they cross is full or they reach their "
           "target."]
    if run.source == "mx":
        out.append("A packet on the wire is its payload plus %d B (preamble "
                   "and SFD 8, MAC 14, FCS 4, IFG 12, IPv4 20, UDP 8) -- the "
                   "figure mx itself uses.  Every request is answered by one "
                   "reply, crossing the same links the other way."
                   % UDP_WIRE_OVERHEAD)
    else:
        mtus = sorted(set((f.mtu, f.mtu_from) for f in run.flows))
        out.append("TCP goodput is the wire rate x (MTU - %d) / (MTU + %d): "
                   "IPv4, TCP and timestamps against Ethernet framing; ACKs "
                   "are not counted.  MTU %s."
                   % (TCP_HEADERS, ETH_FRAMING,
                      ", ".join("%d from %s" % m for m in mtus) or "-"))
        out.append("Mode %s: %s" % (run.mode, {
            "parallel": "every test ran at once and shared the fabric.",
            "sequential-host": "one sender at a time, to all its peers at "
                               "once.",
            "sequential-pair": "one test at a time, each with the fabric to "
                               "itself.",
        }[run.mode]))
    if hw.groups:
        bare = len([g for g in hw.groups.values() if not g.uplink_bps])
        out.append("A %s's uplinks carry uplinks x uplink_gbps each way, "
                   "spread evenly; the layers above the %ss are taken as "
                   "non-blocking." % (kind, kind))
        if bare:
            out.append("%d %s(s) declare no uplinks, so traffic leaving them "
                       "is limited by the NICs alone and their expectations "
                       "are upper bounds." % (bare, kind))
    elif hw.layout:
        out.append("No %s in the layout holds a measured host, so no uplink "
                   "is modelled: the NICs are the only limits." % kind)
    else:
        out.append("No layout: the NICs are the only limits, and the fabric "
                   "between them is taken as non-blocking.")
    return out


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

Rule = namedtuple("Rule", "id title why")
Finding = namedtuple("Finding", "rule severity host say fix")

RULES = [
    Rule("LINK_SPEED", "link speed",
         "A link that negotiated down caps every flow through it, and the "
         "layout cannot know.  When the hosts report their speed (--speeds) "
         "it is compared with what the layout declares: lower is CRITICAL, "
         "because nothing on that host can reach what it was built for; "
         "higher is WARN, because the layout is stale."),
    Rule("NO_REPORT", "never reported",
         "A host named in the run that wrote nothing.  Its own flows are "
         "missing from the model, so every flow that shared a link with "
         "them is expected to get more than it could have."),
    Rule("UNMODELLED", "not modelled",
         "A host with no NIC speed -- none declared, none measured, no "
         "--nic-gbps -- or an attribute that is not a number.  Its flows "
         "get no expectation rather than a guessed one."),
    Rule("NOT_IN_LAYOUT", "not in layout",
         "A measured host the layout does not name.  Its rack is unknown, "
         "so its traffic is not charged to any uplink."),
    Rule("ABOVE_HARDWARE", "above hardware",
         "A flow that carried more than %d%% of what the declared hardware "
         "allows.  That cannot happen, so the declaration is wrong -- a "
         "speed set too low, an uplink count short, a host in the wrong "
         "rack -- and every expectation is suspect until it is fixed."
         % (ABOVE_TOLERANCE * 100)),
    Rule("IDLE_OVERLAP", "baseline busy",
         "netmesh rows taken while the measured run was on the wire are not "
         "a floor; they are left out and counted."),
    Rule("CPU_BOUND", "test host CPU",
         "A short host whose test agent ran out of CPU first: busiest core "
         ">= %.0f%% or busiest mx worker >= %.0f%% of a core (mx summarize's "
         "lines), or one core >= %.0f%% in softirq or the host >= %.0f%% "
         "(iperf).  Its number measures its CPU, not the network."
         % (CPU_CORE_BOUND, AGENT_CPU_BOUND, SOFTIRQ_BOUND, CPU_CORE_BOUND)),
    Rule("HOST_SHORT", "host short",
         "A host whose flows reach less than --short of expected (median) "
         "while the rest of its rack -- or, with no rack, the fleet -- do "
         "not.  The fault is on that host or its link."),
    Rule("GROUP_SHORT", "rack short",
         "Half or more of a rack's hosts short together.  Flows crossing "
         "the rack's boundary worse than flows inside it put the fault on "
         "the uplinks; both alike put it on the switch or on what the "
         "hosts share."),
    Rule("FLEET_SHORT", "fleet short",
         "The median host short of --short: the whole fabric falls below "
         "its hardware at once, which no single faulty part does."),
    Rule("FLOW_SHORT", "path short",
         "Flows short between hosts that are otherwise fine: a path, not a "
         "host -- an ECMP member, a cable, one spine."),
    Rule("LOSS_BELOW_CAPACITY", "loss with room",
         "Flows that lost >= %.0f%% of round trips although the hardware "
         "has room for their target: drops the capacity does not explain."
         % LOSS_PCT),
    Rule("QUEUEING", "queues building",
         "Loaded RTT >= --bloat times the idle RTT, and %d us more.  Where "
         "the model says a link is full that is expected; where it says "
         "there is room, something the layout does not declare is."
         % MIN_ADDED_RTT_US),
]
RULE_BY_ID = dict((r.id, r) for r in RULES)
PRECEDENCE = [r.id for r in RULES]


def _names(hosts, limit=6):
    hosts = list(hosts)
    text = ", ".join(hosts[:limit])
    if len(hosts) > limit:
        text += " (+%d more)" % (len(hosts) - limit)
    return text


def fmt_rate(v, unit):
    if v is None:
        return "-"
    if unit == "pps":
        for div, suffix in ((1e9, "Gpps"), (1e6, "Mpps"), (1e3, "kpps")):
            if v >= div:
                return "%.2f %s" % (v / div, suffix)
        return "%.0f pps" % v
    if v >= 1000:
        return "%.2f Gb/s" % (v / 1000.0)
    return "%.0f Mb/s" % v


def fmt_gbps(bps):
    if not bps:
        return "-"
    return "%s Gb/s" % ("%.2f" % (bps / 1e9)).rstrip("0").rstrip(".")


def fmt_us(v):
    if v is None:
        return "-"
    return "%.2f ms" % (v / 1000.0) if v >= 1000 else "%.0f us" % v


def limit_text(limit, on, c):
    if limit == TARGET:
        return "its target"
    if limit in (NIC, NIC_PPS):
        what = "NIC" if limit == NIC else "NIC packet rate"
        h = c.hw.hosts.get(on[1])
        speed = (" " + fmt_gbps(h.nic_bps)) if limit == NIC and h else ""
        return "%s %s%s %s" % (on[1], what, speed, on[2])
    if limit == UPLINK:
        g = c.hw.groups.get(on[1])
        label = g.label if g else on[1]
        return "%s uplinks %s" % (label, on[2])
    if limit == UNBOUNDED:
        return "nothing declared"
    if limit == UNMODELLED:
        return "not modelled"
    return "-"


def _flow_name(f):
    layer = (" (layer %s)" % f.layer) if f.layer is not None else ""
    return "%s -> %s%s" % (f.src, f.dst, layer)


def r_link_speed(c):
    if c.hw.speeds is None:
        return None, "no --speeds: the declared speeds are taken on trust"
    out = []
    for name in sorted(c.hosts):
        h = c.hw.hosts.get(name)
        if h is None or h.measured_raw is None:
            continue
        if h.measured_bps is None:
            out.append(Finding(RULE_BY_ID["LINK_SPEED"], WARN, name,
                               "%s reports no link speed (%s): a link that "
                               "is down, or a NIC that does not say"
                               % (name, h.measured_raw),
                               "Check the link on %s with `ethtool IFACE`.  "
                               "Until it reports a speed, its flows are "
                               "graded against the declared one." % name))
            continue
        declared = h.declared_bps or h.flag_bps
        said = "the layout" if h.declared_bps else "--nic-gbps"
        if not declared:
            continue
        if h.measured_bps < declared * 0.99:
            c.explained.add(name)
            out.append(Finding(RULE_BY_ID["LINK_SPEED"], CRITICAL, name,
                               "%s negotiated %s on a link %s says is %s"
                               % (name, fmt_gbps(h.measured_bps), said,
                                  fmt_gbps(declared)),
                               "Every flow through %s is capped at the lower "
                               "speed, and is graded against it here.  Find "
                               "out why the link came up slow: `ethtool "
                               "IFACE` for speed and duplex, then the cable, "
                               "the optic and the switch port." % name))
        elif h.measured_bps > declared * 1.01:
            out.append(Finding(RULE_BY_ID["LINK_SPEED"], WARN, name,
                               "%s runs at %s; %s says %s"
                               % (name, fmt_gbps(h.measured_bps), said,
                                  fmt_gbps(declared)),
                               "The layout is out of date for %s: correct "
                               "nic_gbps= so the next run is graded against "
                               "the hardware that is there." % name))
    return out, None


def r_no_report(c):
    out = []
    silent = sorted(h for h in c.hosts if not c.hosts[h].reported)
    if silent:
        c.explained.update(silent)
        why = ("`mx status` says why they were silent"
               if c.run.source == "mx" else
               "The iperf_state and iperf_status overlays say why they were "
               "silent")
        out.append(Finding(RULE_BY_ID["NO_REPORT"], CRITICAL, "*",
                           "%d host(s) took part in the run and reported "
                           "nothing: %s" % (len(silent), _names(silent)),
                           "Their own flows are missing from the model, so "
                           "every flow that shared their links is expected "
                           "to get more than it could have.  %s; compare "
                           "again once they report." % why))
    failed = sorted(set((s, d, st) for s, d, st, _w in c.run.failed))
    if failed:
        s, d, st = failed[0]
        out.append(Finding(RULE_BY_ID["NO_REPORT"], WARN, "*",
                           "%d test(s) failed and carried nothing -- %s -> %s "
                           "(%s)%s" % (len(failed), s, d, st,
                                       "" if len(failed) == 1 else " and more"),
                           "A failed test is not in the model, and the flows "
                           "it would have shared links with are graded as if "
                           "it never ran -- which, as far as the fabric "
                           "knows, it did not.  The iperf_status overlay "
                           "names each one's log file."))
    return out, None


def r_unmodelled(c):
    out = []
    bare = sorted(h for h in c.hosts
                  if c.hw.hosts.get(h) is None or not c.hw.hosts[h].nic_bps)
    if bare:
        out.append(Finding(RULE_BY_ID["UNMODELLED"], WARN, "*",
                           "%d host(s) have no NIC speed, so their flows have "
                           "no expectation: %s" % (len(bare), _names(bare)),
                           "Declare nic_gbps= on them in the layout (on their "
                           "row or rack, if they share one), or pass "
                           "--nic-gbps for every host the layout leaves "
                           "out."))
    for where, text in c.hw.problems:
        out.append(Finding(RULE_BY_ID["UNMODELLED"], WARN, where, text,
                           "Fix the value in the layout.  Until then it is "
                           "treated as not declared, not guessed at."))
    return out, None


def r_not_in_layout(c):
    if not c.hw.layout:
        return None, "no layout: there are no racks to place hosts in"
    lost = sorted(h for h in c.hosts if not c.hw.hosts[h].placed)
    if not lost:
        return [], None
    return [Finding(RULE_BY_ID["NOT_IN_LAYOUT"], WARN, "*",
                    "%d measured host(s) are not in %s, so their traffic is "
                    "charged to no uplink: %s"
                    % (len(lost), c.hw.layout, _names(lost)),
                    "Add them to the layout, or map the names the run used "
                    "onto the layout's with --names.")], None


def r_above(c):
    over = sorted((f for f in c.compared
                   if f.efficiency > ABOVE_TOLERANCE * 100.0),
                  key=lambda f: (-f.efficiency, f.src, f.dst))
    if not over:
        return [], None
    f = over[0]
    return [Finding(RULE_BY_ID["ABOVE_HARDWARE"], WARN, f.src,
                    "%d flow(s) carried more than the declared hardware "
                    "allows -- worst %s at %.0f%% of expected (%s against %s, "
                    "limit %s)"
                    % (len(over), _flow_name(f), f.efficiency,
                       fmt_rate(f.achieved, c.run.unit),
                       fmt_rate(f.expected, c.run.unit),
                       limit_text(f.limit, f.limit_on, c)),
                    "The model of this fabric is wrong, so nothing else here "
                    "can be trusted until it is fixed.  Check the limit "
                    "named: a NIC speed set below the real one (--speeds "
                    "measures it), an uplinks= count short of what is "
                    "cabled, or hosts placed in the wrong rack so that "
                    "traffic staying inside one is charged to its "
                    "uplinks.")], None


def r_idle_overlap(c):
    if c.idle is None:
        return None, "no --idle baseline"
    if not c.idle_dropped:
        return [], None
    return [Finding(RULE_BY_ID["IDLE_OVERLAP"], WARN, "*",
                    "%d netmesh row(s) fall inside the measured run's window "
                    "and were left out: a baseline taken under load is not "
                    "an idle one" % c.idle_dropped,
                    "Probe with nothing else on the fabric -- `netmesh "
                    "check` before the load starts -- and pass only those "
                    "reports to --idle.")], None


def r_cpu_bound(c):
    if not c.run.cpu:
        return None, "the run recorded no per-host CPU"
    out = []
    for name in sorted(c.hosts):
        hs = c.hosts[name]
        if hs.efficiency is None or hs.efficiency >= c.args.short:
            continue
        if name in c.explained:
            continue
        cpu = c.run.cpu.get(name) or {}
        why = []
        if (cpu.get("core") or 0) >= CPU_CORE_BOUND:
            why.append("its busiest core at %.0f%%" % cpu["core"])
        if (cpu.get("agent") or 0) >= AGENT_CPU_BOUND:
            why.append("its busiest mx worker at %.0f%% of a core"
                       % cpu["agent"])
        if (cpu.get("softirq") or 0) >= SOFTIRQ_BOUND:
            why.append("one core %.0f%% in softirq" % cpu["softirq"])
        if (cpu.get("peak") or 0) >= CPU_CORE_BOUND:
            why.append("the host's CPU at %.0f%%" % cpu["peak"])
        if not why:
            continue
        c.explained.add(name)
        sev = CRITICAL if hs.efficiency < c.args.fail else WARN
        if c.run.source == "mx":
            fix = ("The test agent on %s ran out of CPU before the network "
                   "ran out of capacity, so its number measures its CPU.  "
                   "Give it more workers (`mx start --workers N`), send "
                   "fewer, larger packets, or run `during` on it to see "
                   "whether the time went to the agent or to receive "
                   "processing." % name)
        else:
            fix = ("%s ran out of CPU before the network ran out of "
                   "capacity.  `during` around a rerun says whether it was "
                   "the iperf processes or receive processing (softirq), "
                   "and what to change." % name)
        out.append(Finding(RULE_BY_ID["CPU_BOUND"], sev, name,
                           "%s reached %.0f%% of what its hardware allows, "
                           "with %s" % (name, hs.efficiency, " and ".join(why)),
                           fix))
    return out, None


def r_host_short(c):
    out = []
    # Hosts a cause above has explained -- a silent one, one out of CPU --
    # are not evidence about their neighbours: a rack-mate that never
    # reported cannot vouch that the rack is fine, or that it is not.
    fleet = dict((n, h.efficiency) for n, h in c.hosts.items()
                 if h.efficiency is not None and n not in c.explained)
    for name in sorted(fleet, key=lambda n: (fleet[n], n)):
        eff = fleet[name]
        if eff >= c.args.short or name in c.explained:
            continue
        group = c.hosts[name].group
        mates = [fleet[n] for n in fleet
                 if n != name and group is not None
                 and c.hosts[n].group == group]
        where = None
        if mates:
            ref = _median(mates)
            where = "the rest of %s" % c.hw.groups[group].label
        else:
            ref = _median([v for n, v in fleet.items() if n != name])
            where = "the rest of the fleet"
        if ref is None or ref < c.args.short:
            continue
        c.explained.add(name)
        hs = c.hosts[name]
        sev = CRITICAL if eff < c.args.fail else WARN
        out.append(Finding(RULE_BY_ID["HOST_SHORT"], sev, name,
                           "%s reaches %.0f%% of what its hardware allows "
                           "(median of its flows; limit %s) while %s is at "
                           "%.0f%%" % (name, eff,
                                       limit_text(hs.limit, hs.limit_on, c),
                                       where, ref),
                           "The fault is on %s or its link, not the fabric: "
                           "`why-slow --ssh %s` for the box, `during` around "
                           "a rerun for what limited it, and `ethtool -S` on "
                           "its NIC for errors and drops -- then the cable "
                           "or optic." % (name, name)))
    return out, None


def r_group_short(c):
    if not c.hw.groups:
        return None, "no racks: no layout, or none that holds a measured host"
    out = []
    kind = c.hw.group_kind
    for gname in sorted(c.hw.groups):
        g = c.hw.groups[gname]
        members = [h for h in g.hosts
                   if h in c.hosts and c.hosts[h].efficiency is not None]
        if len(members) < 2:
            continue
        # Hosts a cause above has already explained are not evidence about
        # the rack: one sick server in a rack of two is half the rack, and
        # calling that a rack fault would send someone to the switch.
        short = [h for h in members if c.hosts[h].efficiency < c.args.short
                 and h not in c.explained]
        if len(short) < 2 or len(short) * 2 < len(members):
            continue
        # A rack is only a rack fault if the other racks are fine -- the
        # same test a host is put to against its rack-mates.  When they
        # are all short together, that is the fleet, and naming every
        # rack in turn would bury the one finding under ten.
        others = _median([h.efficiency for n, h in c.hosts.items()
                          if h.efficiency is not None and h.group != gname
                          and n not in c.explained])
        if others is None or others < c.args.short:
            continue
        cross, inside = [], []
        for f in c.compared:
            if f.src in c.explained or f.dst in c.explained:
                continue
            a = c.hw.hosts[f.src].group == gname
            b = c.hw.hosts[f.dst].group == gname
            if a and b:
                inside.append(f)
            elif a or b:
                cross.append(f)
        mc = _median([f.efficiency for f in cross])
        mi = _median([f.efficiency for f in inside])
        if mc is None and mi is None:
            continue
        c.explained.update(short)
        worst = min(v for v in (mc, mi) if v is not None)
        sev = CRITICAL if worst < c.args.fail else WARN
        if mc is not None and (mi is None or mi - mc >= 10.0):
            uplinked = len([f for f in cross if f.limit == UPLINK])
            inside_text = ("its flows inside it at %.0f%%" % mi
                           if mi is not None else "no flows inside it")
            if uplinked * 2 >= len(cross) and g.uplink_bps:
                say = ("%s %s: flows crossing its uplinks reach %.0f%% of "
                       "expected, %s -- its uplinks carry less than the "
                       "%g x %s declared"
                       % (kind, g.label, mc, inside_text, g.uplinks,
                          fmt_gbps(g.uplink_gbps * 1e9)))
                fix = ("Check %s's uplinks at the switch: a LAG member down "
                       "or at a lower speed, an optic negotiating down, or "
                       "ECMP hashing its flows onto fewer links than it has.  "
                       "`netmesh paths --compare` shows whether the paths out "
                       "of it changed." % g.label)
            else:
                say = ("%s %s: flows crossing its boundary reach %.0f%% of "
                       "expected, %s -- though the model does not put the "
                       "limit on its uplinks" % (kind, g.label, mc, inside_text))
                fix = ("Traffic out of %s is limited by something the layout "
                       "does not declare: oversubscription at its switch "
                       "(declare uplinks= and uplink_gbps= on it to model "
                       "that), a spine link (the layers above the %ss are "
                       "taken as non-blocking), or other traffic."
                       % (g.label, kind))
        else:
            say = ("every host in %s %s is short, inside it (%s) and across "
                   "its boundary (%s) alike"
                   % (kind, g.label,
                      "%.0f%%" % mi if mi is not None else "-",
                      "%.0f%%" % mc if mc is not None else "-"))
            fix = ("Not the uplinks -- traffic that never leaves %s is short "
                   "too.  Look at what its hosts share: the switch itself, "
                   "and their common NIC, driver, firmware and settings "
                   "(`agree` across them finds the one that differs from "
                   "the healthy racks)." % g.label)
        out.append(Finding(RULE_BY_ID["GROUP_SHORT"], sev, "*", say, fix))
    return out, None


def r_fleet_short(c):
    effs = [h.efficiency for n, h in c.hosts.items()
            if h.efficiency is not None and n not in c.explained]
    if len(effs) < 2:
        return None, "fewer than two hosts compared"
    med = _median(effs)
    if med >= c.args.short:
        return [], None
    short = len([e for e in effs if e < c.args.short])
    counts = {}
    for f in c.compared:
        counts[f.limit] = counts.get(f.limit, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    small = (c.run.source == "mx"
             and all(f.size < 512 for f in c.compared)
             and not any(c.hw.hosts[h].nic_pps for h in c.hw.hosts))
    if top == NIC and small:
        fix = ("Small packets: at these sizes a NIC reaches its packet rate "
               "long before its bit rate, and no host declares nic_mpps=.  "
               "Declare it to model that, and check the hosts' CPU with "
               "`during` -- per-packet cost is usually the host, not the "
               "wire.")
    elif top in (NIC, NIC_PPS):
        fix = ("Every host falls short of its NIC together, which a fault on "
               "one host does not do.  Measure the real link speeds "
               "(--speeds), then look at what all the hosts share: the "
               "test's own settings (mx workers, iperf streams), the NIC "
               "driver and firmware, or the congestion control.")
    elif top == UPLINK:
        fix = ("The uplinks are the modelled limit and none of them delivers "
               "it.  The layers above the racks are taken as non-blocking; "
               "if the spine has less capacity than the racks' uplinks "
               "together, that is the bottleneck.")
    else:
        fix = ("Flows fell short of targets the hardware has room for, "
               "everywhere at once: look at the sending side -- `during` on "
               "a host, and the agents' own logs -- before the network.")
    sev = CRITICAL if med < c.args.fail else WARN
    return [Finding(RULE_BY_ID["FLEET_SHORT"], sev, "*",
                    "the fleet reaches %.0f%% of what its hardware allows "
                    "(median host; %d of %d hosts short of %g%%), mostly "
                    "against %s"
                    % (med, short, len(effs), c.args.short, {
                        TARGET: "targets the hardware has room for",
                        NIC: "the NICs", NIC_PPS: "the NICs' packet rate",
                        UPLINK: "the uplinks",
                    }.get(top, "nothing declared")),
                    fix)], None


def r_flow_short(c):
    ok = c.args.short
    paths = sorted((f for f in c.compared
                    if f.efficiency < ok
                    and f.src not in c.explained and f.dst not in c.explained
                    and (c.hosts[f.src].efficiency or 0) >= ok
                    and (c.hosts[f.dst].efficiency or 0) >= ok),
                   key=lambda f: (f.efficiency, f.src, f.dst))
    if not paths:
        return [], None
    f = paths[0]
    sev = WARN if f.efficiency < c.args.fail or len(paths) > 1 else INFO
    return [Finding(RULE_BY_ID["FLOW_SHORT"], sev, f.src,
                    "%d flow(s) fall short between hosts that are otherwise "
                    "fine -- worst %s at %.0f%% of expected"
                    % (len(paths), _flow_name(f), f.efficiency),
                    "Those are paths, not hosts: `netmesh check %s %s` for "
                    "loss and RTT on that pair, `netmesh paths` for the links "
                    "it takes.  A pattern in the pairs -- all through one "
                    "spine, all into one rack -- names the link."
                    % (f.src, f.dst))], None


def r_loss(c):
    if c.run.source != "mx":
        return None, "iperf reports no loss"
    lossy = sorted((f for f in c.run.flows
                    if f.loss is not None and f.loss >= LOSS_PCT
                    and f.limit == TARGET),
                   key=lambda f: (-f.loss, f.src, f.dst))
    if not lossy:
        return [], None
    f = lossy[0]
    return [Finding(RULE_BY_ID["LOSS_BELOW_CAPACITY"], WARN, f.src,
                    "%d flow(s) lost %.0f%% or more of their round trips "
                    "although the hardware has room for their target -- "
                    "worst %s, %.2f%%" % (len(lossy), LOSS_PCT,
                                          _flow_name(f), f.loss),
                    "Loss below capacity is not congestion the hardware "
                    "explains.  `netmesh check %s %s` splits it into forward "
                    "and return, and `during` on the receiving host shows "
                    "ring and backlog drops." % (f.src, f.dst))], None


def r_queueing(c):
    if c.idle is None:
        return None, "no --idle baseline: queueing needs the idle RTT"
    if c.run.source != "mx":
        return None, "iperf reports no RTT"
    bloat = c.args.bloat
    grown = sorted((f for f in c.run.flows
                    if f.idle_p50 and f.rtt_p50 is not None
                    and f.rtt_p50 >= bloat * f.idle_p50
                    and f.added_rtt >= MIN_ADDED_RTT_US),
                   key=lambda f: (-f.added_rtt, f.src, f.dst))
    if not grown:
        return [], None
    roomy = [f for f in grown if f.limit == TARGET]
    if roomy:
        f = roomy[0]
        return [Finding(RULE_BY_ID["QUEUEING"], WARN, f.src,
                        "%d flow(s) queue although the hardware has room for "
                        "their traffic -- worst %s, %s idle and %s loaded"
                        % (len(roomy), _flow_name(f), fmt_us(f.idle_p50),
                           fmt_us(f.rtt_p50)),
                        "A queue on a path the declared hardware does not "
                        "fill means traffic this test did not send, or a "
                        "bottleneck the layout does not declare.  `netmesh "
                        "run --baseline` under this load names the hop the "
                        "latency appears at.")], None
    f = grown[0]
    return [Finding(RULE_BY_ID["QUEUEING"], INFO, f.src,
                    "%d flow(s) queue where the model says a link is full -- "
                    "worst %s, %s idle and %s loaded at %s"
                    % (len(grown), _flow_name(f), fmt_us(f.idle_p50),
                       fmt_us(f.rtt_p50), limit_text(f.limit, f.limit_on, c)),
                    "")], None


EVALUATORS = [
    ("LINK_SPEED", r_link_speed), ("NO_REPORT", r_no_report),
    ("UNMODELLED", r_unmodelled), ("NOT_IN_LAYOUT", r_not_in_layout),
    ("ABOVE_HARDWARE", r_above), ("IDLE_OVERLAP", r_idle_overlap),
    ("CPU_BOUND", r_cpu_bound), ("HOST_SHORT", r_host_short),
    ("GROUP_SHORT", r_group_short), ("FLEET_SHORT", r_fleet_short),
    ("FLOW_SHORT", r_flow_short), ("LOSS_BELOW_CAPACITY", r_loss),
    ("QUEUEING", r_queueing),
]


def evaluate(c):
    """Run the rules in precedence order: causes first, so a host already
    explained -- its link came up slow, its agent ran out of CPU -- is not
    reported again as merely short."""
    findings, skipped = [], []
    for rid, fn in EVALUATORS:
        found, why = fn(c)
        if found is None:
            skipped.append((RULE_BY_ID[rid], why))
            continue
        findings.extend(found)
    findings.sort(key=lambda f: (-SEVERITY_ORDER[f.severity],
                                 PRECEDENCE.index(f.rule.id), f.host))
    return findings, skipped


LEADS = {
    "LINK_SPEED": "A link came up below its hardware.",
    "NO_REPORT": "The run is incomplete.",
    "UNMODELLED": "Part of the run could not be modelled.",
    "NOT_IN_LAYOUT": "Part of the run is missing from the layout.",
    "ABOVE_HARDWARE": "The run beat the declared hardware, so the "
                      "declaration is wrong.",
    "IDLE_OVERLAP": "The latency baseline was not idle.",
    "CPU_BOUND": "A test host ran out of CPU before the network ran out of "
                 "capacity.",
    "HOST_SHORT": "The shortfall is on a host, not the fabric.",
    "GROUP_SHORT": "The shortfall is a whole rack.",
    "FLEET_SHORT": "The whole fleet falls short of its hardware.",
    "FLOW_SHORT": "Some paths fall short while the hosts at both ends are "
                  "fine.",
    "LOSS_BELOW_CAPACITY": "Packets are lost below capacity.",
    "QUEUEING": "Queues are building.",
}


def verdict_line(c, findings):
    serious = [f for f in findings if f.severity in (CRITICAL, WARN)]
    if serious:
        f = serious[0]
        # A colon, not a capital: the finding usually opens with a host
        # name, and `B2` is not the host that was measured.
        return "%s: %s." % (LEADS[f.rule.id].rstrip("."), f.say)
    effs = [h.efficiency for h in c.hosts.values() if h.efficiency is not None]
    if not effs:
        return ("Nothing could be compared: no flow has both a measurement "
                "and an expectation.")
    return ("Every host is within %g%% of what its hardware allows (median "
            "host %.0f%%): the fabric delivers what it was built to."
            % (c.args.short, _median(effs)))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Colors(object):
    def __init__(self, on):
        self.on = on

    def _c(self, code, s):
        return "\033[%sm%s\033[0m" % (code, s) if self.on else s

    def crit(self, s):
        return self._c("1;31", s)

    def warn(self, s):
        return self._c("1;33", s)

    def info(self, s):
        return self._c("36", s)

    def dim(self, s):
        return self._c("2", s)

    def bold(self, s):
        return self._c("1", s)


def _wrap(text, indent, width=78):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width - indent:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    return ("\n" + " " * indent).join(lines)


def _pct(v):
    return "-" if v is None else "%.0f%%" % v


def render_hardware(c):
    hw, run = c.hw, c.run
    names = sorted(c.hosts)
    sources = {}
    for n in names:
        src = hw.hosts[n].nic_source or "none"
        sources[src] = sources.get(src, 0) + 1
    said = {"layout": "the layout", "measured": "--speeds",
            "flag": "--nic-gbps", "none": "nothing"}
    parts = ["%d from %s" % (sources[k], said[k])
             for k in ("measured", "layout", "flag", "none") if k in sources]
    speeds = sorted(set(hw.hosts[n].nic_bps for n in names
                        if hw.hosts[n].nic_bps))
    speed_text = (" at %s" % ", ".join(fmt_gbps(s) for s in speeds)
                  if 0 < len(speeds) <= 3 else "")
    out = ["  HARDWARE   %d hosts%s: NIC speed %s"
           % (len(names), speed_text, ", ".join(parts))]
    if hw.groups:
        declared = [g for g in hw.groups.values() if g.uplink_bps]
        shapes = sorted(set("%g x %s" % (g.uplinks, fmt_gbps(g.uplink_gbps * 1e9))
                            for g in declared))
        line = "%d %ss: %d declare uplinks" % (
            len(hw.groups), hw.group_kind, len(declared))
        if shapes and len(shapes) <= 3:
            line += " (%s)" % ", ".join(shapes)
        bare = len(hw.groups) - len(declared)
        if bare:
            line += ", %d do not" % bare
        out.append("             " + line)
    compared = c.compared
    effs = [h.efficiency for h in c.hosts.values() if h.efficiency is not None]
    if compared:
        exp = sum(f.expected for f in compared)
        got = sum(f.achieved for f in compared)
        groups = len(set(f.group for f in compared))
        line = "%d of %d flows compared; " % (len(compared), len(run.flows))
        if groups == 1:
            line += "expected %s, achieved %s (%.0f%%)" % (
                fmt_rate(exp, run.unit), fmt_rate(got, run.unit),
                got / exp * 100.0)
        else:
            line += "achieved %.0f%% of expected, rate-weighted" % (
                got / exp * 100.0)
        if effs:
            line += "; median host %.0f%%" % _median(effs)
        out.append("  EXPECTED   " + _wrap(line, 13))
    else:
        out.append("  EXPECTED   no flow could be compared")
    for note in run.notes:
        out.append("  NOTE       " + _wrap(note, 13))
    out.append("")
    return out


def render_hosts(c, top):
    rows = sorted((h for h in c.hosts.values() if h.efficiency is not None),
                  key=lambda h: (h.efficiency, h.name))[:top]
    if not rows:
        return []
    unit = c.run.unit
    width = max([len(h.name) for h in rows] + [4])
    # The last number is the median of the host's flows, not achieved over
    # expected: a host with one slow peer has one slow flow and stays near
    # 100%, and a host that is itself slow has them all.  Its column says
    # so, or b1 at 559k of 781k reading "98%" looks like arithmetic gone
    # wrong.
    out = ["  HOSTS, worst first"]
    out.append("    %-*s  %12s  %12s  %6s  %s"
               % (width, "host", "expected", "achieved", "median", "limit"))
    for h in rows:
        out.append("    %-*s  %12s  %12s  %6s  %s"
                   % (width, h.name, fmt_rate(h.expected, unit),
                      fmt_rate(h.achieved, unit), _pct(h.efficiency),
                      limit_text(h.limit, h.limit_on, c)))
    out.append("")
    return out


def render_flows(c, top):
    rows = sorted(c.compared, key=lambda f: (f.efficiency, f.src, f.dst))[:top]
    if not rows:
        return []
    unit = c.run.unit
    names = [_flow_name(f) for f in rows]
    width = max(len(n) for n in names)
    extra = c.run.source == "mx"
    out = ["  FLOWS, worst first"]
    head = "    %-*s  %12s  %12s  %5s" % (width, "flow", "expected",
                                          "achieved", "eff")
    if extra:
        head += "  %6s  %8s" % ("loss", "+rtt")
    out.append(head + "  limit")
    for f, name in zip(rows, names):
        line = "    %-*s  %12s  %12s  %5s" % (
            width, name, fmt_rate(f.expected, unit),
            fmt_rate(f.achieved, unit), _pct(f.efficiency))
        if extra:
            # mx writes loss as 100 - replies/requests, so a reply that
            # landed an interval late reads a hair below zero.  The CSV and
            # JSON keep it; the table does not print "-0.00%".
            loss = "-" if f.loss is None else "%.2f%%" % (
                0.0 if abs(f.loss) < 0.005 else f.loss)
            line += "  %6s  %8s" % (loss, fmt_us(f.added_rtt))
        out.append(line + "  " + limit_text(f.limit, f.limit_on, c))
    out.append("")
    return out


def render_human(c, findings, skipped, args, C):
    out = ["%s -- %d hosts, %d flows, %s"
           % (PROG, len(c.hosts), len(c.run.flows), c.run.describe()), ""]
    out.append("  %s  %s" % (C.bold("VERDICT"),
                             _wrap(verdict_line(c, findings), 11)))
    out.append("")
    floor = SEVERITY_ORDER[args.min_severity]
    shown = [f for f in findings if SEVERITY_ORDER[f.severity] >= floor]
    for f in shown:
        tag = {CRITICAL: C.crit("CRITICAL"), WARN: C.warn("WARN    "),
               INFO: C.info("INFO    ")}[f.severity]
        out.append("  %s  %-15s %s" % (tag, f.rule.title, _wrap(f.say, 28)))
    if args.all:
        for r, why in skipped:
            out.append("  %s   %-15s %s" % (C.dim("skipped"), r.title,
                                            C.dim(_wrap(why, 28))))
    if shown or args.all:
        out.append("")
    if not args.quiet:
        out += render_hardware(c)
        out += render_hosts(c, args.top)
        out += render_flows(c, args.top)
        out.append("  %s" % C.bold("ASSUMED"))
        for text in c.assumptions:
            out.append("    * %s" % _wrap(text, 6))
        out.append("")
    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    fixes = [f.fix for f in shown if f.fix and f.severity != INFO]
    if not fixes:
        out.append("    %s" % _wrap(
            "Nothing to chase: the run reached what its hardware allows.  "
            "Keep this run's --overlay; the next one painted beside it shows "
            "what changed.", 4))
    else:
        seen = set()
        for fix in fixes:
            if fix in seen:
                continue
            seen.add(fix)
            out.append("    * %s" % _wrap(fix, 6))
            if len(seen) == 4:
                break
    out.append("")
    return "\n".join(out)


CSV_FIELDS = ["host", "ts", "rule_id", "severity", "title", "detail", "fix"]


def _stamp(c):
    """The run's own last timestamp: two reckonings of one run compare equal."""
    return int(c.run.last_ts) if c.run.last_ts is not None else ""


def render_csv(c, findings, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        ts = _stamp(c)
        if not findings:
            w.writerow({"host": "*", "ts": ts, "rule_id": "OK",
                        "severity": "INFO", "title": "nothing wrong",
                        "detail": "no rule fired", "fix": ""})
        for f in findings:
            w.writerow({"host": f.host, "ts": ts, "rule_id": f.rule.id,
                        "severity": f.severity, "title": f.rule.title,
                        "detail": f.say, "fix": f.fix})
    finally:
        if path:
            fh.close()


FLOW_FIELDS = ["src", "dst", "layer", "unit", "demand", "expected",
               "achieved", "sent", "efficiency_pct", "limit", "limit_on",
               "loss_pct", "rtt_p50_us", "idle_rtt_p50_us", "added_rtt_us",
               "mtu", "mtu_from", "samples"]


def _cell(v, fmt="%.3f"):
    if v is None:
        return ""
    if isinstance(v, float):
        return fmt % v
    return v


def _on_text(on):
    return "" if not on else ":".join(str(p) for p in on)


def flow_record(f, unit):
    return {
        "src": f.src, "dst": f.dst, "layer": f.layer, "unit": unit,
        "demand": f.demand, "expected": f.expected, "achieved": f.achieved,
        "sent": f.sent, "efficiency_pct": f.efficiency, "limit": f.limit,
        "limit_on": _on_text(f.limit_on), "loss_pct": f.loss,
        "rtt_p50_us": f.rtt_p50, "idle_rtt_p50_us": f.idle_p50,
        "added_rtt_us": f.added_rtt, "mtu": f.mtu, "mtu_from": f.mtu_from,
        "samples": f.samples,
    }


def render_flows_csv(c, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=FLOW_FIELDS, lineterminator="\n")
        w.writeheader()
        for f in sorted(c.run.flows, key=lambda f: (f.src, f.dst, f.layer or "")):
            rec = flow_record(f, c.run.unit)
            w.writerow(dict((k, _cell(v)) for k, v in rec.items()))
    finally:
        if path:
            fh.close()


def render_json(c, findings, skipped, path):
    hw = c.hw
    doc = {
        "meta": {"tool": "reckon", "version": VERSION, "ts": _stamp(c) or None,
                 "source": c.run.source, "unit": c.run.unit,
                 "describe": c.run.describe(), "mode": c.run.mode,
                 "run": c.run.run_id, "window": c.run.window,
                 "layout": hw.layout, "short_pct": c.args.short,
                 "fail_pct": c.args.fail, "notes": c.run.notes},
        "assumptions": c.assumptions,
        "hardware": {
            "group_kind": hw.group_kind,
            "hosts": dict((n, {
                "nic_gbps": h.nic_bps / 1e9 if h.nic_bps else None,
                "nic_source": h.nic_source,
                "declared_gbps": h.declared_bps / 1e9 if h.declared_bps else None,
                "measured_gbps": h.measured_bps / 1e9 if h.measured_bps else None,
                "measured_raw": h.measured_raw,
                "nic_mpps": h.nic_pps / 1e6 if h.nic_pps else None,
                "mtu": h.mtu, "group": h.group, "in_layout": h.placed,
            }) for n, h in sorted(hw.hosts.items())),
            "groups": dict((n, {
                "label": g.label, "uplinks": g.uplinks,
                "uplink_gbps": g.uplink_gbps, "hosts": sorted(g.hosts),
            }) for n, g in sorted(hw.groups.items())),
        },
        "hosts": [{
            "host": h.name, "expected": h.expected, "achieved": h.achieved,
            "efficiency_pct": h.efficiency, "limit": h.limit,
            "limit_on": _on_text(h.limit_on), "verdict": h.verdict,
            "reported": h.reported, "compared_flows": h.compared,
            "short_flows": h.short, "added_rtt_us": h.added_rtt,
        } for _n, h in sorted(c.hosts.items())],
        "flows": [flow_record(f, c.run.unit) for f in
                  sorted(c.run.flows, key=lambda f: (f.src, f.dst, f.layer or ""))],
        "findings": [{"rule_id": f.rule.id, "severity": f.severity,
                      "host": f.host, "title": f.rule.title,
                      "detail": f.say, "fix": f.fix} for f in findings],
        "skipped": [{"rule_id": r.id, "reason": why} for r, why in skipped],
    }
    text = json.dumps(doc, indent=2, sort_keys=True)
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


# The overlay: the datacenter viewer's own results format, the one `mx
# export` and `iperf-orchestrator export-overlay` already write, so it paints
# beside theirs with no importer.  Efficiency is on a diverging ramp pinned
# at 0-200% for the reason mx_achieved is: 100% is "what this hardware
# should do", the midpoint rather than the top, and a flow above it is as
# much a finding as one below.

def overlay_tests(c):
    unit, mx = c.run.unit, c.run.source == "mx"
    dec = 0 if unit == "pps" else 1
    tests = [
        ("expected", 'unit=%s\thigher=good\tdecimals=%d\tshort=EXP\t'
                     'label="Expected from the hardware"' % (unit, dec)),
        ("achieved", 'unit=%s\thigher=good\tdecimals=%d\tshort=ACH\t'
                     'label="%s"' % (unit, dec, "Round trips completed"
                                     if mx else "Achieved")),
        ("efficiency", 'unit=%\thigher=good\tpalette=rdbu\tmin=0\tmax=200\t'
                       'agg=median\tdecimals=0\tshort=EFF\t'
                       'label="Achieved vs what the hardware allows"'),
        ("limit", 'short=LIM\tlabel="What the hardware limits it to"'),
        ("verdict", 'short=VERD\tlabel="Against the hardware"'),
        ("nic_gbps", 'unit=Gb/s\thigher=good\tagg=min\tdecimals=0\tshort=NIC\t'
                     'label="NIC speed the model used"'),
    ]
    if c.idle is not None and mx:
        tests.append(("added_rtt", 'unit=us\thigher=bad\tagg=max\tdecimals=0\t'
                                   'short=+RTT\tlabel="RTT added under load, '
                                   'worst peer"'))
    peer = [("peer_efficiency", 'unit=%\thigher=good\tpalette=rdbu\tmin=0\t'
                                'max=200\tagg=median\tdecimals=0\tshort=EFF\t'
                                'label="Flow vs what the hardware allows"')]
    if c.idle is not None and mx:
        peer.append(("peer_added_rtt", 'unit=us\thigher=bad\tagg=max\t'
                                       'decimals=0\tshort=+RTT\t'
                                       'label="RTT added under load, one peer"'))
    return tests, peer


def _meta_value(v):
    v = str(v)
    if re.search(r'[\s,"\']', v):
        return '"%s"' % v.replace('"', "'")
    return v


def render_overlay(c, prefix, run_label, path):
    tests, peer_tests = overlay_tests(c)
    unit = c.run.unit
    dec = 0 if unit == "pps" else 1
    lines = ["# reckon %s -- what each host and flow achieved against what "
             "its hardware allows" % VERSION,
             "# %s; %d hosts, %d flows, %d compared"
             % (c.run.describe(), len(c.hosts), len(c.run.flows),
                len(c.compared))]
    if c.hw.layout:
        lines.append("# layout %s" % c.hw.layout)
    for text in c.assumptions:
        lines.append("# assumed: %s" % text)
    for name, meta in tests + peer_tests:
        lines.append("!test\t%s%s\t%s" % (prefix, name, meta))

    extra = ("\trun=%s" % _meta_value(run_label)) if run_label else ""

    def sample(test, target, value, **meta):
        bits = "".join("\t%s=%s" % (k, _meta_value(v))
                       for k, v in sorted(meta.items()) if v not in (None, ""))
        lines.append("%s%s\t%s\t%s%s%s" % (prefix, test, target, value, bits,
                                           extra))

    for name in sorted(c.hosts):
        h = c.hosts[name]
        hh = c.hw.hosts.get(name)
        lim = (limit_text(h.limit, h.limit_on, c)
               if h.limit not in (None, TARGET) else None)
        if h.expected is not None:
            sample("expected", name, "%.*f" % (dec, h.expected), limit=lim)
        if h.achieved is not None and h.expected is not None:
            sample("achieved", name, "%.*f" % (dec, h.achieved))
        if h.efficiency is not None:
            # The median of how many flows, and how many of them short: a
            # host at 98% over two flows is not the claim one is over sixty.
            sample("efficiency", name, "%.1f" % h.efficiency,
                   flows=h.compared, short_flows=h.short)
        if h.limit is not None:
            sample("limit", name, h.limit, on=lim)
        if h.verdict is not None:
            sample("verdict", name, h.verdict)
        if hh is not None and hh.nic_bps:
            sample("nic_gbps", name, ("%.3f" % (hh.nic_bps / 1e9)).rstrip("0").rstrip("."),
                   source=hh.nic_source)
        if h.added_rtt is not None and c.idle is not None:
            sample("added_rtt", name, "%.0f" % h.added_rtt)
    for f in sorted(c.compared, key=lambda f: (f.src, f.dst, f.layer or "")):
        sample("peer_efficiency", f.src, "%.1f" % f.efficiency, peer=f.dst,
               layer=f.layer, limit=limit_text(f.limit, f.limit_on, c))
        if f.added_rtt is not None and c.idle is not None:
            sample("peer_added_rtt", f.src, "%.0f" % f.added_rtt, peer=f.dst,
                   layer=f.layer)
    text = "\n".join(lines) + "\n"
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("layout_arg", nargs="?", metavar="LAYOUT",
                   help="the .dc layout the run was measured on")
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("--layout", default=_env("LAYOUT"), metavar="FILE")
    p.add_argument("--mx", nargs="+", metavar="PATH")
    p.add_argument("--iperf", metavar="PATH")
    p.add_argument("--iperf-mode", default=_env("IPERF_MODE"),
                   choices=list(IPERF_MODES) + [None], metavar="MODE")
    p.add_argument("--idle", nargs="+", metavar="PATH")
    p.add_argument("--speeds", metavar="FILE")
    p.add_argument("--names", metavar="FILE")
    p.add_argument("--nic-gbps", type=float, metavar="G",
                   default=_env_num("NIC_GBPS", float, None))
    p.add_argument("--group-kind", metavar="KIND",
                   default=_env("GROUP_KIND", DEFAULT_GROUP_KIND))
    p.add_argument("--window", type=int, metavar="S",
                   default=_env_num("WINDOW", int, DEFAULT_WINDOW))
    p.add_argument("--short", type=float, metavar="PCT",
                   default=_env_num("SHORT", float, DEFAULT_SHORT))
    p.add_argument("--fail", type=float, metavar="PCT",
                   default=_env_num("FAIL", float, DEFAULT_FAIL))
    p.add_argument("--bloat", type=float, metavar="F",
                   default=_env_num("BLOAT", float, DEFAULT_BLOAT))
    p.add_argument("--mtu", type=int, metavar="N",
                   default=_env_num("MTU", int, DEFAULT_MTU))
    p.add_argument("--top", type=int, metavar="N",
                   default=_env_num("TOP", int, DEFAULT_TOP))
    p.add_argument("--overlay", nargs="?", const="", metavar="PATH")
    p.add_argument("--prefix", default=_env("PREFIX", DEFAULT_PREFIX))
    p.add_argument("--run", default=_env("RUN"), metavar="LABEL")
    p.add_argument("--flows", nargs="?", const="", metavar="PATH")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--min-severity", default=_env("MIN_SEVERITY", "info"),
                   choices=["info", "warn", "critical"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--exit-code", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--rules", action="store_true")
    p.add_argument("--explain", metavar="RULE_ID")
    return p


def cmd_rules():
    out = []
    for r in RULES:
        out.append("%-20s %s" % (r.id, r.title))
        out.append("    %s" % _wrap(r.why, 4))
        out.append("")
    sys.stdout.write("\n".join(out))
    return 0


def cmd_explain(rid):
    r = RULE_BY_ID.get(rid.upper())
    if r is None:
        die("no rule %s -- `%s --rules` lists them" % (rid, PROG))
    sys.stdout.write("%s -- %s\n\n%s\n" % (r.id, r.title, _wrap(r.why, 0)))
    return 0


def check_args(args):
    """Every number refused here, before any file is read.

    Each of these is a setting a typo can make meaningless, and acting on
    a meaningless one prints a confident report: an MTU of 0 divides by a
    negative payload, a NIC of 0 Gb/s expects nothing of anyone.
    """
    if args.nic_gbps is not None and not (math.isfinite(args.nic_gbps)
                                          and args.nic_gbps > 0):
        die("--nic-gbps must be a speed above zero, not %g" % args.nic_gbps)
    if not 68 <= args.mtu <= 65535:
        die("--mtu must be between 68 and 65535, not %d" % args.mtu)
    if args.window < 0:
        die("--window must be 0 (everything) or a number of seconds")
    for flag, v in (("--short", args.short), ("--fail", args.fail),
                    ("--bloat", args.bloat)):
        if not math.isfinite(v) or v < 0:
            die("%s must be a number, 0 or more, not %g" % (flag, v))
    if args.fail > args.short:
        die("--fail (%g) is the lower line and --short (%g) the upper"
            % (args.fail, args.short))
    if args.top < 0:
        die("--top must be 0 or more")
    if not args.group_kind:
        die("--group-kind needs a container kind, such as rack")
    if bool(args.mx) == bool(args.iperf):
        die("give one run to compare: --mx reports/ or --iperf FILE "
            "(reckon them one at a time)")


def main(argv=None):
    _stdio_safe()
    args = build_parser().parse_args(argv)
    if args.rules:
        return cmd_rules()
    if args.explain:
        return cmd_explain(args.explain)
    if args.layout_arg and args.layout and args.layout_arg != args.layout:
        die("two layouts: %s and --layout %s" % (args.layout_arg, args.layout))
    layout = args.layout_arg or args.layout
    check_args(args)
    # `-` is stdout, as everywhere else.  Taken literally it is a file
    # called `-` in the current directory -- one of those has already been
    # committed to this repository by accident once.
    for key in ("overlay", "flows", "csv", "json"):
        if getattr(args, key) == "-":
            setattr(args, key, "")
    if layout and not os.path.isfile(layout):
        die("no such layout: %s" % layout)

    names = load_names(args.names) if args.names else {}

    def rename(name):
        return names.get(name, name)

    if args.mx:
        run = load_mx(args.mx, args.window, rename)
    else:
        run = load_iperf(args.iperf, args.iperf_mode, rename)
    speeds = load_speeds(args.speeds, rename) if args.speeds else None
    hw = build_hardware(sorted(run.seen | run.reported), layout, speeds, args)
    idle, dropped = None, 0
    if args.idle:
        busy = ((run.first_ts, run.last_ts)
                if run.first_ts is not None else None)
        idle, dropped = load_idle(args.idle, rename, busy)
    args.min_severity = {"info": INFO, "warn": WARN,
                         "critical": CRITICAL}[args.min_severity]

    c = analyse(run, hw, idle, dropped, args)
    findings, skipped = evaluate(c)

    if args.overlay is not None:
        with _WriteGuard(args.overlay or None):
            render_overlay(c, args.prefix, args.run, args.overlay or None)
    if args.flows is not None:
        with _WriteGuard(args.flows or None):
            render_flows_csv(c, args.flows or None)
    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            render_csv(c, findings, args.csv or None)
    if args.json is not None:
        with _WriteGuard(args.json or None):
            render_json(c, findings, skipped, args.json or None)
    to_stdout = [v for v in (args.overlay, args.flows, args.csv, args.json)
                 if v == ""]
    if not to_stdout:
        use_color = (not args.no_color and not os.environ.get("NO_COLOR")
                     and sys.stdout.isatty())
        sys.stdout.write(render_human(c, findings, skipped, args,
                                      Colors(use_color)) + "\n")

    if not c.compared:
        if not any(h.nic_bps for h in hw.hosts.values()):
            why = ("no host has a NIC speed -- declare nic_gbps= in the "
                   "layout, or pass --nic-gbps")
        else:
            why = "no flow has both a measurement and an expectation"
        sys.stderr.write("%s: nothing to compare: %s\n" % (PROG, why))
        return 1
    if args.exit_code and findings:
        worst = max(SEVERITY_ORDER[f.severity] for f in findings)
        # 10/20 rather than 1/2, so a severity can never be mistaken for a
        # usage error or for "nothing to compare".
        return {3: 20, 2: 10, 1: 0}[worst]
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)

# ---------------------------------------------------------------------------
# Why this file has no imports from its siblings
# ---------------------------------------------------------------------------
#
# Every module in binnacle is a complete, standalone program: standard
# library only, and nothing imported from the rest of the package. That is
# not tidiness, it is a requirement. These files get copied to machines that
# have never heard of binnacle -- `agree script ./reckon.py` pushes this file
# to a fleet, and a copy beside a run's reports on a jump host is the usual
# way it is used -- and a relative import would break the moment it landed.
# A helper duplicated across two modules, with a comment naming the
# canonical copy, is the accepted cost of that.
