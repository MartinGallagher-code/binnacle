#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""dredge.py -- bring an answer back from every host, named so you can tell them apart.

Usage: dredge /var/log/syslog --servers hosts.txt    one file from every host
       dredge --cmd 'ss -s' --servers hosts.txt      what a command says, instead
       dredge /var/log/syslog --tail 200 -S 'web[01-40]'   only the last 200 lines
       dredge /etc/nginx --servers hosts.txt         a whole directory each
       dredge /var/log/app.log --since -1h           only what changed lately
       dredge --cmd 'uptime' --tag before --servers h  labelled, to keep runs apart
       dredge /var/log/app.log --follow -d out       only what is new since last time
       dredge /var/log/app.log --daemon -d out       ... and again every five minutes
       dredge --cmd probe --tsv m.tsv --servers h     a table of what each one said
       dredge --cmd uptime --relay bastion --servers h  through a jump box

Options:
  -c, --cmd CMD       a bash command to run on each host; what it says comes
                      back as the artifact, in place of a file
  -t, --tag NAME      label this run's artifacts, so several runs can share
                      one directory and still be told apart
      --suffix EXT    put EXT on the end of every file this run creates;
                      a bare word gains a dot: `log` and `.log` both mean
                      `.log`.  One starting with a dash needs the joined
                      spelling, `--suffix=-raw`          (DREDGE_SUFFIX)
  -S, --server TOKEN  servers, repeatable; ranges expand (`web[01-40]`)
      --servers FILE  a server list, one per line -- reachable's output works
  -d, --dir DIR       where collected files land   (default: dredge-<stamp>)
      --head N        only the first N lines of each file
      --tail N        only the last N lines of each file
      --since T       only files modified since T: -30m, 14:20, an ISO stamp
  -f, --follow        only what is new since the last pass: each file is
                      resumed from the byte it stopped at
      --daemon        keep going -- a pass, a wait, another pass, until you
                      stop it            (implies --follow, not with --tsv)
      --every T       how long between passes: 30s, 5m, 2h  (DREDGE_EVERY, 5m)
      --passes N      stop after N passes                  (0: until stopped)
      --state FILE    where a --follow run keeps its marks
                                       (DREDGE_STATE, DIR/dredge-state.json)
      --append        add what came back to the end of what is already here
      --prepend       add it to the beginning instead
      --replace       overwrite what is here (the default, except under
                      --follow, where --append is)
      --mark          write a marker line where old meets new
      --max-bytes N   skip a file larger than this  (default 100M, 0 none)
      --max-files N   stop after this many files per host   (default 500)
  -j, --jobs N        hosts contacted at once               (DREDGE_JOBS, 20)
      --timeout S     ssh timeout per host                  (DREDGE_TIMEOUT)
      --user NAME     ssh user                              (DREDGE_USER)
      --ssh CMD       ssh command                           (DREDGE_SSH)
      --csv [PATH]    host,source,local_path,bytes,outcome,exit_status,
                      pass,unchanged  (appended to under --daemon)
      --tsv [PATH]    a table of the variables --cmd printed: date, host,
                      then a column each, one row per host per pass,
                      appended across runs                 (DREDGE_TSV)
      --parse HOW     the shape they arrive in: kv (name=value, the
                      default), json, row (a header line then a values
                      line), values (bare, named by --columns)
                                                          (DREDGE_PARSE)
      --columns A,B,C name the variable columns in order; --parse values
                      needs it, elsewhere it pins the header
                                                        (DREDGE_COLUMNS)
      --relay HOST    a jump box: send this file to HOST, run the whole
                      collection from there, bring it back (DREDGE_RELAY)
      --relay-dir DIR where the copy and the collection live there while
                      the run lasts               (DREDGE_RELAY_DIR)
      --relay-python P  the python to run it with over there
                                               (DREDGE_RELAY_PYTHON)
      --keep-relay    leave the spool on the jump box, to look at
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

  A command that printed nothing at all is the other way round: there is
  no artifact in silence, so no file is written for that host and it is
  reported as empty instead.  `--cmd ls` across a fleet whose login
  directories hold nothing visible collects nothing, and one line saying
  so is worth more than forty zero-byte files that look like a broken
  run.  The host still has a row in `--csv`, carrying its exit status.

A jump box
  The fleet is behind a bastion: this box can reach B, and only B can
  reach C-Z.  `--relay B` sends *this file* to B, runs the whole
  collection from there, and brings it back:

      A  --ssh-->  B  --ssh--> C, D, E ... Z
         <--tar--     <--------

  The tunnelling answer is `ssh -J`, and it works.  This is the other
  answer, and the one that depends on no configuration at all: B needs a
  Python and a shell and nothing else -- no dredge installed, no agent,
  no package, no cron entry, no line of config -- because the copy that
  runs there is the copy that was running here, sent over the same
  connection that carries the answer back.  It is removed when the run
  ends.

  One transfer crosses the A-B link instead of forty, which is the
  reason to prefer this over a tunnel when that link is the slow one.
  The fan-out, and the connections it opens, happen on B.

  The copy and the server list travel on *stdin*, as a tar: this file is
  three thousand lines, and base64 of it in an argv is past ARG_MAX
  before ssh ever sees it.  The list goes over already expanded, so B
  collects from exactly the fleet that was asked for here rather than
  re-expanding a range against its own idea of the syntax.

  The spool is a named path rather than a `mktemp -d`, so it can be
  removed even when the run that made it was killed: `--timeout` ends
  the session with SIGKILL, and a killed shell runs no trap.
  `--keep-relay` leaves it, for when the question is what went wrong
  over there.

  The cost is worth saying plainly: the collection exists on B, in the
  clear, for as long as the run lasts.  On a bastion somebody else owns
  that is a disclosure, and `ssh -J` through `--ssh` is the shape that
  does not make it.

  `--follow` is refused through a relay.  Its marks would live in the
  spool, which is removed when the run ends, so every pass would be a
  first sight and would carry the whole collection again.  `--daemon`
  with `--cmd` and `--tsv` repeats the command rather than resuming a
  file, and that does work: the timer stays on this side, one round trip
  per pass.

A table of what the fleet said
  `--csv` answers "did the collection work".  `--tsv` answers the other
  question, the one you pointed this at the fleet to ask -- what were
  the numbers:

      date                   host    load1   procs
      2026-09-18T09:14:02    web01   0.41    212
      2026-09-18T09:14:02    web02   1.93    318

  One row per host per pass -- the time the pass ran, the host, then a
  column per variable the command printed -- appended to one file across
  runs.  That is the shape a week of `--daemon` passes has to have for a
  spreadsheet, a plot or an awk one-liner to read it as a time series
  rather than as forty files.

  The date is the clock *here*, to the second, sortable as text.  Every
  row in a pass carries the same stamp however far apart the hosts' own
  clocks are; `skew` is the instrument for whose clock is wrong, and this
  column deliberately does not pretend to answer it.

  `--parse` says what shape the variables arrive in: `kv` (`name=value`
  per line, and the default because it is the shape that describes
  itself), `json`, `row` (a header line then a values line), or `values`
  (bare, in a fixed order, named by `--columns` -- which is required
  there, because nothing in `3 41 0.7` says which is which).

  The header is written once, when the file is created, and every row
  after it is counted from that header: rebuilding it each pass would
  renumber every column the first time a host answered differently, and
  the rows behind it would quietly start meaning something else.  A name
  that appears later therefore has nowhere to go, and is reported rather
  than dropped.  `--columns` pins the header up front, which is how you
  leave room for a variable nothing is printing yet.

  A variable a host does not have is an empty cell.  A host whose answer
  will not parse gets no row and a finding saying so -- a host quietly
  missing from a table reads as a machine that was fine -- and its answer
  is collected whole either way.  A host that printed nothing gets no row
  for the same reason: an empty line under a timestamp claims the fleet
  reported zero, which is a different and much worse claim than saying
  nothing.

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

  `--suffix EXT` goes on the end of every name a run creates:

      dredge --cmd 'ss -s' --suffix .txt        ss~web01.txt
      dredge /var/log/syslog --suffix .log      web01~var~log~syslog.log

  That is how a collection gets an extension the rest of your tooling
  recognises.  A `--cmd` artifact has no path, so it has no extension at
  all, and an editor opening `ss~web01` is left guessing where
  `ss~web01.txt` is not.  A bare word gains a dot -- `--suffix log` and
  `--suffix .log` both mean `.log` -- and a suffix that already starts
  with `.`, `_`, `-`, `+` or `~` is appended as typed.  A leading dash
  needs the joined spelling `--suffix=-raw`, because a separate `-raw`
  is something argparse has to read as an option.  A suffix with no
  letter or digit in it at all is refused rather than put on the end of
  every name in the run.

  It is part of the name, so changing it between two `--follow` passes
  makes the local copy the last pass wrote unfindable, and that file is
  collected again from the start under the new name.  That is reported
  as a RESYNC rather than being silent, but it is worth knowing before
  changing a suffix mid-follow.

  `-d DIR` names the directory yourself.  Without it every run gets one
  of its own, stamped with the time: collecting the same path twice an
  hour apart is the normal way to use this, and the second run quietly
  replacing the first is not a result anybody wants to find later.

  Host names are what make these unique, so a list naming one host twice
  is refused rather than quietly collecting it twice into the same place.

Collecting the same thing again
  `--append` adds what came back to the end of the local file instead of
  replacing it, and `--prepend` adds it to the beginning.  `--replace`
  is the third and the default -- what came back is what is here now.
  `--mark` writes a line saying which host and when, at the seam.

  Nothing is de-duplicated: appending a whole file twice gives you it
  twice.  The pairing that makes sense is `--tail`/`--since` with
  `--append`, where each run brings back a slice that the last one did not
  have.  Overlap is still possible -- two runs an hour apart with
  `--tail 200` will repeat whatever both ends saw.

  `--follow` is the pairing without the overlap, and is the next section.

A remote tail
  `--follow` brings back only what is new.  Each file is resumed from the
  byte the last pass stopped at, so the second pass over a log that has
  grown by forty lines carries forty lines -- not the file, and not a
  `--tail 200` window that repeats whatever the last pass already had.

  It works the same way whether you point it at a file, a directory or a
  command:

      dredge /var/log/app.log --follow -d out --servers hosts.txt
      dredge /var/log/nginx   --follow -d out --servers hosts.txt
      dredge --cmd 'dmesg -T' --follow -d out --servers hosts.txt

  A directory brings back the files that grew, and a file that appeared
  since the last pass comes back whole -- that is what new means.  A
  command has no byte offset to resume from, so its output is compared
  with what came back last time: identical output is nothing new, output
  that starts with the last one carries only the part that was added to
  it, and output that changed from the first byte comes back whole.

  What arrives then lands as `--append`, `--prepend` or `--replace` says,
  and under `--follow` the default is `--append` -- the local file is the
  log, growing here as it grows there.  `--replace` under `--follow` is
  the other useful shape: the local file holds only what the last pass
  brought back.

  `--tail N` decides where a *first* sight starts: the last N lines,
  exactly as `tail -n N -f` does.  Without it a file that has not been
  seen before comes back whole.  `--head` cannot mean anything here --
  the first N lines of a file never change -- and is refused.

  Where the marks live
    A pass has to know where the last one stopped, so `--follow` keeps a
    small JSON file -- one offset per host per file -- in the collection
    directory, and needs to be told which directory that is:

        dredge /var/log/app.log --follow -d out      # out/dredge-state.json

    That is the one piece of state this package keeps on its own, and it
    is named by you: no `-d` means a new stamped directory each run and
    nothing to resume from, so `--follow` asks for one rather than
    quietly starting over.  `--state FILE` puts it somewhere else.
    Delete it and the next pass starts the follow again from scratch.
    Several collections can share one directory and one state file: each
    is kept apart by its tag and by what it collects.

  When the file is not the file it was
    A log that was rotated is not a log that was truncated to nothing,
    and neither is a log that grew.  A file whose inode changed, or whose
    size went *backwards*, is reported as rotated and comes back from
    byte zero, because resuming at the old offset would hand you the
    middle of the new file.

    A rotation is a seam, never a stop: the new file is followed from
    there exactly as the old one was, and the next pass carries only what
    was added to it.  The warning is there to explain the seam in the
    local copy, not to say that something was abandoned.

    What that cannot see is a file replaced in place, keeping its inode
    and ending up longer than the old one -- the same blind spot `tail
    -f` has, and the reason `logrotate`'s `copytruncate` is visible here
    (the size goes backwards) while an in-place rewrite is not.

  `--max-bytes` means the new part under `--follow`, not the whole file,
  and it bounds a pass rather than ending one.  When more than the
  ceiling was added since the last pass -- a busy log, or a rotation,
  where the whole new file is the new part -- the newest `--max-bytes`
  come back and the follow resumes from the end of the file.

  What did not fit is a hole in the local copy that will not fill, so it
  is reported as a GAP naming the bytes and the file, and it is a column
  in `--csv`.  It is deliberately not a refusal: refusing would leave
  the mark where it was, the next pass would have even more to carry,
  and that artifact would never be collected again -- losing the whole
  of the rest of the log to protect the part of it that did not fit.

  A gap does not change the exit status.  A log busy enough to outrun
  its ceiling does it on most passes, and a daemon whose every pass
  reported failure for working exactly as designed is a daemon whose
  exit status stops being read.  `gap_bytes` in `--csv` is what a script
  watches instead, and it is there for that reason.

