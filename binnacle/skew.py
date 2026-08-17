#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""skew.py -- work out whether this box knows what time it is.

Usage: skew.py                          check the configured time sources
       skew.py --server 10.0.0.1        ask one source instead
       skew.py --csv                    one row per finding, for fleet use
       skew.py --rules                  every rule and its thresholds
       skew.py --explain RULE_ID        why one rule exists

Options:
  --server ADDR[:PORT]  ask this time source instead of the configured
                     list (repeatable)
  --timeout S        per-query timeout, seconds          (default 2.0)
  --attempts N       tries per query before giving up    (default 2)
  --samples N        queries per source, best delay wins (default 2)
  --max-offset S     offset at or above which the clock is CRITICAL
                                                         (default 300.0)
  --warn-offset S    offset at or above which it is a WARN
                                                         (default 1.0)
  --min-severity L   info | warn | critical -- hide findings below L
  --all              also list the rules that were skipped, and why
  --quiet            the verdict and the findings, nothing else
  --exit-code        exit 0 ok / 10 warn / 20 critical (default: always 0)
  --csv [PATH]       findings as CSV (stdout if no path)
  --json [PATH]      meta + facts + findings + skipped
  --facts            print the fact dictionary and exit
  --from-facts FILE  run the rules against a saved fact file, measuring
                     nothing -- this is how the rules are tested
  --chrony-conf F    read sources from here              (SKEW_CHRONY_CONF)
  --ntp-conf F       read sources from here              (SKEW_NTP_CONF)
  --timesyncd-conf F read sources from here              (SKEW_TIMESYNCD_CONF)
  --proc-root DIR    read the process table from here    (SKEW_PROC)
  --rtc-path F       read the hardware clock from here   (SKEW_RTC)
  --no-exec          never run a subprocess
  --no-color         plain output (also honours NO_COLOR)

What it does
  Every box runs something that corrects its clock -- chronyd, ntpd,
  systemd-timesyncd -- and the failure that matters is not that service
  crashing.  A crashed one is already caught: why-slow's SYSTEMD_FAILED
  rule names it and says every timestamp on the box is a lie.

  The failure that goes unnoticed is the service running perfectly while
  the clock is still wrong.  Its sources are unreachable, or none was ever
  selected, or the machine resumed from a snapshot and the correction was
  never made.  `systemctl status chronyd` says active (running) through
  every one of those, so nothing looks broken and the clock is minutes out.

  So this ignores what the daemon says about itself and asks the time
  sources directly:

    * each configured source is queried on the wire over SNTP and its own
      offset, delay and stratum recorded, so a source that is wrong is
      attributed to itself rather than averaged away
    * the sources are compared against each other, because two that
      disagree means one of them is lying and the daemon may have picked
      the liar -- a single query can never see that
    * the hardware clock is read alongside the system clock, since they
      drift apart independently and it is the hardware one the box will
      come back with after a reboot
    * the timezone is recorded, because a fleet whose boxes disagree about
      what to call 14:20 cannot have its logs lined up at all

  Then it says which of those is the problem, in the same voice as the
  rest of binnacle: the verdict names the cause rather than the biggest
  number.  A box with no reachable source is diagnosed as having no
  reachable source, not as being four minutes fast -- the offset is what
  that produced.

Why it belongs next to the others
  A wrong clock is never reported as a wrong clock.  It arrives as logins
  failing, as certificates that look expired, as a distributed system
  disagreeing with itself, and as two hosts' log files that refuse to line
  up when you are trying to establish what happened first.  The rest of
  this package already works around it rather than checking it: logtriage
  --split-at trusts a timestamp, agree has to mask the ts column before
  any two hosts can agree about anything, and netmesh is built to read
  only the sender's clock precisely so it never has to trust two at once.

Robustness properties
  * a fact that could not be measured is None, never 0, and any rule that
    needed it is reported as skipped with the reason
  * a reply whose originate timestamp does not echo the transmit timestamp
    that was sent is discarded and the wait continues, so neither a late
    reply to an earlier query nor an off-path forgery can be counted as
    this one's answer
  * offset and delay come from the four-timestamp NTP calculation, so the
    network path's own latency is subtracted rather than being charged to
    the clock; only this box's clock is read twice, and the source's twice,
    which is what makes the result independent of the path being symmetric
    in throughput
  * the best of --samples queries is kept, chosen by lowest round trip,
    because a single sample over a congested link measures the congestion
  * a source that answers with stratum 16 or the leap alarm set is
    reporting itself unsynchronised, and is counted as an answer that says
    "do not trust me" rather than as a working source
  * needs no root, no packages and no ntpdate: the queries are built and
    parsed here, standard library only, Python 3.6+
  * every rule is a pure function of the fact dictionary, so --from-facts
    reproduces any diagnosis with no clock being read at the time
  * exit status is 0 unless you ask for --exit-code, so it is safe in a
    pipeline by default

Grouping a fleet
  The findings carry measured numbers, so two boxes that are wrong by
  different amounts are different rows.  To group by *what is wrong*
  rather than by how much, scrub the magnitudes:

    agree script ./skew.py --hosts prod.txt --fleet-csv \\
        --scrub '[0-9]+[.][0-9]+' -- --csv

  which collapses "4m 12s fast" and "3m 51s fast" onto the same finding
  and leaves you with the shape of the fleet: in sync, drifting, or with
  nothing to sync against.
