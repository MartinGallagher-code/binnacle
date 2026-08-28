#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""logtriage.py -- find the ten lines that matter in a million lines of log.

Usage: logtriage.py FILE [FILE...]        rank what is interesting
       logtriage.py -                     read standard input
       logtriage.py FILE --split-at 14:20 what started at 14:20?
       logtriage.py FILE --baseline OLD   what is here that was not there?
       logtriage.py FILE --csv            one row per template
       logtriage.py --mask 'LINE'         show how one line is templated
       logtriage.py --parse 'LINE'        show how one line is split up

Options:
  --top N              how many templates to show            (10)
  --min-count N        ignore templates seen fewer times     (1)
  --min-severity L     info | warn | error | crit
  --split F            fraction of the log that is baseline  (0.5)
  --split-at TIME      baseline is everything before TIME (HH:MM, ISO,
                       or a relative -30m / -2h)
  --baseline FILE      use a separate reference log instead of splitting
  --baseline-templates FILE   load a saved template digest as the baseline
  --save-templates FILE       write a template digest for next time
  --since T / --until T       only consider records in this window
  --bucket S           seconds per time bucket (default: chosen to fit)
  --burst-factor N     peak/mean ratio that counts as a burst   (5)
  --weights K=V,...    sev, novel, burst, freq  (default 3,3,2,1)
  --by program|none    keep templates from different programs apart (program)
  --multiline auto|off attach stack traces to the line above    (auto)
  --rule 'RE=>NAME'    mask something of your own, before the built-ins
  --keep-paths / --keep-quoted / --keep-numbers   turn off a built-in mask
  --max-templates N    memory guard; the rest are pooled       (20000)
  --max-template-len N truncate a template at this many chars   (512)
  --example-len N      how much of an example line to keep     (300)
  --csv [PATH] / --json [PATH]
  --ascii              plain sparklines (automatic when LANG is C/POSIX)

How it works
  Log lines are not distinct events; they are a few hundred *shapes* with
  the variable parts filled in differently.  This masks the variable parts
  -- numbers, addresses, paths, hex, quoted strings, timestamps -- and what
  is left is the shape, which is what you want to count.

  Ranking is deliberately not by frequency.  The most frequent line in any
  log is something like "session opened for user", and it has never once
  been the answer.  What matters is what is NEW, what is SEVERE, and what
  SUDDENLY started happening -- so novelty, severity and burstiness carry
  more weight than count, and the score is printed so you can see why
  something is at the top.

  Novelty needs a baseline.  By default the log is split in half and the
  first half is the baseline, which answers "what changed" with no state,
  no config and no second file.  --split-at is the sharper version: give it
  the time the pager went off and it tells you what started then.

Masking, in order (the order is the algorithm)
  ANSI, timestamps (ISO/syslog/CLF/dmesg/clock), UUID, MAC, IPv6, IPv4,
  URL, email, pid, absolute path, hex, quoted string, size+unit, number.
  A UUID has to be caught before the hex rule would eat it, and timestamps
  before the number rule; --rule inserts your own patterns ahead of all of
  them so you always win.

Robustness properties
  * streams: one pass, bounded memory, fine on a multi-gigabyte file
  * template ids are a hash of the masked shape, so the same line gives the
    same id on every machine -- which is what lets a fleet be compared
  * bounded memory is honest: if --max-templates is hit, the evicted ones
    are pooled into <OTHER> and the report says how many, rather than
    quietly dropping them
  * stack traces attach to the line above, so 400 tracebacks are one
    finding and not 4,800 fake ones
  * no timestamps at all is fine: ordering falls back to line number and
    the report says so
  * compressed input (.gz, .bz2, .xz) is read directly
  * pure ASCII output under LANG=C, like the rest of these tools