Daemon mode
  `--daemon` does that on a timer: a pass, a wait, another pass, until
  something stops it.

      dredge /var/log/app.log --daemon -d out --servers hosts.txt
      dredge --cmd 'systemctl --failed' --daemon --every 30s -d out -S 'web[01-40]'

  The wait is `--every`, which takes `30s`, `5m`, `2h` or a plain number
  of seconds, and defaults to five minutes.  It is the gap between the
  end of one pass and the start of the next, so a pass that runs long
  does not stack up behind itself -- one that outlasts the interval is
  reported and the next begins immediately.

  `--daemon` implies `--follow`, because a timer that re-fetched every
  file in full every five minutes would be a denial of service against
  the fleet it is watching.

  Each pass prints one line, and anything worth reading -- a host that
  failed, a file that rotated, a command that exited non-zero -- prints
  its findings under it.  `--passes N` stops after N of them; without it,
  it runs until SIGINT or SIGTERM, which are answered by finishing the
  pass in hand and saying what the run did.

  It does not fork, detach, write a pidfile or leave anything behind but
  the files it collected and its state file.  `&`, `tmux`, or a systemd
  unit with `Restart=on-failure` is how it becomes a background service,
  which is the shape where `--every` in a unit file and `DREDGE_EVERY` in
  the environment mean the same thing.

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

  Under `--follow` a pass that brought nothing back is a 0: nothing new
  is the answer a tail spends most of its time giving, and a script that
  polls one would otherwise read a quiet fleet as a broken run.  A GAP
  is a 0 as well, for the reason under it above -- `gap_bytes` in
  `--csv` is the machine-readable half of that finding.  A `--daemon`
  stopped by a signal exits 0 -- it was asked to stop -- and one that
  ran out its `--passes` exits 1 if any pass in it had a failure.
"""

import argparse
import base64
import binascii
import csv
import io
import json
import os
import random
import re
import select
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

VERSION = "0.8.0"
PROG = os.path.basename(sys.argv[0]) or "dredge.py"
# What this tool is, as opposed to what it was invoked as: the state file
# is stamped with it, so a copy running under another name still
# recognises marks its sibling wrote.
PROG_NAME = "dredge"

DEFAULT_JOBS = 20
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 500
# Long enough that a fleet is not being asked forty questions a minute,
# short enough that a log tail is still a tail.  It is the gap between
# passes rather than a period, so a slow pass cannot stack up behind
# itself.
DEFAULT_INTERVAL = 300.0

# The one file this package keeps on its own, and only when asked to:
# --follow has to know where the last pass stopped, and re-deriving that
# by asking the fleet is the transfer it exists to avoid.  It lives in
# the collection directory, which is named by you -- see --follow's
# refusal to run without -d.
STATE_NAME = "dredge-state.json"
STATE_VERSION = 1
# A mark nothing has answered to in a month is a file that was rotated
# away or a host that left the list; keeping it for ever would grow the
# state file for the life of the daemon.  Only ever dropped for a host
# that answered in this pass, so an unreachable machine does not lose
# its place by being unreachable.
STATE_TTL = 30 * 86400

# canonical copy: binnacle/agree.py SSH_OPTS.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]

OK, MISSING, FAILED, TIMEOUT, UNREACHABLE = (
    "ok", "missing", "failed", "timeout", "unreachable")

# What the far side decided about one artifact under --follow, and what
# the local side does with it.
#
#   NEW       never seen before: all of it, from byte zero
#   TAILED    never seen before, and --tail N said where to start
#   MORE      seen before and longer: the bytes past where we stopped
#   ROTATED   seen before and not the same file: all of it again
#   SAME      seen before and unchanged: nothing at all, and nothing is
#             written here either -- an untouched local file is how a
#             follow says "no news"
NEW, TAILED, MORE, ROTATED, SAME = "new", "tail", "more", "rotated", "same"
FOLLOW_MODES = (NEW, TAILED, MORE, ROTATED, SAME)

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
    for spec in (args.servers or []):
        from_file = read_host_file(spec)
        if from_file is not None:
            tokens.extend(from_file)
        else:
            tokens.extend(split_commas(spec))
    for spec in (args.server or []):
        tokens.extend(split_commas(spec))
    if not tokens:
        for default in ("hosts.txt", "servers.txt"):
            got = read_host_file(default)
            if got:
                tokens.extend(got)
                break
    if not tokens:
        die("no hosts given (use --servers FILE, --servers 'node[01-09]' or "
            "-S a,b,c; hosts.txt and servers.txt are used if present)")

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
# Where the last pass stopped
# ---------------------------------------------------------------------------

class Marks(object):
    """One offset per host per artifact, so the next pass asks for the rest.

    This is the only file any instrument here writes without being told to
    write a file, and it exists because the alternative is worse: the
    offsets could be re-derived from the size of what is already collected
    here, but only when the local copy is byte-for-byte what came back --
    which `--prepend`, `--mark`, `--replace` and a half-finished run each
    make false.  A mark that is wrong is a gap in a log or a repeated
    page, and neither announces itself.

    So it is kept, named, and in the directory you named.  Several
    collections can share one file: each stream is keyed by its tag and by
    what it collects, so `--tag audit /etc/hosts` and `--cmd 'ss -s'`
    following into one directory do not read each other's marks.

    Written by the worker threads as their hosts answer, so every read and
    write of the table goes through one lock.
    """

    __slots__ = ("path", "streams", "_lock")

    def __init__(self, path, streams=None):
        self.path = path
        self.streams = streams or {}
        self._lock = threading.Lock()

    @staticmethod
    def stream_key(args):
        """What this run is following, as one string.

        The tag and the source together: two runs collecting different
        things into one directory are two streams, and a run that changes
        what it collects is a different stream rather than a resume of
        something else's offsets.
        """
        what = ("cmd " + args.cmd) if args.cmd else ("path " + args.path)
        return "%s\t%s" % (_clean_tag(args.tag) if args.tag else "-", what)

    @classmethod
    def load(cls, path):
        """The marks that are there, or an empty set if none are.

        A file that exists and cannot be read as marks is refused rather
        than started over: starting over means every host sending every
        file again, which on the fleet this is pointed at is the event
        `--follow` was written to avoid.
        """
        if not os.path.exists(path):
            return cls(path)
        try:
            with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            die("cannot read the follow marks in %s: %s\n"
                "       delete it to start the follow again from scratch"
                % (path, exc))
        if not isinstance(doc, dict) or doc.get("tool") != PROG_NAME:
            die("%s is not a dredge state file -- name another with --state, "
                "or move it out of the way" % path)
        if doc.get("state_version") != STATE_VERSION:
            die("the follow marks in %s were written by another version of "
                "this format (%s, this is %d)\n"
                "       delete it to start the follow again from scratch"
                % (path, doc.get("state_version"), STATE_VERSION))
        return cls(path, cls._clean(doc.get("streams")))

    @staticmethod
    def _clean(streams):
        """The marks that are the shape marks are, and nothing else.

        This file is editable by hand -- that is half the point of it
        being JSON in a directory you named -- so every level of it is
        checked once here rather than being trusted at four call sites
        inside worker threads, where a TypeError comes back as a host
        that mysteriously failed.
        """
        out = {}
        if not isinstance(streams, dict):
            return out
        for key, stream in streams.items():
            if not isinstance(stream, dict):
                continue
            hosts = stream.get("hosts")
            if not isinstance(hosts, dict):
                continue
            kept = {}
            for name, items in hosts.items():
                if not isinstance(items, dict):
                    continue
                marks = {}
                for item, mark in items.items():
                    if not isinstance(mark, dict):
                        continue
                    try:
                        offset = int(mark.get("offset"))
                    except (TypeError, ValueError):
                        continue
                    if offset < 0:
                        continue
                    seen = mark.get("seen")
                    marks[item] = {
                        "offset": offset,
                        "stamp": str(mark.get("stamp") or "0"),
                        "seen": (seen if isinstance(seen, (int, float))
                                 else 0)}
                if marks:
                    kept[name] = marks
            if kept:
                out[key] = {"hosts": kept}
        return out

    def of(self, key, host_name):
        """{artifact: {offset, stamp, seen}} for one host in one stream."""
        with self._lock:
            stream = self.streams.get(key) or {}
            hosts = stream.get("hosts") or {}
            got = hosts.get(host_name) or {}
            return dict((k, dict(v)) for k, v in got.items()
                        if isinstance(v, dict))

    def record(self, key, host_name, item, offset, stamp):
        with self._lock:
            stream = self.streams.setdefault(key, {})
            hosts = stream.setdefault("hosts", {})
            hosts.setdefault(host_name, {})[item] = {
                "offset": int(offset), "stamp": str(stamp),
                "seen": int(time.time())}

    def touch(self, key, host_name, item):
        """Nothing new here, but it is still there -- so it is not stale."""
        with self._lock:
            entry = ((self.streams.get(key) or {}).get("hosts")
                     or {}).get(host_name, {}).get(item)
            if entry is not None:
                entry["seen"] = int(time.time())

    def forget(self, key, host_name, item):
        """Drop a mark whose local file is gone, so the next pass refetches."""
        with self._lock:
            items = ((self.streams.get(key) or {}).get("hosts")
                     or {}).get(host_name)
            if items is not None:
                items.pop(item, None)

    def prune(self, key, host_names, now=None):
        """Drop marks nothing has answered to in a month.

        Only for hosts that answered in this pass: a mark is how an
        unreachable machine keeps its place, and dropping one because the
        host was down is how a day of downtime becomes a re-transfer of
        every file on it.
        """
        now = time.time() if now is None else now
        with self._lock:
            hosts = (self.streams.get(key) or {}).get("hosts") or {}
            for name in host_names:
                items = hosts.get(name) or {}
                for item in list(items):
                    if now - items[item].get("seen", 0) > STATE_TTL:
                        del items[item]

    def save(self):
        """Written whole and renamed into place, once a pass.

        A daemon is killed in the middle of things by definition -- that
        is what stopping one is -- and half a state file is a follow that
        cannot resume.

        Once a pass rather than once a host, so a run killed outright
        resumes from the last pass's marks: that repeats at most one
        pass's bytes into the local copy, where a half-written file would
        cost the lot.  It is called between passes, from the one thread,
        and the temporary name it renames from is per-process rather than
        per-thread -- two workers saving at once would share it.
        """
        with self._lock:
            doc = {"tool": PROG_NAME, "state_version": STATE_VERSION,
                   "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "streams": self.streams}
            body = json.dumps(doc, indent=1, sort_keys=True)
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d)
            except OSError as exc:
                if not os.path.isdir(d):
                    die("cannot make %s for the follow marks: %s" % (d, exc))
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        try:
            with io.open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            die("cannot write the follow marks to %s: %s" % (self.path, exc))


def parse_interval(s):
    """'30s', '5m', '2h', '1d', or a bare number of seconds.

    Spelled the way `--since -30m` is spelled, because a person who has
    just typed one should not have to look up the other.
    """
    text = (s or "").strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$", text)
    if not m:
        die("cannot understand an interval of %r (try 30s, 5m, 2h, or a "
            "plain number of seconds)" % s)
    mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return float(m.group(1)) * mult


def fmt_interval(secs):
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size and secs % size == 0:
            return "%g%s" % (secs / size, unit)
    return "%gs" % secs


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


def _find_expr(args, since_epoch, size_clause=True):
    """The selection, shared by every transport.

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
    if args.max_bytes and size_clause:
        bits.append("-size -%dc" % (int(args.max_bytes) + 1))
    return " ".join(bits)


