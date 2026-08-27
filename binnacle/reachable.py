#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""reachable.py -- check a server list and comment out the ones that failed.

Usage: reachable.py hosts.txt                 report, print the result to stdout
       reachable.py hosts.txt -o pruned.txt   write the result to a new file
       reachable.py hosts.txt -i              rewrite hosts.txt in place (+ .bak)
       reachable.py hosts.txt --csv           one row per host, for a record

Options:
  -o, --output FILE   write the annotated list here      (default: stdout)
  -i, --in-place      rewrite the input file             (keeps a .bak)
      --no-backup     with -i, do not keep the .bak
  -j, --jobs N        hosts contacted at once            (REACHABLE_JOBS)
      --timeout S     ssh connect timeout, seconds       (REACHABLE_TIMEOUT)
      --ping-timeout S  ping deadline, seconds           (default 2)
      --ping-count N  echo requests per host             (default 1)
      --require WHAT  what counts as usable: ssh (default), ping, both, any
      --user NAME     ssh user                           (REACHABLE_USER)
      --ssh CMD       ssh command                        (REACHABLE_SSH)
      --ping CMD      ping command                       (REACHABLE_PING)
      --no-ping       skip ping entirely, judge on ssh alone
      --keep-auth     treat an ssh key/permission failure as usable
      --recheck-only  re-test the already-commented hosts and nothing else
      --csv [PATH]    host,addr,ping,ssh,usable,outcome,detail,duration_s
      --json [PATH]   the same, with a summary block
      --quiet         no progress and no summary, just the list
      --dry-run       check nothing; print what would be contacted

What it does
  A server list rots.  Boxes get decommissioned, renamed, rebuilt without
  your key, or moved behind a firewall, and the list goes on naming them
  until some later run fans out to 200 hosts and quietly does nothing on
  eleven of them.  This checks every entry and comments out the ones that
  are not usable, leaving a file the other tools can still read.

  Both probes are run because they answer different questions.  **ssh is
  the gate** -- it is what the fleet tools actually need -- and **ping is
  the explanation**: a host that pings but refuses ssh is a key or firewall
  problem, and a host that does neither is off or gone.  A host that
  answers ssh is kept even when it does not answer ping, because plenty of
  networks drop ICMP and commenting those out would throw away working
  machines.

  Running it again is the point.  Entries this tool commented out are
  re-tested on the next run and **uncommented if they come back**, so the
  list converges on the truth instead of decaying in one direction.  Your
  own comments are never touched: only lines carrying this tool's marker
  are managed.

Output format
  The file is rewritten line for line.  Blank lines, your comments, inline
  comments and the order of entries all survive; the only change is a
  leading '# ' on failures and a marker explaining why:

      web01
      # db07  #[unreachable] no ping, no ssh: timed out - 2026-08-15
      cache02  # the slow one

Robustness properties
  * your comments and blank lines are preserved exactly; only lines with
    the #[unreachable] marker are ever managed by this tool
  * previously-commented hosts are re-checked and restored when they come
    back, so the list does not decay in one direction
  * a probe that could not be run is reported as unknown, never as failure
    -- if ping is missing, that is said, not silently treated as a down host
  * ssh answering outranks ping failing: ICMP is blocked on plenty of
    healthy networks
  * the failure reason is recorded in the file, so a later reader knows
    whether a host was off, firewalled, or just missing your key
  * writes are atomic (temp file + rename), and -i keeps a .bak by default
  * output order is the input order, so two runs diff cleanly
  * needs no root and no packages; ssh runs with BatchMode=yes so it can
    never sit waiting for a password
