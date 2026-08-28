#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""muster.py -- hand out work from a pool, once each, and know what is left.

Usage: muster add hosts.txt                 put items in the pool
       muster add 'web01 web02,web03'       or a delimited list, quoted
       muster add 'web[01-40]'              ranges expand, as everywhere else
       muster take 10 -o mine.txt           lease 10 items, write the ticket
       muster take --item web07             lease one by name
       muster done mine.txt                 the work is finished on those
       muster release mine.txt              give them back, unfinished
       muster status                        what is done, held, left, stuck
       muster status --csv                  the same as one row of numbers
       muster list --state available        the rows, as CSV

Options:
      --pool FILE     the pool to work in          (MUSTER_POOL,
                      default muster.csv here)
      --lease DUR     how long a take holds for    (MUSTER_LEASE, default 1h)
      --as WHO        holder name to record        (MUSTER_AS,
                      default user@host:pid)
  -o, --output FILE   write the ticket here        (default stdout)
      --item NAME     name items; repeatable, and
                      itself a delimited list       (take/done/release/reset)
      --state STATE   filter: available, held, done, any     (list)
      --csv           one summary row instead of the page:      (status)
                      pool,total,done,held,available,done_pct,stuck
      --lock-timeout S  how long to wait for the pool lock   (default 30)
      --stale-lock S  break a lock older than this           (default 120)
      --quiet         no summary, just the data

What it does
  Some jobs are a list and a promise: forty hosts to patch, nine hundred
  files to re-encode, every switch in a rack to walk up to.  The work is
  handed out to whoever is free, it must happen once each, and the thing
  that actually goes wrong is not the work -- it is the bookkeeping.  Two
  people take the same host.  Somebody's laptop shuts and eleven items
  are held by nobody, forever.  At the end nobody can say which twelve of
  the forty are left, so the whole list gets re-walked to be sure.

  This is the bookkeeping, and nothing else.  It does not do the work, it
  does not know what the work is, and it never touches the items -- they
  are strings to it, hostnames or filenames or ticket numbers.  It only
  answers: who has what, for how long, and what is still outstanding.

The lease is the whole idea
  `take` marks items held by you until a deadline.  If you finish, `done`
  closes them.  If you do not -- the job died, the laptop shut, you went
  home -- the lease simply runs out and the items are available again.
  Nothing has to notice this and nothing has to run: an expired lease is
  not a lease, and every command works that out from the timestamps as it
  opens the pool.  There is no daemon, no reaper and no cleanup step.

  A lease that has to be reclaimed is not a silent event.  `attempts`
  counts every time an item was taken, so an item taken four times and
  finished none of them is the thing `status` puts in front of you: that
  is not a scheduling problem, it is a host nobody can actually patch.

Finishing late
  If your lease lapsed while you were working, someone else may already
  hold the item.  Reporting it done is still accepted -- the work did
  happen, and refusing it would send the pool out to have it done twice
  more -- but it is reported as a CONFLICT naming both holders and how
  late it was, because two people on one host is exactly what the lease
  existed to prevent.  The fix is nearly always a longer --lease, and
  `status` says so when it sees leases expiring mid-run.

Sharing a pool between machines
  The pool is one file; put it on a shared filesystem and workers on any
  number of hosts draw from it.  Locking is an O_EXCL sentinel beside the
  pool rather than flock, because flock over NFS is not dependable, and
  every write is a temporary file renamed into place, so a reader never
  sees half a pool.  A lock whose holder died is broken after
  --stale-lock seconds rather than blocking the fleet forever.

  Leases are wall-clock deadlines, so they assume the workers roughly
  agree about the time.  If they do not, leases expire early on the fast
  box and late on the slow one -- `skew` is the tool for that question.

Item names, and how a list of them is written
  A newline, a space and a comma all separate one item from the next, so
  a file with one per line, `muster add 'web01 web02'` and `muster add
  web01,web02` are the same command.  That works because **an item name
  never contains whitespace or a comma** -- those are the delimiters,
  which is also what makes a ticket unambiguously one item per line.

  Commas are split outside brackets only, so `node[1,3,5]` is still one
  range.  A space inside a range is refused rather than half-expanded:
  `web[01-04, 06]` would otherwise leave a literal item called
  `web[01-04,` for somebody to find weeks later.

  Everything else is fine -- a name may hold dots, colons, slashes or
  non-ASCII, so hostnames, paths and ticket numbers all work.