def _oversize_report(args):
    """Name what was left behind, without carrying it."""
    if not args.max_bytes:
        return ""
    return ('find -H "$rel" -type f -size +%dc '
            '-printf "dredge-skip: %%s %%p\\n" >&2 2>/dev/null\n'
            % int(args.max_bytes))


# The line a tar is announced by, and the reason there is one.
#
# A login may greet a command before the command runs: a banner, a MOTD,
# a "last login" line, a compliance notice a bastion prints from
# /etc/bashrc.  All of it lands on the same stdout the answer comes back
# on.  Every framed transport here already steps over a line it does not
# recognise, so a banner costs those nothing.  A tar cannot do that: the
# first byte of the stream is the first byte of a header, so one line of
# welcome in front of it makes the whole tar unreadable -- and the
# failure that follows names the wrong thing, because by then all this
# side knows is that it could not read what it was sent.
#
# So the far side says where the tar starts, and everything before that
# line is the login talking and is dropped.
MARK_TAR = " TAR"


def remote_tar_command(args, since_epoch, mark):
    return (_PREAMBLE % {"path": shlex.quote(args.path)}
            + _oversize_report(args)
            + "printf '%%s\\n' %s\n" % shlex.quote(mark + MARK_TAR)
            + '%s -print0 2>/dev/null | tar -h --null -T - -cf - '
              '2>/dev/null\n' % _find_expr(args, since_epoch))


def _noise_lines(raw, keep=5):
    """The first few things the far side said, fit to put in a report.

    A banner is text.  Anything else arriving on this stream is payload
    that came without its mark, and a line of that quoted into a report
    is not a diagnosis -- so unprintable bytes go, and a line that was
    nothing else does not survive at all.
    """
    out = []
    for line in raw.split(b"\n"):
        text = "".join(c if c.isprintable() else " "
                       for c in line.decode("utf-8", "replace")).strip()
        if text:
            out.append(text[:120])
        if len(out) >= keep:
            break
    return out


class _PushedBack(object):
    """A stream with the bytes already read off it put back in front.

    Finding the mark means reading past it, and what came after it in
    the same chunk is the start of the tar.  tarfile cannot be asked to
    rewind a pipe, so the bytes are handed back to it here instead.
    """

    __slots__ = ("_head", "_fh")

    def __init__(self, head, fh):
        self._head, self._fh = head, fh

    def read(self, size=-1):
        if size is None or size < 0:
            head, self._head = self._head, b""
            return head + self._fh.read()
        if not self._head:
            return self._fh.read(size)
        out, self._head = self._head[:size], self._head[size:]
        if len(out) == size:
            return out
        return out + self._fh.read(size - len(out))

    def close(self):
        """Nothing of its own to close, and the stream is not its to close.

        tarfile leaves a fileobj it was handed alone, so this is never
        reached -- but a wrapper that cannot be closed at all is one
        `AttributeError` away from turning a collection into a crash.
        """


def skip_to_tar(stream, mark, deadline=None, budget=262144, chunk=65536):
    """Read past the login's chatter and stop where the tar starts.

    Answers `(found, noise, rest)`: whether the mark arrived, the first
    few lines that came before it, and the bytes read past it that
    belong to the tar.  NOISE is what makes a report say *banner* rather
    than *broken* -- naming the line the far side printed turns an
    unreadable stream into an obvious misconfiguration.

    Two bounds, because a search for something that may never come must
    end either way.  DEADLINE is the important one: a far side that
    sends a little and then stalls is what the run's watchdog exists
    for, and a read waiting on a pipe something else still holds open
    would sit past the kill and hang the whole run on one host.  BUDGET
    is the other: a stream that is somehow all payload and no mark ends
    the search rather than being pulled into memory looking for a line
    that is not coming.

    The read is done on the descriptor rather than through the buffered
    reader, so that nothing is left sitting in a buffer tarfile cannot
    see.
    """
    needle = (mark + MARK_TAR).encode("utf-8")
    try:
        fd = stream.fileno()
    except (AttributeError, io.UnsupportedOperation, ValueError):
        fd = None
    buf = b""
    while True:
        at = buf.find(needle)
        if at >= 0:
            end = buf.find(b"\n", at)
            if end >= 0:
                return True, _noise_lines(buf[:at]), buf[end + 1:]
        if len(buf) >= budget:
            break
        if deadline is not None and fd is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                ready, _w, _x = select.select([fd], [], [], left)
            except (OSError, ValueError):
                break
            if not ready:
                break
        try:
            # A stream with no descriptor is not one of ssh's -- a test
            # holding bytes in memory, say -- and it cannot stall, so it
            # is read directly and needs no watching.
            data = os.read(fd, chunk) if fd is not None else stream.read(chunk)
        except (OSError, ValueError):
            break
        if not data:
            break
        buf += data
    return False, _noise_lines(buf), buf


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


_FOLLOW_SCRIPT = r'''
MARK=%(mark)s
export MARK
RT=$(mktemp 2>/dev/null) || RT=/tmp/dredge-marks.$$
export RT
printf '%%s' %(marks)s | base64 -d > "$RT" 2>/dev/null
MAXB=%(maxb)d
export MAXB
TAILN=%(tailn)d
export TAILN
%(find)s -print0 2>/dev/null | xargs -0 -r -n1 sh -c '
f=$1
meta=$(stat -c "%%s %%i" -- "$f" 2>/dev/null)
if [ -n "$meta" ]; then size=${meta%% *}; ino=${meta##* }
else size=$(wc -c < "$f" 2>/dev/null | tr -d " "); ino=0; fi
case "$size" in ""|*[!0-9]*) exit 0;; esac
key=$(printf "%%s" "$f" | base64 | tr -d "\n")
prev=$(grep "^$key " "$RT" 2>/dev/null | head -n 1)
mode=new
start=0
if [ -n "$prev" ]; then
  rest=${prev#* }
  off=${rest%% *}
  was=${rest##* }
  case "$off" in ""|*[!0-9]*) off="" ;; esac
  if [ -z "$off" ]; then mode=new
  elif [ "$was" != 0 ] && [ "$ino" != 0 ] && [ "$was" != "$ino" ]; then mode=rotated
  elif [ "$size" -lt "$off" ]; then mode=rotated
  elif [ "$size" -eq "$off" ]; then mode=same
  else
    mode=more
    start=$off
  fi
elif [ "$TAILN" -gt 0 ]; then mode=tail
fi
if [ "$mode" != same ] && [ "$mode" != tail ] && [ "$MAXB" -gt 0 ] \
   && [ $((size - start)) -gt "$MAXB" ]; then
  echo "dredge-gap: $((size - start - MAXB)) $f" >&2
  start=$((size - MAXB))
fi
printf "%%s FILE\n" "$MARK"
printf "%%s" "$f" | base64 | tr -d "\n"
printf "\n%%s META %%s %%s %%s %%s\n" "$MARK" "$mode" "$start" "$ino" "$size"
if [ "$mode" != same ]; then
  printf "%%s DATA\n" "$MARK"
  if [ "$mode" = tail ]; then
    head -c "$size" -- "$f" | tail -n "$TAILN" | base64
  else
    tail -c "+$((start + 1))" -- "$f" | head -c $((size - start)) | base64
  fi
fi
printf "%%s END\n" "$MARK"
' sh
rm -f "$RT"
'''


def marks_table(marks):
    """The resume table, as the far side reads it: `<b64 path> <off> <ino>`.

    The path is base64'd because a filename may hold a space or a newline,
    and a table a filename can break is a table the fleet gets to choose
    the shape of.  Everything after it is digits, so the far side can
    anchor a plain `grep` on the key and take the rest by splitting.
    """
    lines = []
    for item in sorted(marks):
        mark = marks[item]
        key = base64.b64encode(item.encode("utf-8")).decode("ascii")
        stamp = str(mark.get("stamp") or "0")
        if not stamp.isdigit():
            stamp = "0"
        lines.append("%s %d %s" % (key, int(mark.get("offset") or 0), stamp))
    return "\n".join(lines) + ("\n" if lines else "")


def remote_follow_command(args, mark, marks):
    """Only what is new, decided on the far side, one file at a time.

    The decision cannot be made here: knowing whether a file grew, was
    rotated or is untouched means knowing its size and its inode *now*,
    and asking for that is the round trip this exists to avoid.  So the
    marks go over instead -- one line per file we already have some of --
    and the far side compares them against what it finds, sends only the
    difference, and says in a META frame which of the five things it
    decided.  The local side never has to trust that decision: the frame
    carries the offset the bytes start at, so a mark is rebuilt from what
    actually arrived rather than from what was promised.

    The ceiling applies to the new part rather than to the file.  A log
    that grows past `--max-bytes` is still a log being followed, and
    refusing it at the size it has *reached* would mean a tail that
    quietly stops at 100MB -- which is a gap in the collected copy that
    nothing announces.  What the ceiling catches here is a single pass
    carrying more than it, which is the runaway it is there for.
    """
    table = marks_table(marks)
    return (_PREAMBLE % {"path": shlex.quote(args.path)}
            + _FOLLOW_SCRIPT % {
                "mark": shlex.quote(mark),
                "marks": shlex.quote(base64.b64encode(
                    table.encode("utf-8")).decode("ascii")),
                "maxb": int(args.max_bytes or 0),
                "tailn": int(args.tail or 0),
                "find": _find_expr(args, args.since_epoch, size_clause=False),
            })


_CMD_FOLLOW_SCRIPT = r'''
MARK=%(mark)s
export MARK
command -v bash >/dev/null 2>&1 || {
  echo "dredge: no bash on this host, and --cmd runs a bash command" >&2
  exit 5
}
st=$(mktemp 2>/dev/null) || st=/tmp/dredge-status.$$
ob=$(mktemp 2>/dev/null) || ob=/tmp/dredge-out.$$
CMDB64=%(cmd)s
export CMDB64
{ CMD=$(printf "%%s" "$CMDB64" | base64 -d); export CMD
  bash -c "eval \"\$CMD\""
  echo $? > "$st"
} %(sink)s
OFF=%(off)s
SUM=%(sum)s
size=$(wc -c < "$ob" 2>/dev/null | tr -d " ")
case "$size" in ""|*[!0-9]*) size=0;; esac
sum=$(cksum < "$ob" 2>/dev/null | cut -d" " -f1)
case "$sum" in ""|*[!0-9]*) sum=0;; esac
mode=new
start=0
if [ -n "$OFF" ] && [ "$sum" != 0 ] && [ "$size" -ge "$OFF" ]; then
  pre=$(head -c "$OFF" -- "$ob" 2>/dev/null | cksum 2>/dev/null | cut -d" " -f1)
  if [ -n "$pre" ] && [ "$pre" = "$SUM" ]; then
    if [ "$size" -eq "$OFF" ]; then mode=same; else
      mode=more
      start=$OFF
    fi
  fi
fi
printf "%%s FILE\n" "$MARK"
printf "%%s" %(name)s
printf "\n%%s META %%s %%s %%s %%s\n" "$MARK" "$mode" "$start" "$sum" "$size"
if [ "$mode" != same ]; then
  printf "%%s DATA\n" "$MARK"
  tail -c "+$((start + 1))" -- "$ob" | base64
fi
printf "%%s END\n" "$MARK"
printf "%%s EXIT %%s\n" "$MARK" "$(cat "$st" 2>/dev/null || echo unknown)"
rm -f "$st" "$ob"
'''


