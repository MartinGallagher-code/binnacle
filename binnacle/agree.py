#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""agree.py -- run a command across a fleet and show who disagrees.

Usage: agree.py [run] [OPTIONS] -- COMMAND...
       agree.py script PATH [OPTIONS] -- ARGS...   push a script, run it, collect
       agree.py hosts  [OPTIONS]                   expand and print the host list
       agree.py doctor [OPTIONS]                   can this fleet be reached?
       agree.py help                               every flag of every verb

Examples:
  agree.py -H web01,web02,db01 -- rpm -q openssl
  agree.py --hosts prod.txt -- 'sysctl net.core.somaxconn'
  agree.py --hosts 'node[01-24]' --loose -- uname -r
  agree.py script ./why_slow.py --hosts prod.txt --fleet-csv -- --csv

Options:
  --hosts FILE|SPEC   host list: a file, a range like node[01-24], or - for
                      stdin                                  (AGREE_HOSTS)
  -H a,b,c            hosts inline, repeatable
  --jobs N            hosts contacted at once                 (AGREE_JOBS)
  --timeout S         per-host command timeout, seconds       (AGREE_TIMEOUT)
  --max-output BYTES  per-host output cap; a host past it is
                      killed and reported, not grouped      (AGREE_MAX_OUTPUT)
  --user NAME         ssh user                                (AGREE_USER)
  --ssh CMD/--scp CMD transports                     (AGREE_SSH / AGREE_SCP)
  --sudo              prefix the command with `sudo -n`
  --sudo-user NAME    sudo to this user rather than root
  --push FILE         copy FILE to each host first, repeatable
  --pull GLOB         copy matching files back afterwards
  --pull-dir DIR      where pulled files land, one subdir per host (results)
  --keep-remote       leave the pushed files behind instead of cleaning up
  --remote-dir DIR    where pushed files go           (AGREE_REMOTE_DIR)
  --first N           only the first N hosts -- the canary before the fleet
  --limit N           refuse to run wider than N hosts without --yes
  --dry-run           print the ssh command per host and stop
  --yes               proceed past the mutation guard
  --full              print each group's output instead of diffing it
  --show-hosts        list every host in a group, not just the first few
  --max-diff-lines N  truncate a long diff                    (40)
  --stderr MODE       separate | merge | ignore               (separate)
  --csv PATH / --json PATH / --merge-csv PATH
  --quiet             groups only, no progress

Normalization (outputs almost never match byte for byte):
  --loose             trim + squeeze-ws + mask-times + mask-hosts + numbers
  --strict            nothing at all: byte-exact comparison
  --fleet-csv         mask-hosts + mask-times: what grouping the CSV these
                      tools emit needs, since every row begins host,ts
  --trim              drop trailing whitespace                (on by default)
  --no-trim           keep trailing whitespace
  --no-strip-ansi     keep ANSI escapes instead of stripping them
  --squeeze-ws        collapse internal whitespace runs
  --sort-lines        order does not matter (rpm -qa, ls, ip addr)
  --ignore-case       case does not matter
  --drop-empty        drop blank lines
  --mask-hosts        replace each host's own name/address with %HOST%
  --mask-times        replace timestamps with <TS>
  --mask-numbers      replace every number with #
  --scrub RE          replace matches with %X%, repeatable
  --grep RE           keep only matching lines
  --vgrep RE          drop matching lines
  --head N/--tail N   only the first/last N lines
  --field N           only whitespace-field N of each line

Why this exists
  pssh and friends print N outputs and leave you to read them.  What you
  actually want to know is which hosts differ from the rest, and that is
  a different question: 47 agree, 3 do not, here is the diff.

  A host that could not be reached is a group, not an error.  "3 hosts
  never answered" is usually the finding, and a tool that prints those to
  stderr and moves on lets you believe you checked them.

  It composes: `agree.py script ./why-slow.py -- --csv` fans a diagnosis
  across the fleet and groups the hosts by what is wrong with them, which
  is fleet-wide triage in one command.

Robustness properties
  * output is replayed in host-list order, never completion order, so two
    runs over the same list diff cleanly
  * a failed, timed-out or unreachable host becomes its own group and is
    always reported
  * the active normalizations are printed with the result, because "47
    agree" means nothing if you masked every number to get there
  * `sudo -n` always: a password prompt would hang a fan-out, and a host
    without NOPASSWD becomes a visible finding rather than a hang
  * no password is ever read, passed or stored, and no key is distributed
  * the mutation guard is a typo guard, not security -- it is trivially
    bypassed with --yes and says so
  * exit 0 unanimous, 1 divergence, 3 some host failed, 2 usage