Conventions
  The pool is a CSV you can read, diff, and put in git:

      item,state,holder,lease_id,taken_ts,expires_ts,done_ts,attempts,note

  A ticket is one item per line, so it is already the input to whatever
  does the work -- `for h in $(grep -v '^#' mine.txt); do ...` -- with the
  lease recorded in comment lines above it, which `done` reads to tell
  your completion from somebody else's.  A hand-written list of item
  names works too; it is only the conflict detection that gets quieter.

Exit status
  0   nothing wrong
  1   something worth seeing: a conflict, an unknown item, a stuck item
  2   usage error, or the pool could not be locked
"""

import argparse
import csv
import errno
import io
import math
import os
import random
import re
import socket
import sys
import time
from datetime import datetime, timedelta

VERSION = "0.4.0"
PROG = os.path.basename(sys.argv[0]) or "muster.py"

DEFAULT_POOL = "muster.csv"
DEFAULT_LEASE = "1h"
DEFAULT_LOCK_TIMEOUT = 30.0
DEFAULT_STALE_LOCK = 120.0

AVAILABLE, HELD, DONE = "available", "held", "done"
STATES = (AVAILABLE, HELD, DONE)

FIELDS = ["item", "state", "holder", "lease_id", "taken_ts", "expires_ts",
          "done_ts", "attempts", "note"]

# An item is a line in a ticket file, so it cannot contain a newline; it
# is a CSV cell, so a control character in it would come back as
# something else.  Everything else -- spaces, commas, unicode -- is fine.
BAD_ITEM_RE = re.compile(r"[\x00-\x1f\x7f]")

# canonical copy: binnacle/agree.py RANGE_RE.
RANGE_RE = re.compile(r"\[([^\]]+)\]")


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    An item is whatever the caller put in the pool, which on a fleet with
    non-ASCII hostnames or filenames is not spellable under LANG=C -- and
    losing the whole status page to one of them would be absurd.
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
    v = os.environ.get("MUSTER_" + name)
    return v if v not in (None, "") else default


def note(msg, quiet=False):
    if not quiet:
        sys.stderr.write("[%s] %s\n" % (PROG, msg))


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
# Time
# ---------------------------------------------------------------------------

def _float_or_die(num, text):
    """A run of digits and dots is not always a number: "." and "1.2.3"
    both get this far and both used to raise a bare ValueError, which is
    a traceback where a one-line refusal belongs."""
    try:
        return float(num)
    except ValueError:
        die("bad duration: %s" % text)


def parse_duration(text):
    """'45m', '2h', '90', '1h30m' -> seconds.  A bare number is seconds."""
    s = str(text).strip().lower()
    if not s:
        die("empty duration")
    total, num, seen = 0.0, "", False
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in units:
            if not num:
                die("bad duration: %s" % text)
            total += _float_or_die(num, text) * units[ch]
            num, seen = "", True
        else:
            die("bad duration: %s" % text)
    if num:
        total += _float_or_die(num, text)
        seen = True
    if not seen:
        die("bad duration: %s" % text)
    if total <= 0:
        die("duration must be more than zero: %s" % text)
    return total


def fmt_secs(sec):
    sec = int(sec)
    if sec < 0:
        return "-" + fmt_secs(-sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd%02dh" % (sec // 86400, (sec % 86400) // 3600)


def fmt_ts(ts):
    if not ts:
        return "-"
    # Not utcfromtimestamp: deprecated from 3.12, and the epoch plus a
    # timedelta means the same thing on every version back to the floor.
    base = datetime(1970, 1, 1) + timedelta(seconds=int(ts))
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


def whoami():
    user = (os.environ.get("USER") or os.environ.get("LOGNAME") or "?")
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "?"
    return "%s@%s:%d" % (user, host, os.getpid())


def new_lease_id():
    # Short, and unique enough across a fleet: the holder is recorded
    # beside it, so this only has to distinguish one take from the next.
    return "%08x" % random.getrandbits(32)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
#
# An O_EXCL sentinel beside the pool, not flock: this pool is meant to be
# shared between machines over NFS, where flock is advisory at best and
# silently useless at worst, while O_EXCL create is the one primitive NFS
# has always had to get right.  The token inside is checked before the
# lock is released, so a lock that was broken out from under us is never
# deleted while its new owner holds it.

class PoolLock(object):
    def __init__(self, path, timeout, stale, quiet=False):
        self.path = path + ".lock"
        self.timeout = float(timeout)
        self.stale = float(stale)
        self.quiet = quiet
        self.token = "%s %s" % (whoami(), new_lease_id())
        self.held = False

    def _try_acquire(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            die("cannot lock %s: %s" % (self.path, exc))
        try:
            os.write(fd, ("%s\n%d\n" % (self.token, int(time.time()))).encode())
        finally:
            os.close(fd)
        self.held = True
        return True

    def _age(self):
        try:
            return time.time() - os.stat(self.path).st_mtime
        except OSError:
            return None

    def __enter__(self):
        deadline = time.time() + self.timeout
        broke = False
        while True:
            if self._try_acquire():
                return self
            age = self._age()
            if age is not None and age > self.stale and not broke:
                # The holder is gone: a worker killed mid-write, or a box
                # that went away.  Break it once -- the O_EXCL create is
                # still what decides the winner, so several breakers
                # racing here cannot both end up holding it.
                note("breaking a stale lock, %s old" % fmt_secs(age),
                     self.quiet)
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                broke = True
                continue
            if time.time() >= deadline:
                die("could not lock %s within %s (held %s)"
                    % (self.path, fmt_secs(self.timeout),
                       fmt_secs(age) if age is not None else "?"))
            time.sleep(0.05 + random.random() * 0.15)

    def __exit__(self, exc_type, exc, tb):
        if not self.held:
            return False
        try:
            with io.open(self.path, encoding="utf-8") as fh:
                if fh.readline().strip() != self.token:
                    # Ours was broken and somebody else owns this file
                    # now.  Deleting it would hand the pool to a third.
                    return False
        except OSError:
            return False
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

class Item(object):
    __slots__ = tuple(FIELDS)

    def __init__(self, item, state=AVAILABLE, holder="", lease_id="",
                 taken_ts="", expires_ts="", done_ts="", attempts="0",
                 note=""):
        self.item = item
        self.state = state
        self.holder = holder
        self.lease_id = lease_id
        self.taken_ts = taken_ts
        self.expires_ts = expires_ts
        self.done_ts = done_ts
        self.attempts = attempts
        self.note = note

    def row(self):
        return dict((f, getattr(self, f)) for f in FIELDS)

    def expired(self, now):
        if self.state != HELD:
            return False
        # A held row carrying no deadline is not a lease either: nothing
        # could ever free it, so the item would be held forever and the
        # report would print the epoch as a countdown. Hand-edited pools
        # and half-written rows are where these come from.
        if not self.expires_ts:
            return True
        return _num(self.expires_ts) <= now

    def free(self):
        self.state = AVAILABLE
        self.holder = ""
        self.lease_id = ""
        self.taken_ts = ""
        self.expires_ts = ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_pool(path):
    """The pool as it stands, or an empty one if the file is not there."""
    if not os.path.exists(path):
        return []
    items = []
    dupes = []
    try:
        with io.open(path, encoding="utf-8-sig", newline="",
                     errors="replace") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return []
            missing = [f for f in ("item", "state") if f not in reader.fieldnames]
            if missing:
                die("%s is not a muster pool (no %s column)"
                    % (path, ", ".join(missing)))
            seen = set()
            for row in reader:
                name = (row.get("item") or "").strip()
                if not name:
                    continue
                if name in seen:
                    dupes.append(name)
                seen.add(name)
                items.append(Item(
                    name,
                    state=(row.get("state") or AVAILABLE).strip() or AVAILABLE,
                    holder=row.get("holder") or "",
                    lease_id=row.get("lease_id") or "",
                    taken_ts=row.get("taken_ts") or "",
                    expires_ts=row.get("expires_ts") or "",
                    done_ts=row.get("done_ts") or "",
                    attempts=row.get("attempts") or "0",
                    note=row.get("note") or ""))
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))
    if dupes:
        # add() cannot make these and every write is under the lock, so a
        # duplicate means the file was edited or merged by hand. Two rows
        # for one item disagree about its state, every count is wrong,
        # and only one of them would ever be updated again -- so this is
        # worth stopping for rather than guessing which row is real.
        die("%s names %s twice: %s\n"
            "%s: two rows for one item disagree about its state; keep one"
            % (path, "an item" if len(set(dupes)) == 1 else "items",
               ", ".join(sorted(set(dupes))[:5]), PROG))
    return items


def write_pool(path, items):
    """Replace the pool atomically, so a reader never sees half of it."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with _WriteGuard(tmp):
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            # lineterminator is explicit: csv defaults to CRLF, and this
            # file is meant to be read, diffed and committed.
            writer = csv.DictWriter(fh, fieldnames=FIELDS,
                                    lineterminator="\n")
            writer.writeheader()
            for it in items:
                writer.writerow(it.row())
            fh.flush()
            os.fsync(fh.fileno())
    try:
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die("cannot replace %s: %s" % (path, exc))