def remote_cmd_follow_command(args, mark, prev):
    """The same command again, and only the part of its answer that is new.

    A command has no inode and no offset to seek to -- it has to be run
    again in full, and the whole of what it says exists only after it has
    said it.  What can still be avoided is *carrying* it: the answer goes
    to a temp file on the far side, and a checksum of its first OFF bytes
    is compared with the checksum of the OFF bytes that came back last
    time.  The three outcomes are the three that mean anything:

      same length and same checksum   nothing was added: send nothing
      longer, same prefix             `dmesg`, `journalctl`, a `cat` of a
                                      log: send the part past OFF
      the prefix differs              a different answer, not a longer
                                      one: send all of it

    `cksum` rather than a digest because it is in POSIX and on every box
    this will ever land on; a checksum on an *exact prefix length* is not
    being asked to tell two files apart, it is being asked whether the
    output it already carried is still the start of this one.  A host
    without `cksum` reports a sum of 0 and every pass is a whole answer,
    which is the safe way to be wrong.

    The temp file is what buys this, and it is the reason a follow of a
    command does not stream: the far side has to know the length before
    it can decide what to send.  Without `--follow` nothing here applies
    and the answer streams as it always did.
    """
    cut = ""
    if args.head:
        cut = "2>&1 | head -n %d > \"$ob\"" % args.head
    elif args.tail:
        cut = "2>&1 | tail -n %d > \"$ob\"" % args.tail
    else:
        # `2>&1 >file` would send stderr to the terminal and stdout to the
        # file, which is the opposite of one answer in the order it was
        # said.  The redirection order is load-bearing.
        cut = "> \"$ob\" 2>&1"
    return _CMD_FOLLOW_SCRIPT % {
        "mark": shlex.quote(mark),
        "cmd": shlex.quote(base64.b64encode(
            args.cmd.encode("utf-8")).decode("ascii")),
        "name": shlex.quote(base64.b64encode(
            args.tag.encode("utf-8")).decode("ascii")),
        "sink": cut,
        "off": shlex.quote(str(int(prev.get("offset") or 0))
                           if prev else ""),
        "sum": shlex.quote(str(prev.get("stamp") or "0") if prev else "0"),
    }


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


SUFFIX_LEAD = "._-+~"


def _clean_suffix(suffix):
    """A suffix, reduced to something safe at the end of a filename.

    The same treatment the tag gets, and for the same reason: it is
    written freely by the caller, so it is a place a `/` could arrive and
    quietly mean a directory.

    A bare word gains a dot -- `--suffix log` and `--suffix .log` both
    give `.log`, because that is what somebody typing the first one
    meant.  A suffix that already starts with a separator is appended as
    typed, so `--suffix=-raw` stays `-raw` and does not become `.-raw`.

    A suffix with no letter or digit left in it is nothing: `--suffix //`
    cleans to `-`, which would put a dash on the end of every name in the
    run and mean nothing at all.  Empty comes back so the caller can
    refuse it by name rather than quietly renaming the collection.
    """
    cleaned = TAG_RE.sub("-", (suffix or "").strip())
    if not any(ch.isalnum() for ch in cleaned):
        return ""
    if cleaned[0] not in SUFFIX_LEAD:
        cleaned = "." + cleaned
    return cleaned


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

    `--suffix` goes on the end of every name a run creates, which is how
    a collection gets an extension the rest of your tooling recognises:
    a `--cmd` artifact has no path and so has no extension at all, and
    `ss~web01.txt` opens in an editor where `ss~web01` asks it to guess.
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
    return os.path.join(args.dir, FLAT_SEP.join(parts) + (args.suffix or ""))


# ---------------------------------------------------------------------------
# Writing what came back
# ---------------------------------------------------------------------------

class RemoteNoiseError(Exception):
    """The far side said something, and it was not the answer.

    Raised rather than settled on the spot: the classification in `_run`
    weighs this against a timeout and against the far side's own exit
    status, and a stream that stopped being readable because the
    watchdog killed the session underneath it is a timeout, whatever it
    left in the buffer.
    """


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

    True if the bytes landed here.  False means a name that was already
    taken, which is the one outcome a follow must not write a mark for:
    the mark would say those bytes had been collected, the next pass
    would start after them, and the gap would never close.

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
        return False
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d)
        except OSError as exc:
            if not os.path.isdir(d):
                raise LocalWriteError("cannot make %s: %s" % (d, exc))
    # What came back, before anything already here is added to it: the
    # report is about the transfer, and under --append the file on disk
    # is a week of transfers rather than this one.
    carried = len(data)
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
    taken.append((host, path, carried))
    return True


# ---------------------------------------------------------------------------
# One host
# ---------------------------------------------------------------------------

class Result(object):
    __slots__ = ("host", "outcome", "detail", "files", "bytes", "skipped",
                 "collisions", "truncated", "exit_status", "duration",
                 "unchanged", "rotated", "resynced", "gapped",
                 "remote_says", "answer", "variables", "unknown_vars",
                 "parse_error")

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
        # What the far side wrote on stderr and this tool has no other
        # name for.  Kept even when the run succeeded: a host that sent
        # nothing back is the case where the only explanation there will
        # ever be is the line the remote shell printed on its way past.
        self.remote_says = []
        # What --follow found and did not have to carry. Unchanged is a
        # count rather than a list because it is the ordinary answer and
        # naming four hundred untouched files is not a report.
        self.unchanged = 0
        # Files that were not the file they were: a new inode, or a size
        # that went backwards. Each came back whole, and each is worth
        # saying out loud -- it is the one case where a followed copy has
        # a seam in it that is not a seam in the original.
        self.rotated = []
        # The local copy was gone, so the mark was dropped rather than
        # believed. Nothing is more confidently wrong than a follow
        # reporting "nothing new" about a file that is no longer here.
        self.resynced = []
        # (bytes, path) for each artifact that grew by more than the
        # ceiling in one pass. The newest --max-bytes came back and the
        # rest did not: a hole in the collected copy, said out loud,
        # rather than a follow that stops.
        self.gapped = []
        # What --cmd's command said, and what --tsv made of it. The raw
        # bytes are kept because the parse happens once, on the main
        # thread, after every host is in: a table whose columns depend on
        # what the fleet said cannot be written a row at a time from
        # inside a worker.
        self.answer = None
        self.variables = {}
        # Names this host had that the table's header does not. Not
        # dropped quietly: a column that appeared halfway through a
        # week of passes is a finding, not a formatting detail.
        self.unknown_vars = []
        # Why this host has no row, when it had an answer to parse.
        self.parse_error = ""


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
GAP_RE = re.compile(r"^dredge-gap:\s+(\d+)\s+(.*)$")


def _parse_stderr(text):
    """(skipped, gapped, other) -- what was left behind, and anything else.

    Two different things, and they must not be confused: a *skipped* file
    was not carried at all, and a *gapped* one was carried from further
    along than it should have been.  The first can be come back for; the
    second is a hole that will not fill.
    """
    skipped, gapped, other = [], [], []
    for line in (text or "").splitlines():
        m = SKIP_RE.match(line.strip())
        if m:
            skipped.append((int(m.group(1)), m.group(2)))
            continue
        m = GAP_RE.match(line.strip())
        if m:
            gapped.append((int(m.group(1)), m.group(2)))
        elif line.strip():
            other.append(line.strip())
    return skipped, gapped, other