"""

import argparse
import concurrent.futures
import csv
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import date

VERSION = "0.4.0"
PROG = os.path.basename(sys.argv[0]) or "reachable.py"

# Lines this tool has commented out carry this marker, so a later run can
# tell its own work from a note somebody wrote by hand.  Anything without
# it is left completely alone.
MARKER = "#[unreachable]"

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]

# Outcomes, most specific first.  The distinction is the whole point: "your
# key is not on it" and "it is switched off" are different problems.
OK = "ok"
AUTH = "auth"
REFUSED = "refused"
DNS = "dns"
TIMEOUT = "timeout"
NO_ROUTE = "no-route"
DOWN = "down"
UNKNOWN = "unknown"

OUTCOME_TEXT = {
    OK: "reachable",
    AUTH: "ssh refused your key",
    REFUSED: "host is up, sshd refused the connection",
    DNS: "name does not resolve",
    TIMEOUT: "ssh timed out",
    NO_ROUTE: "no route to host",
    DOWN: "no ping, no ssh",
    UNKNOWN: "could not be determined",
}


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
    v = os.environ.get("REACHABLE_" + name)
    return v if v not in (None, "") else default


def note(msg, quiet=False):
    if not quiet:
        sys.stderr.write("[%s] %s\n" % (PROG, msg))
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Host tokens and ranges
# ---------------------------------------------------------------------------
#
# Same grammar as servers.txt in iperf_orchestrator and matrix_orchestrator:
# '#' comments, blank lines, name[=addr[:port]] tokens.
# canonical copy of the range expander: binnacle/agree.py expand_range

RANGE_RE = re.compile(r"\[([^\]]+)\]")


def expand_range(spec):
    """node[01-24] / node[1,3,5-8] / rack[a-c]-node[01-04] (cartesian)."""
    m = RANGE_RE.search(spec)
    if not m:
        return [spec]
    # [::1] and [fe80::1]:22 are addresses, not ranges -- expanding one
    # strips the brackets and glues the port back on as more colons.
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


def is_ipv6(addr):
    """A colon in an address can only be IPv6: v4 and names have none."""
    return ":" in addr


def split_host_port(spec):
    """'host', 'host:port', '[v6]', '[v6]:port', bare v6 -> (addr, port).

    canonical copy: binnacle/agree.py split_host_port.  IPv6 needs the
    bracket form to carry a port, because the address is made of colons
    and the last one would otherwise be read as a port separator -- which
    is how "::1" once became the address ":" on port 1, and this tool
    rewrites the file it is given on the strength of that answer.
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


def token_address(tok):
    """'name[=addr[:port]]' -> (name, addr, port_or_None)."""
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
    return name or tok, addr or tok, port


# ---------------------------------------------------------------------------
# The file, line by line
# ---------------------------------------------------------------------------

BLANK, COMMENT, ENTRY = "blank", "comment", "entry"


class Line(object):
    """One line of the list, kept close enough to the original to put back.

    `payload` is the line as it would look uncommented and unmarked -- that
    is what gets re-commented or restored, so inline comments survive a
    round trip.
    """

    __slots__ = ("raw", "kind", "payload", "was_commented", "tokens",
                 "results")

    def __init__(self, raw):
        self.raw = raw.rstrip("\n")
        self.kind = BLANK
        self.payload = ""
        self.was_commented = False
        self.tokens = []
        self.results = {}
        self._classify()

    def _classify(self):
        stripped = self.raw.strip()
        if not stripped:
            self.kind = BLANK
            return
        if stripped.startswith("#"):
            if MARKER in self.raw:
                # One of ours: recover the line as it was before we
                # commented it, so a host that comes back is restored
                # exactly, inline comment and all.
                body = self.raw.split(MARKER, 1)[0]
                body = body.rstrip()
                body = re.sub(r"^\s*#\s?", "", body, count=1)
                self.kind = ENTRY
                self.payload = body.rstrip()
                self.was_commented = True
            else:
                self.kind = COMMENT
            return
        self.kind = ENTRY
        self.payload = self.raw

    def host_specs(self):
        """The host tokens on this line, ignoring any inline comment."""
        code = self.payload.split("#", 1)[0].strip()
        if not code:
            return []
        out = []
        for tok in code.split():
            out.extend(expand_range(tok))
        return out

    def render(self, usable_by_spec, today):
        """This line as it should appear in the output."""
        if self.kind in (BLANK, COMMENT):
            return self.raw
        specs = self.tokens
        if not specs:
            return self.raw
        bad = [s for s in specs if not usable_by_spec.get(s, True)]
        if not bad:
            # Reachable: strip our marker if it was there, restoring the
            # line to exactly what the user originally wrote.
            return self.payload
        if len(bad) < len(specs):
            # A range line with mixed results cannot be half-commented, so
            # it is expanded into one line per host and each judged on its
            # own.  Doing anything else would either comment out working
            # hosts or leave dead ones live.
            lines = []
            trailing = ""
            if "#" in self.payload:
                trailing = "  " + self.payload.split("#", 1)[1].strip()
                trailing = "  #" + trailing.lstrip("# ").rstrip()
            for s in specs:
                if usable_by_spec.get(s, True):
                    lines.append("%s%s" % (s, trailing))
                else:
                    lines.append(_comment(s + trailing,
                                          self.results.get(s), today))
            return "\n".join(lines)
        return _comment(self.payload, self.results.get(bad[0]), today)