def reclaim(items, now):
    """Expired leases are not leases.  Returns the items that went back.

    Nothing runs to make this happen; it is worked out from the deadlines
    every time the pool is opened, which is why there is no daemon.
    """
    back = []
    for it in items:
        if it.expired(now):
            it.note = "lease from %s expired" % (it.holder or "?")
            it.free()
            back.append(it)
    return back


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

TICKET_LEASE_RE = re.compile(r"^#\s*lease\s+([0-9a-f]+)\b")
TICKET_POOL_RE = re.compile(r"^#\s*pool\s+(.+?)\s*$")


def render_ticket(items, pool, holder, lease_id, expires_ts):
    out = [
        "# muster ticket -- %d item(s)" % len(items),
        "# pool %s" % os.path.abspath(pool),
        "# taken %s by %s" % (fmt_ts(time.time()), holder),
        "# lease %s expires %s" % (lease_id, fmt_ts(expires_ts)),
        "#",
        "# One item per line. Comments are ignored when this comes back",
        "# through `muster done`; the lease line is how a completion is",
        "# told from somebody else's.",
    ]
    out.extend(it.item for it in items)
    return "\n".join(out) + "\n"


def read_ticket(path):
    """(items, lease_id) from a ticket -- or from a plain list of names."""
    lease = ""
    items = []
    try:
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n").rstrip("\r")
                if line.startswith("#"):
                    m = TICKET_LEASE_RE.match(line)
                    if m:
                        lease = m.group(1)
                    continue
                items.extend(names_in(line))
    except OSError as exc:
        die("cannot read %s: %s" % (path, exc))
    return items, lease