"""

import argparse
import csv
import io
import json
import os
import socket
import struct
import subprocess
import sys
import time
from collections import namedtuple

VERSION = "0.2.1"
PROG = os.path.basename(sys.argv[0]) or "skew.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

# Two locations because the distributions never agreed: Debian and Ubuntu
# ship /etc/chrony/chrony.conf, RHEL and SUSE /etc/chrony.conf.  Looking in
# one and reporting "no sources configured" on a box that has them is the
# exact false negative this tool exists to avoid.
CHRONY_CONFS = ("/etc/chrony/chrony.conf", "/etc/chrony.conf")
NTP_CONFS = ("/etc/ntp.conf", "/etc/ntpsec/ntp.conf")
TIMESYNCD_CONFS = ("/etc/systemd/timesyncd.conf",)

PROC_ROOT = "/proc"
RTC_PATH = "/sys/class/rtc/rtc0/since_epoch"
NTP_PORT = 123
NO_EXEC = False

# Five minutes is where Kerberos and AD stop accepting a ticket, so it is
# the point past which the clock is breaking things rather than merely
# being wrong.  One second is where correlating this box's logs against
# another's stops being safe.
DEFAULT_MAX_OFFSET_S = 300.0
DEFAULT_WARN_OFFSET_S = 1.0

# comm is truncated to 15 characters by the kernel, which is why the
# systemd one is spelled short here -- matching on the full name finds
# nothing and reports a box with no time daemon at all.
DAEMON_COMMS = {
    "chronyd": "chronyd",
    "ntpd": "ntpd",
    "ntpsec": "ntpd",
    "openntpd": "openntpd",
    "systemd-timesyn": "systemd-timesyncd",
}

# Seconds between the NTP epoch (1900-01-01) and the Unix one.
NTP_EPOCH = 2208988800


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
    v = os.environ.get("SKEW_" + name)
    return v if v not in (None, "") else default


def _read(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return default


def _run(cmd, timeout=5):
    """Subprocess, or nothing at all under --no-exec."""
    if NO_EXEC:
        return None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             universal_newlines=True,
                             encoding="utf-8-sig", errors="replace")
        out, _ = p.communicate(timeout=timeout)
        return out if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# SNTP on the wire
# ---------------------------------------------------------------------------
#
# Written here rather than shelled out to ntpdate/chronyc for the reason
# every other probe in this package is: these files land on machines that
# have neither, and a tool that reports "not measured" because a binary is
# missing is the failure mode the first convention forbids.
#
# The packet is RFC 5905's 48-byte header.  Only the four timestamps, the
# stratum and the leap indicator are used; everything else is sent as zero
# and ignored on the way back.

_HEADER = "!BBBb11I"


class NTPError(Exception):
    pass


def _to_ntp(t):
    """Unix float -> (seconds, fraction) since 1900."""
    sec = int(t) + NTP_EPOCH
    frac = int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF
    return sec, frac


def _from_ntp(sec, frac):
    """(seconds, fraction) since 1900 -> Unix float, or None if unset.

    All-zero is how the protocol spells "this timestamp was never
    written", and it has to come back as None rather than as 1900, or a
    source that omits one produces an offset of a century.
    """
    if sec == 0 and frac == 0:
        return None
    return (sec - NTP_EPOCH) + float(frac) / (1 << 32)


def build_query(tx_sec, tx_frac):
    # 0x23: leap 0, version 4, mode 3 (client).
    return struct.pack(_HEADER, 0x23, 0, 6, -20,
                       0, 0, 0,
                       0, 0,
                       0, 0,
                       0, 0,
                       tx_sec, tx_frac)


def parse_response(data, tx_sec, tx_frac):
    if len(data) < 48:
        raise NTPError("short reply (%d bytes)" % len(data))
    fields = struct.unpack(_HEADER, data[:48])
    li_vn_mode, stratum = fields[0], fields[1]
    mode = li_vn_mode & 0x7
    leap = (li_vn_mode >> 6) & 0x3
    if mode not in (4, 5):
        raise NTPError("not a server reply (mode %d)" % mode)
    orig_sec, orig_frac = fields[9], fields[10]
    # The one check that makes a reply this query's reply.  Without it a
    # stale datagram from an earlier attempt -- or one from anywhere at
    # all -- is accepted as the answer, and the offset it carries is
    # believed.
    if (orig_sec, orig_frac) != (tx_sec, tx_frac):
        raise NTPError("reply does not echo the transmit timestamp")
    return {
        "leap": leap,
        "stratum": stratum,
        "root_delay": fields[4] / 65536.0,
        "root_dispersion": fields[5] / 65536.0,
        "recv": _from_ntp(fields[11], fields[12]),
        "tx": _from_ntp(fields[13], fields[14]),
    }


def query_ntp(server, port, timeout):
    """One SNTP exchange.  Returns a dict, or raises NTPError.

    offset and delay are the standard four-timestamp calculation:

        offset = ((T2 - T1) + (T3 - T4)) / 2
        delay  = (T4 - T1) - (T3 - T2)

    T1 and T4 are read from this box's clock, T2 and T3 from the source's.
    Subtracting the server's own turnaround is what leaves the network
    path, and halving the remainder is what removes it from the offset --
    so a slow link makes the delay large without making the clock look
    wrong.
    """
    try:
        infos = socket.getaddrinfo(server, port, 0, socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        raise NTPError("cannot resolve: %s" % exc)
    if not infos:
        raise NTPError("cannot resolve")
    family, socktype, proto, _, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    try:
        sock.settimeout(timeout)
        t1 = time.time()
        tx_sec, tx_frac = _to_ntp(t1)
        try:
            sock.sendto(build_query(tx_sec, tx_frac), sockaddr)
        except OSError as exc:
            raise NTPError("send failed: %s" % exc)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise NTPError("no reply")
            sock.settimeout(remaining)
            try:
                data, _peer = sock.recvfrom(512)
            except socket.timeout:
                raise NTPError("no reply")
            except OSError as exc:
                raise NTPError("receive failed: %s" % exc)
            t4 = time.time()
            try:
                r = parse_response(data, tx_sec, tx_frac)
            except NTPError:
                # Not ours.  Keep waiting on what is left of the budget
                # rather than treating someone else's packet as a failure.
                continue
            if r["recv"] is None or r["tx"] is None:
                raise NTPError("reply carries no timestamp")
            r["offset"] = ((r["recv"] - t1) + (r["tx"] - t4)) / 2.0
            r["delay"] = max((t4 - t1) - (r["tx"] - r["recv"]), 0.0)
            r["server"] = server
            return r
    finally:
        sock.close()


def probe_source(server, port, timeout, attempts, samples):
    """The best of `samples` exchanges, chosen by lowest round trip."""
    best, error = None, None
    for _ in range(max(1, samples)):
        for _ in range(max(1, attempts)):
            try:
                r = query_ntp(server, port, timeout)
            except NTPError as exc:
                error = str(exc)
                continue
            if best is None or r["delay"] < best["delay"]:
                best = r
            break
    if best is None:
        return {"reachable": False, "error": error or "no reply",
                "offset_s": None, "delay_ms": None, "stratum": None,
                "leap": None, "root_dispersion_s": None}
    return {
        "reachable": True, "error": None,
        "offset_s": best["offset"],
        "delay_ms": best["delay"] * 1000.0,
        "stratum": best["stratum"],
        "leap": best["leap"],
        "root_dispersion_s": best["root_dispersion"],
    }


# ---------------------------------------------------------------------------
# Reading what this box was told to sync against
# ---------------------------------------------------------------------------

def _conf_sources(text):
    """`server`/`pool`/`peer` lines out of a chrony or ntp config."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() in ("server", "pool", "peer"):
            out.append(parts[1])
    return out