"""

import argparse
import csv
import difflib
import hashlib
import io
import json
import os
import re
import selectors
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.2.0"
PROG = os.path.basename(sys.argv[0]) or "agree.py"

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]

DEFAULT_REMOTE_DIR = "/var/tmp/agree"

OK, FAILED, TIMEOUT, UNREACHABLE, PUSH_FAILED = (
    "ok", "failed", "timeout", "unreachable", "push-failed")


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
    v = os.environ.get("AGREE_" + name)
    return v if v not in (None, "") else default


def progress(msg):
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K[%s] %s" % (PROG, msg))
        sys.stderr.flush()


def progress_done():
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Host lists
# ---------------------------------------------------------------------------
#
# Same grammar as servers.txt in iperf_orchestrator and matrix_orchestrator
# -- '#' comments, blank lines, name[=addr[:port]] tokens -- so an existing
# fleet list works here unchanged.

RANGE_RE = re.compile(r"\[([^\]]+)\]")


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


class Host(object):
    __slots__ = ("name", "addr", "port")

    def __init__(self, name, addr, port=None):
        self.name, self.addr, self.port = name, addr, port

    def __repr__(self):
        return self.name


def is_ipv6(addr):
    """A colon in an address can only be IPv6: v4 and names have none."""
    return ":" in addr


def split_host_port(spec):
    """'host', 'host:port', '[v6]', '[v6]:port', bare v6 -> (addr, port).

    IPv6 needs the bracket form to carry a port, because the address is
    made of colons and the last one would otherwise be read as a port
    separator -- which is how "::1" once became the address ":" on port 1,
    collapsing every v6 entry in a list to the same nonsense host.  A bare
    literal is fine and simply has no port.

    Validated with inet_pton rather than a pattern, so "2001:db8::1::2" is
    refused here instead of failing later as a connection error that names
    the wrong cause.
    """
    spec = spec.strip()
    port = None
    if spec.startswith("["):
        addr, sep, rest = spec[1:].partition("]")
        if not sep:
            die("no closing ] in host token %r" % spec)
        if rest.startswith(":"):
            if not rest[1:].isdigit():
                die("bad port %r in host token %r" % (rest[1:], spec))
            port = int(rest[1:])
        elif rest:
            die("unexpected %r after ] in host token %r" % (rest, spec))
    elif spec.count(":") > 1:
        addr = spec                       # a bare v6 literal carries no port
    elif ":" in spec:
        addr, _, p = spec.rpartition(":")
        if p.isdigit():
            port = int(p)
        else:
            addr = spec
    else:
        addr = spec
    if is_ipv6(addr):
        try:
            socket.inet_pton(socket.AF_INET6, addr)
        except (OSError, ValueError):
            die("not a valid IPv6 address: %r (in host token %r)"
                % (addr, spec))
    return addr, port


def parse_host_token(tok):
    tok = tok.strip()
    bare = "=" not in tok
    if bare:
        name = addr = tok
    else:
        name, addr = tok.split("=", 1)
        name, addr = name.strip(), addr.strip()
    addr, port = split_host_port(addr)
    if bare:
        name = addr
    if not name or not addr:
        die("bad host token %r (want name[=addr[:port]])" % tok)
    return Host(name, addr, port)


def read_host_file(path):
    if path == "-":
        # a pipe can carry a BOM too, e.g. type hosts.txt | ssh box ...
        text = sys.stdin.read().lstrip("\ufeff")
    else:
        if not os.path.isfile(path):
            # A spec that was meant as a file must not fall through to
            # being a hostname: a typo'd path or a directory would become
            # a fleet of one bogus host.  Nothing with a path separator
            # is a valid hostname, so refuse it here by name.
            if os.sep in path or os.path.isdir(path):
                die("no such host file: %s" % path)
            return None
        try:
            with io.open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            die("cannot read %s: %s" % (path, exc))
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            for tok in line.split():
                out.extend(split_commas(tok))
    return out


def collect_hosts(args):
    tokens = []
    for spec in (args.hosts or []):
        from_file = read_host_file(spec)
        if from_file is not None:
            tokens.extend(from_file)
        else:
            tokens.extend(split_commas(spec))
    for spec in (args.H or []):
        tokens.extend(split_commas(spec))
    if not tokens:
        for default in ("hosts.txt", "servers.txt"):
            got = read_host_file(default)
            if got:
                tokens.extend(got)
                break
    if not tokens:
        die("no hosts given (use --hosts FILE, --hosts 'node[01-09]' or "
            "-H a,b,c; hosts.txt and servers.txt are used if present)")

    expanded = []
    for tok in tokens:
        expanded.extend(expand_range(tok))

    hosts, seen = [], set()
    for tok in expanded:
        h = parse_host_token(tok)
        if h.name in seen:
            continue
        seen.add(h.name)
        hosts.append(h)
    if args.first:
        hosts = hosts[:args.first]
    return hosts


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
#
# Raw outputs almost never match byte for byte -- a hostname, a timestamp or
# a pid is usually enough to make every host look unique.  These run in a
# fixed order, and the active ones are always printed with the result.
#
# canonical copy of the timestamp table: binnacle/logtriage.py TIME_MASKS.
# The epoch entry at the end of this one is deliberately *not* in the
# canonical copy: logtriage's own number mask already covers it, and this
# table needs it because every tool here stamps its CSV with an epoch.

TIME_MASKS = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
               r"(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b[A-Z][a-z]{2}\s{1,2}\d{1,2} \d{2}:\d{2}:\d{2}\b"),
    re.compile(r"\[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]"),
    re.compile(r"\[\s*\d+\.\d{6}\]"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
    # Epoch seconds.  Every tool in this package stamps its CSV with one,
    # and two hosts answering either side of a tick would otherwise land in
    # different groups for no reason connected to what is wrong with them.
    # Narrowed to the 1_000_000_000-1_999_999_999 range -- 2001 to 2033 --
    # so an ordinary ten-digit number is not silently masked as a date.
    re.compile(r"\b1\d{9}\b"),
]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NUM_RE = re.compile(r"[+-]?\b\d+(?:\.\d+)?\b")
WS_RE = re.compile(r"[ \t]+")


def active_normalizations(args):
    names = []
    for flag, label in (("strip_ansi", "strip-ansi"), ("trim", "trim"),
                        ("squeeze_ws", "squeeze-ws"),
                        ("sort_lines", "sort-lines"),
                        ("ignore_case", "ignore-case"),
                        ("drop_empty", "drop-empty"),
                        ("mask_hosts", "mask-hosts"),
                        ("mask_times", "mask-times"),
                        ("mask_numbers", "mask-numbers")):
        if getattr(args, flag, False):
            names.append(label)
    if args.scrub:
        names.append("scrub x%d" % len(args.scrub))
    for flag, label in (("grep", "grep"), ("vgrep", "vgrep"),
                        ("head", "head"), ("tail", "tail"),
                        ("field", "field")):
        if getattr(args, flag, None) is not None:
            names.append(label)
    return names or ["none (byte-exact)"]


def normalize(text, host, args):
    if args.strip_ansi:
        text = ANSI_RE.sub("", text)

    lines = text.splitlines()
    if args.vgrep:
        rx = re.compile(args.vgrep)
        lines = [l for l in lines if not rx.search(l)]
    if args.grep:
        rx = re.compile(args.grep)
        lines = [l for l in lines if rx.search(l)]
    if args.head is not None:
        lines = lines[:args.head]
    if args.tail is not None:
        lines = lines[-args.tail:] if args.tail else []
    if args.field is not None:
        picked = []
        for l in lines:
            parts = l.split()
            picked.append(parts[args.field - 1]
                          if 0 < args.field <= len(parts) else "")
        lines = picked

    if args.mask_hosts and host is not None:
        # Per host, and before hashing: without this, anything that echoes
        # the machine's own name can never agree with anything.
        #
        # Bounded, never a bare substring.  A plain replace turns a host
        # called "sql" into %HOST% inside "postgresql" and a host called
        # "a" into %HOST% inside every word containing an a -- which
        # corrupts the comparison instead of enabling it, and does so
        # differently on each host, producing exactly the spurious groups
        # it was meant to remove.
        #
        # The boundary is written out rather than using \b, because \b is
        # defined against word characters and half these tokens do not
        # start with one: \b::1\b can never match, so an IPv6 address
        # would silently stop being masked.  A trailing dot is deliberately
        # allowed, so a bare name still masks the first label of its own
        # FQDN -- that prefix is the part that differs between hosts, and
        # the domain after it is what they have in common.
        #
        # Single characters are skipped: they appear in ordinary prose far
        # more often than they appear as a hostname.
        for token in sorted(filter(None, {host.name, host.addr,
                                          host.name.split(".")[0]}),
                            key=len, reverse=True):
            if len(token) < 2:
                continue
            rx = re.compile(r"(?<![\w.-])%s(?!\w)" % re.escape(token))
            lines = [rx.sub("%HOST%", l) for l in lines]
    if args.mask_times:
        for rx in TIME_MASKS:
            lines = [rx.sub("<TS>", l) for l in lines]
    for pat in (args.scrub or []):
        rx = re.compile(pat)
        lines = [rx.sub("%X%", l) for l in lines]
    if args.mask_numbers:
        lines = [NUM_RE.sub("#", l) for l in lines]
    if args.ignore_case:
        lines = [l.lower() for l in lines]
    if args.squeeze_ws:
        lines = [WS_RE.sub(" ", l) for l in lines]
    if args.trim:
        lines = [l.rstrip() for l in lines]
    if args.drop_empty:
        lines = [l for l in lines if l.strip()]
    if args.sort_lines:
        lines = sorted(lines)
    if args.trim:
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

class Result(object):
    __slots__ = ("host", "outcome", "rc", "stdout", "stderr", "duration",
                 "norm", "digest", "group")

    def __init__(self, host):
        self.host = host
        self.outcome = OK
        self.rc = 0
        self.stdout = ""
        self.stderr = ""
        self.duration = 0.0
        self.norm = ""
        self.digest = ""
        self.group = None


class Runner(object):
    def __init__(self, args):
        self.args = args
        self.ssh = (args.ssh or "ssh").split()
        self.scp = (args.scp or "scp").split()
        self.user = args.user
        self.timeout = args.timeout
        self.max_output = args.max_output
        self.remote_dir = args.remote_dir

    def target(self, host):
        """ssh takes a bare address, v6 included: `ssh user@2001:db8::1`."""
        return "%s@%s" % (self.user, host.addr) if self.user else host.addr

    def scp_target(self, host):
        """scp splits its argument on the last colon to find the path, so a
        v6 literal has to be bracketed or the address loses its tail."""
        addr = "[%s]" % host.addr if is_ipv6(host.addr) else host.addr
        return "%s@%s" % (self.user, addr) if self.user else addr

    def ssh_argv(self, host, command):
        argv = list(self.ssh) + SSH_OPTS
        if host.port:
            argv += ["-p", str(host.port)]
        argv += [self.target(host), command]
        return argv

    def _exec(self, argv, timeout):
        """Bounded capture: one runaway host must not sink the whole run.

        communicate() holds everything a child ever prints, so a single
        box caught in a log storm -- exactly the kind of box this tool is
        pointed at -- could grow this process by gigabytes, multiplied by
        --jobs.  Output is read incrementally and cut at --max-output;
        a host that blows the cap is killed and reported as such, because
        grouping the front half of a flood would be an answer to a
        question nobody asked.
        """
        limit = int(self.max_output)
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL)
        except OSError as exc:
            return 127, "", str(exc)
        chunks = {p.stdout: [], p.stderr: []}
        sizes = {p.stdout: 0, p.stderr: 0}
        deadline = time.monotonic() + timeout
        flooded = False
        # selectors rather than select(): with --jobs worth of children a
        # descriptor number can pass select()'s 1024 ceiling
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        sel.register(p.stderr, selectors.EVENT_READ)
        open_count = 2
        while open_count:
            left = deadline - time.monotonic()
            if left <= 0:
                sel.close()
                p.kill()
                p.wait()
                return 124, "", "timed out after %ss" % timeout
            for key, _ in sel.select(left):
                f = key.fileobj
                chunk = os.read(f.fileno(), 65536)
                if not chunk:
                    sel.unregister(f)
                    open_count -= 1
                    continue
                room = limit - sizes[f]
                if room > 0:
                    chunks[f].append(chunk[:room])
                sizes[f] += len(chunk)
                if sizes[f] > limit:
                    flooded = True
            if flooded:
                p.kill()
                break
        sel.close()
        try:
            rc = p.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
        for f in (p.stdout, p.stderr):
            f.close()

        def text(f):
            return b"".join(chunks[f]).decode("utf-8-sig", "replace")

        if flooded:
            return 125, "", ("output exceeded --max-output (%d bytes); "
                             "host killed" % limit)
        return rc, text(p.stdout), text(p.stderr)

    def push_files(self, host, files):
        d = self.remote_dir
        rc, _, err = self._exec(
            self.ssh_argv(host, "mkdir -p %s" % shlex.quote(d)), 60)
        if rc != 0:
            return rc, err
        dest = "%s:%s/" % (self.scp_target(host), d)
        argv = list(self.scp) + SSH_OPTS
        if host.port:
            argv += ["-P", str(host.port)]
        argv += ["-q"] + list(files) + [dest]
        rc, _, err = self._exec(argv, max(60, self.timeout))
        if rc != 0:
            return rc, err
        names = " ".join(shlex.quote("%s/%s" % (d, os.path.basename(f)))
                         for f in files)
        return self._exec(self.ssh_argv(host, "chmod +x %s" % names), 60)[0:3:2]

    def pull_files(self, host, pattern, outdir):
        hostdir = os.path.join(outdir, host.name)
        os.makedirs(hostdir, exist_ok=True)
        src = "%s:%s/%s" % (self.scp_target(host),
                             self.remote_dir, pattern)
        argv = list(self.scp) + SSH_OPTS
        if host.port:
            argv += ["-P", str(host.port)]
        argv += ["-q", src, hostdir + "/"]
        return self._exec(argv, max(60, self.timeout))[0]

    def run_one(self, host, command, push=None, pull=None, cleanup=True):
        r = Result(host)
        t0 = time.monotonic()
        if push:
            rc, err = self.push_files(host, push)
            if rc != 0:
                r.outcome = PUSH_FAILED
                r.rc = rc
                r.stderr = err
                r.duration = time.monotonic() - t0
                return r
        rc, out, err = self._exec(self.ssh_argv(host, command), self.timeout)
        r.rc, r.stdout, r.stderr = rc, out, err
        if pull:
            self.pull_files(host, pull, self.args.pull_dir)
        if push and cleanup:
            self._exec(self.ssh_argv(
                host, "rm -rf %s" % shlex.quote(self.remote_dir)), 60)
        r.duration = time.monotonic() - t0

        if rc == 124:
            r.outcome = TIMEOUT
        elif rc == 255 or (rc == 127 and not out):
            # 255 is ssh's own failure code; anything from the remote shell
            # comes back as that command's status instead.
            low = (err or "").lower()
            r.outcome = UNREACHABLE if (
                "connect" in low or "resolve" in low or "route" in low
                or "refused" in low or "timed out" in low
                or "permission denied" in low or not out) else FAILED
        elif rc != 0:
            r.outcome = FAILED
        return r


def fan_out(hosts, fn, jobs, quiet=False):
    """Capped concurrency, results in host-list order.

    Order matters more than it looks: two runs of the same command over the
    same list must produce byte-identical reports, or you cannot diff
    yesterday's against today's.
    """
    workers = max(1, min(jobs, len(hosts)))
    done = [0]
    total = len(hosts)

    def wrapped(h):
        try:
            return fn(h)
        finally:
            done[0] += 1
            if not quiet:
                progress("%d/%d done" % (done[0], total))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(wrapped, hosts))
    if not quiet:
        progress_done()
    return results


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------

class Group(object):
    def __init__(self, gid, key, results):
        self.id = gid
        self.outcome, self.digest = key
        self.results = results
        self.baseline = False

    @property
    def size(self):
        return len(self.results)

    @property
    def hosts(self):
        return [r.host.name for r in self.results]

    @property
    def sample(self):
        return self.results[0]


def group_results(results, args):
    for r in results:
        base = r.stdout
        if args.stderr_mode == "merge":
            base = base + r.stderr
        r.norm = normalize(base, r.host, args)
        # Group on outcome as well as content: two hosts with identical
        # stdout but different exit codes did not give the same answer, and
        # merging them hides that.
        r.digest = hashlib.sha256(
            r.norm.encode("utf-8", "replace")).hexdigest()[:12]

    buckets = {}
    order = []
    for i, r in enumerate(results):
        key = (r.outcome if r.outcome != OK else OK,
               r.digest if r.outcome == OK else r.outcome + str(r.rc))
        if key not in buckets:
            buckets[key] = []
            order.append((key, i))
        buckets[key].append(r)

    ranked = sorted(order, key=lambda kv: (-len(buckets[kv[0]]), kv[1]))
    groups = []
    for gid, (key, _) in enumerate(ranked, 1):
        g = Group(gid, key, buckets[key])
        for r in g.results:
            r.group = gid
        groups.append(g)
    # The baseline is the largest *successful* group; a fleet where most
    # hosts failed should still diff against the ones that worked.
    for g in groups:
        if g.outcome == OK:
            g.baseline = True
            break
    return groups


def render_groups(groups, args, meta):
    out = []
    total = sum(g.size for g in groups)
    biggest = max((g.size for g in groups if g.outcome == OK), default=0)
    out.append("%s -- %d hosts, %d group%s, %d agree      [normalized: %s]"
               % (PROG, total, len(groups), "" if len(groups) == 1 else "s",
                  biggest, ", ".join(meta["norms"])))
    out.append("        %s%s"
               % (meta["command"][:60],
                  "   %.1fs, %d jobs" % (meta["elapsed"], meta["jobs"])))
    out.append("")

    baseline = next((g for g in groups if g.baseline), None)
    for g in groups:
        tag = "   <- baseline" if g.baseline else ""
        label = g.outcome if g.outcome != OK else "ok"
        extra = ""
        if g.outcome == FAILED:
            label = "exit:%d" % g.sample.rc
        out.append("  GROUP %-3d %3d host%s  %-12s %s%s"
                   % (g.id, g.size, " " if g.size == 1 else "s", label,
                      "d=%s" % g.digest if g.outcome == OK else "", tag))
        hosts = g.hosts
        shown = hosts if (args.show_hosts or len(hosts) <= 6) else hosts[:5]
        line = "    hosts: %s" % " ".join(shown)
        if len(shown) < len(hosts):
            line += " ... (+%d, --show-hosts)" % (len(hosts) - len(shown))
        out.append(line)

        if g.outcome != OK:
            err = (g.sample.stderr or "").strip().splitlines()
            if err:
                out.append("    %s" % err[0][:150])
            out.append("")
            continue

        if args.full or baseline is None or g is baseline:
            body = g.sample.norm.splitlines()
            for l in body[:args.max_diff_lines]:
                out.append("    %s" % l[:160])
            if len(body) > args.max_diff_lines:
                out.append("    ... (%d more lines, --full)"
                           % (len(body) - args.max_diff_lines))
        else:
            diff = list(difflib.unified_diff(
                baseline.sample.norm.splitlines(),
                g.sample.norm.splitlines(),
                fromfile="baseline", tofile="group %d" % g.id, lineterm="",
                n=1))
            shown_d = diff[:args.max_diff_lines]
            for l in shown_d:
                out.append("    %s" % l[:160])
            if len(diff) > len(shown_d):
                out.append("    ... (%d more lines, --full)"
                           % (len(diff) - len(shown_d)))
        out.append("")

    out.extend(_hints(groups, args, meta))
    return "\n".join(out)


def _hints(groups, args, meta):
    out = ["  WHAT TO DO NEXT"]
    ok_groups = [g for g in groups if g.outcome == OK]
    bad = [g for g in groups if g.outcome != OK]
    minorities = [g for g in ok_groups if not g.baseline]

    if len(groups) == 1 and not bad:
        norms = meta["norms"]
        if any(n.startswith(("mask-numbers", "scrub")) for n in norms):
            out.append("    * Every host agrees -- but you masked numbers to "
                       "get there, so hosts differing only in a version or a "
                       "count would look identical.  Re-run with --strict to "
                       "check that is what you wanted.")
        else:
            out.append("    * Every host gave the same answer.  Nothing to "
                       "chase here.")
    for g in minorities[:2]:
        out.append("    * %s %s from the other %d.  If this is a version or "
                   "config check, those are the ones to fix:"
                   % (" and ".join(g.hosts[:3]),
                      "differs" if g.size == 1 else "differ",
                      sum(x.size for x in ok_groups if x.baseline)))
        out.append("      %s -H %s -- <the fix>"
                   % (PROG, ",".join(g.hosts[:3])))
    for g in bad[:2]:
        if g.outcome in (UNREACHABLE, TIMEOUT):
            out.append("    * %s never answered.  That is not \"fine because "
                       "it produced no output\" -- it is unknown.  Fix it or "
                       "take it out of the list."
                       % " ".join(g.hosts[:4]))
        elif g.outcome == PUSH_FAILED:
            out.append("    * Could not copy files to %s, so the command "
                       "never ran there."% " ".join(g.hosts[:4]))
        else:
            err = (g.sample.stderr or "").lower()
            if "password" in err and "sudo" in err:
                out.append("    * %s need a sudo password, so they were not "
                           "checked.  That is a configuration difference in "
                           "itself." % " ".join(g.hosts[:4]))
            else:
                out.append("    * %s exited %d.  Read the error above before "
                           "trusting the groups."
                           % (" ".join(g.hosts[:4]), g.sample.rc))
    if len(out) == 1:
        out.append("    * Nothing needs attention.")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

CSV_FIELDS = ["host", "group_id", "outcome", "exit_code", "duration_s",
              "digest", "stdout_bytes", "stderr_bytes", "is_baseline"]


def write_csv(results, groups, path):
    base = {g.id for g in groups if g.baseline}
    with io.open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in results:
            w.writerow({
                "host": r.host.name, "group_id": r.group,
                "outcome": r.outcome, "exit_code": r.rc,
                "duration_s": "%.2f" % r.duration, "digest": r.digest,
                "stdout_bytes": len(r.stdout), "stderr_bytes": len(r.stderr),
                "is_baseline": "1" if r.group in base else "0",
            })


def write_json(results, groups, path, meta):
    doc = {
        "meta": meta,
        "groups": [{"id": g.id, "size": g.size, "outcome": g.outcome,
                    "digest": g.digest, "baseline": g.baseline,
                    "hosts": g.hosts,
                    "sample": g.sample.norm[:4000]} for g in groups],
        "hosts": [{"host": r.host.name, "group": r.group,
                   "outcome": r.outcome, "exit_code": r.rc,
                   "duration_s": round(r.duration, 3)} for r in results],
    }
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")


def write_merged_csv(results, path):
    """Treat each host's stdout as a CSV and stack them under one header.

    ssh_host and the CSV's own host column are deliberately both kept: they
    differ (a login alias versus what the machine calls itself) and knowing
    which is which matters when they disagree.

    Rows are aligned by column *name*, not position.  These tools get
    scp'd to machines and stay there, so a fleet runs mixed versions, and
    stacking an older host's rows positionally files its values under the
    wrong headers.  A column a host does not have is left blank, and the
    skew is said out loud rather than silently misfiled.
    """
    header = ["ssh_host"]
    first_cols = None
    rows = []
    skewed = []
    unparsed = []
    for r in results:
        if r.outcome != OK or not r.stdout.strip():
            continue
        rdr = csv.DictReader(io.StringIO(r.stdout))
        cols = rdr.fieldnames
        # A host that printed something other than this CSV -- an error
        # message on stdout, a tool that exited 0 with a complaint -- must
        # not contribute its text as a *column*.  One field is not a CSV
        # header, and sharing nothing with the established schema is a
        # different tool's output rather than an older version of this one.
        if not cols or len(cols) < 2 or (
                first_cols is not None
                and not set(cols) & set(first_cols)):
            unparsed.append("%s: %s" % (r.host.name,
                                        (cols or [""])[0][:60]))
            continue
        for col in cols:
            if col not in header:
                header.append(col)
        if first_cols is None:
            first_cols = list(cols)
        elif list(cols) != first_cols:
            missing = [c for c in first_cols if c not in cols]
            extra = [c for c in cols if c not in first_cols]
            bits = []
            if missing:
                bits.append("lacks %s" % ",".join(missing))
            if extra:
                bits.append("adds %s" % ",".join(extra))
            skewed.append("%s %s" % (r.host.name, "; ".join(bits)
                                     or "reorders its columns"))
        for row in rdr:
            row.pop(None, None)        # cells past the host's own header
            if list(row.values()) == cols:
                continue               # a header line repeated mid-stream
            row["ssh_host"] = r.host.name
            rows.append(row)
    if first_cols is None:
        die("no host produced parseable CSV output", 1)
    if unparsed:
        sys.stderr.write("[%s] not merged, output is not this CSV: %s\n"
                         % (PROG, " / ".join(unparsed)))
    if skewed:
        sys.stderr.write("[%s] column skew across hosts (mixed tool "
                         "versions?): %s\n" % (PROG, " / ".join(skewed)))
    with io.open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, restval="",
                           extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
#
# This is a typo guard, not security: --yes defeats it and so does any
# amount of quoting cleverness.  It exists because `agree -- rm -rf /var/log`
# across 400 hosts at 3am is a different kind of bad day.

DANGER = [
    (r"\brm\s+(-\w+\s+)*-\w*[rf]", "rm -rf"),
    (r"\bmkfs(\.\w+)?\b", "mkfs"),
    (r"\bdd\b.*\bof=", "dd of="),
    (r">\s*/dev/[sh]d[a-z]", "writing to a raw disk"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutdown/reboot"),
    (r"\bsystemctl\s+(stop|restart|disable|mask)\b", "systemctl stop/restart"),
    (r"\b(apt|apt-get|yum|dnf|zypper)\b.*\b(remove|purge|erase)\b",
     "package removal"),
    (r"\buserdel\b|\bgroupdel\b", "user deletion"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b", "recursive chmod/chown"),
    (r"\btruncate\b", "truncate"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\biptables\s+-F\b|\bnft\s+flush\b", "firewall flush"),
]


def check_danger(command, args):
    for pat, label in DANGER:
        if re.search(pat, command):
            if args.yes:
                return
            extra = ""
            if not sys.stdin.isatty():
                extra = ("  (nothing is attached to this terminal, so there "
                         "is nobody to confirm)")
            die("refusing to run this on %s: it looks like %s.%s\n"
                "  If you meant it, add --yes:\n"
                "    %s --yes ... -- %s"
                % ("a fleet", label, extra, PROG, command), 2)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def cmd_hosts(args):
    for h in collect_hosts(args):
        if h.name == h.addr and not h.port:
            print(h.name)
        else:
            # A v6 address carrying a port has to be printed bracketed, or
            # this output does not round-trip: "fe80::1:2222" reads back as
            # a different address -- a valid one -- with no port at all,
            # and this verb exists to be fed to something else.
            addr = ("[%s]" % h.addr if h.port and is_ipv6(h.addr)
                    else h.addr)
            print("%s=%s%s" % (h.name, addr,
                               ":%d" % h.port if h.port else ""))
    return 0


DOCTOR_SCRIPT = (
    "printf 'python3='; command -v python3 >/dev/null 2>&1 && echo yes || "
    "echo MISSING; "
    "printf 'sudo='; sudo -n true >/dev/null 2>&1 && echo nopasswd || "
    "echo needs-password; "
    "printf 'writable='; [ -w /var/tmp ] && echo yes || echo NO; "
    "printf 'uptime='; uptime 2>/dev/null | sed 's/^ *//'")


def cmd_doctor(args):
    args.command = DOCTOR_SCRIPT
    args.strict = True
    return run_fleet(args, DOCTOR_SCRIPT, label="doctor")


def cmd_script(args):
    path = args.script_path
    if not os.path.isfile(path):
        die("no such script: %s" % path)
    remote = "%s/%s" % (args.remote_dir, os.path.basename(path))
    extra = " ".join(shlex.quote(a) for a in args.command_argv)
    command = "%s %s" % (shlex.quote(remote), extra) if extra else \
        shlex.quote(remote)
    args.push = list(args.push or []) + [path]
    return run_fleet(args, command, label="script")


def cmd_run(args):
    if not args.command_argv:
        die("no command given (put it after --, e.g. %s -- uname -r)" % PROG)
    command = " ".join(args.command_argv) if len(args.command_argv) > 1 \
        else args.command_argv[0]
    return run_fleet(args, command)


def run_fleet(args, command, label=None):
    hosts = collect_hosts(args)
    if args.limit and len(hosts) > args.limit and not args.yes:
        die("refusing to run across %d hosts (--limit %d); pass --yes to "
            "proceed" % (len(hosts), args.limit))
    check_danger(command, args)

    if args.sudo:
        prefix = "sudo -n -u %s " % shlex.quote(args.sudo_user) \
            if args.sudo_user else "sudo -n "
        command = prefix + command

    runner = Runner(args)

    if args.dry_run:
        for h in hosts:
            print(" ".join(shlex.quote(c)
                           for c in runner.ssh_argv(h, command)))
        return 0

    t0 = time.monotonic()
    results = fan_out(
        hosts,
        lambda h: runner.run_one(h, command, push=args.push,
                                 pull=args.pull,
                                 cleanup=not args.keep_remote),
        args.jobs, quiet=args.quiet)
    elapsed = time.monotonic() - t0

    groups = group_results(results, args)
    meta = {"command": command, "elapsed": round(elapsed, 2),
            "jobs": args.jobs, "hosts": len(hosts),
            "norms": active_normalizations(args), "label": label or "run"}

    if args.merge_csv:
        with _WriteGuard(args.merge_csv):
            n = write_merged_csv(results, args.merge_csv)
        sys.stderr.write("[%s] merged %d rows into %s\n"
                         % (PROG, n, args.merge_csv))
    if args.csv:
        with _WriteGuard(args.csv):
            write_csv(results, groups, args.csv)
    if args.json:
        with _WriteGuard(args.json):
            write_json(results, groups, args.json, meta)
    sys.stdout.write(render_groups(groups, args, meta) + "\n")

    failed = any(g.outcome != OK for g in groups)
    diverged = len([g for g in groups if g.outcome == OK]) > 1
    if failed:
        return 3
    return 1 if diverged else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common(p):
    p.add_argument("--hosts", "-f", action="append",
                   default=([_env("HOSTS")] if _env("HOSTS") else None))
    p.add_argument("-H", action="append")
    p.add_argument("--jobs", "-j", type=int,
                   default=int(_env("JOBS", min(32, 4 * (os.cpu_count() or 4)))))
    p.add_argument("--timeout", type=float, default=float(_env("TIMEOUT", 60)))
    p.add_argument("--max-output", type=int,
                   default=int(_env("MAX_OUTPUT", 16 * 1024 * 1024)))
    p.add_argument("--user", "-u", default=_env("USER", os.environ.get(
        "SSH_USER")))
    p.add_argument("--ssh", default=_env("SSH", "ssh"))
    p.add_argument("--scp", default=_env("SCP", "scp"))
    p.add_argument("--sudo", action="store_true")
    p.add_argument("--sudo-user")
    p.add_argument("--push", action="append", metavar="FILE")
    p.add_argument("--pull", metavar="GLOB")
    p.add_argument("--pull-dir", default="results")
    p.add_argument("--remote-dir",
                   default=_env("REMOTE_DIR", DEFAULT_REMOTE_DIR))
    p.add_argument("--keep-remote", action="store_true")
    p.add_argument("--first", type=int)
    p.add_argument("--limit", type=int,
                   default=int(_env("LIMIT", 0)) or None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--quiet", action="store_true")

    p.add_argument("--full", action="store_true")
    p.add_argument("--show-hosts", action="store_true")
    p.add_argument("--max-diff-lines", type=int, default=40)
    p.add_argument("--stderr", dest="stderr_mode", default="separate",
                   choices=["separate", "merge", "ignore"])
    p.add_argument("--csv", metavar="PATH")
    p.add_argument("--json", metavar="PATH")
    p.add_argument("--merge-csv", metavar="PATH")

    p.add_argument("--loose", action="store_true")
    p.add_argument("--fleet-csv", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--no-trim", dest="trim", action="store_false", default=True)
    p.add_argument("--no-strip-ansi", dest="strip_ansi",
                   action="store_false", default=True)
    p.add_argument("--squeeze-ws", action="store_true")
    p.add_argument("--sort-lines", action="store_true")
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--drop-empty", action="store_true")
    p.add_argument("--mask-hosts", action="store_true")
    p.add_argument("--mask-times", action="store_true")
    p.add_argument("--mask-numbers", action="store_true")
    p.add_argument("--scrub", action="append", metavar="RE")
    p.add_argument("--grep", metavar="RE")
    p.add_argument("--vgrep", metavar="RE")
    p.add_argument("--head", type=int)
    p.add_argument("--tail", type=int)
    p.add_argument("--field", type=int)


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__, add_help=True,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version="%s %s" % (PROG, VERSION))
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run a command everywhere (the default)")
    _add_common(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("script", help="push a script, run it, collect output")
    s.add_argument("script_path", metavar="PATH")
    _add_common(s)
    s.set_defaults(func=cmd_script)

    h = sub.add_parser("hosts", help="expand and print the host list")
    _add_common(h)
    h.set_defaults(func=cmd_hosts)

    d = sub.add_parser("doctor", help="can each host be reached and used?")
    _add_common(d)
    d.set_defaults(func=cmd_doctor)

    hp = sub.add_parser("help", help="every flag of every verb")
    hp.set_defaults(func=None)
    return p, sub


def apply_presets(args):
    # A pattern that will not compile is refused here, naming the flag and
    # the reason, rather than surfacing as a traceback from inside the
    # per-host normalizer once the fan-out is already underway.
    for flag in ("grep", "vgrep"):
        pat = getattr(args, flag, None)
        if pat:
            try:
                re.compile(pat)
            except re.error as exc:
                die("bad --%s pattern %r: %s" % (flag, pat, exc))
    for pat in (getattr(args, "scrub", None) or []):
        try:
            re.compile(pat)
        except re.error as exc:
            die("bad --scrub pattern %r: %s" % (pat, exc))
    if args.strict and args.fleet_csv:
        die("--strict and --fleet-csv contradict each other: one turns every "
            "normalization off, the other turns two on")
    if args.fleet_csv:
        # Every tool here emits CSV beginning host,ts -- both per-host by
        # construction, so without these two no host can ever agree with
        # another and the fleet grouping is impossible.  A preset rather
        # than a default because the normalizations are still disclosed
        # with the result, and a reader has to see that the host column
        # was masked to get there.
        args.mask_hosts = True
        args.mask_times = True
    if args.strict:
        for f in ("trim", "strip_ansi", "squeeze_ws", "sort_lines",
                  "ignore_case", "drop_empty", "mask_hosts", "mask_times",
                  "mask_numbers"):
            setattr(args, f, False)
        args.scrub = None
    elif args.loose:
        args.trim = True
        args.squeeze_ws = True
        args.mask_times = True
        args.mask_hosts = True
        args.mask_numbers = True


def cmd_full_help(parser, sub):
    parser.print_help()
    for name, p in sorted(sub.choices.items()):
        if name == "help":
            continue
        print("")
        print("=" * 72)
        p.print_help()
    return 0


def main(argv=None):
    _stdio_safe()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Everything after `--` is the command, and must never be parsed as
    # flags: `agree -- ls -l` means ls's -l, not ours.
    command_argv = []
    if "--" in argv:
        i = argv.index("--")
        argv, command_argv = argv[:i], argv[i + 1:]

    parser, sub = build_parser()
    if argv and argv[0] == "help":
        return cmd_full_help(parser, sub)
    # Top-level flags are dealt with before the verb defaulting below.
    # Otherwise `agree --version` becomes `agree run --version` and dies in
    # the run parser, which has no such flag.
    if argv and argv[0] in ("--version", "-h", "--help"):
        parser.parse_args(argv)     # argparse prints and exits
        return 0
    if not argv or argv[0] not in sub.choices:
        if not argv and not command_argv:
            parser.print_help()
            return 2
        argv = ["run"] + argv

    args = parser.parse_args(argv)
    args.command_argv = command_argv
    apply_presets(args)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)

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