def _feed(stream, payload):
    """Push SEND down the child's stdin, in a thread of its own.

    Writing it inline would deadlock the moment it outgrew the pipe
    buffer: this side would block on the write while the far side blocked
    on a stdout nobody was reading yet.  A far side that gave up early
    closes the pipe, and that is an ordinary end to a transfer rather
    than a failure -- its exit status says what happened.
    """
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, ValueError):
        pass
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _run(args, host, command, consume, send=None):
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
                             stdin=(subprocess.PIPE if send is not None
                                    else subprocess.DEVNULL),
                             start_new_session=True)
    except OSError as exc:
        r.outcome, r.detail = FAILED, str(exc)
        return r
    errbuf = []
    rc = None
    t = threading.Thread(target=_drain, args=(p.stderr, errbuf))
    t.daemon = True
    t.start()
    if send is not None:
        feeder = threading.Thread(target=_feed, args=(p.stdin, send))
        feeder.daemon = True
        feeder.start()

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
        consume_err = (str(exc)
                       if isinstance(exc, (LocalWriteError, RemoteNoiseError))
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
    r.skipped, r.gapped, other = _parse_stderr(err)
    r.remote_says = other
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
        found, noise, rest = skip_to_tar(
            stream, args.mark_token, time.monotonic() + args.timeout)
        if not found:
            # Nothing said the tar had started. If the host talked first
            # and then stopped, what it said is the whole diagnosis --
            # usually a login that prints a banner and a shell that never
            # ran the script behind it. If it said nothing at all, it
            # never got that far, and its exit status -- or the watchdog
            # -- is a better witness than a guess made here.
            r.files, r.bytes = [], 0
            if noise:
                raise RemoteNoiseError(
                    "the host answered, but not with a collection: %s"
                    % noise[0])
            return
        # r| is the streaming mode: members are read in order off a pipe,
        # with no seeking back, which is what lets the unpacking start
        # before the far side has finished sending.
        try:
            tar = tarfile.open(fileobj=_PushedBack(rest, stream), mode="r|*")
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
    return _run(args, host,
                remote_tar_command(args, args.since_epoch, args.mark_token),
                consume)


def collect_command(args, host):
    """One command's output, through the same framed stream a slice uses.

    The same transport for the same reason: the length of what a command
    will say is not known until it has said it, so it cannot be tarred,
    and base64 inside a per-run token frame carries any byte it produces.
    """
    if args.follow:
        prev = args.marks.of(args.stream_key, host.name).get("cmd")
        command = remote_cmd_follow_command(args, args.mark_token, prev)
    else:
        command = remote_cmd_command(args, args.mark_token)
    return _run(args, host, command, _frame_consumer(args, host))


def collect_follow(args, host):
    """Only what is new, resumed from this host's own marks."""
    marks = args.marks.of(args.stream_key, host.name)
    return _run(args, host,
                remote_follow_command(args, args.mark_token, marks),
                _frame_consumer(args, host))


def collect_slices(args, host):
    """A head or a tail of each file, out of the framed base64 stream."""
    return _run(args, host,
                remote_slice_command(args, args.since_epoch,
                                     args.mark_token),
                _frame_consumer(args, host))


def parse_meta(text):
    """`<mode> <start> <stamp> <size>`, or None if it is not that.

    Nothing the far side says is trusted further than it has to be: an
    unknown mode, a non-numeric offset or a missing field makes the whole
    frame meaningless rather than half-believed, and a half-believed
    offset is a gap in a collected log.
    """
    bits = text.split()
    if len(bits) != 4 or bits[0] not in FOLLOW_MODES:
        return None
    if not bits[1].isdigit() or not bits[3].isdigit():
        return None
    return {"mode": bits[0], "start": int(bits[1]), "stamp": bits[2],
            "size": int(bits[3])}


def _land_follow(args, host, r, item, path, data, meta, taken):
    """One artifact's worth of new bytes, and the mark that follows it.

    The mark is rebuilt from what *arrived*, not from what the far side
    said it would send: `start` plus the bytes actually decoded here. A
    transfer cut off halfway therefore resumes from where it was cut,
    where believing the promised size would step over whatever never
    made it.

    The exception is a first sight cut by `--tail N`, which is a count of
    lines rather than of bytes -- there the far side clamped its read at
    the size it measured, and that size is where the next pass starts.
    """
    local = local_path(args, host, path)
    if meta["mode"] == SAME:
        # Nothing new there. If what we already have is still here, say
        # so and move on; if it is not, the mark is a lie and goes.
        if os.path.exists(local):
            r.unchanged += 1
            args.marks.touch(args.stream_key, host.name, item)
        else:
            r.resynced.append(path)
            args.marks.forget(args.stream_key, host.name, item)
        return
    if meta["mode"] == ROTATED:
        r.rotated.append(path)
    if not write_file(args, host, local, data, taken, r.collisions, path):
        # The name was already taken, so these bytes are not here. A mark
        # now would say they were, and the next pass would start after
        # them.
        return
    offset = (meta["size"] if meta["mode"] == TAILED
              else meta["start"] + len(data))
    args.marks.record(args.stream_key, host.name, item, offset, meta["stamp"])


def _frame_consumer(args, host):
    """Read `<MARK> FILE|META|DATA|END|EXIT` frames off a stream."""
    mark = args.mark_token

    def consume(stream, r):
        taken = []
        state, path, chunks, meta = None, None, [], None
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line == mark + " FILE":
                state, path, chunks, meta = "path", None, [], None
            elif line == mark + " DATA":
                state = "data"
            elif line.startswith(mark + " META "):
                meta = parse_meta(line[len(mark) + 6:])
                state = None
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
                    # A command that printed nothing said nothing, and
                    # there is no artifact in that.  The frame arrives
                    # either way -- it carries the exit status, which is
                    # worth having -- so landing it would write a
                    # zero-byte file per host and call the run a
                    # collection, which is how `--cmd ls` over a fleet of
                    # empty login directories comes back looking broken
                    # rather than empty.  A *file* that is zero bytes is a
                    # different thing: it exists on the far side, and a
                    # faithful copy of it is empty.
                    #
                    # --follow is left to its own accounting.  There an
                    # empty pass is what SAME already means, the mark has
                    # to be kept either way, and a stream that is empty
                    # the first time it is seen is a stream, not a
                    # failed collection.
                    silent = args.cmd and not args.follow and not data
                    if args.cmd and data is not None:
                        # What the command said, kept for --tsv to read
                        # variables out of.  Taken here rather than read
                        # back off the landed file: --tsv is the parsed
                        # view of this answer, and a silent command
                        # lands no file to read back at all.
                        r.answer = data
                    if data is not None and not silent:
                        if args.follow and meta is None:
                            r.detail = ("the far side sent no follow frame "
                                        "for %s" % path)
                        elif args.follow:
                            _land_follow(args, host, r,
                                         "cmd" if args.cmd else path,
                                         path, data, meta, taken)
                        else:
                            write_file(args, host,
                                       local_path(args, host, path),
                                       data, taken, r.collisions, path)
                state, path, chunks, meta = None, None, [], None
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
    if args.follow:
        return collect_follow(args, host)
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

def render(results, args, elapsed, compact=False):
    """The report, or -- for one pass of a repeating run -- one line of it.

    A daemon printing a full report every five minutes buries the one
    that mattered under three hundred that said nothing.  So a pass says
    its one line, and keeps the findings block underneath it for the
    passes that have something in it: a host that failed, a file that
    rotated, a command that exited non-zero.  The block is word for word
    the one-off report's, because a thing worth reporting does not change
    its wording for being on a timer.
    """
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
    if args.follow:
        what += "   [follow, %s]" % args.mode
    if args.every:
        what += "   [every %s]" % fmt_interval(args.every)
    if compact:
        out.append("%s  pass %-4d %d new, %s from %d of %d host%s in %.1fs"
                   % (time.strftime("%H:%M:%S"), args.passno, nfiles,
                      fmt_bytes(nbytes), len(good), len(results),
                      "" if len(results) == 1 else "s", elapsed))
        return _render_findings(out, results, args, compact)
    out.append("%s -- %s" % (PROG, what))
    if args.follow:
        noun = "%s with new data" % ("artifact" if nfiles == 1
                                     else "artifacts")
    else:
        noun = "file" if nfiles == 1 else "files"
    out.append("        %d %s from %d of %d host%s, %s in %.1fs -> %s/"
               % (nfiles, noun, len(good), len(results),
                  "" if len(results) == 1 else "s", fmt_bytes(nbytes),
                  elapsed, args.dir))
    out.append("")
    return _render_findings(out, results, args, compact)


def _render_findings(out, results, args, compact=False):
    """Everything that is not the headline: what went wrong, or nearly."""
    good = [r for r in results if r.outcome == OK and r.files]
    empty = [r for r in results if r.outcome == OK and not r.files]
    bad = [r for r in results if r.outcome != OK]
    nfiles = sum(len(r.files) for r in results)
    # Counted over the hosts this line is about, not over the run: the
    # sub-line sits under the list of hosts that had nothing new, and a
    # total that included the busy hosts' untouched files would be read
    # as belonging to the quiet ones.
    steady = sum(r.unchanged for r in empty)
    if empty and args.follow:
        names = " ".join(r.host.name for r in empty[:6])
        more = "" if len(empty) <= 6 else " (+%d)" % (len(empty) - 6)
        out.append("  UNCHANGED %d host%s had nothing new: %s%s"
                   % (len(empty), "" if len(empty) == 1 else "s", names, more))
        if steady:
            out.append("            %d artifact%s checked and unchanged there"
                       % (steady, "" if steady == 1 else "s"))
    elif empty:
        names = " ".join(r.host.name for r in empty[:6])
        more = "" if len(empty) <= 6 else " (+%d)" % (len(empty) - 6)
        out.append("  EMPTY     %d host%s had nothing to send: %s%s"
                   % (len(empty), "" if len(empty) == 1 else "s", names, more))
        if args.since:
            out.append("            nothing under %s changed since %s there"
                       % (args.path, args.since))
        elif args.cmd:
            out.append("            the command printed nothing there, on "
                       "stdout or stderr, so there was no artifact to keep")
        # Whatever came back on that host's stderr, but only for the
        # hosts that sent nothing: that is the run with no other
        # explanation in it, and a shell complaining on the way past --
        # `base64: command not found`, a profile that died -- is usually
        # the whole answer.  On a host that did send something the same
        # line is noise, every `stdin: is not a tty` in the fleet, so it
        # stays unsaid there.  Not attributed to the command: ssh writes
        # here too, and which of them spoke is not ours to guess.
        said = [(r.host, r.remote_says[0]) for r in empty if r.remote_says]
        for host, line in said[:5]:
            out.append("            %-12s stderr: %s"
                       % (host.name, line[:100]))
        if len(said) > 5:
            out.append("            ... and %d more wrote to stderr"
                       % (len(said) - 5))
    turned = [(r.host, x) for r in results for x in r.rotated]
    if turned:
        # Not "came back whole": a rotated file over the ceiling comes
        # back from part way in, and the GAP block below says so. The two
        # blocks must not contradict each other.
        out.append("  ROTATED   %d file%s was not the file it was and came "
                   "back as a new one:" % (len(turned),
                                           "" if len(turned) == 1 else "s"))
        for host, remote in turned[:5]:
            out.append("            %-12s %s" % (host.name, remote))
        if len(turned) > 5:
            out.append("            ... and %d more" % (len(turned) - 5))
        out.append("            A new inode, or a size that went backwards: "
                   "resuming at the old")
        out.append("            offset would have handed you the middle of "
                   "a different file.")
        out.append("            The follow carries on from the new one -- "
                   "this is a seam, not a stop.")
    holes = [(r.host, n, x) for r in results for n, x in r.gapped]
    if holes:
        out.append("  GAP       %d artifact%s grew by more than --max-bytes "
                   "(%s) in one pass:"
                   % (len(holes), "" if len(holes) == 1 else "s",
                      fmt_bytes(args.max_bytes)))
        for host, n, remote in holes[:5]:
            out.append("            %-12s %8s not carried  %s"
                       % (host.name, fmt_bytes(n), remote))
        if len(holes) > 5:
            out.append("            ... and %d more" % (len(holes) - 5))
        out.append("            The newest %s came back and the follow is at "
                   "the end of the file" % fmt_bytes(args.max_bytes))
        out.append("            again, so this is one hole rather than a "
                   "stop.  Raise --max-bytes,")
        out.append("            or pass more often, to stop it happening "
                   "again.")
    lost = [(r.host, x) for r in results for x in r.resynced]
    if lost:
        out.append("  RESYNC    %d local cop%s gone, so the mark went with "
                   "it:" % (len(lost), "y is" if len(lost) == 1 else "ies are"))
        for host, remote in lost[:5]:
            out.append("            %-12s %s" % (host.name, remote))
        if len(lost) > 5:
            out.append("            ... and %d more" % (len(lost) - 5))
        out.append("            The next pass brings each of them back "
                   "whole.")
    unparsed = [r for r in results if r.parse_error]
    if unparsed:
        # A host whose answer would not parse has no row, and a table
        # with a host quietly missing from it is a table that reads as
        # "that machine was fine" -- so this is said every time, and
        # says which host and what was wrong with what it printed.
        out.append("  UNPARSED  %d host%s printed something --parse %s "
                   "could not read:"
                   % (len(unparsed), "" if len(unparsed) == 1 else "s",
                      args.parse))
        for r in unparsed[:5]:
            out.append("            %-12s %s" % (r.host.name,
                                                 r.parse_error))
        if len(unparsed) > 5:
            out.append("            ... and %d more" % (len(unparsed) - 5))
        out.append("            Its answer is still collected whole -- "
                   "only its row is missing.")
    fresh = []
    for r in results:
        for name in r.unknown_vars:
            if name not in fresh:
                fresh.append(name)
    if fresh:
        # The header is fixed by the file, so a name that appeared later
        # has nowhere to go. Dropping it silently is how a week of
        # passes quietly stops recording the thing you added.
        out.append("  NEWVAR    %d name%s the table has no column for: %s"
                   % (len(fresh), "" if len(fresh) == 1 else "s",
                      " ".join(fresh[:8])
                      + ("" if len(fresh) <= 8 else " ...")))
        out.append("            The header was written when %s was "
                   "created and every row since" % args.tsv)
        out.append("            is counted from it.  Start a new table, "
                   "or name the columns")
        out.append("            up front with --columns.")
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
        # "either way" is about the exit status, not about there being
        # something to keep: a command can fail and print nothing, and
        # then the EMPTY line above is the one telling the truth.
        if all(r.files for r in angry):
            out.append("            What it said is collected either way.")
        else:
            out.append("            What it said is collected either way, "
                       "where it said anything.")
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
        out.append("            --tail N brings back the end of one "
                   "without the rest of it.")
    if empty or bad or skipped or clashed or cut or angry or turned \
            or lost or holes:
        out.append("")

    if good and not args.quiet and not compact:
        shown = [p for r in good for p, _n in r.files][:6]
        for p in shown:
            out.append("  %s" % p)
        if nfiles > len(shown):
            out.append("  ... and %d more" % (nfiles - len(shown)))
        out.append("")
    return "\n".join(out) + "\n"


# Appended to, never reordered: the header is an interface, and a
# column that moves breaks every reader of every CSV already written.
# `pass` is 1 for a single run and counts up under --daemon; `unchanged`
# is what a follow checked and did not have to carry, which is the number
# that says the tail is working; `gap_bytes` is what a pass could not
# carry and nothing will bring back.
#
# That last one is load-bearing rather than decorative. A gap does not
# change the exit status -- a busy log under a tight ceiling would make
# every pass of a perfectly healthy daemon look like a failure -- so this
# column is the only thing a script can read to learn that part of the
# log is missing. A finding that is in the report and not in the CSV is a
# finding automation cannot see.
CSV_FIELDS = ["host", "source", "local_path", "bytes", "outcome",
              "exit_status", "pass", "unchanged", "gap_bytes"]


def write_csv(results, args, path, append=False):
    """One row per file, per host, per pass.

    A daemon appends: the file is the log of the collection, and each
    pass adds its rows to the end of it rather than replacing what the
    last one wrote. The header goes down once, on the first pass.
    """
    if path in (None, "-"):
        fh = sys.stdout
    else:
        fh = io.open(path, "a" if append else "w", newline="",
                     encoding="utf-8")
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        if not append:
            w.writeheader()
        for r in results:
            source = args.cmd if args.cmd else args.path
            st = "" if r.exit_status is None else r.exit_status
            # Keyed by the local name, because that is what the rows are
            # keyed by -- the remote path is rebuilt into one here and is
            # never read back off the far side.
            holes = {}
            for n, remote in r.gapped:
                where = local_path(args, r.host, remote)
                holes[where] = holes.get(where, 0) + n
            row = {"host": r.host.name, "source": source,
                   "outcome": r.outcome, "exit_status": st,
                   "pass": args.passno or 1, "unchanged": r.unchanged,
                   "gap_bytes": 0}
            if not r.files:
                row.update({"local_path": "", "bytes": 0,
                            "gap_bytes": sum(holes.values())})
                w.writerow(row)
                continue
            for p, n in r.files:
                row.update({"local_path": p, "bytes": n,
                            "gap_bytes": holes.pop(p, 0)})
                w.writerow(row)
    finally:
        if fh is not sys.stdout:
            fh.close()


# ---------------------------------------------------------------------------
# A table of what the fleet said
# ---------------------------------------------------------------------------
#
# `--csv` is a row per file: what was collected, how big it was, how it
# went.  This is the other question -- not "did the collection work" but
# "what were the numbers" -- and it wants a different shape:
#
#     date                   host    load1   procs   uptime_s
#     2026-09-18T09:14:02    web01   0.41    212     884411
#     2026-09-18T09:14:02    web02   1.93    318     12904
#
# One row per host per pass, stamped with the time the pass ran, appended
# to one file across runs.  That is the shape a week of `--daemon` passes
# has to have for anything downstream -- a spreadsheet, a plot, an awk
# one-liner -- to read it as a time series rather than as forty files.

TSV_META = ("date", "host")

# What a name is allowed to be. A variable that arrives called `load 1`
# or `a\tb` would put a tab or a space where a column boundary goes, so
# it is refused by name rather than quietly making the table unreadable.
VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def tsv_stamp(when=None):
    """The date column: local time, to the second, sortable as text.

    `%Y-%m-%dT%H:%M:%S` and nothing else -- no offset, no fraction.  It
    is the clock *here*, the one machine in the run whose time is not in
    question, so every row in a pass carries the same stamp however far
    apart the hosts' own clocks are.  `skew` is the instrument for the
    question of whose clock is wrong; this column deliberately does not
    pretend to answer it.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(when))


def _tsv_escape(value):
    """A value that cannot break the row it is in.

    A tab ends a column and a newline ends a row, so a value carrying
    either would silently turn one row into two or shift every column
    after it.  They travel as `\\t` and `\\n` instead, which is what awk,
    a spreadsheet import and a human reader all expect to see.
    """
    return (str(value).replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\r", "\\r").replace("\n", "\\n"))


def parse_kv(text):
    """`name=value` per line: `sysctl -a`, `/etc/os-release`, most probes.

    The default because it is the one shape that describes itself.  The
    command names its own variables, so the table's columns come from
    the data rather than from a flag that has to be kept in step with
    it, and a host that is missing one is a visible blank rather than a
    row whose columns have all shifted along by one.

    Blank lines and `#` comments are skipped, whitespace around the `=`
    is not part of either side, and the first `=` is the separator so a
    value may contain more.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return None, "not name=value: %r" % line[:60]
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip()
    return out, None


def parse_json(text):
    """A JSON object, one level deep.

    Nesting has no column to go in, so an object or a list inside is
    refused by name rather than landing as a Python repr that nothing
    downstream can read.  A command with nested data can flatten it
    itself -- it knows what the names should be, and this does not.
    """
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return None, "not JSON: %s" % exc
    if not isinstance(obj, dict):
        return None, ("JSON is a %s, and a row wants an object of "
                      "name/value pairs" % type(obj).__name__)
    out = {}
    for name, value in obj.items():
        if isinstance(value, (dict, list)):
            return None, ("%r is a %s, and there is no column for one -- "
                          "flatten it on the far side"
                          % (name, type(value).__name__))
        if value is None:
            value = ""
        elif value is True:
            value = "true"
        elif value is False:
            value = "false"
        out[str(name)] = value
    return out, None


def parse_row(text):
    """A header line, then a values line: the command names its own columns.

    Split on tabs where there is a tab, and on runs of whitespace where
    there is not, so `printf 'a\\tb\\n1\\t2\\n'` and `echo a b; echo 1 2`
    both work and a value with a space in it survives the first.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None, ("wanted a header line and a values line, got %d"
                      % len(lines))
    names = _split_fields(lines[0])
    values = _split_fields(lines[1])
    if len(names) != len(values):
        return None, ("%d column name%s and %d value%s"
                      % (len(names), "" if len(names) == 1 else "s",
                         len(values), "" if len(values) == 1 else "s"))
    return dict(zip(names, values)), None


def parse_values(text, columns):
    """Bare values in a fixed order, named here by --columns.

    The one shape that is not self-describing, which is why the names
    have to come from the command line: the far side prints `3 41 0.7`
    and nothing in it says which is which.  A host that prints a
    different number of values is refused rather than filled in, because
    a short row here is not a missing value -- it is every column after
    the gap holding the wrong one.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None, "no values"
    values = _split_fields(lines[0])
    if len(values) != len(columns):
        return None, ("--columns names %d, the host printed %d: %s"
                      % (len(columns), len(values),
                         " ".join(values[:8])[:80]))
    return dict(zip(columns, values)), None


def _split_fields(line):
    """Tabs where there are tabs, whitespace where there are not."""
    return line.split("\t") if "\t" in line else line.split()


def parse_variables(text, how, columns):
    """(variables, error) -- one host's answer, in the shape it was promised."""
    if how == "kv":
        return parse_kv(text)
    if how == "json":
        return parse_json(text)
    if how == "row":
        return parse_row(text)
    return parse_values(text, columns)


def read_tsv_header(path):
    """The columns a table already has, or None if it has none yet.

    Read rather than assumed, because the whole value of appending to one
    file is that column 4 means the same thing in row 2 and in row 900.
    Rebuilding the header from this pass's data would quietly renumber
    every column the first time a host failed to answer.
    """
    try:
        with io.open(path, encoding="utf-8") as fh:
            first = fh.readline()
    except (OSError, IOError):
        return None
    first = first.rstrip("\n")
    if not first:
        return None
    return first.split("\t")


def tsv_columns(results, args):
    """The header this table will have, and whether it is already on disk.

    `--columns` decides it outright.  Otherwise it is the names the fleet
    actually used, in the order they were first seen -- and host order is
    fixed by the server list, so two runs over one list build the same
    header.
    """
    existing = read_tsv_header(args.tsv) if args.tsv != "-" else None
    if existing:
        return existing, True
    if args.columns:
        return list(TSV_META) + list(args.columns), False
    names = []
    for r in results:
        for name in r.variables:
            if name not in names:
                names.append(name)
    return list(TSV_META) + names, False


def write_tsv(results, args):
    """One row per host that answered, appended under a stable header.

    A host with no answer gets no row: an empty line under a timestamp
    says the fleet reported zero, which is a different and much worse
    claim than saying nothing.  Which hosts those were, and why, is in
    the report and in `--csv`.
    """
    stamp = tsv_stamp()
    for r in results:
        if r.answer is None or not r.answer.strip():
            continue
        text = r.answer.decode("utf-8", "replace")
        variables, err = parse_variables(text, args.parse, args.columns)
        if err:
            r.parse_error = err
            continue
        for name in variables:
            if not VAR_NAME_RE.match(name):
                r.parse_error = ("%r is not a name a column can have "
                                 "(letters, digits, _ . -)" % name[:40])
                variables = None
                break
        if variables is None:
            continue
        r.variables = variables

    header, had_header = tsv_columns(results, args)
    known = set(header)
    rows = []
    for r in results:
        if not r.variables:
            continue
        r.unknown_vars = [n for n in r.variables if n not in known]
        cells = {"date": stamp, "host": r.host.name}
        cells.update(r.variables)
        rows.append([_tsv_escape(cells.get(c, "")) for c in header])
    if not rows:
        return 0

    if args.tsv == "-":
        fh, close = sys.stdout, False
    else:
        try:
            fh, close = io.open(args.tsv, "a", encoding="utf-8"), True
        except (OSError, IOError) as exc:
            raise LocalWriteError("cannot write %s: %s" % (args.tsv, exc))
    try:
        if not had_header:
            fh.write("\t".join(header) + "\n")
        for row in rows:
            fh.write("\t".join(row) + "\n")
        fh.flush()
    finally:
        if close:
            fh.close()
    return len(rows)


# ---------------------------------------------------------------------------
# A pass, and passes
# ---------------------------------------------------------------------------

def one_pass(args, hosts):
    """Contact every host once, and write down where each one got to."""
    t0 = time.monotonic()
    results = fan_out(hosts, lambda h: collect_one(args, h), args.jobs,
                      args.quiet)
    elapsed = time.monotonic() - t0
    if args.marks is not None:
        # Only hosts that answered: a mark is how an unreachable machine
        # keeps its place, and dropping one because the host was down is
        # how an afternoon of downtime becomes a re-transfer of every
        # file on it.
        args.marks.prune(args.stream_key,
                         [r.host.name for r in results if r.outcome == OK])
        args.marks.save()
    return results, elapsed


def pass_failed(args, results):
    """Whether what came back is not what was asked for.

    Nothing collected is a failure for a one-off run -- it is the whole
    point of the run -- and the ordinary answer under `--follow`, which
    spends most of its life having nothing to say.  A tail that exits 1
    every time the log is quiet is a tail nothing can be built on.
    """
    if any(r.outcome != OK or r.collisions or r.truncated
           or r.exit_status not in (None, 0) for r in results):
        return True
    return not args.follow and not sum(len(r.files) for r in results)


def report_pass(args, results, elapsed, compact=False):
    """Say what the pass did -- unless --quiet, and nothing went wrong.

    Quiet means the same thing in both shapes: a failure is still a
    finding and still gets said, because a daemon that swallows an
    unreachable host is a daemon you cannot leave running.
    """
    if args.quiet and not any(r.outcome != OK for r in results):
        return
    sys.stdout.write(render(results, args, elapsed, compact=compact))
    sys.stdout.flush()


def run_repeating(args, hosts, relay=None):
    """A pass, a wait, another pass, until something says stop.

    The wait is between passes rather than a period, so a pass that runs
    longer than `--every` cannot stack up behind itself: forty hosts that
    take eleven minutes on a five-minute timer would otherwise queue, and
    every pass after the first would be contending with the one before it
    for the link it is measuring.

    Stopping is a signal, and a signal arrives mid-pass: the hosts
    already contacted are finished, their marks are written, and the run
    ends after that rather than halfway through a file.  Asked twice, it
    is not asked a third time -- the second signal puts the default
    handler back, so the next one ends the process the way it always
    would have.
    """
    stop = threading.Event()
    hits = []

    def _stop(signum, _frame):
        hits.append(signum)
        stop.set()
        if len(hits) > 1:
            signal.signal(signum, signal.SIG_DFL)
        else:
            note("stopping after this pass", args.quiet)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError, RuntimeError):
            # No handler, then: the default one already ends the run, and
            # the only thing lost is finishing the pass in hand.
            pass

    worst = 0
    while True:
        args.passno += 1
        if relay is not None:
            # A pass is one round trip through the jump box, and the
            # timer stays on this side: a daemon looping over there would
            # hold one connection open for hours and hand back nothing
            # until it ended.
            t0 = time.monotonic()
            if run_relay(args, hosts, relay):
                worst = 1
            elapsed = time.monotonic() - t0
            if stop.is_set():
                break
            if args.passes and args.passno >= args.passes:
                break
            if elapsed > args.every:
                note("that pass took %.1fs, longer than --every %s: the "
                     "next one starts now"
                     % (elapsed, fmt_interval(args.every)), args.quiet)
                continue
            if stop.wait(args.every):
                break
            continue
        results, elapsed = one_pass(args, hosts)
        if args.csv is not None:
            write_csv(results, args, args.csv, append=args.passno > 1)
        if args.tsv is not None:
            write_tsv(results, args)
        report_pass(args, results, elapsed, compact=True)
        if pass_failed(args, results):
            worst = 1
        if stop.is_set():
            break
        if args.passes and args.passno >= args.passes:
            break
        if elapsed > args.every:
            # Said or not said, the next pass still starts now: --quiet
            # decides what is printed, never what is done.
            note("that pass took %.1fs, longer than --every %s: the next "
                 "one starts now" % (elapsed, fmt_interval(args.every)),
                 args.quiet)
            continue
        if stop.wait(args.every):
            break
    if not args.quiet:
        note("stopped after %d pass%s" % (args.passno,
                                          "" if args.passno == 1 else "es"))
    # A run that was asked to stop did what it was asked. Only a run that
    # finished its --passes reports on what those passes found.
    return 0 if hits else worst


