#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""dredge.py -- bring an answer back from every host, named so you can tell them apart.

Usage: dredge /var/log/syslog --hosts hosts.txt      one file from every host
       dredge --cmd 'ss -s' --hosts hosts.txt        what a command says, instead
       dredge /var/log/syslog --tail 200 -H 'web[01-40]'   only the last 200 lines
       dredge /etc/nginx --hosts hosts.txt           a whole directory each
       dredge /var/log/app.log --since -1h           only what changed lately
       dredge --cmd 'uptime' --tag before --hosts h  labelled, to keep runs apart

Options:
  -c, --cmd CMD       a bash command to run on each host; what it says comes
                      back as the artifact, in place of a file
  -t, --tag NAME      label this run's artifacts, so several runs can share
                      one directory and still be told apart
  -H, --host TOKEN    hosts, repeatable; ranges expand (`web[01-40]`)
      --hosts FILE    a server list, one per line -- reachable's output works
  -d, --dir DIR       where collected files land   (default: dredge-<stamp>)
      --head N        only the first N lines of each file
      --tail N        only the last N lines of each file
      --since T       only files modified since T: -30m, 14:20, an ISO stamp
      --append        add what came back to the end of what is already here
      --prepend       add it to the beginning instead
      --mark          write a marker line where old meets new
      --max-bytes N   skip a file larger than this  (default 100M, 0 none)
      --max-files N   stop after this many files per host   (default 500)
  -j, --jobs N        hosts contacted at once               (DREDGE_JOBS, 20)
      --timeout S     ssh timeout per host                  (DREDGE_TIMEOUT)
      --user NAME     ssh user                              (DREDGE_USER)
      --ssh CMD       ssh command                           (DREDGE_SSH)
      --csv [PATH]    host,source,local_path,bytes,outcome,exit_status
      --dry-run       print the remote command and stop
      --quiet         no progress and no summary, just the findings

What it does
  Something is wrong on some of forty machines and the evidence is in a
  file on each of them.  Collecting it by hand is forty scp commands whose
  results all land on top of each other, because every one of them is
  called `syslog`.  This runs the copy once, in parallel, and lands the
  results under a name that says which machine each came from -- so the
  next command can be `grep -r`, or `agree`, or `logtriage` over the lot.

  It is the gathering half of what `agree` does with commands: agree runs
  one command everywhere and groups the answers, this brings one *file*
  back from everywhere and keeps them apart.

Only the part you need, and only if it changed
  A log is usually gigabytes and the interesting part is the end of it.
  `--tail 200` runs the tail **on the far side** and brings back the two
  hundred lines, not the four gigabytes -- which is the difference between
  a run that takes a second and one that saturates the link it is meant to
  be diagnosing.  `--head` is the same at the other end of the file.

  `--since` filters by modification time, also on the far side, so a
  directory of a thousand files hands back the six that moved.  The
  timestamp goes over as an epoch second rather than a wall-clock string,
  so it means the same thing on a box in another timezone.

  It still means *that box's* idea of the time.  A machine whose clock is
  wrong will hand you the wrong files and nothing here can tell -- `skew`
  is the instrument for that question, and worth a run across the fleet
  before trusting a tight `--since` window.

A file or a command, the same way round
  Half of what you want off a fleet is in a file and half of it is only
  ever printed: `ss -s`, `sysctl -a`, `rpm -q`, `systemctl is-failed`.
  `--cmd` runs one bash command on each host and lands what it says as
  that host's artifact, so both halves come back into the same directory
  under the same naming, and the command afterwards -- `grep -l`,
  `logtriage`, a diff -- does not care which half it is reading.

  The command is a bash command, and it arrives exactly as typed.  It
  travels base64'd and is decoded into a variable on the far side, so no
  shell parses it on the way: quotes, newlines, `$(...)`, backticks and
  backslashes all survive, and the only shell that ever interprets it is
  the bash that runs it.  Quoting it instead would mean nesting two
  levels correctly and getting both right for whatever the remote login
  shell turns out to be; base64 makes that count zero.

  stderr is merged into stdout, because the answer is what you would have
  seen on the terminal and in the order you would have seen it -- a tool
  that writes its headline to stderr and its table to stdout is giving
  one answer, not two.  A command that wants them apart can say so
  (`... 2>/dev/null`).

  The exit status is the one thing a file has no equivalent of, so it is
  kept rather than folded away: it comes back in its own frame, is
  reported when it is not zero, and is a column in `--csv`.  A command
  that failed still has an answer worth keeping -- its error text is the
  artifact -- so the collection is not a failure, it is a finding.