"""

import argparse
import bz2
import csv
import glob as globmod
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import sys
import time
from datetime import datetime, timedelta

VERSION = "0.5.0"
PROG = os.path.basename(sys.argv[0]) or "logtriage.py"

MAX_BUCKETS = 4096
SPARK_COLS = 20
SPARK_UNI = "▁▂▃▄▅▆▇█"
SPARK_ASCII = ".:-=+*#%@"


def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    On a box with LANG=C -- a RHEL 8 default, and the floor this package
    targets -- stdout is ASCII, so a single UTF-8 name echoed out of a log,
    a process table or a remote command turns the whole report into a
    UnicodeEncodeError.  Degrading the character is the same bargain the
    rest of these tools already make with --ascii: the run is worth more
    than the byte.
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


class _WriteGuard(object):
    """Full disk, quota, missing directory: name the file and stop.

    Wraps a block writing an output the user asked for by path.  Only
    OSError becomes the message, and only when a path was really given,
    so stdout keeps its ordinary pipe semantics.  A traceback here would
    bury the one fact that matters: which file could not be written.

    The partial file is removed too -- a half-written CSV that parses is
    worse than none -- except when appending, where what was already
    there predates this failure and is still good.
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
    v = os.environ.get("LOGTRIAGE_" + name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------
#
# Ordered substitutions.  Order IS the algorithm: a UUID must be caught
# before the hex rule eats it, an IPv4 before the number rule, a timestamp
# before both.  Each entry is (name, compiled_regex, replacement).
#
# This table is the canonical copy; agree.py keeps a trimmed copy of the
# timestamp entries for its --mask-times, and says so.

TIME_MASKS = [
    ("iso8601", re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    ("syslog", re.compile(
        r"\b[A-Z][a-z]{2}\s{1,2}\d{1,2} \d{2}:\d{2}:\d{2}\b"), "<TS>"),
    ("clf", re.compile(
        r"\[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]"), "<TS>"),
    ("dmesg", re.compile(r"\[\s*\d+\.\d{6}\]"), "<TS>"),
    ("clock", re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<TS>"),
]

BASE_MASKS = [
    ("ansi", re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),
] + TIME_MASKS + [
    ("uuid", re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    ("mac", re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "<MAC>"),
    ("ipv6", re.compile(
        r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b|::[0-9a-fA-F:]+"),
     "<IP6>"),
    ("ipv4", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    ("url", re.compile(r"\b[a-z][a-z0-9+.-]*://\S+"), "<URL>"),
    ("email", re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b"), "<EMAIL>"),
    ("pid", re.compile(r"\b(?:pid|PID)[= ]\d+|\[\d{1,7}\](?=[: ])"), "<PID>"),
    ("path", re.compile(r"(?<![\w])/[\w./@+-]{2,}"), "<PATH>"),
    ("hex", re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b"), "<HEX>"),
    ("quoted", re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"), "<STR>"),
    ("unit", re.compile(
        r"\b\d+(?:\.\d+)?\s?(?:ns|us|ms|sec|s|min|m|h|[KMGTP]i?B?)\b"),
     "<NUM><UNIT>"),
    ("number", re.compile(r"[+-]?\b\d+(?:\.\d+)?\b"), "<NUM>"),
]

OPTIONAL = {"path": "keep_paths", "quoted": "keep_quoted",
            "number": "keep_numbers"}


def build_masks(args):
    masks = []
    for spec in (args.rule or []):
        if "=>" not in spec:
            die("--rule wants 'REGEX=>NAME', got %r" % spec)
        pat, name = spec.split("=>", 1)
        try:
            masks.append(("user:" + name.strip(), re.compile(pat),
                          "<%s>" % name.strip().upper()))
        except re.error as exc:
            die("--rule %r is not a valid regex: %s" % (pat, exc))
    for name, rx, rep in BASE_MASKS:
        opt = OPTIONAL.get(name)
        if opt and getattr(args, opt, False):
            continue
        masks.append((name, rx, rep))
    return masks


WS = re.compile(r"\s+")


def mask_line(text, masks, max_len):
    for _, rx, rep in masks:
        text = rx.sub(rep, text)
    return WS.sub(" ", text).strip()[:max_len]


def template_id(program, key):
    h = hashlib.sha1()
    h.update(((program or "") + "\x00" + key).encode("utf-8", "replace"))
    return h.hexdigest()[:10]


# ---------------------------------------------------------------------------
# Prefix parsing
# ---------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

P_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"(?:(?P<host>[\w.-]+)\s+)?"
    r"(?P<prog>[\w./-]+?)(?:\[(?P<pid>\d+)\])?:\s(?P<msg>.*)$")
P_SYSLOG = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s{1,2}(?P<day>\d{1,2}) "
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>[\w.-]+)\s+"
    r"(?P<prog>[\w./-]+?)(?:\[(?P<pid>\d+)\])?:\s(?P<msg>.*)$")
P_DMESG_T = re.compile(
    r"^\[(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d+ "
    r"\d{2}:\d{2}:\d{2} \d{4})\]\s(?P<msg>.*)$")
P_DMESG = re.compile(r"^\[\s*(?P<mono>\d+\.\d{6})\]\s(?P<msg>.*)$")
P_CLF = re.compile(
    r"^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] \"(?P<msg>[^\"]*)\" "
    r"(?P<code>\d{3}) (?P<size>\S+)")

Record = None


def parse_line(line, base_year=None):
    """-> (ts_epoch_or_None, host, program, pid, message, fmt)."""
    line = line.rstrip("\n").rstrip("\r")
    if not line:
        return None

    if line.startswith("{"):
        try:
            doc = json.loads(line)
            if isinstance(doc, dict):
                msg = None
                for k in ("msg", "message", "log", "event"):
                    if isinstance(doc.get(k), str):
                        msg = doc[k]
                        break
                if msg is not None:
                    ts = None
                    for k in ("ts", "time", "timestamp", "@timestamp"):
                        if k in doc:
                            ts = _parse_ts_any(str(doc[k]))
                            break
                    prog = None
                    for k in ("logger", "program", "service", "unit"):
                        if isinstance(doc.get(k), str):
                            prog = doc[k]
                            break
                    lvl = doc.get("level") or doc.get("severity")
                    if isinstance(lvl, str):
                        msg = "%s %s" % (lvl.upper(), msg)
                    return ts, None, prog, None, msg, "json"
        except ValueError:
            pass

    m = P_ISO.match(line)
    if m:
        return (_parse_ts_any(m.group("ts")), m.group("host"),
                m.group("prog"), m.group("pid"), m.group("msg"), "iso")

    m = P_SYSLOG.match(line)
    if m:
        ts = _syslog_ts(m.group("mon"), m.group("day"), m.group("time"),
                        base_year)
        return (ts, m.group("host"), m.group("prog"), m.group("pid"),
                m.group("msg"), "syslog")

    m = P_DMESG_T.match(line)
    if m:
        return (_parse_ts_any(m.group("ts")), None, "kernel", None,
                m.group("msg"), "dmesg-T")

    m = P_DMESG.match(line)
    if m:
        return None, None, "kernel", None, m.group("msg"), "dmesg"

    m = P_CLF.match(line)
    if m:
        return (_parse_clf(m.group("ts")), None, "http", None,
                "%s %s" % (m.group("code"), m.group("msg")), "clf")

    return None, None, None, None, line, "bare"


def _parse_ts_any(s):
    s = s.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%a %b %d %H:%M:%S %Y"):
        try:
            return time.mktime(datetime.strptime(s[:26], fmt).timetuple())
        except (ValueError, OverflowError):
            continue
    # Numeric epoch (seconds, milliseconds or nanoseconds).
    try:
        v = float(s)
        if v > 1e17:
            return v / 1e9
        if v > 1e11:
            return v / 1000.0
        if v > 1e8:
            return v
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
            return time.mktime(d.timetuple()) + int(m.group(2)) * 3600 \
                + int(m.group(3)) * 60 + int(m.group(4))
        except ValueError:
            pass
    return None


def _syslog_ts(mon, day, clock, base_year):
    """Syslog has no year.  Use the file's year, or this one."""
    year = base_year or datetime.now().year
    try:
        h, mi, s = (int(x) for x in clock.split(":"))
        dt = datetime(year, MONTHS[mon], int(day), h, mi, s)
        return time.mktime(dt.timetuple())
    except (ValueError, KeyError):
        return None


def _parse_clf(s):
    try:
        return time.mktime(datetime.strptime(s.split()[0],
                                             "%d/%b/%Y:%H:%M:%S").timetuple())
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Multi-line records
# ---------------------------------------------------------------------------
#
# A traceback is one event, not forty.  Left alone it manufactures forty
# fake templates, which does not merely add noise -- it destroys the
# ranking, because each fragment looks novel.

CONTINUATION = re.compile(
    r"^\s*(?:at [\w$.<>]+\(|\.\.\. \d+ more|Caused by:|"
    r"File \".*\", line \d+|#\d+ +0x[0-9a-f]+|"
    r"Traceback \(most recent call last\):)|"
    r"^[ \t]+\S")


def is_continuation(line):
    if not line or not line.strip():
        return False
    if CONTINUATION.match(line):
        # A line that itself carries a recognisable prefix is a new record,
        # even if it happens to start with whitespace.
        ts, _, prog, _, _, fmt = parse_line(line)
        return fmt == "bare"
    return False


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

SEV_NAMES = {0: "info", 1: "warn", 2: "error", 3: "crit"}
SEV_LEVELS = {"info": 0, "warn": 1, "error": 2, "crit": 3}

SEV_PATTERNS = [
    (3, re.compile(
        r"\bpanic\b|\boops\b|BUG:|segfault|general protection|oom-kill|"
        r"out of memory|I/O error|corrupt|kernel NULL|call trace|"
        r"\bemerg\b|\bfatal\b|\balert\b", re.I)),
    (2, re.compile(
        r"\berror\b|\berr\b|\bfail(?:ed|ure|s)?\b|refused|denied|"
        r"timed? ?out|unreachable|cannot|can't|unable to|no space|"
        r"too many open files|\bcrit\b|exception|rejected|aborted", re.I)),
    (1, re.compile(
        r"\bwarn(?:ing)?\b|deprecat|retry|retrying|slow|throttl|degraded|"
        r"backoff|\bnotice\b", re.I)),
]


def severity_of(raw):
    for level, rx in SEV_PATTERNS:
        if rx.search(raw):
            return level
    return 0


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class Template(object):
    __slots__ = ("tid", "key", "program", "count", "first", "last",
                 "example", "sev", "buckets", "early", "late",
                 "first_idx", "last_idx")

    def __init__(self, tid, key, program):
        self.tid = tid
        self.key = key
        self.program = program
        self.count = 0
        self.first = None
        self.last = None
        self.first_idx = None
        self.last_idx = None
        self.example = ""
        self.sev = 0
        self.buckets = {}
        self.early = 0
        self.late = 0

    def add(self, ts, idx, raw, sev, example_len):
        self.count += 1
        if self.first_idx is None:
            self.first_idx = idx
            self.example = raw[:example_len]
        self.last_idx = idx
        if ts is not None:
            if self.first is None or ts < self.first:
                self.first = ts
            if self.last is None or ts > self.last:
                self.last = ts
        if sev > self.sev:
            self.sev = sev
            self.example = raw[:example_len]


class Counter(object):
    """Streaming template counter with a hard memory bound."""

    def __init__(self, args, masks):
        self.args = args
        self.masks = masks
        self.templates = {}
        self.order = []
        self.bucket_width = 1.0
        self.bucket_origin = None
        self.n_records = 0
        self.n_lines = 0
        self.evicted = 0
        self.evicted_count = 0
        self.no_ts = 0
        self.min_ts = None
        self.max_ts = None
        self.formats = {}

    # -- bucketing ---------------------------------------------------------
    def _bucket(self, ts):
        if self.bucket_origin is None:
            self.bucket_origin = ts
        return int((ts - self.bucket_origin) // self.bucket_width)

    def _maybe_widen(self, idx):
        """Double the bucket width when the span outgrows the budget.

        Amortised O(total buckets) across all doublings, and it means the
        caller never has to know the log's span in advance.
        """
        while idx >= MAX_BUCKETS:
            self.bucket_width *= 2
            idx //= 2
            for t in self.templates.values():
                merged = {}
                for k, v in t.buckets.items():
                    merged[k // 2] = merged.get(k // 2, 0) + v
                t.buckets = merged
        return idx

    # -- admission ---------------------------------------------------------
    def _evict(self):
        """Drop the coldest tenth into <OTHER>, and keep admitting.

        Never stop learning: the whole point is the template that turns up
        late, so a full table must not become a closed one.
        """
        if len(self.templates) <= 1:
            return
        victims = sorted(self.templates.values(),
                         key=lambda t: t.count)[:max(1, len(self.templates) // 10)]
        for t in victims:
            if t.tid == "<OTHER>":
                continue
            self.evicted += 1
            self.evicted_count += t.count
            del self.templates[t.tid]

    def add(self, ts, raw, program):
        self.n_records += 1
        idx = self.n_records
        if ts is not None:
            self.min_ts = ts if self.min_ts is None else min(self.min_ts, ts)
            self.max_ts = ts if self.max_ts is None else max(self.max_ts, ts)
        else:
            self.no_ts += 1

        key = mask_line(raw, self.masks, self.args.max_template_len)
        prog = program if self.args.by == "program" else None
        tid = template_id(prog, key)

        t = self.templates.get(tid)
        if t is None:
            if len(self.templates) >= self.args.max_templates:
                self._evict()
            t = self.templates[tid] = Template(tid, key, prog)
        t.add(ts, idx, raw, severity_of(raw), self.args.example_len)

        if ts is not None:
            b = self._maybe_widen(self._bucket(ts))
            t.buckets[b] = t.buckets.get(b, 0) + 1

    # -- baseline ----------------------------------------------------------
    def split_point(self):
        """Where does 'before' end and 'after' begin?"""
        if self.args.split_at:
            return self.args.split_at_epoch
        if self.min_ts is not None and self.max_ts is not None and \
                self.max_ts > self.min_ts:
            return self.min_ts + (self.max_ts - self.min_ts) * self.args.split
        return None

    def apply_split(self):
        cut = self.split_point()
        if cut is None:
            # No usable clock: split by record index instead, and say so.
            cut_idx = int(self.n_records * self.args.split)
            for t in self.templates.values():
                if t.last_idx is not None and t.last_idx <= cut_idx:
                    t.early, t.late = t.count, 0
                elif t.first_idx is not None and t.first_idx > cut_idx:
                    t.early, t.late = 0, t.count
                else:
                    t.early = t.late = t.count // 2
            return None
        for t in self.templates.values():
            early = late = 0
            for b, n in t.buckets.items():
                bts = (self.bucket_origin or 0) + b * self.bucket_width
                if bts < cut:
                    early += n
                else:
                    late += n
            if not t.buckets:
                early, late = t.count, 0
            t.early, t.late = early, late
        return cut


# ---------------------------------------------------------------------------
# Reading input
# ---------------------------------------------------------------------------

def open_any(path):
    if path == "-":
        return sys.stdin
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace")
    if path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8-sig", errors="replace")
    if path.endswith(".xz") or path.endswith(".lzma"):
        return lzma.open(path, "rt", encoding="utf-8-sig", errors="replace")
    return io.open(path, "r", encoding="utf-8-sig", errors="replace")


def expand_inputs(paths):
    out = []
    for p in paths:
        if p == "-":
            out.append(p)
        elif any(c in p for c in "*?["):
            out.extend(sorted(globmod.glob(p)))
        else:
            out.append(p)
    return out


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _shift_year(ts, delta):
    dt = datetime.fromtimestamp(ts)
    try:
        return time.mktime(dt.replace(year=dt.year + delta).timetuple())
    except ValueError:          # Feb 29 shifted into a non-leap year
        return ts + delta * 31536000


def _unroll_year(ts, anchor, prev_ts):
    """Put a yearless syslog timestamp in the right year.

    Syslog lines carry no year, and one file crosses New Year's Eve every
    year it survives long enough.  Stamping every line with the file's
    year put "Dec 31" eleven months into the future, which reversed the
    span, dropped the midnight incident into the baseline, and ranked a
    routine heartbeat above it.  Two facts pin the year down: no line can
    be written after the file's last write (so anything "later" than
    mtime belongs to the year before), and a jump half a year backwards
    between adjacent lines is a rollover, not time travel.
    """
    if anchor is not None and ts > anchor + 172800:      # 2 days of skew
        ts = _shift_year(ts, -1)
    if prev_ts is not None and ts < prev_ts - 15552000:  # ~180 days
        ts = _shift_year(ts, 1)
    return ts


def feed(counter, paths, args):
    for path in paths:
        anchor = file_mtime(path) if path != "-" else time.time()
        year = datetime.fromtimestamp(anchor).year if anchor else None
        prev_syslog_ts = None
        try:
            fh = open_any(path)
        except OSError as exc:
            die("cannot read %s: %s" % (path, exc))
        pending_ts = pending_prog = None
        pending_raw = None
        try:
            for line in fh:
                counter.n_lines += 1
                if args.multiline == "auto" and pending_raw is not None and \
                        is_continuation(line):
                    # Keep the top frame only: it is what distinguishes one
                    # call site from another, and the rest is noise.
                    if pending_raw.count("\n") < 2:
                        pending_raw += "\n" + line.strip()[:200]
                    continue
                if pending_raw is not None:
                    _emit(counter, pending_ts, pending_raw, pending_prog, args)
                parsed = parse_line(line, year)
                if parsed is None:
                    pending_raw = None
                    continue
                ts, _host, prog, _pid, msg, fmt = parsed
                if fmt == "syslog" and ts is not None:
                    ts = _unroll_year(ts, anchor, prev_syslog_ts)
                    prev_syslog_ts = ts
                counter.formats[fmt] = counter.formats.get(fmt, 0) + 1
                if args.multiline == "auto":
                    pending_ts, pending_prog, pending_raw = ts, prog, msg
                else:
                    _emit(counter, ts, msg, prog, args)
            if pending_raw is not None:
                _emit(counter, pending_ts, pending_raw, pending_prog, args)
        finally:
            if fh is not sys.stdin:
                fh.close()


def _emit(counter, ts, raw, prog, args):
    if ts is not None:
        if args.since_epoch and ts < args.since_epoch:
            return
        if args.until_epoch and ts > args.until_epoch:
            return
    counter.add(ts, raw, prog)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

import math


def score_templates(counter, baseline, args):
    """novelty, severity and burst over frequency -- see the header."""
    maxc = max((t.count for t in counter.templates.values()), default=1)
    rows = []
    for t in counter.templates.values():
        if t.count < args.min_count:
            continue
        if t.sev < args.min_severity_level:
            continue

        if baseline is not None:
            base = baseline.get(t.tid, 0)
            if base == 0:
                novel = 1.0
            else:
                novel = max(0.0, 1.0 - (base / float(t.count)))
        else:
            early, late = t.early, t.late
            if late == 0:
                novel = 0.0
            elif early == 0:
                novel = 1.0
            else:
                novel = max(0.0, 1.0 - (early / float(late)))

        burst = 0.0
        bf = 0.0
        if t.buckets:
            vals = [v for v in t.buckets.values() if v > 0]
            peak = max(vals)
            mean = sum(vals) / float(len(vals))
            if mean > 0 and peak >= 5:
                bf = peak / mean
                if bf >= args.burst_factor:
                    burst = min(1.0, math.log(bf) / math.log(20.0))

        freq = math.log10(1 + t.count) / math.log10(1 + maxc) if maxc else 0.0

        w = args.weights
        score = (w["sev"] * (t.sev / 3.0) + w["novel"] * novel
                 + w["burst"] * burst + w["freq"] * freq)
        rows.append({"t": t, "score": score, "novel": novel, "burst": burst,
                     "burst_factor": bf, "freq": freq})
    rows.sort(key=lambda r: -r["score"])
    return rows


def sparkline(t, counter, ascii_mode):
    if not t.buckets:
        return ""
    lo, hi = min(t.buckets), max(t.buckets)
    span = max(1, hi - lo + 1)
    step = max(1, int(math.ceil(span / float(SPARK_COLS))))
    cols = []
    for i in range(0, span, step):
        s = sum(t.buckets.get(lo + j, 0) for j in range(i, min(i + step, span)))
        cols.append(s)
    if not cols:
        return ""
    peak = max(cols) or 1
    chars = SPARK_ASCII if ascii_mode else SPARK_UNI
    n = len(chars)
    return "".join(chars[min(n - 1, int(c * n / (peak + 1)))] for c in cols)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_time(ts, idx=None):
    if ts is None:
        return "#%s" % (idx if idx is not None else "?")
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "?"


def render_human(counter, rows, cut, args):
    out = []
    span = ""
    if counter.min_ts and counter.max_ts:
        span = "%s -> %s" % (fmt_time(counter.min_ts),
                             fmt_time(counter.max_ts))
    out.append("%s -- %s, %s lines, %s templates%s"
               % (PROG, args.source_label, "{:,}".format(counter.n_lines),
                  "{:,}".format(len(counter.templates)),
                  ", " + span if span else ""))
    base_desc = ("baseline: %s" % args.baseline) if args.baseline else (
        "baseline: everything before %s" % fmt_time(cut) if cut
        else "baseline: first %d%% by line number" % int(args.split * 100))
    out.append("             %s, buckets %s"
               % (base_desc, _fmt_bucket(counter.bucket_width)))
    if counter.no_ts and counter.no_ts == counter.n_records:
        out.append("             no timestamps found -- ordering by line "
                   "number")
    out.append("")

    if not rows:
        out.append("  Nothing met the filters.  Lower --min-count or "
                   "--min-severity.")
        return "\n".join(out)

    for i, r in enumerate(rows[:args.top], 1):
        t = r["t"]
        tags = []
        if r["novel"] >= 0.99:
            tags.append("NEW")
        elif r["novel"] >= 0.5:
            tags.append("RISING")
        tag = " ".join(tags)
        out.append("  #%-2d score %-5.1f %-7s sev=%-6s %sx %s"
                   % (i, r["score"], tag, SEV_NAMES[t.sev],
                      "{:,}".format(t.count),
                      sparkline(t, counter, args.ascii)))
        prog = ("%s: " % t.program) if t.program else ""
        out.append("      %s%s" % (prog, t.key[:200]))
        detail = "      first %s   last %s" % (
            fmt_time(t.first, t.first_idx), fmt_time(t.last, t.last_idx))
        if r["burst"] > 0:
            detail += "   burst %.0fx" % r["burst_factor"]
        out.append(detail)
        if r["novel"] >= 0.99 and t.first is not None:
            out.append("      not seen before %s; %s since"
                       % (fmt_time(t.first), "{:,}".format(t.count)))
        out.append("")

    if len(rows) > args.top:
        out.append("  ... %d more (--top %d)"
                   % (len(rows) - args.top, len(rows)))
    if counter.evicted:
        out.append("  %s templates were evicted into <OTHER> (%s records) -- "
                   "raise --max-templates to see them"
                   % ("{:,}".format(counter.evicted),
                      "{:,}".format(counter.evicted_count)))
    out.append("")
    out.extend(_hints(counter, rows, args))
    return "\n".join(out)


def _fmt_bucket(w):
    if w >= 3600:
        return "%gh" % (w / 3600.0)
    if w >= 60:
        return "%gm" % (w / 60.0)
    return "%gs" % w


def _hints(counter, rows, args):
    """Read the ranking and say what it means.  Two things matter most:
    findings that start together are usually one event, and a huge count
    with no novelty is almost always background noise."""
    out = ["  WHAT TO DO NEXT"]
    top = rows[:args.top]
    news = [r for r in top if r["novel"] >= 0.99 and r["t"].first is not None]

    grouped = []
    if len(news) >= 2:
        news_sorted = sorted(news, key=lambda r: r["t"].first)
        window = max(counter.bucket_width * 2, 120)
        cluster = [news_sorted[0]]
        for r in news_sorted[1:]:
            if r["t"].first - cluster[-1]["t"].first <= window:
                cluster.append(r)
            else:
                if len(cluster) >= 2:
                    grouped.append(list(cluster))
                cluster = [r]
        if len(cluster) >= 2:
            grouped.append(cluster)

    if grouped:
        c = grouped[0]
        names = " and ".join("#%d" % (rows.index(r) + 1) for r in c[:3])
        out.append("    * %s start within %s of each other. Read them as one "
                   "event rather than three: the first one is the cause and "
                   "the others are what it broke."
                   % (names, _fmt_bucket(max(counter.bucket_width * 2, 120))))
        out.append("      Earliest is at %s -- start there."
                   % fmt_time(c[0]["t"].first))
    elif news:
        r = news[0]
        out.append("    * #%d is new in this window (%s occurrences from %s). "
                   "That is the thing that changed."
                   % (rows.index(r) + 1, "{:,}".format(r["t"].count),
                      fmt_time(r["t"].first)))

    noisy = [r for r in top
             if r["novel"] < 0.2 and r["freq"] > 0.8 and r["t"].count > 1000]
    if noisy:
        r = noisy[0]
        out.append("    * #%d is %s lines and steady across the whole window, "
                   "so it is background, not your problem -- it is only "
                   "listed because of its volume."
                   % (rows.index(r) + 1, "{:,}".format(r["t"].count)))

    if not news and not grouped:
        out.append("    * Nothing here is new relative to the baseline: this "
                   "log looks the same at the end as at the start.")
        out.append("      If you know when the trouble started, say so and "
                   "the comparison gets sharper:  %s ... --split-at HH:MM"
                   % PROG)
    if counter.no_ts and counter.no_ts == counter.n_records:
        out.append("    * These lines carry no timestamps, so first/last are "
                   "line numbers and bursts cannot be detected.")
    out.append("")
    return out


CSV_FIELDS = ["template_id", "rank", "count", "pct", "first_seen",
              "last_seen", "severity", "program", "novel", "burst_factor",
              "score", "template", "example"]


def render_csv(counter, rows, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        total = float(counter.n_records or 1)
        for i, r in enumerate(rows, 1):
            t = r["t"]
            w.writerow({
                "template_id": t.tid, "rank": i, "count": t.count,
                "pct": "%.3f" % (t.count * 100.0 / total),
                "first_seen": ("%.0f" % t.first) if t.first else t.first_idx,
                "last_seen": ("%.0f" % t.last) if t.last else t.last_idx,
                "severity": SEV_NAMES[t.sev], "program": t.program or "",
                "novel": "%.3f" % r["novel"],
                "burst_factor": "%.1f" % r["burst_factor"],
                "score": "%.2f" % r["score"],
                "template": t.key, "example": t.example,
            })
    finally:
        if path:
            fh.close()


def render_json(counter, rows, path):
    doc = {
        "meta": {"tool": PROG, "version": VERSION,
                 "lines": counter.n_lines, "records": counter.n_records,
                 "templates": len(counter.templates),
                 "evicted": counter.evicted,
                 "bucket_width_s": counter.bucket_width,
                 "formats": counter.formats},
        "templates": [{
            "template_id": r["t"].tid, "rank": i, "count": r["t"].count,
            "severity": SEV_NAMES[r["t"].sev], "program": r["t"].program,
            "novel": round(r["novel"], 3),
            "burst_factor": round(r["burst_factor"], 2),
            "score": round(r["score"], 3), "template": r["t"].key,
            "example": r["t"].example,
            "first_seen": r["t"].first, "last_seen": r["t"].last,
            "buckets": r["t"].buckets,
        } for i, r in enumerate(rows, 1)],
    }
    text = json.dumps(doc, indent=2, sort_keys=True, default=str)
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


def save_templates(counter, path):
    with io.open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["template_id", "count", "program", "example"])
        for t in sorted(counter.templates.values(), key=lambda x: -x.count):
            w.writerow([t.tid, t.count, t.program or "", t.example])


def load_templates(path):
    out = {}
    try:
        with io.open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[row["template_id"]] = int(row["count"])
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError as exc:
        die("cannot read template digest %s: %s" % (path, exc))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_when(s, ref=None):
    """'14:20', an ISO stamp, or a relative '-30m' / '-2h'."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^-(\d+)([smhd])$", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return (ref or time.time()) - int(m.group(1)) * mult
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        base = datetime.fromtimestamp(ref or time.time())
        dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                          second=int(m.group(3) or 0), microsecond=0)
        return time.mktime(dt.timetuple())
    v = _parse_ts_any(s)
    if v is None:
        die("cannot understand time %r (try HH:MM, an ISO stamp, or -30m)" % s)
    return v


def parse_weights(s):
    w = {"sev": 3.0, "novel": 3.0, "burst": 2.0, "freq": 1.0}
    if not s:
        return w
    for part in s.split(","):
        if "=" not in part:
            die("--weights wants k=v pairs, got %r" % part)
        k, v = part.split("=", 1)
        if k.strip() not in w:
            die("unknown weight %r (want sev, novel, burst or freq)" % k)
        try:
            w[k.strip()] = float(v)
        except ValueError:
            die("weight %r is not a number" % v)
    return w


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", metavar="FILE")
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("--top", type=int, default=int(_env("TOP", 10)))
    p.add_argument("--min-count", type=int, default=1)
    p.add_argument("--min-severity", default="info",
                   choices=["info", "warn", "error", "crit"])
    p.add_argument("--split", type=float, default=0.5)
    p.add_argument("--split-at")
    p.add_argument("--baseline", default=_env("BASELINE"))
    p.add_argument("--baseline-templates")
    p.add_argument("--save-templates")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--bucket", type=float, default=_env("BUCKET"))
    p.add_argument("--burst-factor", type=float, default=5.0)
    p.add_argument("--weights", default=_env("WEIGHTS"))
    p.add_argument("--by", default="program", choices=["program", "none"])
    p.add_argument("--multiline", default="auto", choices=["auto", "off"])
    p.add_argument("--rule", action="append", metavar="RE=>NAME")
    p.add_argument("--keep-paths", action="store_true")
    p.add_argument("--keep-quoted", action="store_true")
    p.add_argument("--keep-numbers", action="store_true")
    p.add_argument("--max-template-len", type=int, default=512)
    p.add_argument("--max-templates", type=int,
                   default=int(_env("MAX_TEMPLATES", 20000)))
    p.add_argument("--example-len", type=int, default=300)
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--ascii", action="store_true")
    p.add_argument("--mask", metavar="LINE",
                   help="show how one line is templated, then exit")
    p.add_argument("--parse", metavar="LINE",
                   help="show how one line is split up, then exit")
    return p


def main(argv=None):
    _stdio_safe()
    args = build_parser().parse_args(argv)
    args.weights = parse_weights(args.weights)
    args.min_severity_level = SEV_LEVELS[args.min_severity]
    if not args.ascii and os.environ.get("LANG", "") in ("C", "POSIX", ""):
        args.ascii = True
    masks = build_masks(args)

    if args.mask is not None:
        key = mask_line(args.mask, masks, args.max_template_len)
        sys.stdout.write("%s\n" % key)
        sys.stdout.write("id: %s\n" % template_id(None, key))
        return 0
    if args.parse is not None:
        parsed = parse_line(args.parse)
        if parsed is None:
            sys.stdout.write("empty line\n")
            return 0
        ts, host, prog, pid, msg, fmt = parsed
        sys.stdout.write(
            "format:  %s\nts:      %s\nhost:    %s\nprogram: %s\npid:     %s\n"
            "message: %s\n"
            % (fmt, ("%s (%.0f)" % (fmt_time(ts), ts)) if ts else "-",
               host or "-", prog or "-", pid or "-", msg))
        return 0

    # Remembered before the stdin fallback rewrites it, so a later error
    # can tell "you named no file" from "you named one".
    args.paths_given = bool(args.paths)
    if not args.paths:
        if sys.stdin.isatty():
            build_parser().print_help()
            return 2
        args.paths = ["-"]
    paths = expand_inputs(args.paths)
    if not paths:
        die("no input files matched")
    args.source_label = paths[0] if len(paths) == 1 else \
        "%d files" % len(paths)

    args.since_epoch = parse_when(args.since) if args.since else None
    args.until_epoch = parse_when(args.until) if args.until else None

    counter = Counter(args, masks)
    if args.bucket:
        counter.bucket_width = float(args.bucket)
    feed(counter, paths, args)

    if not counter.n_records:
        # The commonest way to get here is not an empty log: it is
        # `logtriage --csv /var/log/syslog`, where --csv takes an optional
        # path and swallows the file you meant to read, leaving stdin --
        # which under ssh or a pipeline is empty rather than a terminal, so
        # the usual help-on-no-input guard never fires.  Say so, because
        # the bare message sends people looking at the log instead.
        hint = ""
        for flag, value in (("--csv", args.csv), ("--json", args.json)):
            if value and not args.paths_given and os.path.isfile(value):
                hint = ("  Note %s was given %r, which exists -- if that is "
                        "the log you meant to read, it has been taken as the "
                        "output path instead.  The file comes first: "
                        "`%s %s %s`." % (flag, value, PROG, value, flag))
                break
        die("no log records found in %s%s" % (args.source_label, hint), 1)

    args.split_at_epoch = None
    if args.split_at:
        args.split_at_epoch = parse_when(
            args.split_at, counter.max_ts or time.time())

    baseline = None
    if args.baseline_templates:
        baseline = load_templates(args.baseline_templates)
    elif args.baseline:
        bpaths = expand_inputs([args.baseline])
        bc = Counter(args, masks)
        feed(bc, bpaths, args)
        baseline = {tid: t.count for tid, t in bc.templates.items()}

    cut = counter.apply_split() if baseline is None else None
    rows = score_templates(counter, baseline, args)

    if args.save_templates:
        with _WriteGuard(args.save_templates):
            save_templates(counter, args.save_templates)

    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            render_csv(counter, rows, args.csv or None)
    if args.json is not None:
        with _WriteGuard(args.json or None):
            render_json(counter, rows, args.json or None)
    if args.csv is None and args.json is None:
        sys.stdout.write(render_human(counter, rows, cut, args) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
        # `logtriage ... | head` is a normal way to use this.
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