def _timesyncd_sources(text):
    """NTP= and FallbackNTP= out of a timesyncd config."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() in ("ntp", "fallbackntp"):
            out.extend(value.split())
    return out


def read_configured_sources(args):
    """(path, source-kind, servers) for the first config that names any."""
    candidates = []
    if args.chrony_conf:
        candidates.append((args.chrony_conf, "chrony", _conf_sources))
    else:
        for p in CHRONY_CONFS:
            candidates.append((p, "chrony", _conf_sources))
    if args.ntp_conf:
        candidates.append((args.ntp_conf, "ntp", _conf_sources))
    else:
        for p in NTP_CONFS:
            candidates.append((p, "ntp", _conf_sources))
    if args.timesyncd_conf:
        candidates.append((args.timesyncd_conf, "timesyncd",
                           _timesyncd_sources))
    else:
        for p in TIMESYNCD_CONFS:
            candidates.append((p, "timesyncd", _timesyncd_sources))

    seen_any_file = False
    for path, kind, parse in candidates:
        text = _read(path)
        if text is None:
            continue
        seen_any_file = True
        servers = parse(text)
        if servers:
            # Dedupe, keeping order: a pool line repeated across an
            # include is one source, not four.
            uniq, out = set(), []
            for s in servers:
                if s not in uniq:
                    uniq.add(s)
                    out.append(s)
            return path, kind, out
    return (None, None, [] if seen_any_file else None)


def read_daemon(proc_root):
    """Which time daemon is running, from the process table.

    Read rather than exec'd: /proc/<pid>/comm is world readable, so this
    works under --no-exec and on a box with no systemd at all.
    """
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None, None
    for pid in entries:
        if not pid.isdigit():
            continue
        comm = _read(os.path.join(proc_root, pid, "comm"))
        if comm is None:
            continue
        comm = comm.strip()
        if comm in DAEMON_COMMS:
            return DAEMON_COMMS[comm], int(pid)
    return None, None


_TD_KEYS = {"NTP": "ntp_enabled", "NTPSynchronized": "synchronized"}


def read_timedatectl():
    """NTP= and NTPSynchronized= from `timedatectl show`.

    Advisory only.  Every rule that could be answered from it is also
    answered from the wire, because this is the daemon's opinion of
    itself and the whole point of the tool is that the opinion can be
    wrong.
    """
    out = _run(["timedatectl", "show"])
    if out is None:
        return {}
    found = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in _TD_KEYS:
            found[_TD_KEYS[key.strip()]] = value.strip().lower() == "yes"
    return found


def read_rtc(path):
    """The hardware clock, in seconds since the Unix epoch."""
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _split_server(spec):
    """ADDR, ADDR:PORT, [v6]:PORT -- port defaults to 123."""
    spec = spec.strip()
    if spec.startswith("["):
        end = spec.find("]")
        if end < 0:
            die("bad --server %r: missing ]" % spec)
        host = spec[1:end]
        rest = spec[end + 1:]
        if rest.startswith(":"):
            return host, _port_or_die(rest[1:], spec)
        return host, NTP_PORT
    if spec.count(":") == 1:
        host, port = spec.split(":", 1)
        return host, _port_or_die(port, spec)
    return spec, NTP_PORT


def _port_or_die(text, spec):
    try:
        port = int(text)
    except ValueError:
        die("bad --server %r: %r is not a port" % (spec, text))
    if not 1 <= port <= 65535:
        die("bad --server %r: port out of range" % spec)
    return port


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def _median(values):
    vals = sorted(values)
    n = len(vals)
    if not n:
        return None
    if n % 2:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def collect_facts(args):
    facts = {}
    try:
        facts["sys.hostname"] = socket.gethostname()
    except OSError:
        facts["sys.hostname"] = None

    # Timezone.  Recorded rather than judged here: what it means depends
    # on the rest of the fleet, which is why the rule that reads it is
    # INFO and the fleet grouping is where it earns its place.
    try:
        is_dst = time.daylight and time.localtime().tm_isdst > 0
        facts["tz.utc_offset_s"] = -(time.altzone if is_dst else time.timezone)
        facts["tz.name"] = time.tzname[1 if is_dst else 0]
    except (AttributeError, OSError, IndexError):
        facts["tz.utc_offset_s"] = None
        facts["tz.name"] = None

    daemon, pid = read_daemon(args.proc_root)
    facts["sync.daemon"] = daemon
    facts["sync.daemon_pid"] = pid
    facts["sync.daemon_running"] = daemon is not None

    td = read_timedatectl()
    facts["sync.ntp_enabled"] = td.get("ntp_enabled")
    facts["sync.synchronized"] = td.get("synchronized")

    if args.servers:
        pairs = [_split_server(s) for s in args.servers]
        facts["cfg.path"] = None
        facts["cfg.kind"] = "--server"
        facts["cfg.servers"] = [p[0] for p in pairs]
    else:
        path, kind, servers = read_configured_sources(args)
        facts["cfg.path"] = path
        facts["cfg.kind"] = kind
        facts["cfg.servers"] = servers
        pairs = [(s, NTP_PORT) for s in (servers or [])]

    facts["probe.timeout_s"] = args.timeout
    facts["probe.attempts"] = args.attempts
    facts["probe.samples"] = args.samples

    results, order = {}, []
    for host, port in pairs:
        key = host if port == NTP_PORT else "%s:%d" % (host, port)
        if key in results:
            continue
        order.append(key)
        results[key] = probe_source(host, port, args.timeout, args.attempts,
                                    args.samples)

    facts["srv.list"] = order
    facts["srv.all"] = results
    # None means "the configuration could not be read", which is not the
    # same as zero sources configured -- the rules have to be able to tell
    # those apart or an unreadable config reports as a box with no time
    # source, which is a different finding with a different fix.
    facts["srv.count"] = (None if facts.get("cfg.servers") is None
                          else len(order))
    if order:
        alive = [k for k in order if results[k]["reachable"]]
        dead = [k for k in order if not results[k]["reachable"]]
        facts["srv.alive"] = alive
        facts["srv.dead"] = dead
        # A source answering with stratum 16 or the leap alarm is telling
        # you it is not synchronised itself.  It answered, so it is not
        # dead; it is not a source of time either, and conflating the two
        # is how a box "has three working servers" and no time.
        facts["srv.unusable"] = [k for k in alive
                                 if results[k]["stratum"] in (0, 16)
                                 or results[k]["leap"] == 3]
        offsets = [results[k]["offset_s"] for k in alive]
        facts["clock.offset_s"] = _median(offsets) if offsets else None
        facts["clock.spread_s"] = (max(offsets) - min(offsets)
                                   if len(offsets) >= 2 else None)
        strata = [results[k]["stratum"] for k in alive
                  if results[k]["stratum"] is not None]
        facts["srv.max_stratum"] = max(strata) if strata else None
    else:
        facts["srv.alive"] = None
        facts["srv.dead"] = None
        facts["srv.unusable"] = None
        facts["clock.offset_s"] = None
        facts["clock.spread_s"] = None
        facts["srv.max_stratum"] = None

    rtc = read_rtc(args.rtc_path)
    facts["rtc.path"] = args.rtc_path
    facts["rtc.since_epoch"] = rtc
    facts["rtc.delta_s"] = (rtc - time.time()) if rtc is not None else None
    # The thresholds are not measured here.  They are policy, and main()
    # stamps them in once it has resolved flag over fact file over default.
    return facts


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

Rule = namedtuple("Rule", "id title needs level say fix why")

MISSING_REASONS = {
    "cfg.servers": "no time sync configuration could be read -- neither "
                   "chrony, ntp nor timesyncd has a config file here",
    "srv.count": "no sources to probe",
    "srv.dead": "no sources to probe",
    "srv.alive": "no sources to probe",
    "srv.unusable": "no sources to probe",
    "srv.max_stratum": "no source answered, so no stratum was reported",
    "clock.offset_s": "no source answered, so there is no offset to compare "
                      "against",
    "clock.spread_s": "fewer than two sources answered, so they cannot be "
                      "compared against each other",
    "sync.ntp_enabled": "timedatectl not available (--no-exec, or no "
                        "systemd)",
    "sync.synchronized": "timedatectl not available (--no-exec, or no "
                         "systemd)",
    "sync.daemon_running": "the process table could not be read",
    "rtc.delta_s": "no readable hardware clock (a container or a VM "
                   "without one)",
    "tz.utc_offset_s": "the timezone could not be determined",
}

RULES = []


def rule(rid, title, needs, why):
    def deco(fn):
        level, say, fix = fn()
        RULES.append(Rule(rid, title, needs, level, say, fix, why))
        return fn
    return deco


def _join(items, n=4):
    items = list(items)
    shown = " ".join(str(i) for i in items[:n])
    return shown + ("" if len(items) <= n else " (+%d)" % (len(items) - n))


def human_seconds(s):
    """A signed duration a person can read, at the precision it deserves."""
    if s is None:
        return "?"
    a = abs(s)
    if a < 0.001:
        return "%.0fus" % (a * 1e6)
    if a < 1:
        return "%.0fms" % (a * 1000.0)
    if a < 60:
        return "%.1fs" % a
    if a < 3600:
        return "%dm %02ds" % (int(a // 60), int(a % 60))
    if a < 86400:
        return "%dh %02dm" % (int(a // 3600), int((a % 3600) // 60))
    return "%dd %02dh" % (int(a // 86400), int((a % 86400) // 3600))


def fast_or_slow(offset):
    """Which way round the error is, said the way a person would.

    offset is (source - this box), so a positive offset means the source
    is ahead and this box is behind.  Getting this backwards would send
    someone to fix the wrong end, so it is one function used everywhere.
    """
    return "behind" if offset > 0 else "fast"


# 1 ------------------------------------------------------------------------
@rule("NO_SYNC_CONFIGURED", "no time source configured", ("cfg.servers",),
      "A box with no configured source never corrects its clock at all. "
      "It keeps whatever time it booted with and drifts from there, and "
      "nothing on the box will ever say so.")
def _no_sync_configured():
    def level(f):
        return CRITICAL if not f["cfg.servers"] else None

    def say(f):
        where = f.get("cfg.path") or "chrony, ntp or timesyncd config"
        return "no server, pool or peer line in %s" % where

    def fix(f):
        return ("This clock is free-running and will drift for as long as "
                "the box is up.  Add a source and start the daemon -- on a "
                "VM check whether the hypervisor was supposed to be "
                "providing time instead, because a guest with neither is "
                "the usual way this happens.")
    return level, say, fix


# 2 ------------------------------------------------------------------------
@rule("NO_SYNC_DAEMON", "no time daemon running", ("sync.daemon_running",),
      "Sources configured and nothing running to use them is the same "
      "clock as no sources at all, and it is easy to miss because the "
      "config file looks right.")
def _no_sync_daemon():
    def level(f):
        if not f.get("cfg.servers"):
            return None          # already covered, and better, by rule 1
        return CRITICAL if not f["sync.daemon_running"] else None

    def say(f):
        return ("%d source%s configured and no chronyd, ntpd or "
                "systemd-timesyncd running"
                % (len(f.get("cfg.servers") or []),
                   "" if len(f.get("cfg.servers") or []) == 1 else "s"))

    def fix(f):
        return ("Nothing is acting on the configuration.  Start and enable "
                "the daemon that owns %s.  If it was running and is not "
                "now, why-slow's failed-unit rule will name the unit and "
                "the reason it stopped."
                % (f.get("cfg.path") or "the config"))
    return level, say, fix


# 3 ------------------------------------------------------------------------
@rule("SYNC_DISABLED", "time sync switched off", ("sync.ntp_enabled",),
      "systemd will happily report a healthy box with NTP disabled. The "
      "daemon is not running because it was told not to, which looks "
      "identical to it not being installed.")
def _sync_disabled():
    def level(f):
        return CRITICAL if f["sync.ntp_enabled"] is False else None

    def say(f):
        return "timedatectl reports NTP=no"

    def fix(f):
        return ("Someone turned it off: `timedatectl set-ntp true` turns "
                "it back on.  Worth finding out who and why first -- this "
                "is occasionally deliberate on a box whose time comes from "
                "the hypervisor or a PTP daemon instead.")
    return level, say, fix


# 4 ------------------------------------------------------------------------
@rule("NO_SYNC_SOURCE", "no source answered",
      ("srv.dead", "srv.count"),
      "Configured, running, and every source unreachable. This is the "
      "failure the tool exists for: the daemon is up, the box looks "
      "healthy, and the clock has not been corrected since it started.")
def _no_sync_source():
    def level(f):
        if not f["srv.count"]:
            return None
        return CRITICAL if len(f["srv.dead"]) == f["srv.count"] else None

    def say(f):
        return ("all %d configured source%s unreachable: %s"
                % (f["srv.count"], "" if f["srv.count"] == 1 else "s",
                   _join(f["srv.dead"])))

    def fix(f):
        return ("Nothing is correcting this clock, whatever the daemon's "
                "status says.  Check egress on UDP 123 first -- it is the "
                "usual cause, and a firewall added for something else is "
                "the usual reason.  Until this is fixed every timestamp "
                "this box writes drifts further from the rest of the "
                "fleet.")
    return level, say, fix


# 5 ------------------------------------------------------------------------
@rule("SOURCE_DEAD", "a source not answering", ("srv.dead", "srv.count"),
      "One dead source among several is invisible: the clock is still "
      "correct because the others cover for it, and the redundancy you "
      "think you have is smaller than it looks.")
def _source_dead():
    def level(f):
        dead = f["srv.dead"]
        if not dead or len(dead) == f["srv.count"]:
            return None          # none, or all -- rule 4 owns the latter
        return WARN

    def say(f):
        return ("%d of %d sources unreachable: %s"
                % (len(f["srv.dead"]), f["srv.count"], _join(f["srv.dead"])))

    def fix(f):
        return ("The clock is fine; the redundancy is not.  %d source%s "
                "left, and a wrong one among those cannot be outvoted if "
                "it comes to it."
                % (f["srv.count"] - len(f["srv.dead"]),
                   "" if f["srv.count"] - len(f["srv.dead"]) == 1 else "s"))
    return level, say, fix


# 6 ------------------------------------------------------------------------
@rule("CLOCK_OFFSET", "the clock is wrong",
      ("clock.offset_s", "thresh.max_offset_s", "thresh.warn_offset_s"),
      "The measured difference between this box's clock and the sources "
      "it is meant to follow. Past five minutes Kerberos and AD "
      "authentication fail outright; past one second, correlating this "
      "box's logs with another's stops being safe.")
def _clock_offset():
    def level(f):
        a = abs(f["clock.offset_s"])
        if a >= f["thresh.max_offset_s"]:
            return CRITICAL
        if a >= f["thresh.warn_offset_s"]:
            return WARN
        return None

    def say(f):
        o = f["clock.offset_s"]
        return ("%s %s the %d source%s that answered"
                % (human_seconds(o), fast_or_slow(o),
                   len(f.get("srv.alive") or []),
                   "" if len(f.get("srv.alive") or []) == 1 else "s"))

    def fix(f):
        o = f["clock.offset_s"]
        a = abs(o)
        s = ("Every timestamp this box writes is %s %s, including the ones "
             "you are about to compare against another machine's."
             % (human_seconds(o), fast_or_slow(o)))
        if a >= 128:
            s += ("  Past 128 seconds chronyd will not step the clock "
                  "without makestep, so it may never close this gap on its "
                  "own -- `chronyc makestep` once, then find out how it got "
                  "this far out.")
        if a >= 300:
            s += ("  Kerberos and AD reject a ticket this far out, so "
                  "authentication failures on this box are a symptom of "
                  "this and not a separate problem.")
        return s
    return level, say, fix


# 7 ------------------------------------------------------------------------
@rule("NOT_SYNCHRONIZED", "daemon reports not synchronised",
      ("sync.synchronized",),
      "The daemon's own opinion, kept as a finding rather than as the "
      "verdict: it is the one signal here that does not need the network, "
      "and it disagreeing with a measured-good clock is itself worth "
      "seeing.")
def _not_synchronized():
    def level(f):
        return WARN if f["sync.synchronized"] is False else None

    def say(f):
        return "timedatectl reports NTPSynchronized=no"

    def fix(f):
        off = f.get("clock.offset_s")
        if off is not None and abs(off) < 1.0:
            return ("The clock measures correct from here, so this is "
                    "either a daemon that has not finished selecting a "
                    "source since it started, or one that has given up. "
                    "`chronyc tracking` or `timedatectl timesync-status` "
                    "says which.")
        return ("The daemon knows it is not synchronised.  Whatever the "
                "sources above say, it is not currently steering this "
                "clock towards them.")
    return level, say, fix


# 8 ------------------------------------------------------------------------
@rule("SOURCES_DISAGREE", "sources disagree", ("clock.spread_s",),
      "Two sources that differ by more than the network could explain "
      "means one of them is wrong, and nothing on this box knows which. "
      "A daemon that picked the wrong one is correct-looking and wrong.")
def _sources_disagree():
    def level(f):
        return WARN if f["clock.spread_s"] >= 1.0 else None

    def say(f):
        return ("the answering sources span %s"
                % human_seconds(f["clock.spread_s"]))

    def fix(f):
        return ("They cannot all be right.  Compare the per-source offsets "
                "above: the outlier is usually a box someone stood up as a "
                "local time server that is itself unsynchronised.  Until "
                "it is removed, which time this box keeps depends on which "
                "source the daemon happens to favour.")
    return level, say, fix


# 9 ------------------------------------------------------------------------
@rule("SOURCE_UNUSABLE", "a source says do not trust it",
      ("srv.unusable",),
      "Stratum 16 or the leap alarm is a server reporting itself "
      "unsynchronised. It answers, so every reachability check passes, "
      "and it is not a source of time.")
def _source_unusable():
    def level(f):
        return WARN if f["srv.unusable"] else None

    def say(f):
        return ("%s answer%s but report%s itself unsynchronised"
                % (_join(f["srv.unusable"]),
                   "s" if len(f["srv.unusable"]) == 1 else "",
                   "s" if len(f["srv.unusable"]) == 1 else ""))

    def fix(f):
        return ("A ping or a port check calls this source healthy, which "
                "is why it can sit in a config for years.  It is a box "
                "that lost its own upstream: fix it there, or point this "
                "one somewhere else.")
    return level, say, fix


# 10 -----------------------------------------------------------------------
@rule("STRATUM_HIGH", "a long way from a real clock", ("srv.max_stratum",),
      "Every hop from the reference clock adds error. Past stratum 10 "
      "the chain is long enough that the time arriving here is worth "
      "less than the network delay measuring it.")
def _stratum_high():
    def level(f):
        s = f["srv.max_stratum"]
        if s in (0, 16):
            return None          # unsynchronised, which rule 9 owns
        return WARN if s >= 10 else None

    def say(f):
        return "furthest answering source is stratum %d" % f["srv.max_stratum"]

    def fix(f):
        return ("Long chains are usually accidental: a box syncing from a "
                "box that syncs from a box.  Point this at whatever is "
                "nearest the top of that chain, or at the site's own "
                "servers if it has them.")
    return level, say, fix


# 11 -----------------------------------------------------------------------
@rule("RTC_DRIFT", "hardware clock disagrees", ("rtc.delta_s",),
      "The hardware clock keeps running when the box is off, and it is "
      "what the box comes back with. A system clock corrected daily on a "
      "machine whose RTC is an hour out boots wrong every time.")
def _rtc_drift():
    def level(f):
        return WARN if abs(f["rtc.delta_s"]) > 60 else None

    def say(f):
        d = f["rtc.delta_s"]
        return "hardware clock is %s %s the system clock" % (
            human_seconds(d), "ahead of" if d > 0 else "behind")

    def fix(f):
        d = abs(f["rtc.delta_s"])
        # Whole hours is not drift.  Saying so here saves the reader from
        # chasing a battery that is fine.
        near_hour = abs((d % 3600) - 0) < 90 or abs((d % 3600) - 3600) < 90
        if near_hour and d > 1800:
            return ("This is almost exactly %d hour(s), which is a "
                    "hardware clock kept in local time rather than UTC -- "
                    "normal on a box that dual boots, and not drift.  "
                    "`timedatectl set-local-rtc 0` if that was not "
                    "deliberate." % int(round(d / 3600.0)))
        return ("`hwclock --systohc` writes the corrected time back, and "
                "is what the daemon does periodically on most "
                "distributions.  A hardware clock that keeps drifting "
                "back is usually a dead CMOS battery, which is worth "
                "knowing before the box reboots on its own.")
    return level, say, fix


# 12 -----------------------------------------------------------------------
@rule("SINGLE_SOURCE", "only one time source", ("srv.count",),
      "With one source there is no way to notice it is wrong. Two is "
      "barely better -- when they disagree neither is outvoted -- which "
      "is why the usual advice is at least three.")
def _single_source():
    def level(f):
        return INFO if f["srv.count"] == 1 else None

    def say(f):
        return "one configured source, so it cannot be checked against anything"

    def fix(f):
        return ("Whatever that source says is what this box believes.  Add "
                "two more, ideally not all from the same place, so a wrong "
                "one can be outvoted rather than followed.")
    return level, say, fix


# 13 -----------------------------------------------------------------------
@rule("TZ_NOT_UTC", "local time is not UTC", ("tz.utc_offset_s",),
      "Not a fault, and it matters anyway: when this box's logs are read "
      "next to another's, an hour of offset between them is indis"
      "tinguishable from an hour of delay.")
def _tz_not_utc():
    def level(f):
        return INFO if f["tz.utc_offset_s"] != 0 else None

    def say(f):
        off = f["tz.utc_offset_s"]
        return ("timezone %s, UTC%s%s"
                % (f.get("tz.name") or "?", "+" if off >= 0 else "-",
                   human_seconds(off)))

    def fix(f):
        return ("Worth knowing rather than worth changing.  If the rest of "
                "the fleet is UTC, this box's logs will not line up with "
                "theirs by eye -- run this across the fleet and the "
                "grouping will show whether it is the odd one out.")
    return level, say, fix


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------
#
# Causes first, exactly as why-slow and resolve do it.  A box with no
# reachable source is also, necessarily, a box with a drifting clock: the
# offset is the symptom and naming it as the verdict sends the reader to
# `chronyc makestep` rather than to the firewall that is the actual
# problem.
VERDICT_PRECEDENCE = [
    "NO_SYNC_CONFIGURED", "SYNC_DISABLED", "NO_SYNC_DAEMON",
    "NO_SYNC_SOURCE", "CLOCK_OFFSET", "SOURCES_DISAGREE", "SOURCE_UNUSABLE",
    "NOT_SYNCHRONIZED", "SOURCE_DEAD", "STRATUM_HIGH", "RTC_DRIFT",
    "SINGLE_SOURCE", "TZ_NOT_UTC",
]

Finding = namedtuple("Finding", "rule severity say fix")

NOTHING_CHECKED = Rule("NOTHING_CHECKED", "nothing could be checked", (),
                       None, None, None,
                       "Every rule was skipped for want of the facts it "
                       "needs, so the run says nothing about the clock.")


def missing_reason(f, key):
    if key in MISSING_REASONS:
        return MISSING_REASONS[key]
    return "%s not available" % key


def evaluate(facts):
    findings, skipped, passed = [], [], []
    for r in RULES:
        missing = [k for k in r.needs if facts.get(k) is None]
        if missing:
            skipped.append((r, missing_reason(facts, missing[0])))
            continue
        try:
            sev = r.level(facts)
        except (TypeError, ValueError, ZeroDivisionError, KeyError,
                IndexError) as exc:
            skipped.append((r, "rule error: %s" % exc))
            continue
        if sev is None:
            passed.append(r)
            continue
        findings.append(Finding(r, sev, r.say(facts), r.fix(facts)))

    findings.sort(key=lambda x: (-SEVERITY_ORDER[x.severity],
                                 VERDICT_PRECEDENCE.index(x.rule.id)
                                 if x.rule.id in VERDICT_PRECEDENCE else 99))
    return findings, skipped, passed


def verdict_line(findings, facts):
    if not findings:
        if not facts.get("srv.alive"):
            return ("Nothing about this clock was measured -- no source "
                    "answered, so this is not a clean bill of health.")
        return ("This box knows what time it is: every configured source "
                "answered, they agree, and the clock is within %s of them."
                % human_seconds(facts.get("thresh.warn_offset_s") or 1.0))
    top = findings[0]
    consequences = {
        "NO_SYNC_CONFIGURED": "This clock has never been corrected and "
                              "never will be -- there is no time source "
                              "configured at all.",
        "NO_SYNC_DAEMON": "Sources are configured and nothing is running "
                          "to use them, so this clock is free-running.",
        "SYNC_DISABLED": "Time sync is switched off on this box, so the "
                         "clock is free-running whatever the config says.",
        "NO_SYNC_SOURCE": "No time source is reachable from this box.  "
                          "The daemon is running and has nothing to steer "
                          "towards, so the clock drifts and nothing on the "
                          "box reports a fault.",
        "CLOCK_OFFSET": None,   # filled in below: the number is the point
        "SOURCES_DISAGREE": "This box's time sources disagree with each "
                            "other, so which time it keeps depends on "
                            "which one the daemon favours.",
        "SOURCE_UNUSABLE": "A source this box relies on is reporting "
                           "itself unsynchronised, so it answers every "
                           "health check and provides no time.",
        "NOTHING_CHECKED": "Nothing about the clock was measured here, so "
                           "this says nothing about whether it is right.  "
                           "It is an empty report, not a clean one.",
    }
    if top.rule.id == "CLOCK_OFFSET":
        off = facts.get("clock.offset_s")
        return ("This box's clock is %s %s the sources it follows.  "
                "Everything it timestamps is wrong by that much, including "
                "the logs you would correlate against another machine."
                % (human_seconds(off), fast_or_slow(off)))
    return consequences.get(top.rule.id) or (
        "%s: %s" % (top.rule.title.capitalize(), top.say))


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


def render_human(facts, findings, skipped, passed, args, C):
    out = []
    bits = []
    if facts.get("srv.count") is not None:
        bits.append("%d source%s" % (facts["srv.count"],
                                     "" if facts["srv.count"] == 1 else "s"))
    if facts.get("sync.daemon"):
        bits.append(facts["sync.daemon"])
    if facts.get("cfg.path"):
        bits.append("from %s" % facts["cfg.path"])
    head = "%s -- %s" % (PROG, facts.get("sys.hostname") or "?")
    out.append("%s, %s" % (head, ", ".join(bits)) if bits else head)
    out.append("")

    out.append("  %s   %s" % (C.bold("VERDICT"),
                              _wrap(verdict_line(findings, facts), 12)))
    out.append("")

    shown = [f for f in findings
             if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[args.min_severity]]
    for f in shown:
        tag = {CRITICAL: C.crit("CRITICAL"), WARN: C.warn("WARN    "),
               INFO: C.info("INFO    ")}[f.severity]
        out.append("  %s  %-26s %s" % (tag, f.rule.title, f.say))
    if passed and not args.quiet:
        titles = sorted(set(r.title for r in passed))
        names = ", ".join(titles[:5])
        if len(titles) > 5:
            names += " (+%d)" % (len(titles) - 5)
        out.append("  %s        %s" % (C.dim("ok"), C.dim(_wrap(names, 12))))
    if args.all:
        for r, why in skipped:
            out.append("  %s   %-26s %s" % (C.dim("skipped"), r.title,
                                            C.dim(why)))
    out.append("")

    if not args.quiet and facts.get("srv.list"):
        out.append("  %s" % C.bold("SOURCES"))
        for name in facts["srv.list"]:
            d = (facts.get("srv.all") or {}).get(name) or {}
            if d.get("reachable"):
                state = "%9s  %6.1fms  stratum %-2s" % (
                    human_seconds(d.get("offset_s")),
                    d.get("delay_ms") or 0.0,
                    d.get("stratum") if d.get("stratum") is not None else "?")
                if d.get("stratum") in (0, 16) or d.get("leap") == 3:
                    state += "  <- unsynchronised"
            else:
                state = "no answer  (%s)" % (d.get("error") or "unknown")
            out.append("    %-24s %s" % (name[:24], state))
        out.append("")

    if not args.quiet:
        out.append("  %s" % C.bold("CLOCK"))
        if facts.get("clock.offset_s") is not None:
            o = facts["clock.offset_s"]
            out.append("    %-24s %s %s" % ("system vs sources",
                                            human_seconds(o),
                                            fast_or_slow(o)))
        if facts.get("rtc.delta_s") is not None:
            d = facts["rtc.delta_s"]
            out.append("    %-24s %s %s" % ("hardware vs system",
                                            human_seconds(d),
                                            "ahead" if d > 0 else "behind"))
        if facts.get("tz.name"):
            out.append("    %-24s %s" % ("timezone", facts["tz.name"]))
        out.append("")

    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    if not shown:
        if not facts.get("srv.alive"):
            out.append("    %s" % _wrap(
                "No source answered, so nothing here says the clock is "
                "right. Point this at a source you know works before "
                "reading anything else into it.", 4))
            out.append("")
            out.append("    %s --server pool.ntp.org" % PROG)
        else:
            out.append("    %s" % _wrap(
                "This clock is right, so timestamps from this box can be "
                "trusted and compared against other machines'. If logs "
                "still will not line up, the other end is the one to "
                "check.", 4))
            out.append("")
            out.append("    %s" % _wrap(
                "agree script ./skew.py --hosts prod.txt --fleet-csv "
                "-- --csv    (the whole fleet)", 4))
    else:
        for f in shown[:4]:
            out.append("    * %s" % _wrap(f.fix, 6))
    out.append("")
    return "\n".join(out)


CSV_FIELDS = ["host", "ts", "rule_id", "severity", "title", "detail", "fix"]


def render_csv(facts, findings, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        ts = int(time.time())
        host = facts.get("sys.hostname", "")
        if not findings:
            w.writerow({"host": host, "ts": ts, "rule_id": "OK",
                        "severity": "INFO", "title": "nothing wrong",
                        "detail": "no rule fired", "fix": ""})
        for f in findings:
            w.writerow({"host": host, "ts": ts, "rule_id": f.rule.id,
                        "severity": f.severity, "title": f.rule.title,
                        "detail": f.say, "fix": f.fix})
    finally:
        if path:
            fh.close()


def render_json(facts, findings, skipped, path):
    doc = {
        "meta": {"tool": PROG, "version": VERSION,
                 "host": facts.get("sys.hostname"), "ts": int(time.time())},
        "facts": dict((k, v) for k, v in sorted(facts.items())),
        "findings": [{"rule_id": f.rule.id, "severity": f.severity,
                      "title": f.rule.title, "detail": f.say, "fix": f.fix}
                     for f in findings],
        "skipped": [{"rule_id": r.id, "reason": why} for r, why in skipped],
    }
    text = json.dumps(doc, indent=2, sort_keys=True, default=str)
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version="%s %s" % (PROG, VERSION))
    p.add_argument("--server", action="append", dest="servers",
                   metavar="ADDR[:PORT]",
                   default=(_env("SERVERS").split(",") if _env("SERVERS")
                            else None))
    p.add_argument("--timeout", type=float, default=float(_env("TIMEOUT", 2.0)))
    p.add_argument("--attempts", type=int, default=int(_env("ATTEMPTS", 2)))
    p.add_argument("--samples", type=int, default=int(_env("SAMPLES", 2)))
    # Default None rather than the number, so main() can tell "not given"
    # from "given the same value the default happens to be" -- which is
    # what lets a saved fact file supply it under --from-facts.
    p.add_argument("--max-offset", type=float,
                   default=(float(_env("MAX_OFFSET"))
                            if _env("MAX_OFFSET") else None))
    p.add_argument("--warn-offset", type=float,
                   default=(float(_env("WARN_OFFSET"))
                            if _env("WARN_OFFSET") else None))
    p.add_argument("--min-severity", default=_env("MIN_SEVERITY", "info"),
                   choices=["info", "warn", "critical"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--exit-code", action="store_true")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--facts", action="store_true")
    p.add_argument("--from-facts", metavar="FILE")
    p.add_argument("--rules", action="store_true")
    p.add_argument("--explain", metavar="RULE_ID")
    p.add_argument("--chrony-conf", default=_env("CHRONY_CONF"))
    p.add_argument("--ntp-conf", default=_env("NTP_CONF"))
    p.add_argument("--timesyncd-conf", default=_env("TIMESYNCD_CONF"))
    p.add_argument("--proc-root", default=_env("PROC", PROC_ROOT))
    p.add_argument("--rtc-path", default=_env("RTC", RTC_PATH))
    p.add_argument("--no-exec", action="store_true")
    p.add_argument("--no-color", action="store_true")
    return p


def cmd_rules():
    out = []
    for r in RULES:
        out.append("%-20s %s" % (r.id, r.title))
        out.append("    needs: %s" % ", ".join(r.needs))
        out.append("    %s" % _wrap(r.why, 4))
        out.append("")
    sys.stdout.write("\n".join(out))
    return 0


def cmd_explain(rid):
    for r in RULES:
        if r.id == rid.upper():
            sys.stdout.write("%s -- %s\n\n%s\n\nneeds: %s\n"
                             % (r.id, r.title, _wrap(r.why, 0),
                                ", ".join(r.needs)))
            return 0
    die("no such rule: %s  (try --rules)" % rid)


def main(argv=None):
    _stdio_safe()
    global NO_EXEC
    args = build_parser().parse_args(argv)

    if args.rules:
        return cmd_rules()
    if args.explain:
        return cmd_explain(args.explain)

    NO_EXEC = args.no_exec
    args.min_severity = {"info": INFO, "warn": WARN,
                         "critical": CRITICAL}[args.min_severity]
    for name in ("timeout", "attempts", "samples"):
        if getattr(args, name) <= 0:
            die("--%s must be positive" % name)

    if args.from_facts:
        try:
            with io.open(args.from_facts, encoding="utf-8-sig",
                         errors="replace") as fh:
                facts = json.load(fh)
        except (OSError, ValueError) as exc:
            die("cannot read facts file: %s" % exc)
        if isinstance(facts, dict) and "facts" in facts \
                and isinstance(facts["facts"], dict):
            facts = facts["facts"]
        if not isinstance(facts, dict):
            die("%s does not hold a fact dictionary (found %s) -- pass a "
                "file written by --facts or --json"
                % (args.from_facts, type(facts).__name__))
    else:
        facts = collect_facts(args)

    # Thresholds are policy rather than measurement, so they resolve as
    # flag, then whatever the fact file recorded -- which is what makes
    # --from-facts reproduce the run it came from -- then the default.
    # They are stamped back into the facts either way, so a saved --json
    # says which line each finding was judged against instead of leaving
    # a reader to assume the defaults were in force.
    for attr, key, default in (
            ("max_offset", "thresh.max_offset_s", DEFAULT_MAX_OFFSET_S),
            ("warn_offset", "thresh.warn_offset_s", DEFAULT_WARN_OFFSET_S)):
        value = getattr(args, attr)
        if value is None:
            value = facts.get(key)
        if value is None:
            value = default
        setattr(args, attr, value)
        facts[key] = value

    if args.warn_offset > args.max_offset:
        die("--warn-offset (%g) is above --max-offset (%g), so no offset "
            "could ever be a warning rather than critical"
            % (args.warn_offset, args.max_offset))

    if args.facts:
        sys.stdout.write(json.dumps(facts, indent=2, sort_keys=True,
                                    default=str) + "\n")
        return 0

    findings, skipped, passed = evaluate(facts)
    if not findings and not passed:
        findings = [Finding(NOTHING_CHECKED, WARN,
                            "none of the %d rules could run: every fact they "
                            "need is missing" % len(RULES),
                            "The fact dictionary is empty or unrecognisable, "
                            "so this says nothing about whether the clock is "
                            "right.  Run %s on the box itself, or pass a "
                            "file written by --facts." % PROG)]

    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            render_csv(facts, findings, args.csv or None)
    if args.json is not None:
        with _WriteGuard(args.json or None):
            render_json(facts, findings, skipped, args.json or None)
    if args.csv is None and args.json is None:
        use_color = (not args.no_color and not os.environ.get("NO_COLOR")
                     and sys.stdout.isatty())
        sys.stdout.write(render_human(facts, findings, skipped, passed, args,
                                      Colors(use_color)) + "\n")

    if args.exit_code and findings:
        worst = max(SEVERITY_ORDER[f.severity] for f in findings)
        # 10/20 rather than 1/2, so a severity can never be mistaken for a
        # usage error by whatever is wrapping this.
        return {3: 20, 2: 10, 1: 0}[worst]
    return 0


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
# mesh, and `agree script ./skew.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
#
# canonical copy: binnacle/why_slow.py  (_stdio_safe, _WriteGuard, _read)