def wanted_items(args, verb):
    """The items a verb was pointed at: --item flags, or a ticket file."""
    if args.item:
        if args.ticket:
            die("give either a ticket file or --item, not both")
        # --item takes a delimited list too, so `--item a,b` and
        # `--item a --item b` are the same.
        return names_in(" ".join(args.item)), ""
    if not args.ticket:
        die("%s needs a ticket file or --item NAME" % verb)
    return read_ticket(args.ticket)


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def _open(args):
    """Lock the pool, read it, and expire what has run out.

    The caller releases the lock in its own finally, but that finally is
    not in force until this has returned -- so anything that fails in
    here has to hand the lock back itself. It used to not, and a pool
    that would not parse left its lock behind: every later run then
    waited out --stale-lock before it could do anything, and on a shared
    pool that is the whole fleet stopped by one bad row.
    """
    lock = PoolLock(args.pool, args.lock_timeout, args.stale_lock, args.quiet)
    lock.__enter__()
    try:
        items = read_pool(args.pool)
        freed = reclaim(items, time.time())
    except BaseException:
        lock.__exit__(None, None, None)
        raise
    return lock, items, freed


def cmd_add(args):
    names = []
    for source in args.source:
        if source == "-":
            names.extend(names_in(sys.stdin.read()))
        elif os.path.isfile(source):
            try:
                with io.open(source, encoding="utf-8", errors="replace") as fh:
                    names.extend(names_in(fh.read()))
            except OSError as exc:
                die("cannot read %s: %s" % (source, exc))
        elif os.path.isdir(source):
            die("%s is a directory, not a list of items" % source)
        else:
            # Not a file, so it is a name or a range.  A typo'd path
            # would otherwise be silently added as an item called
            # "./hosts.tzt", which is the kind of thing found weeks later.
            if os.sep in source or source.endswith((".txt", ".csv", ".lst")):
                die("no such file: %s" % source)
            names.extend(names_in(source))

    for name in names:
        if BAD_ITEM_RE.search(name):
            die("item contains a control character: %r" % name)
    if not names:
        die("nothing to add", 1)

    lock, items, freed = _open(args)
    try:
        have = set(it.item for it in items)
        added, dup = 0, 0
        for name in names:
            if name in have:
                dup += 1
                continue
            have.add(name)
            items.append(Item(name))
            added += 1
        if added or freed:
            write_pool(args.pool, items)
    finally:
        lock.__exit__(None, None, None)

    note("added %d item(s) to %s%s"
         % (added, args.pool,
            ", %d already there" % dup if dup else ""), args.quiet)
    return 0


# A comment opens the line or follows whitespace.  Splitting on a bare
# "#" instead turned `file#1` and `file#2` into two copies of `file`:
# one item where there were two, and the other silently never worked on.
COMMENT_RE = re.compile(r"(?:^|\s)#")