Names that stay apart
  One directory per run, and everything in it is told apart by its *name*
  rather than by where it sits:

      dredge-20260910-172845/web01~var~log~syslog
      dredge-20260910-172845/web02~var~log~syslog
      dredge-20260910-172845/web02~var~log~nginx~error.log

  Rebuilding each host's directory tree locally reads well and greps
  badly.  The command you actually want next is `grep -l oom *` or
  `logtriage dredge-*/web*syslog`, and both of those want one directory
  of distinctly-named files, not forty identical paths under forty host
  directories.

  `--tag NAME` puts a label in front of every name in the run:

      audit~web01~etc~ssh~sshd_config      (a file, tagged)
      ss~web01                             (--cmd 'ss -s', tag from the
                                            command's own first word)

  That is the order that makes a shared directory readable: several runs
  land side by side, `ls` groups them by run, `rm audit~*` clears one of
  them, and a file says which collection it belongs to without anyone
  having to remember.

  `-d DIR` names the directory yourself.  Without it every run gets one
  of its own, stamped with the time: collecting the same path twice an
  hour apart is the normal way to use this, and the second run quietly
  replacing the first is not a result anybody wants to find later.

  Host names are what make these unique, so a list naming one host twice
  is refused rather than quietly collecting it twice into the same place.

Collecting the same thing again
  `--append` adds what came back to the end of the local file instead of
  replacing it, and `--prepend` adds it to the beginning.  That is how a
  local copy grows over a week of runs.  `--mark` writes a line saying
  which host and when, at the seam.

  Nothing is de-duplicated: appending a whole file twice gives you it
  twice.  The pairing that makes sense is `--tail`/`--since` with
  `--append`, where each run brings back a slice that the last one did not
  have.  Overlap is still possible -- two runs an hour apart with
  `--tail 200` will repeat whatever both ends saw.

How it goes over the wire
  One ssh per host, and one round trip: a `find` on the far side selects
  the files and the whole selection comes back down the same connection.
  Forty hosts with a thousand files each is forty connections, not forty
  thousand.

  Whole files travel as a tar stream, which costs nothing over the bytes
  themselves.  `--head` and `--tail` cannot be tarred -- the size of a
  slice is not known until it has been cut, and a live log changes size
  between measuring and reading -- so those come back base64 inside a
  framed stream instead.  The 33% that costs is nothing when the payload
  is two hundred lines, and it cannot desynchronise on a file being
  written to.

  Nothing from a remote host is ever used as a local path.  Names are
  rebuilt here from the path you asked for, so a host that answers with
  `../../etc/cron.d/x` writes nothing outside the collection directory.

  `--timeout` bounds the whole transfer rather than just the connection:
  a host that goes quiet halfway through is one row in the report and
  not a run that never returns.  Each ssh gets a session of its own, so
  ending one takes with it anything the remote command left behind
  holding the connection open.

Exit status
  0   every host answered, everything asked for came back, and any
      command exited zero
  1   a host failed, a path was missing, a command exited non-zero, a
      ceiling was hit, a name collided, or nothing was collected
  2   usage error
"""

import argparse
import base64
import binascii
import csv
import io
import os
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

VERSION = "0.6.0"
PROG = os.path.basename(sys.argv[0]) or "dredge.py"

DEFAULT_JOBS = 20
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 500

# canonical copy: binnacle/agree.py SSH_OPTS.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]

OK, MISSING, FAILED, TIMEOUT, UNREACHABLE = (
    "ok", "missing", "failed", "timeout", "unreachable")

# canonical copy: binnacle/agree.py RANGE_RE.
RANGE_RE = re.compile(r"\[([^\]]+)\]")

# The character that stands in for a path separator in a collected name.
# A hostname cannot contain it and a path rarely does, which is what makes
# the folded name readable; where two remote paths do fold onto one local
# name the collision is detected rather than silently overwritten.
FLAT_SEP = "~"


def default_dir():
    """A directory of this run's own, when the caller did not name one.

    Every run lands somewhere new rather than on top of the last one:
    collecting the same path twice an hour apart is the normal way to
    use this, and the second run silently replacing the first is not a
    result anybody wants to discover later.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "dredge-%s" % stamp
    if not os.path.exists(base):
        return base
    # Two runs inside one second. Rare, and cheaper to number than to
    # think about.
    for n in range(2, 100):
        candidate = "%s-%d" % (base, n)
        if not os.path.exists(candidate):
            return candidate
    return base


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    Hostnames and paths come off somebody else's machine, and under
    LANG=C -- a RHEL 8 default, and the floor this package targets --
    stdout is ASCII, so one UTF-8 filename would end the run.
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
    v = os.environ.get("DREDGE_" + name)
    return v if v not in (None, "") else default


def _env_num(name, default, cast=float):
    raw = _env(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        die("DREDGE_%s is not a number: %r" % (name, raw))


# canonical copy: binnacle/agree.py progress/progress_done.
_progress_on = [False]


def progress(msg):
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K[%s] %s" % (PROG, msg))
        sys.stderr.flush()
        _progress_on[0] = True


def progress_done():
    if _progress_on[0]:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _progress_on[0] = False


def fmt_bytes(n):
    if n is None:
        return "-"
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return "%.1f%s" % (n / float(size), unit)
    return "%dB" % n


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

class Host(object):
    __slots__ = ("name", "addr", "port")

    def __init__(self, name, addr, port=None):
        self.name, self.addr, self.port = name, addr, port

    def __repr__(self):
        return self.name


# canonical copy: binnacle/agree.py is_ipv6.
def is_ipv6(addr):
    """A colon in an address can only be IPv6: v4 and names have none."""
    return ":" in addr


# canonical copy: binnacle/agree.py split_commas.
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


# canonical copy: binnacle/agree.py expand_range.
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


# canonical copy: binnacle/agree.py _port_of.
def _port_of(text, spec):
    """A port number, or a refusal naming the token it came from."""
    if not text.isdigit():
        die("bad port %r in host token %r" % (text, spec))
    port = int(text)
    if not 1 <= port <= 65535:
        die("port %d is out of range in host token %r (want 1-65535)"
            % (port, spec))
    return port


# canonical copy: binnacle/agree.py split_host_port.
def split_host_port(spec):
    """'host', 'host:port', '[v6]', '[v6]:port', bare v6 -> (addr, port)."""
    spec = spec.strip()
    port = None
    if spec.startswith("["):
        addr, sep, rest = spec[1:].partition("]")
        if not sep:
            die("no closing ] in host token %r" % spec)
        if rest.startswith(":"):
            port = _port_of(rest[1:], spec)
        elif rest:
            die("unexpected %r after ] in host token %r" % (rest, spec))
    elif spec.count(":") > 1:
        addr = spec                       # a bare v6 literal carries no port
    elif ":" in spec:
        addr, _, p = spec.rpartition(":")
        if p.isdigit():
            port = _port_of(p, spec)
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


# canonical copy: binnacle/agree.py parse_host_token.
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


# canonical copy: binnacle/agree.py read_host_file.
def read_host_file(path):
    """The host tokens in a server list, or None if PATH is not a file.

    Comments are dropped, which is what lets `reachable`'s output feed
    straight in: the entries it commented out stay out of the fan-out.
    """
    if path == "-":
        text = sys.stdin.read().lstrip("\ufeff")
    else:
        if not os.path.isfile(path):
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
    """Every host named, expanded, in the order given and each named once."""
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

    hosts, seen = [], {}
    for tok in tokens:
        for one in expand_range(tok):
            host = parse_host_token(one)
            # The host name is what keeps one machine's files apart from
            # another's, so two hosts answering to one name would collect
            # into the same directory and the second would overwrite the
            # first -- silently, and only for the files they had in common.
            if host.name in seen:
                die("host %r is named twice (as %r and %r): the name is what "
                    "keeps the collected files apart, so it has to be unique"
                    % (host.name, seen[host.name], one))
            seen[host.name] = one
            hosts.append(host)
    return hosts


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------

# canonical copy: binnacle/logtriage.py parse_when.  Kept to the same three
# spellings on purpose: `--since -30m` means the same thing in both tools.
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
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(datetime.strptime(s, fmt).timetuple())
        except ValueError:
            continue
    die("cannot understand time %r (try HH:MM, an ISO stamp, or -30m)" % s)


# ---------------------------------------------------------------------------
# The far side
# ---------------------------------------------------------------------------
#
# Two shapes of remote command, because a slice of a file and a whole one
# want different transports.
#
#   whole files   `find ... | tar` -- the bytes and nothing else, and tar
#                 frames itself, so a file being written to while it is
#                 read is tar's problem to report rather than a desync.
#
#   head/tail     framed base64.  The length of a slice is not known until
#                 it has been cut, and asking twice (once to measure, once
#                 to read) is a race on exactly the growing log this is
#                 pointed at -- so the frame is delimited by a token drawn
#                 fresh for each run instead, and the payload is base64 so
#                 no byte in it can be mistaken for that token.
#
# Both start by resolving the path the same way, so `dredge /var/log/x` and
# `dredge logs/x` mean what they look like: absolute from /, relative from
# the login directory.

_PREAMBLE = r'''
p=%(path)s
case "$p" in
  /*) base=/ ; rel=${p#/} ;;
  *)  base=$PWD ; rel=$p ;;
esac
cd "$base" 2>/dev/null || { echo "dredge: cannot cd to $base" >&2; exit 3; }
if [ ! -e "$rel" ]; then echo "dredge: no such path: $p" >&2; exit 4; fi
'''


def _find_expr(args, since_epoch):
    """The selection, shared by both transports.

    `-H` follows a symlink named on the command line and nothing else,
    which is the distinction that matters here: `dredge /var/log/current`
    means the file that name points at, while a link *inside* a tree
    being collected is not evidence and is left alone.  Without it a
    symlinked path passes the `[ -e ]` check, matches no `-type f`, and
    the host is reported as having nothing to send.
    """
    bits = ['find -H "$rel" -type f']
    if since_epoch is not None:
        # An epoch second rather than a wall-clock string: the window is
        # decided by the clock here, so a host in another timezone selects
        # the same files. (Its own clock still decides each file's mtime,
        # which is why a skewed box hands back the wrong ones.)
        bits.append("-newermt %s" % shlex.quote("@%d" % int(since_epoch)))
    if args.max_bytes:
        bits.append("-size -%dc" % (int(args.max_bytes) + 1))
    return " ".join(bits)


def _oversize_report(args):
    """Name what was left behind, without carrying it."""
    if not args.max_bytes:
        return ""
    return ('find -H "$rel" -type f -size +%dc '
            '-printf "dredge-skip: %%s %%p\\n" >&2 2>/dev/null\n'
            % int(args.max_bytes))


def remote_tar_command(args, since_epoch):
    return (_PREAMBLE % {"path": shlex.quote(args.path)}
            + _oversize_report(args)
            + '%s -print0 2>/dev/null | tar -h --null -T - -cf - '
              '2>/dev/null\n' % _find_expr(args, since_epoch))


_CMD_SCRIPT = r'''
MARK=%(mark)s
export MARK
command -v bash >/dev/null 2>&1 || {
  echo "dredge: no bash on this host, and --cmd runs a bash command" >&2
  exit 5
}
st=$(mktemp 2>/dev/null) || st=/tmp/dredge-status.$$
CMDB64=%(cmd)s
export CMDB64
printf "%%s FILE\n" "$MARK"
printf "%%s" %(name)s
printf "\n%%s DATA\n" "$MARK"
{ CMD=$(printf "%%s" "$CMDB64" | base64 -d); export CMD
  bash -c "eval \"\$CMD\""
  echo $? > "$st"
} 2>&1 %(cut)s| base64
printf "%%s END\n" "$MARK"
printf "%%s EXIT %%s\n" "$MARK" "$(cat "$st" 2>/dev/null || echo unknown)"
rm -f "$st"
'''


def remote_cmd_command(args, mark):
    """Run one bash command on the far side and frame what it says.

    Three things have to survive the trip, and each is handled where it
    can be: the command itself, its output, and its exit status.

    The command travels base64'd and is decoded into a variable on the
    far side, so nothing in it is ever parsed by a shell on the way --
    not by the local one building this, not by ssh, not by the remote
    login shell.  A command with quotes, newlines, `$(...)`, a stray
    backslash or a lone apostrophe arrives exactly as typed.

    Quoting would be the other way, and it is the one that goes wrong.
    The command sits inside a script that ssh hands to whatever the
    remote *login* shell is, which then runs `bash -c` on it, so quoting
    it means nesting two levels correctly and getting both right for a
    shell nobody here chose -- `shlex.quote` assumes a POSIX one, and a
    csh or a restricted shell on the far side is where that assumption
    is discovered.  Base64 makes the count of levels zero: the payload
    is alphanumeric whatever the command was, so there is nothing left
    for any shell to misread.

    stderr is merged into stdout rather than split out, because the point
    is to get back what you would have seen on the terminal, in the order
    you would have seen it -- a diagnostic that writes its headline to
    stderr and its table to stdout is one answer, not two.  A command
    that wants them apart can say so itself (`... 2>/dev/null`).

    The status cannot come down the same pipe as the output without
    becoming part of it, so it goes to a temp file and comes back in its
    own frame.  It is the one thing a file has no equivalent of, and
    losing it would mean a command that failed and a command that printed
    nothing looked the same.
    """
    cut = ""
    if args.head:
        cut = "| head -n %d " % args.head
    elif args.tail:
        cut = "| tail -n %d " % args.tail
    return _CMD_SCRIPT % {
        "mark": shlex.quote(mark),
        "cmd": shlex.quote(base64.b64encode(
            args.cmd.encode("utf-8")).decode("ascii")),
        "name": shlex.quote(base64.b64encode(
            args.tag.encode("utf-8")).decode("ascii")),
        "cut": cut,
    }


def remote_slice_command(args, since_epoch, mark):
    """head/tail on the far side, framed so any byte can come back.

    The token travels in the environment rather than inside the inner
    script: the script is single-quoted for `sh -c`, and a token quoted
    into it would close that quote and take the rest of the script with
    it.  Nothing inside the quotes needs a quote of its own as a result.

    The path is base64'd along with the content, because a filename may
    hold a newline and a frame a filename could break is a frame the fleet
    gets to choose the shape of.
    """
    cut = ("head -n %d" % args.head) if args.head else ("tail -n %d"
                                                        % args.tail)
    return (_PREAMBLE % {"path": shlex.quote(args.path)}
            + _oversize_report(args)
            + "MARK=%s\nexport MARK\n" % shlex.quote(mark)
            + "%s -print0 2>/dev/null | xargs -0 -r -n1 sh -c '\n"
              'printf "%%s FILE\\n" "$MARK"\n'
              'printf "%%s" "$1" | base64 | tr -d "\\n"\n'
              'printf "\\n%%s DATA\\n" "$MARK"\n'
              "%s -- \"$1\" | base64\n"
              'printf "%%s END\\n" "$MARK"\n'
              "' sh\n" % (_find_expr(args, since_epoch), cut))


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def _clean_relpath(name):
    """A tar member's name, reduced to something safe to build a path from.

    Nothing a remote host says is used as a path.  Every component that
    could climb out -- a leading slash, a `..`, a drive letter, an empty
    piece -- is dropped here, so the worst a compromised or simply broken
    host can do is land its file somewhere dull inside the collection
    directory.
    """
    parts = []
    for piece in name.replace("\\", "/").split("/"):
        if not piece or piece == "." or piece == "..":
            continue
        if ":" in piece:                  # c:\ and friends
            piece = piece.replace(":", "_")
        parts.append(piece)
    return "/".join(parts)


TAG_RE = re.compile(r"[^A-Za-z0-9._+-]+")


def _clean_tag(tag):
    """A tag, reduced to something that is safe as part of a filename.

    The tag is the one part of the name the caller writes freely, so it
    is the one part that could carry a slash or a separator and quietly
    mean something else.
    """
    cleaned = TAG_RE.sub("-", (tag or "").strip()).strip("-")
    return cleaned or "tag"


def default_tag(cmd):
    """A tag for a command nobody named: the command's own first word.

    `--cmd 'ss -s'` lands as `web01~ss`, which is what somebody reading
    the directory later would have called it anyway.
    """
    first = (cmd or "").strip().split()
    return _clean_tag(first[0].rsplit("/", 1)[-1]) if first else "cmd"


def local_path(args, host, relpath):
    """Where a file from HOST lands: one directory, the name says which.

    With `--tag` the tag leads: `audit~web01~etc~hosts`.  That is the
    order that makes a shared directory readable -- several runs land
    side by side and `ls` groups them by run, `rm audit~*` clears one of
    them, and a file tells you which collection it belongs to without
    anyone having to remember.

    Everything from a run goes in one directory and is told apart by its
    *name* rather than by where it sits.  Rebuilding each host's
    directory tree locally reads well and greps badly: the interesting
    command afterwards is `grep -l something *` or `logtriage
    dredge-*/web*syslog`, and both of those want one directory of
    distinctly-named files rather than forty identical paths under forty
    host directories.
    """
    rel = _clean_relpath(relpath)
    if not rel:
        rel = "unnamed"
    if args.cmd:
        # A command has no path; the tag is the whole of its name.
        parts = [_clean_tag(args.tag), host.name]
    else:
        parts = [host.name, rel.replace("/", FLAT_SEP)]
        if args.tag:
            parts.insert(0, _clean_tag(args.tag))
    return os.path.join(args.dir, FLAT_SEP.join(parts))


# ---------------------------------------------------------------------------
# Writing what came back
# ---------------------------------------------------------------------------

class LocalWriteError(Exception):
    """A file came back and could not be landed here.

    Raised rather than passed to die(), because this happens inside a
    worker thread: a SystemExit there does not end the process where it
    was raised, it travels up through the pool and ends the whole run
    with a usage exit code, throwing away every other host's collection
    and printing no report at all.  One unwritable path is one host's
    failure and belongs in that host's row.
    """


def _marker(host):
    """The seam between what was here and what just arrived."""
    return ("\n===== %s %s %s =====\n"
            % (PROG, host.name,
               time.strftime("%Y-%m-%dT%H:%M:%S"))).encode("utf-8")


def write_file(args, host, path, data, taken, collisions=None, remote=""):
    """Land DATA at PATH, replacing, appending or prepending.

    Written whole and renamed into place, so a reader -- the next tool in
    the pipeline, usually -- never sees half a file, and a run interrupted
    halfway leaves what was already there intact.

    Two files from one host can want the same local name, where `a~b/c`
    and `a/b/c` both fold to `a~b~c`.  Distinct remote paths cannot
    collide any other way, and the second silently replacing the first
    is the one outcome worth refusing outright: it looks exactly like a
    successful collection.
    """
    if any(p == path for _h, p, _n in taken):
        if collisions is not None:
            collisions.append(remote or path)
        return
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d)
        except OSError as exc:
            if not os.path.isdir(d):
                raise LocalWriteError("cannot make %s: %s" % (d, exc))
    old = b""
    if (args.append or args.prepend) and os.path.exists(path):
        try:
            with io.open(path, "rb") as fh:
                old = fh.read()
        except OSError as exc:
            raise LocalWriteError("cannot read %s to add to it: %s"
                                  % (path, exc))
    if old:
        seam = _marker(host) if args.mark else b""
        if args.append:
            data = old + seam + data
        else:
            data = data + seam + old
    elif args.mark:
        data = _marker(host).lstrip(b"\n") + data

    tmp = "%s.dredge.%d" % (path, os.getpid())
    try:
        with io.open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise LocalWriteError("cannot write %s: %s" % (path, exc))
    taken.append((host, path, len(data)))


# ---------------------------------------------------------------------------
# One host
# ---------------------------------------------------------------------------

class Result(object):
    __slots__ = ("host", "outcome", "detail", "files", "bytes", "skipped",
                 "collisions", "truncated", "exit_status", "duration")

    def __init__(self, host):
        self.host = host
        self.outcome = OK
        self.detail = ""
        self.files = []          # (local_path, bytes)
        self.bytes = 0
        self.skipped = []        # (size, remote_path) left behind
        self.collisions = []     # remote paths that folded onto one name
        # We stopped reading at --max-files.  The far side then died of a
        # closed pipe, and its exit status describes that decision rather
        # than a failure -- so it must not be read as one.
        self.truncated = False
        # What --cmd's command exited with. None for a file collection:
        # a file has no status, which is the whole reason this is carried
        # separately rather than folded into the outcome.
        self.exit_status = None
        self.duration = 0.0


def ssh_argv(args, host, command):
    argv = (args.ssh or "ssh").split() + SSH_OPTS
    if host.port:
        argv += ["-p", str(host.port)]
    target = "%s@%s" % (args.user, host.addr) if args.user else host.addr
    argv += [target, command]
    return argv


def _drain(stream, into):
    """Read a child's stderr in its own thread.

    The payload comes down stdout and can be large, so stderr has to be
    read as it arrives: left in the pipe it fills, the far side blocks
    writing to it, and both ends wait for the other for ever.
    """
    try:
        into.append(stream.read())
    except (OSError, ValueError):
        pass


SKIP_RE = re.compile(r"^dredge-skip:\s+(\d+)\s+(.*)$")


def _parse_stderr(text):
    """(skipped, other) -- the oversize list, and anything else it said."""
    skipped, other = [], []
    for line in (text or "").splitlines():
        m = SKIP_RE.match(line.strip())
        if m:
            skipped.append((int(m.group(1)), m.group(2)))
        elif line.strip():
            other.append(line.strip())
    return skipped, other


def _run(args, host, command, consume):
    """ssh once, hand the stdout stream to CONSUME, and classify the end."""
    r = Result(host)
    t0 = time.monotonic()
    try:
        # Its own session, so the watchdog below can end the whole of it.
        # ssh on its own would be enough locally, but a remote command
        # that leaves something behind holding the connection is exactly
        # what a stalled transfer looks like, and killing one process out
        # of a group leaves the pipe open and the read blocked.
        # BatchMode=yes means nothing here wants a terminal.
        p = subprocess.Popen(ssh_argv(args, host, command),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        r.outcome, r.detail = FAILED, str(exc)
        return r
    errbuf = []
    rc = None
    t = threading.Thread(target=_drain, args=(p.stderr, errbuf))
    t.daemon = True
    t.start()

    # --timeout has to bound the transfer, not just the wait after it.
    # Waiting on the child only starts once consume() has read the stream
    # to its end, so a far side that stalls mid-stream -- the interesting
    # failure, and the one a saturated link produces -- was never bounded
    # by anything but ssh's own ConnectTimeout, which is long past by
    # then. A watchdog closes the transfer instead of the run hanging on
    # one host for ever.
    timed_out = []

    def _end_it():
        """SIGKILL the whole session, falling back to the one process."""
        try:
            os.killpg(p.pid, signal.SIGKILL)
            return
        except (OSError, AttributeError):
            pass
        try:
            p.kill()
        except OSError:
            pass

    def _watchdog():
        timed_out.append(True)
        _end_it()

    alarm = threading.Timer(args.timeout, _watchdog)
    alarm.daemon = True
    alarm.start()

    consume_err = None
    try:
        consume(p.stdout, r)
    except Exception as exc:                       # noqa: BLE001 - reported
        # Held rather than acted on: the far side's exit status says why
        # far better than a parse error does. `no such path` arrives here
        # as an empty stream and as rc 4, and it is rc 4 that should be
        # reported.
        # A local write failure already says what it is; anything else
        # needs its type to be readable at all.
        consume_err = (str(exc) if isinstance(exc, LocalWriteError)
                       else "%s: %s" % (type(exc).__name__, exc))
    finally:
        alarm.cancel()
        if r.truncated:
            # Nothing more is wanted from it, so end it here rather than
            # waiting for the far side to notice the pipe has closed.
            _end_it()
        try:
            p.stdout.close()
        except OSError:
            pass
        try:
            rc = p.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            _end_it()
            rc = p.wait()
            timed_out.append(True)
        t.join(timeout=5)
        # Only if that join actually finished. Closing a stream another
        # thread is still blocked reading takes the same lock it holds,
        # so this waits for a read that is not coming and the whole run
        # hangs on one host -- the timeout on the join being there
        # precisely because the read might not come.
        if not t.is_alive():
            try:
                p.stderr.close()
            except OSError:
                pass
    err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace")
    r.skipped, other = _parse_stderr(err)
    r.duration = time.monotonic() - t0
    if r.outcome != OK:
        return r
    if timed_out:
        r.outcome, r.detail = TIMEOUT, "timed out after %ss" % args.timeout
    elif r.truncated:
        # Stopped on purpose. The detail already says --max-files, and
        # the far side's status is the SIGPIPE we caused.
        pass
    elif rc == 4:
        r.outcome, r.detail = MISSING, "no such path"
    elif consume_err:
        # Before the generic rc check: closing the stream mid-transfer
        # kills the far side too, so its exit status would otherwise
        # report our own error back to us as `exit 141`.
        r.outcome, r.detail = FAILED, consume_err
    elif rc is None:
        r.outcome, r.detail = FAILED, "ssh did not report an exit status"
    elif rc == 255:
        low = " ".join(other).lower()
        r.outcome = UNREACHABLE if (
            "connect" in low or "resolve" in low or "route" in low
            or "refused" in low or "timed out" in low
            or "permission denied" in low or not other) else FAILED
        r.detail = other[0][:120] if other else "ssh failed"
    elif rc != 0:
        r.outcome = FAILED
        r.detail = other[0][:120] if other else "exit %d" % rc
    return r


def collect_tar(args, host):
    """Whole files, unpacked from the tar stream as it arrives."""
    def consume(stream, r):
        taken = []
        # r| is the streaming mode: members are read in order off a pipe,
        # with no seeking back, which is what lets the unpacking start
        # before the far side has finished sending.
        try:
            tar = tarfile.open(fileobj=stream, mode="r|*")
        except tarfile.ReadError:
            # `find` matched nothing, so tar sent nothing. Zero files is an
            # answer -- --since exists to produce it -- not a broken run.
            r.files, r.bytes = [], 0
            return
        try:
            for member in tar:
                if len(taken) >= args.max_files:
                    r.truncated = True
                    r.detail = ("stopped at --max-files %d" % args.max_files)
                    break
                if member.isdir():
                    continue
                if not member.isfile():
                    # A symlink, socket or device node is not evidence and
                    # recreating one here is how a collection directory
                    # grows a link out of itself.
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                write_file(args, host, local_path(args, host, member.name),
                           fh.read(), taken, r.collisions, member.name)
        finally:
            try:
                tar.close()
            except Exception:
                pass
        r.files = [(p, n) for _h, p, n in taken]
        r.bytes = sum(n for _p, n in r.files)
    return _run(args, host, remote_tar_command(args, args.since_epoch),
                consume)


def collect_command(args, host):
    """One command's output, through the same framed stream a slice uses.

    The same transport for the same reason: the length of what a command
    will say is not known until it has said it, so it cannot be tarred,
    and base64 inside a per-run token frame carries any byte it produces.
    """
    return _run(args, host, remote_cmd_command(args, args.mark_token),
                _frame_consumer(args, host))


def collect_slices(args, host):
    """A head or a tail of each file, out of the framed base64 stream."""
    return _run(args, host,
                remote_slice_command(args, args.since_epoch,
                                     args.mark_token),
                _frame_consumer(args, host))


def _frame_consumer(args, host):
    """Read `<MARK> FILE|DATA|END|EXIT` frames off a stream."""
    mark = args.mark_token

    def consume(stream, r):
        taken = []
        state, path, chunks = None, None, []
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line == mark + " FILE":
                state, path, chunks = "path", None, []
            elif line == mark + " DATA":
                state = "data"
            elif line.startswith(mark + " EXIT "):
                raw_st = line[len(mark) + 6:].strip()
                r.exit_status = int(raw_st) if raw_st.isdigit() else raw_st
            elif line == mark + " END":
                if path is not None:
                    try:
                        data = base64.b64decode("".join(chunks))
                    except (binascii.Error, ValueError):
                        r.detail = "undecodable payload for %s" % path
                        data = None
                    if data is not None:
                        write_file(args, host, local_path(args, host, path),
                                   data, taken, r.collisions, path)
                state, path, chunks = None, None, []
                if len(taken) >= args.max_files:
                    r.truncated = True
                    r.detail = "stopped at --max-files %d" % args.max_files
                    break
            elif state == "path":
                try:
                    path = base64.b64decode(line).decode("utf-8", "replace")
                except (binascii.Error, ValueError):
                    path = None
                state = None
            elif state == "data":
                chunks.append(line)
        r.files = [(p, n) for _h, p, n in taken]
        r.bytes = sum(n for _p, n in r.files)
    return consume


def collect_one(args, host):
    if args.cmd:
        return collect_command(args, host)
    if args.head or args.tail:
        return collect_slices(args, host)
    return collect_tar(args, host)


# canonical copy: binnacle/agree.py fan_out.
def fan_out(hosts, fn, jobs, quiet=False):
    """Capped concurrency, results in host-list order.

    Order matters more than it looks: two runs over the same list must
    produce the same report, or you cannot diff yesterday's against
    today's.
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
                progress("%d/%d hosts" % (done[0], total))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(wrapped, hosts))
    if not quiet:
        progress_done()
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render(results, args, elapsed):
    out = []
    good = [r for r in results if r.outcome == OK and r.files]
    empty = [r for r in results if r.outcome == OK and not r.files]
    bad = [r for r in results if r.outcome != OK]
    nfiles = sum(len(r.files) for r in results)
    nbytes = sum(r.bytes for r in results)

    what = args.cmd if args.cmd else args.path
    if args.tag:
        what = "[%s] %s" % (args.tag, what)
    if args.head:
        what += "   [head %d]" % args.head
    elif args.tail:
        what += "   [tail %d]" % args.tail
    if args.since:
        what += "   [since %s]" % args.since
    out.append("%s -- %s" % (PROG, what))
    out.append("        %d file%s from %d of %d host%s, %s in %.1fs -> %s/"
               % (nfiles, "" if nfiles == 1 else "s", len(good), len(results),
                  "" if len(results) == 1 else "s", fmt_bytes(nbytes),
                  elapsed, args.dir))
    out.append("")

    if empty:
        names = " ".join(r.host.name for r in empty[:6])
        more = "" if len(empty) <= 6 else " (+%d)" % (len(empty) - 6)
        out.append("  EMPTY     %d host%s had nothing to send: %s%s"
                   % (len(empty), "" if len(empty) == 1 else "s", names, more))
        if args.since:
            out.append("            nothing under %s changed since %s there"
                       % (args.path, args.since))
        elif args.cmd:
            out.append("            the command printed nothing there")
    for r in bad:
        out.append("  %-9s %s: %s" % (r.outcome.upper(), r.host.name,
                                      r.detail or "?"))
    # A command that failed still has an answer worth keeping -- its
    # error text is the artifact -- so this is a finding rather than a
    # failed host. It is also the one thing a file collection has no
    # equivalent of, and it must not go unsaid.
    angry = [r for r in results if r.exit_status not in (None, 0)]
    if angry:
        out.append("  NONZERO   the command exited non-zero on %d host%s:"
                   % (len(angry), "" if len(angry) == 1 else "s"))
        for r in angry[:6]:
            out.append("            %-12s exit %s" % (r.host.name,
                                                      r.exit_status))
        if len(angry) > 6:
            out.append("            ... and %d more" % (len(angry) - 6))
        out.append("            What it said is collected either way.")
    cut = [r for r in results if r.truncated]
    if cut:
        names = " ".join(r.host.name for r in cut[:6])
        more = "" if len(cut) <= 6 else " (+%d)" % (len(cut) - 6)
        out.append("  TRUNCATED %d host%s hit --max-files %d and there was "
                   "more: %s%s"
                   % (len(cut), "" if len(cut) == 1 else "s", args.max_files,
                      names, more))
        out.append("            raise it, or narrow what you asked for with "
                   "--since.")
    clashed = [(r.host, c) for r in results for c in r.collisions]
    if clashed:
        out.append("  COLLISION %d file%s folded onto a name already taken "
                   "and %s left behind:"
                   % (len(clashed), "" if len(clashed) == 1 else "s",
                      "was" if len(clashed) == 1 else "were"))
        for host, remote in clashed[:5]:
            out.append("            %-12s %s" % (host.name, remote))
        if len(clashed) > 5:
            out.append("            ... and %d more" % (len(clashed) - 5))
        out.append("            A path already containing %s folds onto the "
                   "same name as one with a /" % FLAT_SEP)
        out.append("            there.  Collect them in separate runs, or "
                   "name them apart on the far side.")
    skipped = [(r.host, s) for r in results for s in r.skipped]
    if skipped:
        out.append("  OVERSIZE  %d file%s larger than --max-bytes (%s), left "
                   "where they are:"
                   % (len(skipped), "" if len(skipped) == 1 else "s",
                      fmt_bytes(args.max_bytes)))
        for host, (size, path) in skipped[:5]:
            out.append("            %-12s %8s  %s"
                       % (host.name, fmt_bytes(size), path))
        if len(skipped) > 5:
            out.append("            ... and %d more" % (len(skipped) - 5))
        out.append("            --tail N brings back the end of one without "
                   "the rest of it.")
    if empty or bad or skipped or clashed or cut or angry:
        out.append("")

    if good and not args.quiet:
        shown = [p for r in good for p, _n in r.files][:6]
        for p in shown:
            out.append("  %s" % p)
        if nfiles > len(shown):
            out.append("  ... and %d more" % (nfiles - len(shown)))
        out.append("")
    return "\n".join(out) + "\n"


CSV_FIELDS = ["host", "source", "local_path", "bytes", "outcome",
              "exit_status"]


def write_csv(results, args, path):
    fh = sys.stdout if path in (None, "-") else io.open(
        path, "w", newline="", encoding="utf-8")
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in results:
            source = args.cmd if args.cmd else args.path
            st = "" if r.exit_status is None else r.exit_status
            if not r.files:
                w.writerow({"host": r.host.name, "source": source,
                            "local_path": "", "bytes": 0,
                            "outcome": r.outcome, "exit_status": st})
                continue
            for p, n in r.files:
                w.writerow({"host": r.host.name, "source": source,
                            "local_path": p, "bytes": n,
                            "outcome": r.outcome, "exit_status": st})
    finally:
        if fh is not sys.stdout:
            fh.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
    p.add_argument("path", metavar="PATH", nargs="?",
                   help="the file or directory to bring back from each host")
    p.add_argument("-c", "--cmd", metavar="CMD",
                   help="a bash command to run on each host; its output "
                        "comes back as the artifact, in place of a file")
    p.add_argument("-t", "--tag", metavar="NAME",
                   help="label this run's artifacts, so several runs can "
                        "share a directory and still be told apart")
    p.add_argument("-H", "--host", dest="H", action="append", metavar="TOKEN")
    p.add_argument("--hosts", action="append", metavar="FILE")
    p.add_argument("-d", "--dir", default=_env("DIR"))
    p.add_argument("--head", type=int, metavar="N")
    p.add_argument("--tail", type=int, metavar="N")
    p.add_argument("--since", metavar="T")
    p.add_argument("--append", action="store_true")
    p.add_argument("--prepend", action="store_true")
    p.add_argument("--mark", action="store_true")
    p.add_argument("--max-bytes", type=int,
                   default=int(_env_num("MAX_BYTES", DEFAULT_MAX_BYTES, int)))
    p.add_argument("--max-files", type=int,
                   default=int(_env_num("MAX_FILES", DEFAULT_MAX_FILES, int)))
    p.add_argument("-j", "--jobs", type=int,
                   default=int(_env_num("JOBS", DEFAULT_JOBS, int)))
    p.add_argument("--timeout", type=float,
                   default=_env_num("TIMEOUT", DEFAULT_TIMEOUT))
    p.add_argument("--user", default=_env("USER"))
    p.add_argument("--ssh", default=_env("SSH", "ssh"))
    p.add_argument("--csv", nargs="?", const="-", metavar="PATH")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


# canonical copy: binnacle/logtriage.py glue_relative_times.
# A relative time starts with '-', which argparse reads as the next option,
# so `--since -30m` was refused -- by a tool whose own error message
# recommends exactly that spelling. Gluing the pair back together here is
# the smallest fix that keeps the documented form working, and it only
# touches a value that looks like a relative time, so a genuine following
# option is left alone.
REL_TIME_RE = re.compile(r"^-\d+[smhd]$")


def glue_relative_times(argv, options):
    """`--since -30m` -> `--since=-30m`, for the options that take a time."""
    out, i = [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in options and i + 1 < len(argv) \
                and REL_TIME_RE.match(argv[i + 1]):
            out.append("%s=%s" % (arg, argv[i + 1]))
            i += 2
            continue
        out.append(arg)
        i += 1
    return out


def main(argv=None):
    _stdio_safe()
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(
        glue_relative_times(argv, ("--since",)))

    if args.cmd and args.path:
        die("give a PATH or --cmd, not both: %r would be collected and %r "
            "would be run, and only one of them can be the artifact"
            % (args.path, args.cmd))
    if not args.cmd and not args.path:
        die("nothing to collect: give a PATH, or --cmd to run something")
    if args.cmd and not args.cmd.strip():
        die("--cmd is empty")
    if args.cmd and args.since is not None:
        # The file-selecting options have nothing to select with --cmd.
        # --since is the one that can be told apart from its default, so
        # it is the one that can be refused rather than quietly ignored;
        # --max-bytes and --max-files always carry a value and are simply
        # not consulted.  What bounds a runaway command is --timeout.
        die("--since selects among files by age, and --cmd has no files to "
            "select from -- it has one command and one answer")
    args.tag = args.tag or (default_tag(args.cmd) if args.cmd else None)
    if args.head and args.tail:
        die("--head and --tail are opposite ends of the same file: pick one")
    if args.append and args.prepend:
        die("--append and --prepend are opposite ends of the same file: "
            "pick one")
    for name in ("head", "tail"):
        v = getattr(args, name)
        if v is not None and v <= 0:
            die("--%s wants a positive number of lines, got %d" % (name, v))
    if args.jobs < 1:
        die("--jobs wants at least 1, got %d" % args.jobs)
    if args.max_files < 1:
        die("--max-files wants at least 1, got %d" % args.max_files)
    if args.max_bytes < 0:
        die("--max-bytes cannot be negative, got %d (0 means no ceiling)"
            % args.max_bytes)
    if args.timeout <= 0:
        die("--timeout wants a positive number of seconds, got %s"
            % args.timeout)
    if args.mark and not (args.append or args.prepend):
        die("--mark writes a line where old meets new, so it needs "
            "--append or --prepend")
    # A run of its own unless the caller named one, so today's collection
    # never lands on top of yesterday's.
    if not args.dir:
        args.dir = default_dir()
    args.since_epoch = parse_when(args.since) if args.since else None
    # Drawn fresh per run and never from the payload: a frame the far side
    # could guess is a frame the far side could forge.
    args.mark_token = "===dredge-%016x" % random.getrandbits(64)

    hosts = collect_hosts(args)

    if args.dry_run:
        if args.cmd:
            cmd = remote_cmd_command(args, args.mark_token)
        elif args.head or args.tail:
            cmd = remote_slice_command(args, args.since_epoch,
                                       args.mark_token)
        else:
            cmd = remote_tar_command(args, args.since_epoch)
        sys.stdout.write("# %d host(s): %s\n"
                         % (len(hosts),
                            " ".join(h.name for h in hosts[:8])
                            + (" ..." if len(hosts) > 8 else "")))
        sys.stdout.write("# on each, over ssh:\n%s" % cmd)
        return 0

    t0 = time.monotonic()
    results = fan_out(hosts, lambda h: collect_one(args, h), args.jobs,
                      args.quiet)
    elapsed = time.monotonic() - t0

    if args.csv is not None:
        write_csv(results, args, args.csv)
    if not args.quiet or any(r.outcome != OK for r in results):
        sys.stdout.write(render(results, args, elapsed))

    nfiles = sum(len(r.files) for r in results)
    # A ceiling that was hit, or a name that was refused, means what came
    # back is not what was asked for -- which is the definition of
    # something worth seeing, and worth stopping a script over.
    if any(r.outcome != OK or r.collisions or r.truncated
           or r.exit_status not in (None, 0)
           for r in results) or not nfiles:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        progress_done()
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
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
#
# This one leans on `agree` more than most: the host list, the ranges, the
# ssh argv and the fan-out are the same problem agree already solved, and
# solving it differently here would mean `dredge --hosts hosts.txt` and
# `agree --hosts hosts.txt` disagreeing about what that file says.