# ---------------------------------------------------------------------------
# A jump box
# ---------------------------------------------------------------------------
#
# The fleet is behind a bastion: A can reach B, and only B can reach C-Z.
#
# The tunnelling answer is `ssh -J`, and it works -- but it needs the
# jump to be configured, it opens one connection per target across the
# A-B link, and every byte of forty hosts' output crosses that link
# separately.  This is the other answer, and the one that depends on no
# configuration at all: send *this file* to B, run the whole collection
# there, and bring the result back in one piece.
#
#     A  --ssh-->  B  --ssh--> C, D, E ... Z
#        <--tar--     <--------
#
# B needs a Python and a shell.  It does not need dredge installed, an
# agent, a package, a cron entry or a line of configuration, because the
# copy that runs there is the copy that was running here, sent over the
# same connection that carries the answer back.  Nothing is left behind:
# the spool is a known path rather than a `mktemp -d` precisely so that
# it can be removed even when the run it belonged to was killed.
#
# The A-B link carries one transfer instead of forty, which is the whole
# reason to prefer this over a tunnel when that link is the slow one.
# The cost is honest and worth saying out loud: the collection exists on
# B, in the clear, for as long as the run lasts.  On a bastion somebody
# else owns, that is a disclosure, and `--via`-style tunnelling is the
# shape that does not make it.