def names_in(text):
    """Every item named in TEXT, however it was delimited.

    A newline, a space and a comma all separate one item from the next,
    so a file with one per line, `muster add 'web01 web02'` and
    `muster add web01,web02` all mean the same thing.  That works
    because an item name can never contain whitespace or a comma: they
    are the delimiters, which is also what makes a ticket unambiguously
    one item per line.

    Commas are split outside brackets only, so `node[1,3,5]` stays one
    range -- but a space inside a range would be cut by the whitespace
    split first, and the leftover would otherwise become a literal item
    named `web[01-04,`.  That is refused rather than added.
    """
    out = []
    for line in text.splitlines():
        m = COMMENT_RE.search(line)
        if m:
            line = line[:m.start()]
        for token in line.split():
            for spec in split_commas(token):
                if spec.count("[") != spec.count("]"):
                    die("unbalanced [ ] in %r -- a range cannot contain "
                        "spaces" % spec)
                out.extend(expand_range(spec))
    return out


def cmd_take(args):
    if args.item and args.count is not None:
        die("give either a count or --item, not both")
    if args.count is not None and args.count < 1:
        die("take how many? %s is not a count" % args.count)

    # -o and --pool sit next to each other and both take a filename, so
    # `--pool p.csv -o p.csv` is one keystroke away -- and it used to
    # report "took 5 item(s)" while the ticket overwrote the pool, taking
    # every item and every completion with it. The same typo against the
    # lock file jammed the pool until --stale-lock ran out. Checked
    # before anything is leased, so a refusal costs nothing.
    if args.output:
        out = os.path.realpath(args.output)
        pool = os.path.realpath(args.pool)
        if out == pool:
            die("the ticket would overwrite the pool itself: %s"
                % args.output)
        if out == pool + ".lock":
            die("the ticket would overwrite the pool's lock file: %s"
                % args.output)

    lease_secs = parse_duration(args.lease)
    holder = args.holder or whoami()
    now = time.time()
    lease_id = new_lease_id()
    # Rounded up, not truncated. int() threw away the fraction, so every
    # lease came out up to a second shorter than it was asked for -- a
    # 1s lease measured 0.93s here. Short is the dangerous direction: a
    # lease that ends early hands the item to somebody else while the
    # first worker is still on it, which is the duplicate work this tool
    # exists to prevent. Up to a second long costs nothing.
    expires = int(math.ceil(now + lease_secs))

    lock, items, freed = _open(args)
    try:
        by_name = dict((it.item, it) for it in items)
        taken, refused = [], []
        if args.item:
            for name in names_in(" ".join(args.item)):
                it = by_name.get(name)
                if it is None:
                    refused.append((name, "not in the pool"))
                elif it.state == DONE:
                    refused.append((name, "already done"))
                elif it.state == HELD:
                    refused.append((name, "held by %s until %s"
                                    % (it.holder, fmt_ts(it.expires_ts))))
                else:
                    taken.append(it)
        else:
            want = args.count if args.count is not None else 1
            for it in items:
                if len(taken) >= want:
                    break
                if it.state == AVAILABLE:
                    taken.append(it)

        for it in taken:
            it.state = HELD
            it.holder = holder
            it.lease_id = lease_id
            it.taken_ts = "%d" % int(now)
            it.expires_ts = "%d" % expires
            it.attempts = "%d" % (int(_num(it.attempts)) + 1)
            it.note = ""
        if taken or freed:
            write_pool(args.pool, items)
        available_left = sum(1 for it in items if it.state == AVAILABLE)
    finally:
        lock.__exit__(None, None, None)

    if freed:
        note("%d expired lease(s) went back in the pile" % len(freed),
             args.quiet)

    ticket = render_ticket(taken, args.pool, holder, lease_id, expires)
    if args.output:
        try:
            with io.open(args.output, "w", encoding="utf-8") as fh:
                fh.write(ticket)
        except OSError as exc:
            # The pool is already written, so the items are leased to a
            # holder who has no record of which ones -- unreachable until
            # the lease runs out, which is the whole hour by default.
            # A full disk or a typo'd -o is enough to hit this, so hand
            # them straight back rather than stranding them.
            back = give_back(args, lease_id)
            die("cannot write %s: %s\n"
                "%s: the %d item(s) taken were put back -- a lease nobody "
                "can read is worse than no lease" % (args.output, exc,
                                                     PROG, back))
    else:
        sys.stdout.write(ticket)

    for name, why in refused:
        note("not taken: %s -- %s" % (name, why), False)
    if taken:
        note("took %d item(s), lease %s for %s, %d still available"
             % (len(taken), lease_id, fmt_secs(lease_secs), available_left),
             args.quiet)
    else:
        note("took nothing: the pool has no available items", args.quiet)
    return 1 if refused else 0