def _comment(payload, result, today):
    reason = result.detail if result else "not checked"
    return "# %s  %s %s - %s" % (payload, MARKER, reason, today)


def read_lines(path):
    if path == "-":
        # a pipe can carry a BOM too, e.g. type hosts.txt | ssh box ...
        text = sys.stdin.read().lstrip("\ufeff")
    else:
        if not os.path.isfile(path):
            die("no such file: %s" % path)
        try:
            with io.open(path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            die("cannot read %s: %s" % (path, exc))
    return [Line(l) for l in text.splitlines()]


def write_atomic(path, text):
    """Temp file plus rename: a half-written server list is worse than none.

    The file being replaced is the user's own, so its identity has to
    survive the replacement.  A symlink is followed rather than clobbered
    -- an inventory is often a link into a shared checkout, and replacing
    the link both destroys it and leaves the file everyone else reads
    untouched.  Mode and ownership are carried across too: without that a
    0600 host list came back 0644, publishing the name of every box in it.
    """
    path = os.path.realpath(path)
    try:
        st = os.stat(path)
    except OSError:
        st = None
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        if st is not None:
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass    # not root: the mode is the half that leaks
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die("cannot write %s: %s" % (path, exc))


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

class Result(object):
    __slots__ = ("spec", "name", "addr", "port", "ping", "ssh", "outcome",
                 "detail", "duration", "usable", "restored")

    def __init__(self, spec):
        self.spec = spec
        self.name, self.addr, self.port = token_address(spec)
        self.ping = None          # True / False / None = not measured
        self.ssh = None
        self.outcome = UNKNOWN
        self.detail = ""
        self.duration = 0.0
        self.usable = False
        self.restored = False     # was commented out, and has come back


def _cannot_ping_this_family(err):
    low = (err or "").lower()
    return ("invalid option" in low or "unrecognized option" in low
            or "unknown option" in low or "usage:" in low
            or "address family not supported" in low)


class Prober(object):
    def __init__(self, args):
        self.args = args
        self.ssh_cmd = (args.ssh or "ssh").split()
        self.ping_cmd = (args.ping or "ping").split()
        self.have_ping = args.no_ping is False and \
            shutil.which(self.ping_cmd[0]) is not None

    def _run(self, argv, timeout):
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL,
                                 universal_newlines=True,
                                 encoding="utf-8-sig", errors="replace")
            out, err = p.communicate(timeout=timeout)
            return p.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            return 124, "", "timed out"
        except OSError as exc:
            return 127, "", str(exc)

    def ping_host(self, addr):
        if not self.have_ping:
            return None
        argv = list(self.ping_cmd)
        if is_ipv6(addr):
            # iputils has taken -6 since 2015; older ones need ping6 and
            # will reject the flag, which is handled below.
            argv += ["-6"]
        argv += ["-n", "-q",
                 "-c", str(self.args.ping_count),
                 "-w", str(int(max(1, self.args.ping_timeout))),
                 addr]
        rc, _out, err = self._run(argv, self.args.ping_timeout + 3)
        if rc == 127:
            return None
        if rc != 0 and _cannot_ping_this_family(err):
            # "ping does not speak IPv6 here" is not "the host is down".
            # A probe that could not be run is unknown, never a failure --
            # and ssh is the gate anyway.
            return None
        return rc == 0

    def ssh_host(self, r):
        argv = list(self.ssh_cmd) + SSH_OPTS + [
            "-o", "ConnectTimeout=%d" % int(max(1, self.args.timeout))]
        if r.port:
            argv += ["-p", str(r.port)]
        target = "%s@%s" % (self.args.user, r.addr) if self.args.user else r.addr
        argv += [target, "true"]
        rc, _out, err = self._run(argv, self.args.timeout + 5)
        return rc, err

    def classify(self, r, rc, err):
        low = (err or "").lower()
        if rc == 0:
            return OK, "reachable"
        if "permission denied" in low or "publickey" in low or \
                "authentication failed" in low:
            return AUTH, "ssh refused your key"
        if "connection refused" in low:
            return REFUSED, "host is up, sshd refused the connection"
        if "not known" in low or "could not resolve" in low or \
                "name or service" in low or "nodename nor servname" in low:
            return DNS, "name does not resolve"
        if "no route to host" in low or "network is unreachable" in low:
            return NO_ROUTE, "no route to host"
        if "timed out" in low or rc == 124:
            return TIMEOUT, "ssh timed out"
        if rc == 127:
            return UNKNOWN, "could not run ssh"
        first = (err or "").strip().splitlines()
        return DOWN, (first[0][:120] if first else "ssh failed (rc %d)" % rc)

    def check(self, spec):
        r = Result(spec)
        t0 = time.monotonic()
        if self.args.dry_run:
            r.detail = "not contacted (--dry-run)"
            r.usable = True
            r.duration = 0.0
            return r
        r.ping = self.ping_host(r.addr)
        rc, err = self.ssh_host(r)
        r.ssh = (rc == 0)
        r.outcome, r.detail = self.classify(r, rc, err)
        r.duration = time.monotonic() - t0

        # ssh is the gate; ping only explains.  A host answering ssh is
        # kept even with no ping, because ICMP is blocked on plenty of
        # healthy networks and dropping those would discard live machines.
        if not r.ssh and r.ping is False and r.outcome in (TIMEOUT, DOWN):
            r.outcome = DOWN
            r.detail = "no ping, no ssh"
        elif not r.ssh and r.ping is True and r.outcome in (TIMEOUT, DOWN):
            r.detail = "pings but ssh does not answer"

        req = self.args.require
        if req == "ssh":
            r.usable = bool(r.ssh)
        elif req == "ping":
            r.usable = r.ping is True
        elif req == "both":
            r.usable = bool(r.ssh) and r.ping is True
        else:                      # any
            r.usable = bool(r.ssh) or r.ping is True
        if self.args.keep_auth and r.outcome == AUTH:
            r.usable = True
        return r


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