RELAY_SPOOL = "dredge-relay"

# The copy of this file and the server list travel on stdin, as a tar --
# not as an argument.  This file is three thousand lines, and base64 of
# it in an argv is past ARG_MAX before it ever reaches ssh: the local
# exec fails with "Argument list too long" and the fleet is never
# contacted.  stdin has no such ceiling, and tar is already the shape
# the answer comes back in.
_RELAY_SCRIPT = r'''
spool=%(spool)s
rm -rf "$spool" 2>/dev/null
mkdir -p "$spool" || { echo "dredge: cannot make $spool" >&2; exit 7; }
cleanup() { [ -n "%(keep)s" ] || rm -rf "$spool"; }
trap cleanup EXIT INT TERM
cd "$spool" || exit 7
base64 -d | tar -xf - || {
  echo "dredge: cannot unpack the copy sent to this host" >&2; exit 7; }
[ -f dredge.py ] && [ -f servers ] || {
  echo "dredge: the copy sent to this host did not arrive whole" >&2
  exit 7; }
PY=%(python)s
[ -n "$PY" ] || PY=$(command -v python3 2>/dev/null) \
             || PY=$(command -v python 2>/dev/null)
[ -n "$PY" ] || {
  echo "dredge: no python3 on this host, and the relay runs dredge here" >&2
  exit 6; }
mkdir -p collect
"$PY" dredge.py %(args)s --servers servers -d collect \
      > report 2>errors
echo $? > status
printf '%%s\n' %(mark)s
tar -cf - collect report errors status 2>/dev/null
'''


def relay_remote_argv(args):
    """The run B is asked to do: this one, minus the parts that are A's.

    Rebuilt from the parsed options rather than by filtering the original
    argv, because `--tag=x` and `--tag x` are the same intent spelled two
    ways and a filter has to know both.  What is left out is as
    deliberate as what is kept:

      --relay*        B is where this is running; it does not relay on.
      --daemon,       the timer stays here.  A pass is a round trip, and
      --every,        a daemon that looped on B would hold one connection
      --passes        open for hours and hand back nothing until it ended.
      -d, --servers   rewritten to the spool, by the script.
      --csv, --tsv    written into the spool and brought back, so that
                      the table A keeps is A's and not one more thing
                      left on the bastion.
    """
    out = []
    if args.cmd:
        out += ["--cmd", args.cmd]
    else:
        out += [args.path]
    for flag, value in (("--tag", args.tag), ("--suffix", args.suffix),
                        ("--since", args.since), ("--user", args.user),
                        ("--ssh", args.ssh), ("--parse", args.parse)):
        if value:
            out += [flag, str(value)]
    for flag, value in (("--head", args.head), ("--tail", args.tail),
                        ("--max-bytes", args.max_bytes),
                        ("--max-files", args.max_files),
                        ("--jobs", args.jobs), ("--timeout", args.timeout)):
        if value is not None:
            out += [flag, str(value)]
    for flag, on in (("--append", args.append), ("--prepend", args.prepend),
                     ("--mark", args.mark)):
        if on:
            out += [flag]
    if args.columns:
        out += ["--columns", ",".join(args.columns)]
    # Into the collection directory, because that is what the tar
    # carries home. An artifact can never collide with these: every
    # collected name has a `~` in it, and neither of these does.
    if args.csv is not None:
        out += ["--csv", "collect/relay.csv"]
    if args.tsv is not None:
        out += ["--tsv", "collect/relay.tsv"]
    return out


def relay_payload(hosts):
    """This file and the server list, as a tar, base64'd for stdin.

    The list is sent already expanded -- `web[01-40]` became forty names
    here, and the names are the ones this side chose -- so the jump box
    collects from exactly the fleet that was asked for rather than
    re-expanding a range against whatever its own idea of the syntax is.
    """
    with io.open(os.path.abspath(__file__), "rb") as fh:
        source = fh.read()
    listing = "".join("%s=%s%s\n"
                      % (h.name, h.addr, ":%d" % h.port if h.port else "")
                      for h in hosts).encode("utf-8")
    buf = io.BytesIO()
    tar = tarfile.open(fileobj=buf, mode="w")
    for name, data in (("dredge.py", source), ("servers", listing)):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    tar.close()
    return base64.b64encode(buf.getvalue())


def relay_command(args):
    """The one script B is sent: unpack, run, hand back, clean up."""
    remote = " ".join(shlex.quote(a) for a in relay_remote_argv(args))
    return _RELAY_SCRIPT % {
        "spool": shlex.quote(args.relay_dir),
        "keep": "1" if args.keep_relay else "",
        "python": shlex.quote(args.relay_python or ""),
        "mark": shlex.quote(args.mark_token + MARK_TAR),
        "args": remote,
    }


def _relay_sweep(args, relay):
    """Take the spool off B, whatever happened to the run that made it.

    The trap on the far side covers an ordinary end and an ordinary
    signal.  It does not cover the one that matters here: `--timeout`
    kills the session with SIGKILL, and a killed shell runs no trap.  A
    known path rather than a `mktemp -d` is what makes this possible at
    all -- there is something to name.
    """
    if args.keep_relay:
        return
    try:
        p = subprocess.Popen(
            ssh_argv(args, relay, "rm -rf %s" % shlex.quote(args.relay_dir)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        p.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        # Best effort by definition: the run's answer is already home,
        # and a bastion that cannot be reached to tidy up is not a
        # reason to fail the collection that succeeded.
        pass


class RelayResult(object):
    """What came back through the jump box."""

    __slots__ = ("status", "report", "errors", "files", "bytes", "detail",
                 "trouble")

    def __init__(self):
        self.status = None
        self.report = ""
        self.errors = ""
        self.files = 0
        self.bytes = 0
        self.detail = ""
        # A whole sentence rather than a fragment, for the case where
        # the stream itself said what went wrong. ssh's own account of
        # the same moment is the further cause and the more misleading
        # one -- a jump box that answered with a banner *did* answer,
        # and the closed pipe that follows is this side's doing.
        self.trouble = ""


def _unpack_relay(args, stream, rr):
    """Land B's tar here: the collection into -d, the rest in hand."""
    found, noise, rest = skip_to_tar(
        stream, args.mark_token, time.monotonic() + args.timeout)
    if not found:
        if noise:
            rr.trouble = ("the jump box answered, but not with a "
                          "collection: %s" % noise[0])
            rr.detail = noise[0]
        else:
            # Nothing came back at all, which is not the stream's
            # account of anything: ssh's is the only one there is.
            rr.detail = "the jump box sent nothing back"
        return
    try:
        tar = tarfile.open(fileobj=_PushedBack(rest, stream), mode="r|*")
    except tarfile.ReadError:
        rr.detail = "the jump box sent nothing back"
        return
    try:
        for member in tar:
            if not member.isfile():
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            name = _clean_relpath(member.name)
            if name == "status":
                raw = fh.read().decode("ascii", "replace").strip()
                rr.status = int(raw) if raw.isdigit() else raw
            elif name == "report":
                rr.report = fh.read().decode("utf-8", "replace")
            elif name == "errors":
                rr.errors = fh.read().decode("utf-8", "replace")
            elif name.startswith("collect/"):
                _land_relay_file(args, name[len("collect/"):], fh.read(), rr)
    finally:
        try:
            tar.close()
        except Exception:                          # noqa: BLE001 - reported
            pass


def _land_relay_file(args, rel, data, rr):
    """One collected file, or one of the tables, put where A wants it.

    The names B built are the names A would have built -- same tool,
    same tag, same fold -- so the collection lands under `-d` unchanged
    and `grep -l oom *` reads the same as it would have without a jump
    box in the way.  The tables are the exception: they belong wherever
    `--csv` and `--tsv` named, which is a path on A.
    """
    if not rel:
        return
    if rel == "relay.csv" and args.csv is not None:
        _append_table(args.csv, data, header_once=True)
        return
    if rel == "relay.tsv" and args.tsv is not None:
        _append_table(args.tsv, data, header_once=True)
        return
    where = os.path.join(args.dir, rel.replace("/", FLAT_SEP))
    d = os.path.dirname(where)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d)
        except OSError as exc:
            raise LocalWriteError("cannot make %s: %s" % (d, exc))
    tmp = "%s.dredge.%d" % (where, os.getpid())
    try:
        with io.open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, where)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise LocalWriteError("cannot write %s: %s" % (where, exc))
    rr.files += 1
    rr.bytes += len(data)