def give_back(args, lease_id):
    """Free every item still held under LEASE_ID.  Returns how many.

    Used when a take succeeded in the pool but the ticket could not be
    written: the lease exists and nobody can act on it.
    """
    lock = PoolLock(args.pool, args.lock_timeout, args.stale_lock, True)
    lock.__enter__()
    try:
        items = read_pool(args.pool)
        freed = 0
        for it in items:
            if it.state == HELD and it.lease_id == lease_id:
                it.note = "ticket could not be written; put back"
                it.free()
                freed += 1
        if freed:
            write_pool(args.pool, items)
        return freed
    finally:
        lock.__exit__(None, None, None)


def _close(args, verb):
    """done and release: the same walk, a different ending."""
    names, lease = wanted_items(args, verb)
    if not names:
        die("%s: the ticket names no items" % verb, 1)
    now = time.time()

    lock, items, freed = _open(args)
    findings = []
    try:
        by_name = dict((it.item, it) for it in items)
        changed = 0
        for name in names:
            it = by_name.get(name)
            if it is None:
                findings.append(("UNKNOWN", name,
                                 "not in %s" % os.path.basename(args.pool)))
                continue
            if verb == "done" and it.state == DONE:
                findings.append(("ALREADY", name, "was already done at %s"
                                 % fmt_ts(it.done_ts)))
                continue

            mine = (not lease) or (it.lease_id == lease)
            if verb == "done":
                if not mine and it.state == HELD:
                    late = now - _num(it.taken_ts)
                    findings.append((
                        "CONFLICT", name,
                        "held by %s since %s -- your lease had lapsed; "
                        "marked done anyway, their work is duplicate"
                        % (it.holder, fmt_ts(it.taken_ts))))
                    it.note = "conflict: also held by %s for %s" % (
                        it.holder, fmt_secs(late))
                elif not mine and it.state == AVAILABLE:
                    findings.append((
                        "LATE", name,
                        "your lease had already expired -- nobody else had "
                        "taken it, so nothing was done twice"))
                    it.note = "completed after its lease expired"
                it.state = DONE
                it.done_ts = "%d" % int(now)
                it.holder = args.holder or it.holder or whoami()
                it.lease_id = ""
                it.expires_ts = ""
                changed += 1
            else:
                if it.state == DONE:
                    findings.append(("ALREADY", name,
                                     "is done; release does not undo that"))
                    continue
                if not mine and it.state == HELD:
                    findings.append((
                        "CONFLICT", name,
                        "is held by %s, not by your lease -- left alone"
                        % it.holder))
                    continue
                it.note = ""
                it.free()
                changed += 1
        if changed or freed:
            write_pool(args.pool, items)
        left = sum(1 for it in items if it.state != DONE)
        total = len(items)
    finally:
        lock.__exit__(None, None, None)

    for kind, name, why in findings:
        sys.stderr.write("  %-9s %s %s\n" % (kind, name, why))
    note("%s %d item(s); %d of %d still outstanding"
         % ("completed" if verb == "done" else "released",
            changed, left, total), args.quiet)
    if any(k == "CONFLICT" for k, _, _ in findings):
        note("a longer --lease is nearly always the fix for a CONFLICT",
             args.quiet)
    return 1 if findings else 0


def cmd_done(args):
    return _close(args, "done")


def cmd_release(args):
    return _close(args, "release")


def cmd_reset(args):
    """Put items back regardless of state -- including completed ones."""
    names, _ = wanted_items(args, "reset")
    lock, items, freed = _open(args)
    try:
        by_name = dict((it.item, it) for it in items)
        missing = [n for n in names if n not in by_name]
        changed = 0
        for name in names:
            it = by_name.get(name)
            if it is None:
                continue
            it.done_ts = ""
            it.note = "reset by %s" % (args.holder or whoami())
            it.free()
            changed += 1
        if changed or freed:
            write_pool(args.pool, items)
    finally:
        lock.__exit__(None, None, None)
    for name in missing:
        sys.stderr.write("  %-9s %s not in the pool\n" % ("UNKNOWN", name))
    note("reset %d item(s) to available" % changed, args.quiet)
    return 1 if missing else 0


def cmd_list(args):
    lock, items, freed = _open(args)
    try:
        if freed:
            write_pool(args.pool, items)
    finally:
        lock.__exit__(None, None, None)
    wanted = args.state
    rows = [it for it in items if wanted == "any" or it.state == wanted]
    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS,
                            lineterminator="\n")
    writer.writeheader()
    for it in rows:
        writer.writerow(it.row())
    return 0


SUMMARY_FIELDS = ["pool", "total", "done", "held", "available",
                  "done_pct", "stuck"]