CSV_FIELDS = ["host", "addr", "ping", "ssh", "usable", "outcome", "detail",
              "duration_s"]


def _tri(v):
    return "" if v is None else ("yes" if v else "no")


def write_csv(results, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in results:
            w.writerow({"host": r.name, "addr": r.addr,
                        "ping": _tri(r.ping), "ssh": _tri(r.ssh),
                        "usable": "yes" if r.usable else "no",
                        "outcome": r.outcome, "detail": r.detail,
                        "duration_s": "%.2f" % r.duration})
    finally:
        if path:
            fh.close()


def write_json(results, path, summary):
    doc = {"meta": {"tool": PROG, "version": VERSION, "ts": int(time.time())},
           "summary": summary,
           "hosts": [{"host": r.name, "addr": r.addr, "ping": r.ping,
                      "ssh": r.ssh, "usable": r.usable, "outcome": r.outcome,
                      "detail": r.detail, "duration_s": round(r.duration, 3)}
                     for r in results]}
    text = json.dumps(doc, indent=2, sort_keys=True, default=str)
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


def summarize(results, restored, commented, args):
    by = {}
    for r in results:
        by[r.outcome] = by.get(r.outcome, 0) + 1
    usable = sum(1 for r in results if r.usable)
    lines = []
    lines.append("%d of %d usable" % (usable, len(results)))
    bad = [r for r in results if not r.usable]
    if bad:
        groups = {}
        for r in bad:
            groups.setdefault(r.outcome, []).append(r.name)
        for outcome in (DNS, NO_ROUTE, DOWN, TIMEOUT, REFUSED, AUTH, UNKNOWN):
            if outcome not in groups:
                continue
            names = groups[outcome]
            lines.append("  %-9s %s%s"
                         % (outcome,
                            " ".join(names[:6]),
                            "" if len(names) <= 6 else " (+%d)"
                            % (len(names) - 6)))
    if commented:
        lines.append("commented out %d" % commented)
    if restored:
        names = sorted(r.name for r in results if r.restored)
        lines.append("restored %d that came back: %s"
                     % (restored, " ".join(names[:6])))

    # The advice, in the same voice as the other tools: name the mechanism.
    hints = []
    if any(r.outcome == AUTH for r in bad):
        hints.append("Hosts under 'auth' are up and running sshd -- your key "
                     "is not on them. They are a provisioning gap, not dead "
                     "boxes; --keep-auth keeps them in the list.")
    if any(r.outcome == REFUSED for r in bad):
        hints.append("Hosts under 'refused' answered but sshd did not. The "
                     "machine is alive: check the service and the firewall "
                     "before removing it.")
    if any(r.outcome == DNS for r in bad):
        hints.append("Hosts under 'dns' never resolved, so nothing was "
                     "contacted. That is usually a stale name in the list "
                     "rather than a broken host.")
    if any(r.ping is False and r.ssh for r in results):
        n = sum(1 for r in results if r.ping is False and r.ssh)
        hints.append("%d host%s answered ssh but not ping -- kept, because "
                     "ICMP is blocked on plenty of healthy networks."
                     % (n, "" if n == 1 else "s"))
    return lines, hints



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", metavar="FILE", help="server list, one per line")
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("-o", "--output", metavar="FILE")
    p.add_argument("-i", "--in-place", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("-j", "--jobs", type=int,
                   default=int(_env("JOBS", min(32, 4 * (os.cpu_count() or 4)))))
    p.add_argument("--timeout", type=float, default=float(_env("TIMEOUT", 8)))
    p.add_argument("--ping-timeout", type=float, default=2.0)
    p.add_argument("--ping-count", type=int, default=1)
    p.add_argument("--require", default="ssh",
                   choices=["ssh", "ping", "both", "any"])
    p.add_argument("--user", default=_env("USER"))
    p.add_argument("--ssh", default=_env("SSH", "ssh"))
    p.add_argument("--ping", default=_env("PING", "ping"))
    p.add_argument("--no-ping", action="store_true")
    p.add_argument("--keep-auth", action="store_true")
    p.add_argument("--recheck-only", action="store_true")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None):
    _stdio_safe()
    args = build_parser().parse_args(argv)
    if args.in_place and args.path == "-":
        die("cannot rewrite stdin in place")
    if args.in_place and args.output:
        die("use either --in-place or --output, not both")

    lines = read_lines(args.path)

    specs = []
    seen = set()
    for ln in lines:
        if ln.kind != ENTRY:
            continue
        if args.recheck_only and not ln.was_commented:
            continue
        ln.tokens = ln.host_specs()
        for s in ln.tokens:
            if s not in seen:
                seen.add(s)
                specs.append(s)
    if not specs:
        die("no host entries found in %s" % args.path, 1)

    # Parse every token before anything is probed or written.  A token this
    # tool cannot understand has to stop the run up front, not surface from
    # inside a worker thread halfway through rewriting the caller's file.
    for spec in specs:
        token_address(spec)

    if args.dry_run:
        for s in specs:
            print(s)
        return 0

    prober = Prober(args)
    if not prober.have_ping and not args.no_ping:
        note("ping is unavailable here, judging on ssh alone", args.quiet)

    note("checking %d hosts, %d at a time" % (len(specs), args.jobs),
         args.quiet)
    workers = max(1, min(args.jobs, len(specs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(prober.check, specs))
    by_spec = {r.spec: r for r in results}
    usable_by_spec = {s: by_spec[s].usable for s in by_spec}

    commented = restored = 0
    for ln in lines:
        if ln.kind != ENTRY or not ln.tokens:
            continue
        ln.results = {s: by_spec[s] for s in ln.tokens if s in by_spec}
        bad = [s for s in ln.tokens if not usable_by_spec.get(s, True)]
        if bad and not ln.was_commented:
            commented += 1
        elif not bad and ln.was_commented:
            restored += 1
            for s in ln.tokens:
                by_spec[s].restored = True

    today = date.today().isoformat()
    out_lines = [ln.render(usable_by_spec, today) for ln in lines]
    text = "\n".join(out_lines) + ("\n" if out_lines else "")

    if args.in_place:
        if not args.no_backup:
            try:
                shutil.copy2(args.path, args.path + ".bak")
            except OSError as exc:
                # A partial .bak is a trap for whoever restores from it:
                # better no backup and a clear failure than a truncated one.
                try:
                    os.unlink(args.path + ".bak")
                except OSError:
                    pass
                die("cannot write backup: %s" % exc)
        write_atomic(args.path, text)
        note("rewrote %s%s" % (args.path,
                               "" if args.no_backup else " (.bak kept)"),
             args.quiet)
    elif args.output:
        write_atomic(args.output, text)
        note("wrote %s" % args.output, args.quiet)
    elif args.csv is None and args.json is None:
        sys.stdout.write(text)

    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            write_csv(results, args.csv or None)

    summary_lines, hints = summarize(results, restored, commented, args)
    summary = {"total": len(results),
               "usable": sum(1 for r in results if r.usable),
               "commented": commented, "restored": restored}
    if args.json is not None:
        with _WriteGuard(args.json or None):
            write_json(results, args.json or None, summary)

    if not args.quiet:
        sys.stderr.write("\n")
        for l in summary_lines:
            sys.stderr.write("  %s\n" % l)
        if hints:
            sys.stderr.write("\n  WHAT TO DO NEXT\n")
            for h in hints:
                sys.stderr.write("    * %s\n" % _wrap(h, 6))
        sys.stderr.write("\n")

    return 1 if any(not r.usable for r in results) else 0


def _wrap(text, indent, width=76):
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