def _append_table(path, data, header_once=True):
    """Add B's rows to A's table, and its header only if A has none.

    B wrote a table of its own with a header on top.  Appending that
    header into a file that already has one would put a row of column
    names in the middle of the data, where a reader takes it for a
    reading -- so it goes down once, the first time, and is dropped on
    every pass after.
    """
    if path in (None, "-"):
        sys.stdout.write(data.decode("utf-8", "replace"))
        return
    text = data.decode("utf-8", "replace")
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if exists and header_once:
        lines = text.split("\n", 1)
        text = lines[1] if len(lines) > 1 else ""
    if not text:
        return
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def run_relay(args, hosts, relay):
    """One round trip through the jump box, start to finish."""
    rr = RelayResult()
    t0 = time.monotonic()

    def consume(stream, _r):
        _unpack_relay(args, stream, rr)

    result = _run(args, relay, relay_command(args), consume,
                  send=relay_payload(hosts))
    rr.detail = rr.detail or result.detail
    _relay_sweep(args, relay)
    elapsed = time.monotonic() - t0

    if result.outcome != OK and rr.status is None:
        # What came down the stream is believed over what ssh made of
        # the end of it: closing an unreadable stream kills the far side,
        # so ssh's `exit 141` here is this side's own hand reported back
        # as the fault, and it sends the reader to the network for
        # something that is wrong in the login.
        trouble = rr.trouble or ("the jump box itself did not answer: %s"
                                 % (result.detail or "?"))
        sys.stdout.write(
            "%s -- %s via %s\n        %s (%s)\n"
            % (PROG, args.cmd if args.cmd else args.path, relay.name,
               trouble, result.outcome))
        for line in (result.remote_says or [])[:3]:
            sys.stdout.write("        %s\n" % line[:120])
        sys.stdout.flush()
        return 1

    if not args.quiet or rr.status not in (0, None):
        sys.stdout.write("%s -- %d host%s via %s, %d file%s (%s) in %.1fs\n"
                         % (PROG, len(hosts), "" if len(hosts) == 1 else "s",
                            relay.name, rr.files,
                            "" if rr.files == 1 else "s",
                            fmt_bytes(rr.bytes), elapsed))
        # B's own report, verbatim and indented: it is the report of the
        # run that actually happened, and rewriting it here would be one
        # more place for the two to disagree.
        for line in rr.report.splitlines():
            sys.stdout.write("  %s\n" % line if line else "\n")
        for line in rr.errors.splitlines()[:10]:
            sys.stdout.write("  ! %s\n" % line[:150])
        sys.stdout.flush()
    if rr.status is None:
        sys.stdout.write("%s: %s\n"
                         % (PROG, rr.trouble or "the jump box ran but sent "
                                                "no exit status back"))
        return 1
    return 1 if rr.status else 0


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
    p.add_argument("--suffix", metavar="EXT", default=_env("SUFFIX"),
                   help="put EXT on the end of every file this run "
                        "creates; a bare word gains a dot")
    p.add_argument("-S", "--server", dest="server", action="append",
                   metavar="TOKEN")
    p.add_argument("--servers", dest="servers",
                   action="append", metavar="FILE")
    p.add_argument("-d", "--dir", default=_env("DIR"))
    p.add_argument("--head", type=int, metavar="N")
    p.add_argument("--tail", type=int, metavar="N")
    p.add_argument("--since", metavar="T")
    p.add_argument("--append", action="store_true")
    p.add_argument("--prepend", action="store_true")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--mark", action="store_true")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--every", metavar="T", default=_env("EVERY"))
    p.add_argument("--passes", type=int, default=0, metavar="N")
    p.add_argument("--state", metavar="FILE", default=_env("STATE"))
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
    p.add_argument("--tsv", nargs="?", const="-", metavar="PATH",
                   default=_env("TSV"),
                   help="append a row per host to a TSV: the time of the "
                        "pass, the host, then the variables its command "
                        "printed")
    p.add_argument("--parse", metavar="HOW", default=_env("PARSE", "kv"),
                   choices=("kv", "json", "row", "values"),
                   help="the shape the command prints its variables in: "
                        "kv (name=value, the default), json, row (a "
                        "header line then a values line), or values "
                        "(bare values, named by --columns)")
    p.add_argument("--columns", metavar="A,B,C", default=_env("COLUMNS"),
                   help="name the variable columns, in order; required "
                        "by --parse values and optional elsewhere, where "
                        "it pins the header rather than taking it from "
                        "what the fleet happened to say")
    p.add_argument("--relay", metavar="HOST", default=_env("RELAY"),
                   help="a jump box: send this file to HOST, run the whole "
                        "collection from there, and bring it back")
    p.add_argument("--relay-dir", metavar="DIR",
                   default=_env("RELAY_DIR"),
                   help="where the copy and the collection live on the "
                        "jump box while the run lasts (default: a path "
                        "under /var/tmp named for this run)")
    p.add_argument("--relay-python", metavar="PY",
                   default=_env("RELAY_PYTHON"),
                   help="the python to run it with on the jump box "
                        "(default: python3, then python)")
    p.add_argument("--keep-relay", action="store_true",
                   help="leave the spool on the jump box instead of "
                        "removing it -- for looking at what went wrong")
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
    if args.tsv is not None:
        # The table is a row of *variables*, and a file has none: it has
        # bytes. What produces named values is a command, so that is what
        # this is tied to rather than being quietly ignored for a path.
        if not args.cmd:
            die("--tsv makes a table out of the variables a command "
                "prints, and a PATH has none -- use --cmd to run "
                "something that prints them")
        args.columns = [c.strip() for c in args.columns.split(",")
                        if c.strip()] if args.columns else []
        for name in args.columns:
            if not VAR_NAME_RE.match(name):
                die("--columns has a name a column cannot have: %r "
                    "(letters, digits, _ . -)" % name)
        if args.parse == "values" and not args.columns:
            die("--parse values is bare values in a fixed order and "
                "nothing in them says which is which: name them with "
                "--columns a,b,c")
        for name in args.columns:
            if name in TSV_META:
                die("--columns cannot name %r: the table already has that "
                    "column, in front of the variables" % name)
        # --follow brings back what was *added* since the last pass, and
        # a row is every variable this pass, not the ones that moved.
        # Under --daemon the two would silently disagree -- a pass where
        # nothing changed would write no row at all, leaving a gap in the
        # series that looks exactly like a host that was down.
        if args.follow:
            die("--tsv wants the whole answer every pass and --follow "
                "brings back only what was added since the last one: a "
                "pass where nothing changed would write no row, which "
                "reads as a host that was down.\n"
                "       --daemon --cmd with --tsv repeats the command "
                "instead, which is what a time series of variables "
                "wants.")
    else:
        args.columns = []
    if args.suffix is not None:
        cleaned = _clean_suffix(args.suffix)
        if not cleaned:
            die("--suffix has nothing in it that can go in a filename: %r"
                % args.suffix)
        args.suffix = cleaned
    if args.head and args.tail:
        die("--head and --tail are opposite ends of the same file: pick one")
    if len([f for f in (args.append, args.prepend, args.replace) if f]) > 1:
        die("--append, --prepend and --replace are three ways of landing "
            "the same bytes: pick one")
    # A daemon that re-fetched every file in full every five minutes would
    # be a denial of service against the fleet it is watching, so the
    # timer brings the incremental collection with it.
    if args.daemon and args.tsv is None:
        args.follow = True
    if args.follow and args.head:
        die("--head takes the first N lines of a file, which never change, "
            "and --follow brings back what was added at the end: they are "
            "opposite ideas.  --tail N says where a first sight starts.")
    # Which end the new bytes land at. Under --follow the local file is
    # the log growing here as it grows there, so appending is the default
    # there and replacing is the default everywhere else.
    if args.append:
        args.mode = "append"
    elif args.prepend:
        args.mode = "prepend"
    elif args.replace:
        args.mode = "replace"
    else:
        args.mode = "append" if args.follow else "replace"
    args.append = args.mode == "append"
    args.prepend = args.mode == "prepend"
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
    if args.relay:
        # The marks a --follow run resumes from would live on the jump
        # box, in a spool that is removed when the run ends -- so every
        # pass would be a first sight, and the whole collection would
        # cross the link again each time.  Keeping the spool instead
        # (--keep-relay) makes the marks survive but leaves the fleet's
        # logs on the bastion between runs, which is the one thing a
        # bastion should not be accumulating.
        if args.daemon and args.tsv is None:
            die("--daemon repeats a --follow pass, and --relay cannot "
                "resume one: the marks live in a spool on the jump box "
                "that is removed when the run ends, so every pass would "
                "be a first sight and would carry the whole collection "
                "again.\n"
                "       --daemon --cmd with --tsv repeats the command "
                "instead, and that works through a relay.")
        if args.follow:
            die("--follow resumes from marks, and through --relay those "
                "marks live in a spool on the jump box that is removed "
                "when the run ends -- so every pass would be a first "
                "sight and would carry the whole collection again.\n"
                "       Run --follow from a box that can reach the fleet, "
                "or collect through the relay without it.")
        if args.relay_python and "/" not in args.relay_python:
            die("--relay-python wants the path to a python on the jump "
                "box, got %r" % args.relay_python)
    if args.mark and not (args.append or args.prepend):
        die("--mark writes a line where old meets new, so it needs "
            "--append or --prepend")
    if args.passes < 0:
        die("--passes cannot be negative, got %d (0 means until stopped)"
            % args.passes)
    if args.passes and not (args.daemon or args.every):
        die("--passes counts the passes of a repeating run, so it needs "
            "--daemon or --every")
    args.every = parse_interval(args.every) if args.every else (
        DEFAULT_INTERVAL if args.daemon else None)
    if args.every is not None and args.every <= 0:
        die("--every wants a positive interval, got %g seconds: a pass "
            "that starts the moment the last one ended is not a timer, "
            "it is a loop" % args.every)
    if args.follow and not args.dir:
        die("--follow resumes from where the last pass stopped, and the "
            "marks that say where that was live in the collection "
            "directory -- name one with -d DIR.\n"
            "       Without it every run gets a directory of its own and "
            "there is nothing to resume from.")
    # A run of its own unless the caller named one, so today's collection
    # never lands on top of yesterday's.
    if not args.dir:
        args.dir = default_dir()
    args.since_epoch = parse_when(args.since) if args.since else None
    # Which pass this is, for the report and the CSV. 0 until a repeating
    # run starts counting.
    args.passno = 0
    args.stream_key = None
    args.marks = None
    if args.follow:
        args.state = args.state or os.path.join(args.dir, STATE_NAME)
        args.stream_key = Marks.stream_key(args)
    # Drawn fresh per run and never from the payload: a frame the far side
    # could guess is a frame the far side could forge.
    args.mark_token = "===dredge-%016x" % random.getrandbits(64)
    if args.relay and not args.relay_dir:
        # Named rather than mktemp'd, so that a run killed by --timeout
        # still leaves something this side knows how to remove.
        args.relay_dir = "/var/tmp/%s-%s" % (RELAY_SPOOL,
                                             args.mark_token[-16:])

    hosts = collect_hosts(args)

    if args.dry_run:
        if args.relay:
            sys.stdout.write("# %d host(s) via %s: %s\n"
                             % (len(hosts), args.relay,
                                " ".join(h.name for h in hosts[:8])
                                + (" ..." if len(hosts) > 8 else "")))
            sys.stdout.write("# on the jump box, over ssh (this file and "
                             "the server list arrive on stdin):\n%s"
                             % relay_command(args))
            return 0
        if args.cmd and args.follow:
            cmd = remote_cmd_follow_command(args, args.mark_token, None)
        elif args.cmd:
            cmd = remote_cmd_command(args, args.mark_token)
        elif args.follow:
            # Per host under --follow: the marks that go over are this
            # host's own. An empty table is what the first pass sends.
            cmd = remote_follow_command(args, args.mark_token, {})
        elif args.head or args.tail:
            cmd = remote_slice_command(args, args.since_epoch,
                                       args.mark_token)
        else:
            cmd = remote_tar_command(args, args.since_epoch,
                                     args.mark_token)
        sys.stdout.write("# %d host(s): %s\n"
                         % (len(hosts),
                            " ".join(h.name for h in hosts[:8])
                            + (" ..." if len(hosts) > 8 else "")))
        sys.stdout.write("# on each, over ssh:\n%s" % cmd)
        return 0

    if args.follow:
        # Read after the dry run, which contacts nothing and should not
        # refuse to run because somebody else is holding the marks.
        args.marks = Marks.load(args.state)

    if args.relay:
        # One host on this side, however many there are on the other: the
        # fan-out is B's to do, and A's connection count is one.
        relay = parse_host_token(args.relay)
        if args.every:
            return run_repeating(args, hosts, relay=relay)
        args.passno = 1
        return run_relay(args, hosts, relay)

    if args.every:
        return run_repeating(args, hosts)

    args.passno = 1
    results, elapsed = one_pass(args, hosts)
    if args.csv is not None:
        write_csv(results, args, args.csv)
    # Before the report, because parsing is where a host stops having a
    # row and starts having a finding, and the report is what says so.
    if args.tsv is not None:
        write_tsv(results, args)
    report_pass(args, results, elapsed)
    # A ceiling that was hit, or a name that was refused, means what came
    # back is not what was asked for -- which is the definition of
    # something worth seeing, and worth stopping a script over.
    return 1 if pass_failed(args, results) else 0


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
# solving it differently here would mean `dredge --servers hosts.txt` and
# `agree --servers hosts.txt` disagreeing about what that file says.