def summary_row(items, pool):
    total = len(items)
    done = sum(1 for it in items if it.state == DONE)
    held = sum(1 for it in items if it.state == HELD)
    return {
        "pool": os.path.basename(pool),
        "total": total,
        "done": done,
        "held": held,
        "available": total - done - held,
        # Two decimals rather than the rendered string: this row is for
        # something that will do arithmetic on it, and ">99%" is not a
        # number.  The page above is where the honest rounding lives.
        "done_pct": "%.2f" % (100.0 * done / total) if total else "",
        "stuck": len(_stuck(items)),
    }


def cmd_status(args):
    lock, items, freed = _open(args)
    try:
        if freed:
            write_pool(args.pool, items)
    finally:
        lock.__exit__(None, None, None)
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=SUMMARY_FIELDS,
                                lineterminator="\n")
        writer.writeheader()
        writer.writerow(summary_row(items, args.pool))
        return 1 if _stuck(items) else 0
    sys.stdout.write(render_status(items, freed, args.pool, time.time()))
    # An expired lease going back in the pile is routine and is reported
    # in the page; a stuck item is the thing worth failing a cron job for.
    return 1 if _stuck(items) else 0


def pct_str(done, total):
    """The completed share, never rounded into a lie.

    39 of 40 is not 100% and 1 of 400 is not 0%, but "%.0f%%" says both.
    Either one reports the job finished, or never started, when neither is
    true -- and this number is exactly the one somebody pastes into a
    status mail.  100% is reserved for actually complete.
    """
    if not total:
        return "0%"
    if done >= total:
        return "100%"
    share = 100.0 * done / total
    if share >= 99.5:
        return ">99%"
    if done and share < 0.5:
        return "<1%"
    return "%.0f%%" % share


def _stuck(items, floor=3):
    """Taken repeatedly and never finished.

    Not a scheduling problem: an item that three separate workers picked
    up and none closed is a host that cannot actually be worked on, and
    it will absorb the pool forever unless someone is told about it.
    """
    out = [it for it in items
           if it.state != DONE and int(_num(it.attempts)) >= floor]
    return sorted(out, key=lambda it: (-int(_num(it.attempts)), it.item))


def render_status(items, freed, pool, now):
    total = len(items)
    done = [it for it in items if it.state == DONE]
    held = [it for it in items if it.state == HELD]
    avail = [it for it in items if it.state == AVAILABLE]
    out = []
    out.append("%s -- %s, %d item(s)" % (PROG, os.path.basename(pool), total))
    out.append("")
    if not total:
        out.append("  The pool is empty. Put something in it:")
        out.append("    %s add hosts.txt" % PROG)
        out.append("")
        return "\n".join(out) + "\n"

    out.append("  PROGRESS   %d of %d done (%s), %d held, %d available"
               % (len(done), total, pct_str(len(done), total),
                  len(held), len(avail)))
    dated = [it for it in held if it.expires_ts]
    if dated:
        soon = [it for it in dated if _num(it.expires_ts) - now < 300]
        nearest = min(_num(it.expires_ts) - now for it in dated)
        out.append("  LEASES     %d held, next expires in %s%s"
                   % (len(held), fmt_secs(nearest),
                      ", %d within 5m" % len(soon) if soon else ""))
    elif held:
        # Only reachable if a row was hand-edited between the reclaim and
        # here; say so rather than counting down from the epoch.
        out.append("  LEASES     %d held, none carrying a deadline" % len(held))
    if freed:
        out.append("  RECLAIMED  %d lease(s) had expired and went back in "
                   "the pile" % len(freed))

    holders = {}
    for it in held:
        holders[it.holder] = holders.get(it.holder, 0) + 1
    if holders:
        out.append("")
        out.append("  WHO HAS WHAT")
        for who in sorted(holders, key=lambda w: (-holders[w], w)):
            out.append("    %-28s %d item(s)" % (who, holders[who]))

    stuck = _stuck(items)
    if stuck:
        out.append("")
        out.append("  STUCK (taken and never finished)")
        for it in stuck[:10]:
            out.append("    %-24s %s attempts   last held by %s"
                       % (it.item, it.attempts, it.holder or "-"))
        if len(stuck) > 10:
            out.append("    ... and %d more" % (len(stuck) - 10))

    out.append("")
    out.append("  WHAT TO DO NEXT")
    if stuck:
        out.append("    * %s has been taken %s times and finished none of"
                   % (stuck[0].item, stuck[0].attempts))
        out.append("      them. That is not scheduling -- something about")
        out.append("      that item defeats the work. Look at it by hand:")
        out.append("        %s take --item %s" % (PROG, stuck[0].item))
    if freed:
        out.append("    * %d lease(s) ran out before anyone finished them."
                   % len(freed))
        out.append("      If that keeps happening the lease is shorter than")
        out.append("      the work is, and the pool is handing items out")
        out.append("      twice for no reason:")
        out.append("        %s take 10 --lease 4h" % PROG)
    # One of these always fires: an empty WHAT TO DO NEXT is worse than
    # no section at all, because it reads as "nothing to say" when the
    # answer is usually "take the next batch".
    if avail:
        out.append("    * %d item(s) are waiting%s:"
                   % (len(avail),
                      ", %d already in flight" % len(held) if held else
                      " and nobody holds any"))
        out.append("        %s take 10 -o mine.txt" % PROG)
    elif held:
        out.append("    * Nothing left to hand out; %d still in flight."
                   % len(held))
        out.append("      They come back on their own as the leases run out.")
    else:
        out.append("    * Every item is done. Nothing to do.")
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _common(p, defaults=False):
    """The flags that mean the same thing whatever the verb.

    Added to the top-level parser *and* to every verb, so `muster --pool
    p.csv status` and `muster status --pool p.csv` are the same command.
    Only the top-level copy carries real defaults; the verb's copies are
    SUPPRESS, so giving a flag after the verb overrides one given before
    it, and leaving it out does not overwrite it with a default.
    """
    def d(value):
        return value if defaults else argparse.SUPPRESS
    p.add_argument("--pool", default=d(_env("POOL", DEFAULT_POOL)))
    p.add_argument("--lock-timeout", type=float,
                   default=d(float(_env("LOCK_TIMEOUT",
                                        DEFAULT_LOCK_TIMEOUT))))
    p.add_argument("--stale-lock", type=float,
                   default=d(float(_env("STALE_LOCK", DEFAULT_STALE_LOCK))))
    p.add_argument("--as", dest="holder", default=d(_env("AS")))
    p.add_argument("--quiet", action="store_true", default=d(False))
    return p


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
    _common(p, defaults=True)
    sub = p.add_subparsers(dest="cmd")

    a = _common(sub.add_parser("add", help="put items in the pool"))
    a.add_argument("source", nargs="+", metavar="SOURCE",
                   help="a file, - for stdin, or a name/range")

    t = _common(sub.add_parser("take", help="lease items"))
    t.add_argument("count", nargs="?", type=int, metavar="N")
    t.add_argument("--item", action="append", default=[], metavar="NAME")
    t.add_argument("-o", "--output", metavar="FILE")
    t.add_argument("--lease", default=_env("LEASE", DEFAULT_LEASE))

    d = _common(sub.add_parser("done", help="the work on these is finished"))
    d.add_argument("ticket", nargs="?", metavar="TICKET")
    d.add_argument("--item", action="append", default=[], metavar="NAME")

    r = _common(sub.add_parser("release", help="give items back unfinished"))
    r.add_argument("ticket", nargs="?", metavar="TICKET")
    r.add_argument("--item", action="append", default=[], metavar="NAME")

    z = _common(sub.add_parser("reset", help="force items back to available"))
    z.add_argument("ticket", nargs="?", metavar="TICKET")
    z.add_argument("--item", action="append", default=[], metavar="NAME")

    st = _common(sub.add_parser("status", help="what is done, held and left"))
    st.add_argument("--csv", action="store_true")

    ls = _common(sub.add_parser("list", help="the rows, as CSV"))
    ls.add_argument("--state", default="any",
                    choices=list(STATES) + ["any"])

    sub.add_parser("help", help="every flag of every verb")
    return p, sub


def cmd_full_help(parser, sub):
    """Generated by walking the real subparsers, so it cannot drift from
    what the tool actually accepts."""
    parser.print_help()
    for name, p in sorted(sub.choices.items()):
        if name == "help":
            continue
        print("")
        print("=" * 72)
        p.print_help()
    return 0


VERBS = {"add": cmd_add, "take": cmd_take, "done": cmd_done,
         "release": cmd_release, "reset": cmd_reset, "status": cmd_status,
         "list": cmd_list}


def main(argv=None):
    _stdio_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser, sub = build_parser()
    if argv and argv[0] == "help":
        return cmd_full_help(parser, sub)
    # No verb is the question people actually have -- including when
    # only the shared flags were given, as in `muster --pool p.csv`.
    if not argv:
        argv = ["status"]
    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["status"] + argv)
    return VERBS[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
        # `muster list | head` is a normal way to use this.
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
# It matters more here than most: a pool is meant to be shared, so this file
# is the one most likely to be scp'd to a worker that has nothing else
# installed.
