#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""during.py -- what limited this run, and can you trust the number?

Usage: during.py -- ./benchmark.sh          wrap a command, diagnose the window
       during.py --seconds 120              watch for two minutes, then report
       during.py --samples run.csv -- make  keep the raw time series too
       during.py --from-samples run.csv     re-analyse a saved series
       during.py --rules                    every rule and its thresholds
       during.py --explain RULE_ID          why one rule exists

Options:
  --seconds S        how long to watch when not wrapping a command
  --interval S       seconds between samples        (default 1.0)
  --settle S         ignore the first S seconds outright (default 0)
  --samples [PATH]   the raw time series as tidy CSV (stdout if no path)
  --from-samples F   analyse a saved series, measuring nothing -- this is
                     how the analysis is tested
  --baseline FILE    compare against a previous run's samples
  --no-procs         do not scan /proc/<pid> (cheaper, loses the interloper)
  --min-severity L   info | warn | critical -- hide findings below L
  --all              also list the rules that were skipped, and why
  --quiet            the verdict and the findings, nothing else
  --no-timeline      omit the per-sample timeline block
  --csv [PATH]       findings as CSV, same shape as why-slow's
  --json [PATH]      meta + summary + findings + skipped
  --peer-samples F   the other end's series, so which machine was the
                     limit becomes a finding rather than something you
                     read off two reports side by side
  --expect-mbps N    what this test should have been pushing; the
                     interface total is checked against it, so traffic
                     that was not yours becomes a finding
                                                        (DURING_EXPECT_MBPS)
  --rtt-ms MS        round trip to the peer, from netmesh or anything
                     else that measured it -- without it the
                     window-limited check skips        (DURING_RTT_MS)
  --exit-code        exit 0 ok / 10 warn / 20 critical instead of passing
                     the wrapped command's status through -- except when
                     that command failed, which always wins
  --proc-root DIR    read /proc from here          (DURING_PROC)
  --sys-root DIR     read /sys from here           (DURING_SYS)
  --no-color         plain output (also honours NO_COLOR)

What it does
  A benchmark number on its own is not a measurement, it is a rumour.  The
  question that matters is what the machine was doing while the number was
  produced -- and specifically which resource was at its ceiling, for how
  much of the run, and whether anything happened during it that makes the
  number a measurement of something else.

  So this samples the box across the whole window and answers three
  questions that a point-in-time tool structurally cannot:

  What was the binding constraint?
    Not "the biggest number at the end" -- the resource that was at its
    ceiling for the largest share of the run.  Each sample is classified
    into exactly one state (cpu, io, memory, network, throttled, stolen,
    or not bound at all) using the same causes-outrank-symptoms precedence
    as why-slow, and the shares are what get reported.

  Was this box the bottleneck at all?
    The most useful benchmark finding there is, and the one nothing prints:
    if no resource was near a ceiling for most of the run, the limit was
    somewhere else -- the load generator, the peer, or a lock inside the
    application -- and tuning this machine cannot help.  A run that is
    "not bound" for most of its samples says so as the verdict.

  Can you trust the number?
    Warmup folded into the average, a cron job that ran during the window,
    thermal or cgroup throttling that started partway through, a cloud
    volume whose burst balance ran out, a hypervisor taking time from
    somebody else's tenant.  Each of those silently turns a benchmark into
    a measurement of something else, and each is visible only as a change
    *across* the run.  Steady state is detected and reported separately
    from warmup, because averaging the two together is how wrong numbers
    get published.

Robustness properties
  * a fact that could not be measured is blank, never 0, and any rule that
    needed it is reported as skipped with the reason
  * the wrapped command's stdout, stderr and exit status pass straight
    through, so `during -- make bench` is a drop-in prefix -- and a command
    that failed keeps its status even under --exit-code, because a verdict
    about a run that never finished is not worth reading
  * the command's own process tree is excluded from interloper detection,
    because the benchmark is not a surprise
  * interrupting with ^C still prints the report for what was sampled, and
    does not wait on a command that ignored the signal -- losing a long
    run to an impatient keystroke would be the worst possible behaviour,
    and so would holding the report until a benchmark that traps SIGINT
    decides to finish.  The command is never killed, only reported as
    still running
  * sampling reads /proc and /sys only: no root, no packages, no agent,
    nothing left behind
  * the analysis is a pure function of the sample series, so
    --from-samples reproduces any verdict with no machine in that state
  * needs no root; without it nothing is lost except per-process I/O
"""

import argparse
import csv
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import namedtuple

VERSION = "0.6.0"
PROG = os.path.basename(sys.argv[0]) or "during.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

PROC = "/proc"
SYS = "/sys"

# The states a sample can be in.  Exactly one, chosen by precedence, so the
# shares add to 100% and "bound by two things at once" cannot be reported --
# which would be a non-answer.
CPU, IO, MEM, NET, THROTTLED, STOLEN, SERIAL, FREE = (
    "cpu", "io", "memory", "network", "throttled", "stolen", "one core",
    "not bound")
STATES = [STOLEN, THROTTLED, MEM, IO, NET, CPU, SERIAL, FREE]

STATE_TEXT = {
    CPU: "at the CPU's ceiling",
    IO: "waiting on storage",
    MEM: "short of memory, or paging",
    NET: "at the interface's line rate",
    THROTTLED: "held back by a quota or by thermal limits",
    STOLEN: "waiting on the hypervisor",
    SERIAL: "one core pinned while the rest idled, so the work is serialised",
    FREE: "not at any ceiling",
}

# Distinct letters for the timeline: 'stolen' and 'one core' would collide
# on a first-initial scheme, and a legend nobody can read is worse than no
# legend at all.
STATE_LETTER = {CPU: "C", IO: "I", MEM: "M", NET: "N", THROTTLED: "T",
                STOLEN: "S", SERIAL: "1", FREE: "."}


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
    v = os.environ.get("DURING_" + name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------------------
# Reading /proc and /sys
# ---------------------------------------------------------------------------
#
# canonical copy of these readers: binnacle/why_slow.py.  They are duplicated
# rather than imported because each tool in this package is a standalone file
# that gets copied onto machines which have never heard of the package -- see
# the note at the foot of this file.
#
# Two kinds of copy, and the difference matters to anyone fixing a parser
# bug upstream:
#
#   verbatim   _read, _read_int, read_vmstat, read_meminfo, read_pressure
#              -- identical to why_slow's, and a fix there belongs here too.
#              tests/test_compose.sh fails if they stop matching.
#
#   trimmed    read_stat, read_diskstats, read_netdev, read_snmp, read_procs
#              -- deliberately narrower.  This tool samples every second for
#              minutes at a time, so each keeps only the fields the sample
#              row needs and aggregates where why_slow keeps the parts
#              separate.  A fix upstream has to be re-thought here, not
#              pasted.

def _read(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return default


def _read_int(path, default=None):
    txt = _read(path)
    if txt is None:
        return default
    txt = txt.strip()
    if txt == "max":
        return None
    try:
        return int(txt.split()[0])
    except (ValueError, IndexError):
        return default


def read_stat():
    txt = _read(os.path.join(PROC, "stat"))
    if txt is None:
        return None
    out = {"running": None, "blocked": None, "cores": []}
    for line in txt.splitlines():
        parts = line.split()
        if not parts:
            continue
        if re.match(r"^cpu\d+$", parts[0]):
            try:
                vals = [int(v) for v in parts[1:11]]
            except ValueError:
                continue
            while len(vals) < 10:
                vals.append(0)
            out["cores"].append({"total": sum(vals),
                                 "idle": vals[3] + vals[4],
                                 # Per-core softirq, which the whole-box
                                 # figure cannot show: receive processing
                                 # landing on one core is invisible in an
                                 # average over sixteen.
                                 "softirq": vals[6]})
        elif parts[0] == "cpu":
            try:
                vals = [int(v) for v in parts[1:11]]
            except ValueError:
                return None
            while len(vals) < 10:
                vals.append(0)
            out["all"] = {"total": sum(vals), "idle": vals[3] + vals[4],
                          "sys": vals[2], "iowait": vals[4], "steal": vals[7],
                          "softirq": vals[6]}
        elif parts[0] == "procs_running":
            out["running"] = int(parts[1])
        elif parts[0] == "procs_blocked":
            out["blocked"] = int(parts[1])
    return out if "all" in out else None


def read_vmstat():
    txt = _read(os.path.join(PROC, "vmstat"))
    if txt is None:
        return None
    out = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out or None


def read_meminfo():
    txt = _read(os.path.join(PROC, "meminfo"))
    if txt is None:
        return None
    out = {}
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().split()
        if v:
            try:
                out[k] = int(v[0])
            except ValueError:
                pass
    return out or None


def read_pressure(kind):
    txt = _read(os.path.join(PROC, "pressure", kind))
    if txt is None:
        return None, None
    some = full = None
    for line in txt.splitlines():
        m = re.match(r"(some|full)\s+avg10=([\d.]+)", line)
        if m:
            if m.group(1) == "some":
                some = float(m.group(2))
            else:
                full = float(m.group(2))
    return some, full


def read_diskstats():
    txt = _read(os.path.join(PROC, "diskstats"))
    if txt is None:
        return None
    out = {}
    for line in txt.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if re.match(r"^(loop|ram|dm-|sr)\d*$", name):
            continue
        if re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)\d+$",
                    name):
            continue
        try:
            out[name] = {"ios": int(f[3]) + int(f[7]),
                         "sectors": int(f[5]) + int(f[9]),
                         "io_ms": int(f[12]), "weighted_ms": int(f[13])}
        except ValueError:
            continue
    return out or None


def read_netdev():
    txt = _read(os.path.join(PROC, "net", "dev"))
    if txt is None:
        return None
    out = {}
    for line in txt.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        f = rest.split()
        if len(f) < 12:
            continue
        try:
            out[name] = {"rx_bytes": int(f[0]), "rx_pkts": int(f[1]),
                         "rx_drop": int(f[2]) + int(f[3]),
                         "tx_bytes": int(f[8]), "tx_pkts": int(f[9])}
        except ValueError:
            continue
    return out or None


def read_link_speed(iface):
    """Mbit/s from ethtool's sysfs view, or None.

    Virtual interfaces report -1 or refuse the read entirely, and a made-up
    link rate would turn every container into a false 'network bound'.
    """
    v = _read_int(os.path.join(SYS, "class", "net", iface, "speed"))
    return v if v and v > 0 else None


def read_snmp():
    out = {}
    for fname, want in (("snmp", ("RetransSegs", "OutSegs")),):
        txt = _read(os.path.join(PROC, "net", fname))
        if txt is None:
            continue
        lines = txt.splitlines()
        for i in range(0, len(lines) - 1, 2):
            keys, vals = lines[i].split(), lines[i + 1].split()
            if len(keys) != len(vals):
                continue
            for k, v in zip(keys[1:], vals[1:]):
                if k in want:
                    try:
                        out[k] = int(v)
                    except ValueError:
                        pass
    return out or None


def read_softnet():
    """Backlog drops and NAPI budget exhaustion, per CPU.

    /proc/net/softnet_stat is one row per CPU of hex counters. Only three
    columns are wanted: processed, dropped and time_squeeze. Nothing else
    on the box reports these, and between them they are the difference
    between "the network was slow" and "this box could not pick the
    packets up fast enough", which are opposite problems with opposite
    fixes.
    """
    txt = _read(os.path.join(PROC, "net", "softnet_stat"))
    if txt is None:
        return None
    processed = dropped = squeeze = 0
    rows = 0
    for line in txt.splitlines():
        cols = line.split()
        if len(cols) < 3:
            continue
        try:
            processed += int(cols[0], 16)
            dropped += int(cols[1], 16)
            squeeze += int(cols[2], 16)
        except ValueError:
            continue
        rows += 1
    if not rows:
        return None
    return {"processed": processed, "dropped": dropped,
            "time_squeeze": squeeze}


# The sysfs names differ by driver: intel calls a ring overflow
# rx_no_buffer_count, others rx_missed_errors, and rx_fifo_errors is the
# generic one. Summing what exists is right because a box has one driver,
# so at most one of these is ever non-zero.
_RING_MISS = ("rx_missed_errors", "rx_no_buffer_count", "rx_fifo_errors")


def read_net_errors(ifaces):
    """Ring-overflow counters, per interface, from sysfs."""
    out = {}
    for iface in ifaces:
        base = os.path.join(SYS, "class", "net", iface, "statistics")
        total, seen = 0, False
        for name in _RING_MISS:
            v = _read_int(os.path.join(base, name))
            if v is not None:
                total += v
                seen = True
        if seen:
            out[iface] = total
    return out or None


def read_nic_numa(iface):
    """Which NUMA node the card is attached to, on a machine with more
    than one. A single-node box has nothing to get wrong, so it reports
    None rather than 0 and the rule that reads it skips."""
    try:
        nodes = [d for d in os.listdir(
            os.path.join(SYS, "devices", "system", "node"))
            if re.match(r"^node\d+$", d)]
    except OSError:
        return None
    if len(nodes) < 2:
        return None
    v = _read_int(os.path.join(SYS, "class", "net", iface, "device",
                               "numa_node"))
    return v if v is not None and v >= 0 else None


def read_tcp_config():
    """The two settings that decide whether a throughput test could ever
    have reached line rate: the congestion control in use, and the largest
    receive buffer autotuning is allowed to reach."""
    out = {}
    cc = _read(os.path.join(PROC, "sys", "net", "ipv4",
                            "tcp_congestion_control"))
    if cc:
        out["cc"] = cc.strip()
    rmem = _read(os.path.join(PROC, "sys", "net", "ipv4", "tcp_rmem"))
    if rmem:
        parts = rmem.split()
        if len(parts) == 3:
            try:
                out["rmem_max"] = int(parts[2])
            except ValueError:
                pass
    return out or None


def read_cgroup_cpu():
    """Throttling counters, v2 then v1."""
    base = os.path.join(SYS, "fs", "cgroup")
    for stat_path in (os.path.join(base, "cpu.stat"),
                      os.path.join(base, "cpu", "cpu.stat")):
        stat = _read(stat_path)
        if not stat:
            continue
        out = {}
        for key in ("nr_periods", "nr_throttled"):
            m = re.search(r"^%s (\d+)" % key, stat, re.M)
            if m:
                out[key] = int(m.group(1))
        if out:
            return out
    return None


def read_freq_ghz():
    base = os.path.join(SYS, "devices", "system", "cpu", "cpu0", "cpufreq")
    cur = _read_int(os.path.join(base, "scaling_cur_freq"))
    return cur / 1e6 if cur else None


def read_procs():
    """pid -> (comm, ppid, cpu jiffies).  Skipped under --no-procs."""
    out = {}
    try:
        pids = [d for d in os.listdir(PROC) if d.isdigit()]
    except OSError:
        return None
    for pid in pids:
        txt = _read(os.path.join(PROC, pid, "stat"))
        if not txt:
            continue
        try:
            head, rest = txt.rsplit(") ", 1)
            comm = head.split("(", 1)[1]
            f = rest.split()
            out[int(pid)] = (comm, int(f[1]), int(f[11]) + int(f[12]))
        except (ValueError, IndexError):
            continue
    return out or None


# ---------------------------------------------------------------------------
# One sample
# ---------------------------------------------------------------------------
#
# A sample is the difference between two snapshots, so nothing here is a
# gauge read once and hoped over: rates are rates.

SAMPLE_FIELDS = [
    "host", "ts", "elapsed_s",
    "cpu_busy_pct", "cpu_sys_pct", "cpu_iowait_pct", "cpu_steal_pct",
    "cpu_max_core_pct", "cpu_cores",
    "runnable", "blocked",
    "psi_cpu_some", "psi_io_some", "psi_io_full", "psi_mem_full",
    "mem_available_pct", "swap_per_s",
    "disk", "disk_util_pct", "disk_await_ms", "disk_mbps",
    "net_if", "net_mbps", "net_link_mbps", "net_drop_per_s",
    "tcp_retrans_pct", "cg_throttled_pct", "cpu_ghz",
    # The receive path, which the whole-box numbers above cannot show. A
    # box that cannot pick packets up fast enough looks idle in every one
    # of them: the work is in softirq on one core, and the drops are in
    # counters nothing else here reads.
    "softirq_max_core_pct", "softnet_drop_per_s", "time_squeeze_per_s",
    "net_rx_missed_per_s",
    # Constant for a run, and carried per row for the same reason
    # net_link_mbps already is: --from-samples has to reproduce the whole
    # diagnosis from the file, with nothing read live.
    "net_cc", "net_rmem_max_kb", "net_numa",
    "top_comm", "top_cpu_pct",
]


def snapshot(with_procs=True):
    net = read_netdev()
    return {"t": time.monotonic(), "stat": read_stat(), "vm": read_vmstat(),
            "disk": read_diskstats(), "net": net,
            "snmp": read_snmp(), "cg": read_cgroup_cpu(),
            "softnet": read_softnet(),
            "neterr": read_net_errors(sorted(net)) if net else None,
            "procs": read_procs() if with_procs else None}


def _rate(a, b, dt):
    if a is None or b is None or not dt:
        return None
    if b < a:
        # the counter reset underneath us -- container restart, module
        # reload, wrap.  Unknown is None, never a negative rate.
        return None
    return (b - a) / dt


def derive(prev, cur, elapsed, host, exclude_tree=None):
    """Two snapshots into one row.  Blank stays blank."""
    dt = cur["t"] - prev["t"]
    s = dict((k, None) for k in SAMPLE_FIELDS)
    s["host"] = host
    s["ts"] = int(time.time())
    s["elapsed_s"] = round(elapsed, 2)

    a, b = prev["stat"], cur["stat"]
    if a and b:
        dtot = b["all"]["total"] - a["all"]["total"]
        didle = b["all"]["idle"] - a["all"]["idle"]
        dsys = b["all"]["sys"] - a["all"]["sys"]
        diow = b["all"]["iowait"] - a["all"]["iowait"]
        dsteal = b["all"]["steal"] - a["all"]["steal"]
        # any component going backwards means the pair is inconsistent
        # (reset between reads); blank stays blank
        if dtot > 0 and min(didle, dsys, diow, dsteal) >= 0:
            s["cpu_busy_pct"] = (dtot - didle) * 100.0 / dtot
            s["cpu_sys_pct"] = dsys * 100.0 / dtot
            s["cpu_iowait_pct"] = diow * 100.0 / dtot
            s["cpu_steal_pct"] = dsteal * 100.0 / dtot
        s["runnable"], s["blocked"] = b["running"], b["blocked"]
        # The busiest single core, because a serialised benchmark pins one
        # and leaves the whole-box average looking idle.
        if a.get("cores") and b.get("cores") and \
                len(a["cores"]) == len(b["cores"]):
            s["cpu_cores"] = len(b["cores"])
            per = []
            for x, y in zip(a["cores"], b["cores"]):
                d = y["total"] - x["total"]
                if d > 0 and y["idle"] >= x["idle"]:
                    per.append((d - (y["idle"] - x["idle"])) * 100.0 / d)
            if per:
                s["cpu_max_core_pct"] = max(per)

    for kind, keys in (("cpu", ("psi_cpu_some", None)),
                       ("io", ("psi_io_some", "psi_io_full")),
                       ("memory", (None, "psi_mem_full"))):
        some, full = read_pressure(kind)
        if keys[0]:
            s[keys[0]] = some
        if keys[1]:
            s[keys[1]] = full

    mi = read_meminfo()
    if mi and mi.get("MemTotal"):
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = (mi.get("MemFree", 0) + mi.get("Cached", 0)
                     + mi.get("Buffers", 0))
        s["mem_available_pct"] = avail * 100.0 / mi["MemTotal"]
    if prev["vm"] and cur["vm"]:
        swin = _rate(prev["vm"].get("pswpin"), cur["vm"].get("pswpin"), dt)
        swout = _rate(prev["vm"].get("pswpout"), cur["vm"].get("pswpout"), dt)
        if swin is not None and swout is not None:
            s["swap_per_s"] = swin + swout

    if prev["disk"] and cur["disk"]:
        worst = None
        for name, y in cur["disk"].items():
            x = prev["disk"].get(name)
            if not x:
                continue
            ios = y["ios"] - x["ios"]
            util = min(100.0, (y["io_ms"] - x["io_ms"]) / (dt * 1000.0) * 100.0)
            await_ms = ((y["weighted_ms"] - x["weighted_ms"]) / ios) \
                if ios > 0 else 0.0
            mbps = (y["sectors"] - x["sectors"]) * 512 / 1e6 / dt
            if worst is None or util > worst[1]:
                worst = (name, util, await_ms, mbps)
        if worst:
            s["disk"], s["disk_util_pct"] = worst[0], worst[1]
            s["disk_await_ms"], s["disk_mbps"] = worst[2], worst[3]

    if prev["net"] and cur["net"]:
        worst = None
        for name, y in cur["net"].items():
            x = prev["net"].get(name)
            if not x:
                continue
            mbps = ((y["rx_bytes"] - x["rx_bytes"])
                    + (y["tx_bytes"] - x["tx_bytes"])) * 8 / 1e6 / dt
            drops = _rate(x["rx_drop"], y["rx_drop"], dt)
            if worst is None or mbps > worst[1]:
                worst = (name, mbps, drops)
        if worst:
            s["net_if"], s["net_mbps"], s["net_drop_per_s"] = worst
            s["net_link_mbps"] = read_link_speed(worst[0])
            s["net_numa"] = read_nic_numa(worst[0])
            if prev["neterr"] and cur["neterr"]:
                a = prev["neterr"].get(worst[0])
                b = cur["neterr"].get(worst[0])
                s["net_rx_missed_per_s"] = _rate(a, b, dt)

    if prev["softnet"] and cur["softnet"]:
        s["softnet_drop_per_s"] = _rate(prev["softnet"]["dropped"],
                                        cur["softnet"]["dropped"], dt)
        s["time_squeeze_per_s"] = _rate(prev["softnet"]["time_squeeze"],
                                        cur["softnet"]["time_squeeze"], dt)

    # The busiest single core's softirq share.  Receive processing pinned
    # to one core is the usual reason a box plateaus well under line rate,
    # and it is invisible in the whole-box softirq figure.
    if prev["stat"] and cur["stat"]:
        a_cores, b_cores = prev["stat"]["cores"], cur["stat"]["cores"]
        if a_cores and len(a_cores) == len(b_cores):
            worst_si = None
            for a, b in zip(a_cores, b_cores):
                dtot = b["total"] - a["total"]
                if dtot <= 0:
                    continue
                pct = (b.get("softirq", 0) - a.get("softirq", 0)) \
                    * 100.0 / dtot
                if worst_si is None or pct > worst_si:
                    worst_si = pct
            if worst_si is not None:
                s["softirq_max_core_pct"] = worst_si

    tcp = read_tcp_config()
    if tcp:
        s["net_cc"] = tcp.get("cc")
        if tcp.get("rmem_max"):
            s["net_rmem_max_kb"] = tcp["rmem_max"] // 1024
    if prev["snmp"] and cur["snmp"]:
        dout = cur["snmp"].get("OutSegs", 0) - prev["snmp"].get("OutSegs", 0)
        dre = cur["snmp"].get("RetransSegs", 0) - prev["snmp"].get("RetransSegs", 0)
        if dout > 0:
            s["tcp_retrans_pct"] = dre * 100.0 / dout

    if prev["cg"] and cur["cg"]:
        dp = cur["cg"].get("nr_periods", 0) - prev["cg"].get("nr_periods", 0)
        dt_ = cur["cg"].get("nr_throttled", 0) - prev["cg"].get("nr_throttled", 0)
        if dp > 0:
            s["cg_throttled_pct"] = dt_ * 100.0 / dp
    s["cpu_ghz"] = read_freq_ghz()

    if prev["procs"] and cur["procs"]:
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        best = None
        for pid, (comm, _ppid, jiffies) in cur["procs"].items():
            if exclude_tree and pid in exclude_tree:
                continue
            old = prev["procs"].get(pid)
            if not old or old[0] != comm:
                continue
            pct = (jiffies - old[2]) / float(hz) / dt * 100.0
            if best is None or pct > best[1]:
                best = (comm, pct)
        if best and best[1] > 0.5:
            s["top_comm"], s["top_cpu_pct"] = best[0], best[1]
    return s


def descendants(procs, root_pid):
    """Every pid under root_pid, so the benchmark is not its own interloper."""
    if not procs or root_pid is None:
        return set()
    children = {}
    for pid, (_comm, ppid, _j) in procs.items():
        children.setdefault(ppid, []).append(pid)
    out, stack = {root_pid}, [root_pid]
    while stack:
        for kid in children.get(stack.pop(), []):
            if kid not in out:
                out.add(kid)
                stack.append(kid)
    return out


# ---------------------------------------------------------------------------
# Watching the window
# ---------------------------------------------------------------------------

def watch(args, child=None):
    """Sample until the command exits, the clock runs out, or ^C.

    ^C finishes the run rather than killing it: losing a twenty-minute
    benchmark's diagnosis to an impatient keystroke would be the worst
    behaviour this could have.
    """
    host = _read(os.path.join(PROC, "sys", "kernel", "hostname"))
    host = (host or "").strip() or "?"
    samples = []
    interrupted = [False]

    def on_int(_sig, _frm):
        interrupted[0] = True
    try:
        old_handler = signal.signal(signal.SIGINT, on_int)
    except ValueError:                      # not the main thread
        old_handler = None

    try:
        prev = snapshot(not args.no_procs)
        tree = descendants(prev["procs"], child.pid if child else None)
        t0 = prev["t"]
        tick = 0
        while True:
            tick += 1
            target = t0 + tick * args.interval
            early = False
            while True:
                if interrupted[0]:
                    break
                # Poll the child inside the wait, not after it: a command
                # that finishes in a tenth of a second must not cost a full
                # --interval of waiting before this returns.
                if child is not None and child.poll() is not None:
                    early = True
                    break
                left = target - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, 0.05))
            if interrupted[0]:
                break
            cur = snapshot(not args.no_procs)
            elapsed = cur["t"] - t0
            dt = cur["t"] - prev["t"]
            if child and cur["procs"]:
                # Re-derived every sample: a benchmark that forks workers
                # mid-run would otherwise look like an interloper.
                tree |= descendants(cur["procs"], child.pid)
            # Two intervals are never sampled.  Below about 20ms the
            # counters have not moved enough to make a rate out of.  And
            # the interval in which the wrapped command exited is half
            # benchmark and half nothing -- averaging it in is the warmup
            # mistake at the other end of the run, so it is dropped rather
            # than recorded as a real dip.
            if not early and elapsed >= args.settle and dt >= 0.02:
                samples.append(derive(prev, cur, elapsed, host, tree))
            prev = cur
            if early or (child is not None and child.poll() is not None):
                break
            if args.seconds and elapsed >= args.seconds:
                break
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
    return samples, interrupted[0]


# ---------------------------------------------------------------------------
# What was it bound by?
# ---------------------------------------------------------------------------
#
# Every sample gets exactly one state.  Reporting two at once would be a
# non-answer, and the shares would not add up -- so the tests below run in
# precedence order and the first that matches wins.  The order is why-slow's
# argument again: a box that is paging also looks CPU-busy, and a box whose
# quota is exhausted looks idle.

def _num(s, key):
    v = s.get(key)
    if v is None or v == "":
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    # A NaN or an infinity in a numeric column is corrupt input, not a
    # measurement, and letting one through would quietly poison every mean
    # and threshold downstream of it.  Blank means not measured.
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def classify(s):
    steal = _num(s, "cpu_steal_pct")
    if steal is not None and steal >= 10:
        return STOLEN
    thr = _num(s, "cg_throttled_pct")
    if thr is not None and thr >= 20:
        return THROTTLED
    swap = _num(s, "swap_per_s")
    memfull = _num(s, "psi_mem_full")
    if (swap is not None and swap >= 1000) or \
            (memfull is not None and memfull >= 10):
        return MEM
    iofull = _num(s, "psi_io_full")
    util = _num(s, "disk_util_pct")
    iowait = _num(s, "cpu_iowait_pct")
    if (iofull is not None and iofull >= 10) or \
            (util is not None and util >= 85) or \
            (iowait is not None and iowait >= 25):
        return IO
    mbps, link = _num(s, "net_mbps"), _num(s, "net_link_mbps")
    if mbps is not None and link and mbps >= 0.8 * link:
        return NET
    busy = _num(s, "cpu_busy_pct")
    if busy is not None and busy >= 85:
        return CPU
    # One core at its ceiling while the box as a whole looks idle: the work
    # is serialised, and this is the state most often mistaken for "the
    # machine was not the bottleneck".  More cores will not help it.
    core = _num(s, "cpu_max_core_pct")
    cores = _num(s, "cpu_cores")
    if core is not None and core >= 95 and (cores or 2) > 1:
        return SERIAL
    return FREE


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _cov(vals):
    """Coefficient of variation, as a percentage of the mean."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    if m == 0:
        return None
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return (var ** 0.5) * 100.0 / abs(m)


KEY_METRICS = [("cpu_busy_pct", "cpu"),
               # The busiest core matters as much as the average: on a
               # serialised run the average is flat and low while the core
               # doing the work is pinned, and judging stability from the
               # average would call a perfectly steady run noisy.
               ("cpu_max_core_pct", "the busiest core"),
               ("disk_util_pct", "disk"),
               ("net_mbps", "network"), ("psi_io_full", "io pressure")]


def pick_key_metric(samples):
    """The series that best represents 'how hard this run was working'.

    Whichever has the highest mean is the one the run was actually leaning
    on, and it is the series steady state and stability are judged from.
    Judging them from the CPU alone would call a disk-bound run unstable
    every time the CPU idled.
    """
    best = None
    for field, label in KEY_METRICS:
        m = _mean([_num(s, field) for s in samples])
        if m is None:
            continue
        scale = 100.0
        if field == "net_mbps":
            links = [_num(s, "net_link_mbps") for s in samples]
            link = _median(links)
            if not link:
                continue
            scale = link
        norm = m * 100.0 / scale
        if best is None or norm > best[2]:
            best = (field, label, norm)
    return best


def find_steady(samples, field, tol=20.0):
    """Index of the first sample that looks like steady state.

    The tail is the reference: compare each sample to the median of the
    last half of the run and take the first index after which nothing
    deviates by more than the tolerance.  Averaging warmup in with steady
    state is how a benchmark reports a number the system never sustained.
    """
    vals = [_num(s, field) for s in samples]
    if len([v for v in vals if v is not None]) < 6:
        return None
    ref = _median(vals[len(vals) // 2:])
    if ref is None or ref == 0:
        return None
    band = abs(ref) * tol / 100.0
    for i in range(len(vals)):
        rest = vals[i:]
        if all(v is None or abs(v - ref) <= band for v in rest):
            return i
    return None


def analyse(samples, meta=None):
    """Sample series -> summary.  A pure function, which is what makes
    --from-samples a complete harness for everything below."""
    f = dict(meta or {})
    f["run.samples"] = len(samples)
    f["run.host"] = samples[0].get("host") if samples else None
    f["run.seconds"] = (_num(samples[-1], "elapsed_s") if samples else None)
    f["run.interval_s"] = None
    if len(samples) >= 2:
        a, b = _num(samples[0], "elapsed_s"), _num(samples[1], "elapsed_s")
        if a is not None and b is not None:
            f["run.interval_s"] = round(b - a, 3)

    states = [classify(s) for s in samples]
    f["bound.states"] = states
    shares = {}
    for st in STATES:
        shares[st] = states.count(st) * 100.0 / len(states) if states else None
    f["bound.shares"] = shares if states else None
    f["bound.by"] = None
    f["bound.share"] = None
    if states:
        # Ties go to the earlier state in STATES, which is precedence order,
        # so a run split evenly between stolen and cpu reports the cause.
        best = max(STATES, key=lambda st: (shares[st], -STATES.index(st)))
        f["bound.by"], f["bound.share"] = best, shares[best]
    f["bound.free_share"] = shares.get(FREE) if states else None
    f["bound.changed"] = None
    if states:
        seen = []
        for st in states:
            if not seen or seen[-1] != st:
                seen.append(st)
        f["bound.changed"] = len([s for s in seen if s != FREE]) > 1

    key = pick_key_metric(samples)
    f["key.metric"] = key[0] if key else None
    f["key.label"] = key[1] if key else None
    f["steady.from_index"] = None
    f["steady.from_s"] = None
    f["steady.warmup_pct"] = None
    f["steady.cov_pct"] = None
    if key:
        idx = find_steady(samples, key[0])
        f["steady.from_index"] = idx
        if idx is not None:
            f["steady.from_s"] = _num(samples[idx], "elapsed_s")
            f["steady.warmup_pct"] = idx * 100.0 / len(samples)
            f["steady.cov_pct"] = _cov([_num(s, key[0])
                                        for s in samples[idx:]])
        else:
            f["steady.cov_pct"] = _cov([_num(s, key[0]) for s in samples])

    # Anything that moved during the run rather than across it.
    f["drift.freq_start_ghz"] = None
    f["drift.freq_end_ghz"] = None
    f["drift.throttle_late_pct"] = None
    f["drift.steal_pct"] = _mean([_num(s, "cpu_steal_pct") for s in samples])
    if samples:
        head = samples[:max(1, len(samples) // 4)]
        tail = samples[-max(1, len(samples) // 4):]
        f["drift.freq_start_ghz"] = _mean([_num(s, "cpu_ghz") for s in head])
        f["drift.freq_end_ghz"] = _mean([_num(s, "cpu_ghz") for s in tail])
        f["drift.throttle_late_pct"] = _mean(
            [_num(s, "cg_throttled_pct") for s in tail])
        f["drift.disk_mbps_head"] = _mean([_num(s, "disk_mbps") for s in head])
        f["drift.disk_mbps_tail"] = _mean([_num(s, "disk_mbps") for s in tail])
        f["drift.disk_util_tail"] = _mean([_num(s, "disk_util_pct")
                                           for s in tail])

    # Somebody else's work, inside your window.
    f["intruders"] = None
    comms = [s.get("top_comm") for s in samples if s.get("top_comm")]
    if comms:
        counts = {}
        peak = {}
        for s in samples:
            c = s.get("top_comm")
            if not c:
                continue
            counts[c] = counts.get(c, 0) + 1
            pct = _num(s, "top_cpu_pct") or 0.0
            peak[c] = max(peak.get(c, 0.0), pct)
        f["intruders"] = sorted(
            ({"comm": c, "samples": n, "share_pct": n * 100.0 / len(samples),
              "peak_cpu_pct": peak[c]}
             for c, n in counts.items()
             if peak[c] >= 20 and n < 0.9 * len(samples)),
            key=lambda d: -d["peak_cpu_pct"])
    # The receive path.  These are worst-of rather than mean: a ring that
    # overflowed for ten seconds of a five-minute run dropped packets, and
    # averaging that to nearly zero would report a clean run.
    def _worst(field):
        vals = [_num(s, field) for s in samples]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def _last(field):
        for s in reversed(samples):
            v = s.get(field)
            if v not in (None, ""):
                return v
        return None

    f["net.softirq_max_core_pct"] = _worst("softirq_max_core_pct")
    f["net.softnet_drop_per_s"] = _worst("softnet_drop_per_s")
    f["net.time_squeeze_per_s"] = _worst("time_squeeze_per_s")
    f["net.rx_missed_per_s"] = _worst("net_rx_missed_per_s")
    f["net.cpu_busy_pct"] = _mean([_num(s, "cpu_busy_pct") for s in samples])
    f["net.link_mbps"] = _last("net_link_mbps")
    f["net.mbps_mean"] = _mean([_num(s, "net_mbps") for s in samples])
    f["net.if_name"] = _last("net_if")
    f["net.cc"] = _last("net_cc")
    f["net.numa"] = _last("net_numa")
    rmem_kb = _last("net_rmem_max_kb")
    try:
        f["net.rmem_max_kb"] = float(rmem_kb) if rmem_kb is not None else None
    except (TypeError, ValueError):
        f["net.rmem_max_kb"] = None

    # Bandwidth-delay product, and so whether the receive window could ever
    # have held enough in flight to fill the link.  The RTT is not measured
    # here -- during watches one box and a round trip needs two -- so it
    # arrives by flag, from netmesh or from anything else that measured it.
    # Without it the rule skips and says so, rather than assuming a number.
    try:
        link = float(f["net.link_mbps"]) if f["net.link_mbps"] else None
    except (TypeError, ValueError):
        link = None
    rtt = f.get("net.rtt_ms")
    f["net.bdp_kb"] = None
    if link and rtt:
        try:
            f["net.bdp_kb"] = link * 1e6 * (float(rtt) / 1000.0) / 8 / 1024.0
        except (TypeError, ValueError):
            f["net.bdp_kb"] = None

    f["run.excluded_tree"] = bool((meta or {}).get("run.wrapped"))
    return f


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

Rule = namedtuple("Rule", "id title needs level say fix why")

MISSING_REASONS = {
    "bound.by": "nothing was sampled -- the window was shorter than one "
                "interval",
    "steady.cov_pct": "too few samples to judge stability -- run for longer "
                      "or shorten --interval",
    "steady.from_index": "no steady state could be identified: fewer than "
                         "six samples, or the run never settled",
    "key.metric": "no usable series -- every candidate metric was blank",
    "intruders": "per-process sampling is off (--no-procs) or /proc/<pid> "
                 "was unreadable",
    "drift.freq_start_ghz": "no cpufreq information on this machine",
    "drift.throttle_late_pct": "not in a CPU-limited cgroup",
    "baseline.key_delta_pct": "no --baseline given",
    "net.softirq_max_core_pct": "per-core CPU time was not sampled",
    "net.rx_missed_per_s": "this interface exposes no ring-overflow "
                           "counter (virtual, or a driver that omits it)",
    "net.softnet_drop_per_s": "no /proc/net/softnet_stat on this kernel",
    "net.time_squeeze_per_s": "no /proc/net/softnet_stat on this kernel",
    "net.bdp_kb": "no round trip known -- pass --rtt-ms, or run "
                  "`netmesh check` against the peer and use its p50",
    "net.expect_mbps": "nothing said what this test should have been "
                       "pushing -- pass --expect-mbps to have the "
                       "interface total checked against it",
    "net.rmem_max_kb": "net.ipv4.tcp_rmem was unreadable",
    "net.numa": "one NUMA node, or the card does not say which it is on",
    "peer.overlap_pct": "no --peer-samples given, or neither run carried "
                        "timestamps",
    "peer.bound_by": "no --peer-samples given -- run during on the other "
                     "end of the test and pass its series here",
    "peer.rx_missed_per_s": "the peer's series carries no ring-overflow "
                            "column, or no --peer-samples was given",
}

RULES = []


def rule(rid, title, needs, why):
    def deco(fn):
        level, say, fix = fn()
        RULES.append(Rule(rid, title, needs, level, say, fix, why))
        return fn
    return deco


def _g(f, key, default=None):
    v = f.get(key)
    return default if v is None else v


# 1 ------------------------------------------------------------------------
@rule("NOT_BOUND", "not the bottleneck",
      ("bound.free_share",),
      "The most useful benchmark finding there is: if nothing here was near "
      "a ceiling, the limit was the load generator, the peer, or a lock "
      "inside the application, and tuning this machine cannot help.")
def _not_bound():
    def level(f):
        if f["bound.free_share"] >= 80:
            return WARN
        if f["bound.free_share"] >= 50:
            return INFO
        return None

    def say(f):
        return ("no resource was near a ceiling for %.0f%% of the run"
                % f["bound.free_share"])

    def fix(f):
        return ("Whatever limited this run was not on this box.  In order of "
                "likelihood: the load generator is saturated (check it with "
                "this same tool while it drives), the work is serialised "
                "inside the application and no amount of hardware will help, "
                "or the peer is the slow end.  Adding CPU or disk here will "
                "change nothing -- the machine was idle waiting for work.")
    return level, say, fix


# 2 ------------------------------------------------------------------------
@rule("BOUND_BY", "what limited the run",
      ("bound.by", "bound.share"),
      "The bottleneck is the resource at its ceiling for the longest, not "
      "the one with the biggest number when you happened to look -- a single "
      "sample can land in either.")
def _bound_by():
    def level(f):
        if f["bound.by"] == FREE:
            return None                 # NOT_BOUND's business
        return INFO if f["bound.share"] >= 50 else None

    def say(f):
        return ("%s for %.0f%% of the run (%s)"
                % (f["bound.by"], f["bound.share"],
                   STATE_TEXT.get(f["bound.by"], "")))

    def fix(f):
        advice = {
            CPU: "This is real compute demand.  The number this run produced "
                 "is a CPU number, so compare it only against runs with the "
                 "same core count and clock -- and check the governor before "
                 "believing any of it.",
            IO: "Storage was the limit, so this benchmark measured your "
                "disk rather than whatever you meant to measure.  Re-run "
                "against a warmed page cache, or on the device you actually "
                "intend to ship on.",
            MEM: "The box was paging during the run, which makes every other "
                 "number here a measurement of the paging.  Fit the working "
                 "set and run it again; nothing else is worth reading.",
            NET: "The interface was at line rate, so this is a link "
                 "measurement.  `netmesh check` for the idle path and "
                 "`iperf_orchestrator` for bandwidth under load will tell "
                 "you whether the link or the host is the limit.",
            THROTTLED: "The cgroup quota stopped this run repeatedly, so the "
                       "number is a measurement of the quota.  Raise it or "
                       "remove it before benchmarking anything else.",
            STOLEN: "The hypervisor took time from this guest during the "
                    "run, so the result is not reproducible and not yours.  "
                    "Benchmark on dedicated capacity, or report the steal "
                    "percentage alongside every number you publish.",
            SERIAL: "One core was pinned while the rest of the box idled, so "
                    "this run was limited by a single thread.  More cores "
                    "will not move the number and neither will a bigger "
                    "instance -- the work is serialised.  Find the stage "
                    "that does not parallelise before changing anything "
                    "about the hardware.",
        }
        return advice.get(f["bound.by"], "")
    return level, say, fix


# 3 ------------------------------------------------------------------------
@rule("UNSTABLE", "never settled", ("steady.cov_pct",),
      "A benchmark whose own load varies by a third from sample to sample "
      "cannot be compared with anything, including itself an hour later.")
def _unstable():
    def level(f):
        if f["steady.cov_pct"] >= 40:
            return WARN
        if f["steady.cov_pct"] >= 20:
            return INFO
        return None

    def say(f):
        return ("%s varied by %.0f%% across the steady window"
                % (_g(f, "key.label", "the load"), f["steady.cov_pct"]))

    def fix(f):
        return ("Numbers from a run this variable cannot be compared with "
                "each other, so a difference between two runs means nothing "
                "yet.  Lengthen the run, pin the frequency governor, and "
                "check the findings above for something arriving in bursts "
                "-- then re-run and expect the variation to fall before you "
                "read any result.")
    return level, say, fix


# 4 ------------------------------------------------------------------------
@rule("WARMUP", "warmup in the average",
      ("steady.warmup_pct", "steady.from_s"),
      "Averaging warmup in with steady state reports a number the system "
      "never actually sustained, in whichever direction the warmup went.")
def _warmup():
    def level(f):
        if f["steady.warmup_pct"] >= 30:
            return WARN
        if f["steady.warmup_pct"] >= 10:
            return INFO
        return None

    def say(f):
        return ("steady only from %.0fs, so %.0f%% of the samples are warmup"
                % (f["steady.from_s"], f["steady.warmup_pct"]))

    def fix(f):
        return ("Any average over the whole window includes that ramp and is "
                "a number the system never sustained.  Re-run with "
                "`--settle %.0f` to discard it, or take the result from the "
                "steady window only -- and say which you did when you "
                "report it." % max(1, _g(f, "steady.from_s", 0)))
    return level, say, fix


# 5 ------------------------------------------------------------------------
@rule("INTRUDER", "something else ran", ("intruders",),
      "A backup, a cron job or somebody's ad-hoc query inside the benchmark "
      "window makes the result a measurement of that instead, and it leaves "
      "no trace in the benchmark's own output.")
def _intruder():
    def level(f):
        if not f["intruders"]:
            return None
        worst = f["intruders"][0]
        if worst["peak_cpu_pct"] >= 50:
            return WARN
        return INFO

    def say(f):
        w = f["intruders"][0]
        return ("%s took %.0f%% CPU in %.0f%% of the samples%s"
                % (w["comm"], w["peak_cpu_pct"], w["share_pct"],
                   " (+%d more)" % (len(f["intruders"]) - 1)
                   if len(f["intruders"]) > 1 else ""))

    def fix(f):
        w = f["intruders"][0]
        s = ("Whatever %s is, it was competing for this machine while the "
             "benchmark ran, so the result is a measurement of the pair of "
             "them.  " % w["comm"])
        if f.get("run.excluded_tree"):
            s += ("The wrapped command's own process tree is already "
                  "excluded, so this is genuinely something else.  ")
        else:
            s += ("Nothing was excluded on this run -- if that is the "
                  "benchmark itself, wrap it (`%s -- ./bench.sh`) and its "
                  "tree will be discounted.  " % PROG)
        s += "Stop it, or re-run when it is not scheduled."
        return s
    return level, say, fix


# 6 ------------------------------------------------------------------------
@rule("CLOCK_DRIFT", "cpu clock fell",
      ("drift.freq_start_ghz", "drift.freq_end_ghz"),
      "A clock that falls partway through means the second half of the run "
      "was done on a slower machine than the first, and the average is of "
      "two different computers.")
def _clock_drift():
    def level(f):
        start, end = f["drift.freq_start_ghz"], f["drift.freq_end_ghz"]
        if start <= 0:
            return None
        drop = (start - end) * 100.0 / start
        if drop >= 15:
            return WARN
        if drop >= 7:
            return INFO
        return None

    def say(f):
        return ("%.2f GHz at the start, %.2f GHz by the end"
                % (f["drift.freq_start_ghz"], f["drift.freq_end_ghz"]))

    def fix(f):
        return ("Thermal or power limits, almost always -- the first half of "
                "this run was done on a faster machine than the second, and "
                "the average is of both.  Check airflow and the BIOS power "
                "profile, pin the governor with `cpupower frequency-set -g "
                "performance`, and re-run.  Until then a longer run will "
                "score worse than a short one for no reason connected to "
                "what you are measuring.")
    return level, say, fix


# 7 ------------------------------------------------------------------------
@rule("BURST_EXHAUSTED", "storage throttled",
      ("drift.disk_mbps_head", "drift.disk_mbps_tail", "drift.disk_util_tail"),
      "A cloud volume whose burst balance runs out mid-run keeps its "
      "utilisation pinned while its throughput collapses, which reads as "
      "'the disk is busy' rather than 'the disk was throttled'.")
def _burst_exhausted():
    def level(f):
        head, tail = f["drift.disk_mbps_head"], f["drift.disk_mbps_tail"]
        if head <= 0 or f["drift.disk_util_tail"] < 50:
            return None
        return WARN if tail <= 0.6 * head else None

    def say(f):
        return ("%.0f MB/s early, %.0f MB/s late, with the device still "
                "%.0f%% busy" % (f["drift.disk_mbps_head"],
                                 f["drift.disk_mbps_tail"],
                                 f["drift.disk_util_tail"]))

    def fix(f):
        return ("Throughput fell while utilisation stayed high, which is the "
                "signature of a credit or burst budget running out rather "
                "than of a device getting busier -- gp2/gp3 balances, a "
                "throttled managed disk, or a shared array.  Any benchmark "
                "longer than the burst window measures the baseline rate, "
                "and any shorter one measures the burst.  Decide which you "
                "meant to publish.")
    return level, say, fix


# 8 ------------------------------------------------------------------------
@rule("STEAL", "hypervisor took time",
      ("drift.steal_pct",),
      "Steal time is capacity given to another tenant. It cannot be tuned "
      "away from inside the guest, and it makes a result unrepeatable by "
      "anyone, including you tomorrow.")
def _steal():
    def level(f):
        if f["drift.steal_pct"] >= 10:
            return WARN
        if f["drift.steal_pct"] >= 3:
            return INFO
        return None

    def say(f):
        return ("%.1f%% of CPU time went to another tenant across the run"
                % f["drift.steal_pct"])

    def fix(f):
        return ("Nothing inside this guest can recover that time, and it "
                "will differ on the next run, so any comparison against "
                "another number is measuring the neighbours as much as the "
                "change.  Benchmark on dedicated capacity, or publish the "
                "steal percentage next to every figure.")
    return level, say, fix


# 9 ------------------------------------------------------------------------
@rule("SHIFTED", "the limit moved", ("bound.changed",),
      "A run that is CPU-bound early and I/O-bound late has no single "
      "answer, and any average over it describes a machine that never "
      "existed.")
def _shifted():
    def level(f):
        return INFO if f["bound.changed"] else None

    def say(f):
        shares = _g(f, "bound.shares", {})
        parts = ["%s %.0f%%" % (k, v) for k, v in sorted(shares.items(),
                                                         key=lambda kv: -kv[1])
                 if v]
        return "the limit moved: %s" % ", ".join(parts[:4])

    def fix(f):
        return ("There is no single bottleneck to report for this run.  "
                "Either the workload has phases -- in which case benchmark "
                "them separately, because the average describes a machine "
                "that never existed -- or something changed underneath it, "
                "which the other findings here will name.")
    return level, say, fix


# 10 -----------------------------------------------------------------------
@rule("SHORT_RUN", "window too short",
      ("run.samples",),
      "Under about ten samples, steady state cannot be separated from noise "
      "and every share below is a coin toss dressed as a percentage.")
def _short_run():
    def level(f):
        if f["run.samples"] < 5:
            return WARN
        if f["run.samples"] < 10:
            return INFO
        return None

    def say(f):
        return ("%d sample%s is not enough to separate steady state from "
                "noise" % (f["run.samples"],
                           "" if f["run.samples"] == 1 else "s"))

    def fix(f):
        return ("Every share above is computed from those samples, so treat "
                "them as a hint and not a result.  Run for at least 30 "
                "seconds, or shorten --interval if the benchmark itself is "
                "genuinely that quick -- but a benchmark shorter than its "
                "own warmup is measuring warmup.")
    return level, say, fix


# 11 -----------------------------------------------------------------------
@rule("BASELINE_DRIFT", "differs from baseline",
      ("baseline.key_delta_pct",),
      "Two runs of the same benchmark on the same box should load it the "
      "same way; when they do not, the difference is the finding, whatever "
      "the benchmark's own number says.")
def _baseline_drift():
    def level(f):
        d = abs(f["baseline.key_delta_pct"])
        # The same benchmark hitting a different ceiling than last time is
        # a finding even when the headline number matches, because the two
        # runs are then measuring different things and agreeing by luck.
        moved = f.get("baseline.bound_by") != f.get("bound.by")
        if d >= 25 or (moved and d >= 5):
            return WARN
        if d >= 10 or moved:
            return INFO
        return None

    def say(f):
        d = f["baseline.key_delta_pct"]
        return ("%s is %.0f%% %s than the baseline run%s"
                % (_g(f, "key.label", "the load"), abs(d),
                   "higher" if d > 0 else "lower",
                   ", and the limit changed from %s to %s"
                   % (f.get("baseline.bound_by"), f.get("bound.by"))
                   if f.get("baseline.bound_by") != f.get("bound.by") else ""))

    def fix(f):
        return ("The two runs did not load the machine the same way, so "
                "comparing their headline numbers compares two different "
                "experiments.  Find out why this one differs -- the findings "
                "above usually say -- before reading anything into the "
                "benchmark's own result.")
    return level, say, fix


# --- the receive path ------------------------------------------------------
#
# Everything above watches the box as a whole. These watch the one part of
# it that a network test lives or dies on, and that a whole-box average is
# structurally incapable of showing: a machine that cannot pick packets up
# fast enough is not busy, it is busy *on one core, in softirq*, and every
# aggregate number on the page reports it as idle.


@rule("SOFTIRQ_BOUND", "one core pinned in softirq",
      ("net.softirq_max_core_pct", "net.cpu_busy_pct"),
      "Receive processing that lands on a single core caps throughput at "
      "whatever that one core can do, however many others are idle. It is "
      "the most common reason a box plateaus well under line rate, and the "
      "whole-box CPU figure reports the machine as almost idle throughout.")
def _softirq_bound():
    def level(f):
        si = f["net.softirq_max_core_pct"]
        if si >= 90:
            return CRITICAL
        # Half a core in softirq while the box overall is quiet is already
        # the shape of the problem, even before it saturates.
        if si >= 50 and f["net.cpu_busy_pct"] < 50:
            return WARN
        return None

    def say(f):
        return ("one core reached %.0f%% softirq while the box averaged "
                "%.0f%% busy" % (f["net.softirq_max_core_pct"],
                                 f["net.cpu_busy_pct"]))

    def fix(f):
        return ("Receive processing is not spreading across cores.  Check "
                "that the card has multiple rx queues and that RSS is "
                "steering to more than one CPU (`ethtool -l`, `ethtool -x`), "
                "and that irqbalance or a manual smp_affinity is not pinning "
                "every queue to the same core.  Until it spreads, this box's "
                "ceiling is one core, not the link.")
    return level, say, fix


@rule("RING_OVERFLOW", "the card dropped packets",
      ("net.rx_missed_per_s",),
      "rx_missed_errors and friends count packets the card had nowhere to "
      "put: the ring was full because the host did not drain it in time. "
      "These are losses the network never caused and every network-side "
      "measurement will blame it for.")
def _ring_overflow():
    def level(f):
        r = f["net.rx_missed_per_s"]
        if r <= 0:
            return None
        return CRITICAL if r >= 1000 else WARN

    def say(f):
        return ("the interface missed %.0f packets/s at its worst"
                % f["net.rx_missed_per_s"])

    def fix(f):
        return ("These were dropped on this box, not in the network, so any "
                "loss figure from the far end is counting them against the "
                "path.  Raise the rx ring (`ethtool -G`) and find out why "
                "the host stopped draining it -- the softirq finding above, "
                "if present, is usually the reason.")
    return level, say, fix


@rule("BACKLOG_DROPS", "the kernel backlog overflowed",
      ("net.softnet_drop_per_s",),
      "Packets that got past the card and were dropped queueing for a CPU. "
      "Distinct from a ring overflow: the card kept up and the host's own "
      "backlog did not.")
def _backlog_drops():
    def level(f):
        return WARN if f["net.softnet_drop_per_s"] > 0 else None

    def say(f):
        return ("softnet backlog dropped %.0f packets/s at its worst"
                % f["net.softnet_drop_per_s"])

    def fix(f):
        return ("net.core.netdev_max_backlog is the queue that overflowed, "
                "and raising it is the symptomatic fix.  The cause is that "
                "some CPU could not keep up: spread the receive load first "
                "and only then raise the backlog.")
    return level, say, fix


@rule("TIME_SQUEEZE", "napi ran out of budget",
      ("net.time_squeeze_per_s",),
      "A time squeeze is the receive poll being cut off with work still "
      "queued. A few are normal under load; a steady stream means the box "
      "is permanently behind the arrival rate, which shows up downstream "
      "as latency and loss that look like network faults.")
def _time_squeeze():
    def level(f):
        ts = f["net.time_squeeze_per_s"]
        if ts >= 100:
            return WARN
        return INFO if ts > 0 else None

    def say(f):
        return ("napi polls cut short %.0f times/s at their worst"
                % f["net.time_squeeze_per_s"])

    def fix(f):
        return ("net.core.netdev_budget and netdev_budget_usecs decide how "
                "long a poll may run.  As with the backlog, raising them "
                "treats the symptom: a box squeezing steadily is one whose "
                "receive path needs more CPUs on it, not longer turns on "
                "the same one.")
    return level, say, fix


@rule("WINDOW_LIMITED", "the receive window capped it",
      ("net.bdp_kb", "net.rmem_max_kb"),
      "Throughput on a TCP flow cannot exceed window / round trip, whatever "
      "the link is capable of. A test whose ceiling was the receive buffer "
      "measured the buffer, and reports a number about the network that the "
      "network had no part in.")
def _window_limited():
    def level(f):
        # A window at least the bandwidth-delay product is what filling the
        # link requires; below that the arithmetic caps you and no amount of
        # tuning elsewhere moves the number.
        if f["net.rmem_max_kb"] >= f["net.bdp_kb"]:
            return None
        return CRITICAL if f["net.rmem_max_kb"] < f["net.bdp_kb"] / 2 \
            else WARN

    def say(f):
        rmem, bdp = f["net.rmem_max_kb"], f["net.bdp_kb"]
        ceiling = rmem * 1024 * 8 / (float(f["net.rtt_ms"]) / 1000.0) / 1e6
        return ("rmem_max %.0f kB against a %.0f kB bandwidth-delay product: "
                "one flow tops out near %.0f Mbit/s" % (rmem, bdp, ceiling))

    def fix(f):
        return ("This run could not have reached line rate on a single flow "
                "no matter what the network did.  Either raise "
                "net.ipv4.tcp_rmem's third value past %.0f kB, or use "
                "parallel streams and read the aggregate -- but do not "
                "record this run's number as what the path can carry."
                % f["net.bdp_kb"])
    return level, say, fix


@rule("NIC_NUMA", "the card is on one numa node", ("net.numa",),
      "On a multi-socket box the card is attached to one node, and load "
      "driven from the other pays a cross-socket hop for every packet. "
      "Worth knowing before a result is recorded as the machine's ceiling.")
def _nic_numa():
    def level(f):
        return INFO

    def say(f):
        return "%s is attached to NUMA node %d" % (
            _g(f, "net.if_name", "the interface"), f["net.numa"])

    def fix(f):
        return ("Pin the load to node %d (`numactl --cpunodebind=%d "
                "--membind=%d`) and compare.  A cross-socket difference of "
                "10-20%% on a throughput test is ordinary, and it is the "
                "kind of difference that gets attributed to the network."
                % (f["net.numa"], f["net.numa"], f["net.numa"]))
    return level, say, fix


@rule("NET_INTRUDER", "traffic that was not yours",
      ("net.mbps_mean", "net.expect_mbps"),
      "The CPU equivalent of this is already checked: something else "
      "running inside the window makes the result a measurement of both. "
      "The link is no different, and it is easier to miss, because a "
      "backup stream through the same interface leaves no trace in the "
      "benchmark's own output and looks exactly like your own traffic in "
      "every whole-interface number.")
def _net_intruder():
    def level(f):
        want, got = f["net.expect_mbps"], f["net.mbps_mean"]
        if want <= 0:
            return None
        # A quarter over is past measurement error and framing overhead;
        # double is a window that measured somebody else as much as you.
        over = got / want
        if over >= 2.0:
            return WARN
        return INFO if over >= 1.25 else None

    def say(f):
        return ("the interface carried %.0f Mbit/s while this test says it "
                "sent %.0f" % (f["net.mbps_mean"], f["net.expect_mbps"]))

    def fix(f):
        return ("Something else was on this link during the window -- a "
                "backup, a replication stream, another test -- so the "
                "throughput you recorded and the load the link actually "
                "carried are different numbers.  Find it before believing "
                "either: the difference is %.0f Mbit/s."
                % (f["net.mbps_mean"] - f["net.expect_mbps"]))
    return level, say, fix


# --- the other end ---------------------------------------------------------
#
# A network test has two machines in it, and every tool in this package
# watches one. These four rules are the ones that need both, and none of
# them can be reached from either machine's own report.


@rule("PEER_NOT_CONCURRENT", "the two runs did not overlap",
      ("peer.overlap_pct",),
      "Two windows that did not happen at the same time are two "
      "experiments. Deciding which end was the limit by comparing them is "
      "meaningless, however carefully each was measured.")
def _peer_not_concurrent():
    def level(f):
        o = f["peer.overlap_pct"]
        if o <= 0:
            return CRITICAL
        return WARN if o < 50 else None

    def say(f):
        o = f["peer.overlap_pct"]
        if o <= 0:
            return "the two windows do not overlap at all"
        return "the two windows overlap for only %.0f%% of the shorter" % o

    def fix(f):
        return ("Run both ends over the same window before comparing them.  "
                "If the clocks are the question rather than the timing, "
                "`skew` on both boxes answers it -- two machines that "
                "disagree about the time will report windows that did not "
                "overlap when they did.")
    return level, say, fix


@rule("PEER_WAS_THE_LIMIT", "the other end was the limit",
      ("peer.bound_by", "bound.by"),
      "This box at no ceiling while the far end sits at one is the single "
      "most common way a load test lies: the number describes the machine "
      "nobody was watching. Neither end's own report can say it.")
def _peer_was_the_limit():
    def level(f):
        if f["bound.by"] != FREE or f["peer.bound_by"] == FREE:
            return None
        return WARN

    def say(f):
        return ("this box was at no ceiling while %s was %s for %.0f%% of "
                "its samples"
                % (_g(f, "peer.host", "the peer"), f["peer.bound_by"],
                   _g(f, "peer.bound_share", 0.0)))

    def fix(f):
        return ("The result is the other machine's ceiling, not this one's "
                "and not the path's.  %s is where to look -- and if it is "
                "the load generator, the benchmark measured the generator, "
                "which is the most common way a load test lies."
                % _g(f, "peer.host", "The peer"))
    return level, say, fix


@rule("NEITHER_END_BOUND", "neither machine was the limit",
      ("peer.bound_by", "bound.by"),
      "Both ends idle and the number still short means the limit is "
      "between them or inside the application: the path, a lock, or a "
      "single-threaded flow. It is the finding that most needs two "
      "machines to reach, and neither box's own report can state it.")
def _neither_end_bound():
    def level(f):
        return INFO if f["bound.by"] == FREE and f["peer.bound_by"] == FREE \
            else None

    def say(f):
        return ("neither this box nor %s was near a ceiling"
                % _g(f, "peer.host", "the peer"))

    def fix(f):
        return ("Both machines had capacity to spare, so the limit is "
                "between them or inside the application.  `netmesh check` "
                "between the two measures the path; a single flow that "
                "cannot fill it, or a lock in the application, accounts for "
                "most of the rest.")
    return level, say, fix


@rule("PEER_DROPPED", "the other end dropped packets",
      ("peer.rx_missed_per_s",),
      "Loss at the far end's card is loss the network never caused, and it "
      "is loss this end cannot see: from here it is indistinguishable from "
      "a lossy path.")
def _peer_dropped():
    def level(f):
        return WARN if f["peer.rx_missed_per_s"] > 0 else None

    def say(f):
        return ("%s missed %.0f packets/s at its worst"
                % (_g(f, "peer.host", "the peer"),
                   f["peer.rx_missed_per_s"]))

    def fix(f):
        return ("Those packets were dropped on %s, not in the network, so "
                "any loss figure measured from this end is counting them "
                "against the path.  The receive-path findings in that "
                "machine's own report say why it could not keep up."
                % _g(f, "peer.host", "the peer"))
    return level, say, fix


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------
#
# Trust first: whether the number can be believed outranks what the number
# was limited by, because a bottleneck attributed from an invalid run is a
# confident wrong answer, which is worse than no answer.
VERDICT_PRECEDENCE = [
    "SHORT_RUN", "STEAL", "INTRUDER", "CLOCK_DRIFT", "BURST_EXHAUSTED",
    "UNSTABLE", "WARMUP", "BASELINE_DRIFT",
    # Trust outranks attribution, and a window-limited run is untrustworthy
    # in the same way a warmup-contaminated one is: its number is about the
    # configuration rather than about the path.
    "WINDOW_LIMITED", "NET_INTRUDER", "PEER_NOT_CONCURRENT",
    # Then the receive-path causes, ahead of the states they produce: a box
    # pinned in softirq on one core is why the run looks network bound, and
    # naming the symptom sends someone to the switch.
    "SOFTIRQ_BOUND", "RING_OVERFLOW", "BACKLOG_DROPS", "TIME_SQUEEZE",
    "PEER_WAS_THE_LIMIT", "PEER_DROPPED", "NEITHER_END_BOUND",
    "NOT_BOUND", "SHIFTED", "BOUND_BY", "NIC_NUMA",
]

Finding = namedtuple("Finding", "rule severity say fix")


def missing_reason(f, key):
    if key in MISSING_REASONS:
        return MISSING_REASONS[key]
    return "%s was not measured" % key


def evaluate(summary):
    findings, skipped, passed = [], [], []
    for r in RULES:
        missing = [k for k in r.needs if summary.get(k) is None]
        if missing:
            skipped.append((r, missing_reason(summary, missing[0])))
            continue
        try:
            sev = r.level(summary)
        except (TypeError, ValueError, ZeroDivisionError, KeyError,
                IndexError) as exc:
            skipped.append((r, "rule error: %s" % exc))
            continue
        if sev is None:
            passed.append(r)
            continue
        findings.append(Finding(r, sev, r.say(summary), r.fix(summary)))
    findings.sort(key=lambda x: (-SEVERITY_ORDER[x.severity],
                                 VERDICT_PRECEDENCE.index(x.rule.id)
                                 if x.rule.id in VERDICT_PRECEDENCE else 99))
    return findings, skipped, passed


def verdict_line(findings, summary):
    trust = [f for f in findings
             if f.rule.id in ("SHORT_RUN", "STEAL", "INTRUDER", "CLOCK_DRIFT",
                              "BURST_EXHAUSTED", "UNSTABLE",
                              # A run the receive window capped is
                              # untrustworthy in the same way: its number
                              # describes the buffer rather than the path,
                              # and no amount of attribution fixes that.
                              "WINDOW_LIMITED", "NET_INTRUDER",
                              # Two windows that did not overlap are two
                              # experiments; every two-ended conclusion
                              # below is derived from comparing them.
                              "PEER_NOT_CONCURRENT")]
    bound_by = summary.get("bound.by")
    share = summary.get("bound.share")
    if trust:
        top = trust[0]
        lead = {
            "SHORT_RUN": "This window is too short to conclude anything from.",
            "STEAL": "Another tenant was using this machine during the run, "
                     "so the number is not repeatable and not yours.",
            "INTRUDER": "Something else was running inside the benchmark "
                        "window, so the result measures both.",
            "CLOCK_DRIFT": "The CPU slowed down partway through, so this run "
                           "averages two different machines.",
            "BURST_EXHAUSTED": "The storage was throttled partway through, so "
                               "this run averages two different disks.",
            "UNSTABLE": "This run never settled, so its number cannot be "
                        "compared with any other -- including the same "
                        "benchmark an hour from now.",
            "WINDOW_LIMITED": "This run could not have filled the link on a "
                              "single flow: the receive window is smaller "
                              "than the bandwidth-delay product, so the "
                              "number is about the buffer, not the path.",
            "NET_INTRUDER": "The interface carried substantially more than "
                            "this test says it sent, so the window includes "
                            "traffic that was not yours and the throughput "
                            "number is a measurement of both.",
            "PEER_NOT_CONCURRENT": "The two ends were not watched over the "
                                   "same window, so which of them was the "
                                   "limit cannot be read from these two "
                                   "runs.",
        }[top.rule.id]
        return lead + "  Fix that before reading the bottleneck below."

    # A receive-path cause explains the state it produced, so it leads.  A
    # run that looks network bound because one core could not keep up is a
    # host limit, and reporting "network" sends someone to the switch --
    # the same mistake why-slow refuses to make with swapping and CPU.
    both = [f for f in findings
            if f.rule.id in ("PEER_WAS_THE_LIMIT", "NEITHER_END_BOUND")]
    if both:
        peer = summary.get("peer.host") or "the other end"
        if both[0].rule.id == "PEER_WAS_THE_LIMIT":
            return ("This box was not the bottleneck -- %s was, at its %s "
                    "ceiling.  The result describes that machine, and "
                    "neither end's own report could have said so."
                    % (peer, summary.get("peer.bound_by")))
        return ("Neither this box nor %s was near a ceiling, so the limit "
                "is between them or inside the application: the path, a "
                "lock, or a single flow that cannot fill it." % peer)

    cause = [f for f in findings
             if f.rule.id in ("SOFTIRQ_BOUND", "RING_OVERFLOW",
                              "BACKLOG_DROPS")]
    if cause:
        lead = {
            "SOFTIRQ_BOUND": "This box could not pick packets up fast "
                             "enough: receive processing saturated a single "
                             "core while the others idled.",
            "RING_OVERFLOW": "This box dropped packets at the card, having "
                             "failed to drain the ring in time.",
            "BACKLOG_DROPS": "This box dropped packets in its own backlog, "
                             "queueing for a CPU that was already behind.",
        }[cause[0].rule.id]
        return lead + ("  That is a host limit rather than a network one, "
                       "and any loss or throughput figure from the far end "
                       "is measuring it.")
    if bound_by == FREE:
        return ("This box was not the bottleneck.  Nothing here was near a "
                "ceiling, so the limit was the load generator, the peer, or "
                "something serialised inside the application.")
    if bound_by:
        return ("This run was %s for %.0f%% of its samples -- %s."
                % (bound_by, share, STATE_TEXT.get(bound_by, "")))
    return "Nothing was sampled, so there is nothing to say about the run."


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


BARS = " .:-=+*#%@"

# The timeline is a fixed width whatever the run's length: a ten-minute run
# at the default interval is 600 samples, and a 600-character line survives
# neither a terminal nor a paste into a ticket.  logtriage sets the same
# precedent with its sparklines.
TIMELINE_COLS = 60


def _bar(pct):
    """One character of utilisation, so a whole run fits on one line."""
    if pct is None:
        return " "
    i = int(max(0.0, min(100.0, pct)) / 100.0 * (len(BARS) - 1))
    return BARS[i]


def _buckets(n, cols=TIMELINE_COLS):
    """Index ranges for at most `cols` columns over n samples."""
    per = max(1, -(-n // cols))          # ceiling division, no float
    return [(i, min(i + per, n)) for i in range(0, n, per)], per


def render_timeline(samples, summary, C):
    """One column per bucket of samples: the shape of the run at a glance.

    Deliberately not a chart -- this has to survive ssh, a pipe and a paste
    into a ticket, and the CSV is there for anything that needs drawing.
    Long runs are bucketed rather than truncated, and each column shows the
    *peak* of its bucket, because a spike that a mean would smooth away is
    the thing worth seeing.  The header says how many samples went into a
    column so nobody reads a 60-wide picture as a 60-sample run.
    """
    ranges, per = _buckets(len(samples))
    out = ["  %s  (%d samples, %ss apart%s)"
           % (C.bold("TIMELINE"), len(samples),
              summary.get("run.interval_s") or "?",
              ", %d per column, peak shown" % per if per > 1 else "")]
    rows = [("cpu", "cpu_busy_pct", None),
            ("core", "cpu_max_core_pct", None),
            ("io", "disk_util_pct", None),
            ("net", "net_mbps", "net_link_mbps"),
            ("mem", "mem_available_pct", "invert")]
    for label, field, scale in rows:
        vals = []
        for s in samples:
            v = _num(s, field)
            if v is not None and scale == "invert":
                v = 100.0 - v
            elif v is not None and scale:
                link = _num(s, scale)
                v = v * 100.0 / link if link else None
            vals.append(v)
        if all(v is None for v in vals):
            continue
        cells = []
        for lo, hi in ranges:
            window = [v for v in vals[lo:hi] if v is not None]
            cells.append(max(window) if window else None)
        out.append("    %-5s %s" % (label, "".join(_bar(v) for v in cells)))
    states = summary.get("bound.states") or []
    if states:
        cells = []
        for lo, hi in ranges:
            window = states[lo:hi]
            # The state holding the most samples in the bucket, ties broken
            # by precedence -- the same rule the headline attribution uses,
            # so the picture and the verdict cannot disagree.
            cells.append(max(STATES, key=lambda st: (window.count(st),
                                                     -STATES.index(st))))
        out.append("    %-5s %s" % ("bound",
                                     "".join(STATE_LETTER.get(st, "?")
                                             for st in cells)))
        out.append("    %s" % C.dim("C cpu  1 one core  I io  M mem  "
                                    "N net  T throttled  S stolen  "
                                    ". not bound"))
    idx = summary.get("steady.from_index")
    if idx:
        # 6 = the width of the label field plus its separator, so the caret
        # sits under the column it refers to rather than near it.
        col = idx // per
        out.append("    %s" % C.dim("%s^ steady from here" % (" " * (col + 6))))
    return out


def render_human(samples, summary, findings, skipped, passed, args, C):
    out = []
    bits = []
    if summary.get("run.seconds") is not None:
        bits.append("%.0fs window" % summary["run.seconds"])
    bits.append("%d samples" % summary.get("run.samples", 0))
    if summary.get("run.wrapped"):
        bits.append("wrapping %s" % summary["run.wrapped"])
    if summary.get("run.interrupted"):
        bits.append("interrupted")
    if summary.get("run.child_running"):
        bits.append("%s still running as pid %s"
                    % (summary.get("run.wrapped") or "the command",
                       summary["run.child_running"]))
    out.append("%s -- %s, %s" % (PROG, summary.get("run.host") or "?",
                                 ", ".join(bits)))
    out.append("")
    out.append("  %s   %s" % (C.bold("VERDICT"),
                              _wrap(verdict_line(findings, summary), 12)))
    out.append("")

    shown = [f for f in findings
             if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[args.min_severity]]
    for f in shown:
        tag = {CRITICAL: C.crit("CRITICAL"), WARN: C.warn("WARN    "),
               INFO: C.info("INFO    ")}[f.severity]
        out.append("  %s  %-22s %s" % (tag, f.rule.title, f.say))
    if passed and not args.quiet:
        titles = sorted(set(r.title for r in passed))
        names = ", ".join(titles[:4])
        if len(titles) > 4:
            names += " (+%d)" % (len(titles) - 4)
        out.append("  %s        %s" % (C.dim("ok"), C.dim(_wrap(names, 12))))
    if args.all:
        for r, why in skipped:
            out.append("  %s   %-22s %s" % (C.dim("skipped"), r.title,
                                            C.dim(why)))
    out.append("")

    if samples and not args.quiet and not args.no_timeline:
        out.extend(render_timeline(samples, summary, C))
        out.append("")

    if not args.quiet:
        out.append("  %s" % C.bold("SHARES"))
        shares = summary.get("bound.shares") or {}
        for st in STATES:
            v = shares.get(st)
            if v:
                out.append("    %-12s %5.0f%%   %s"
                           % (st, v, C.dim(STATE_TEXT.get(st, ""))))
        out.append("")

    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    if not shown:
        out.append("    %s" % _wrap(
            "Nothing about this run looks untrustworthy: it settled, nothing "
            "else competed for the machine, the clock held, and the limit "
            "did not move. Compare the number against another run made the "
            "same way.", 4))
    else:
        for f in shown[:4]:
            if f.fix:
                out.append("    * %s" % _wrap(f.fix, 6))
    out.append("")
    return "\n".join(out)


CSV_FIELDS = ["host", "ts", "rule_id", "severity", "title", "detail", "fix"]


def render_csv(summary, findings, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        ts = int(time.time())
        host = summary.get("run.host") or ""
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


def write_samples(samples, path):
    """The raw series, one row per sample.  Blank means not measured."""
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=SAMPLE_FIELDS, lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        for s in samples:
            row = {}
            for k in SAMPLE_FIELDS:
                v = s.get(k)
                if v is None:
                    row[k] = ""
                elif isinstance(v, float):
                    row[k] = "%.3f" % v
                else:
                    row[k] = v
            w.writerow(row)
    finally:
        if path:
            fh.close()


def read_samples(path):
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error) as exc:
        die("cannot read samples file: %s" % exc)
    if not rows:
        die("no samples in %s" % path)
    # A file with none of the columns a sample carries is some other CSV.
    # Analysing it would classify every row as "not bound" and print a
    # confident "this box was not the bottleneck" from data that measured
    # nothing -- the clean-report-you-cannot-trust failure.
    known = set(SAMPLE_FIELDS) & set(rows[0])
    if not known - {"host", "ts"}:
        die("%s has none of the columns during writes -- is this a "
            "--samples file?" % path)
    return rows


def render_json(summary, findings, skipped, path):
    doc = {
        "meta": {"tool": PROG, "version": VERSION,
                 "host": summary.get("run.host"), "ts": int(time.time())},
        "summary": dict((k, v) for k, v in sorted(summary.items())
                        if k != "bound.states"),
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
    p.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND",
                   help="command to wrap; its output and exit status pass "
                        "through")
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("--seconds", type=float,
                   default=float(_env("SECONDS", 0)) or None)
    p.add_argument("--interval", type=float,
                   default=float(_env("INTERVAL", 1.0)))
    p.add_argument("--settle", type=float, default=float(_env("SETTLE", 0)))
    p.add_argument("--samples", nargs="?", const="", metavar="PATH")
    p.add_argument("--from-samples", metavar="FILE")
    p.add_argument("--baseline", metavar="FILE")
    p.add_argument("--peer-samples", metavar="FILE")
    p.add_argument("--expect-mbps", type=float, metavar="MBPS",
                   default=(float(_env("EXPECT_MBPS"))
                            if _env("EXPECT_MBPS") else None))
    p.add_argument("--rtt-ms", type=float,
                   default=(float(_env("RTT_MS")) if _env("RTT_MS")
                            else None))
    p.add_argument("--no-procs", action="store_true")
    p.add_argument("--min-severity", default=_env("MIN_SEVERITY", "info"),
                   choices=["info", "warn", "critical"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-timeline", action="store_true")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--rules", action="store_true")
    p.add_argument("--explain", metavar="RULE_ID")
    p.add_argument("--exit-code", action="store_true")
    p.add_argument("--proc-root", default=_env("PROC", "/proc"))
    p.add_argument("--sys-root", default=_env("SYS", "/sys"))
    p.add_argument("--no-color", action="store_true")
    return p


def cmd_rules():
    out = []
    for r in RULES:
        out.append("%-16s %s" % (r.id, r.title))
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


def add_baseline(summary, samples, path):
    """Compare like with like: the same key metric, on both runs.

    The key metric comes from this run rather than the baseline's, so the
    comparison is of the series this run was actually leaning on -- and if
    the baseline never measured it, there is no comparison to make and the
    rule skips rather than inventing one.
    """
    base_samples = read_samples(path)
    base = analyse(base_samples)
    summary["baseline.bound_by"] = base.get("bound.by")
    summary["baseline.key_delta_pct"] = None
    key = summary.get("key.metric")
    if not key:
        return summary
    now = _mean([_num(s, key) for s in samples])
    then = _mean([_num(s, key) for s in base_samples])
    if now is not None and then:
        summary["baseline.key_delta_pct"] = (now - then) * 100.0 / abs(then)
    return summary


def add_peer(summary, samples, path):
    """Fold the other end of the test into this run's facts.

    A network test has two machines in it and every tool here watches one.
    The composing guide already tells you to run `during` on the load
    generator as well as the target, and says what to look for -- a
    generator at a ceiling while the target sits idle means the benchmark
    measured the generator. Nothing computed it, so it stayed a thing you
    had to notice by reading two reports side by side.

    Both ends are analysed by the same pure function, so the peer's facts
    are derived exactly as this run's were.
    """
    peer_samples = read_samples(path)
    peer = analyse(peer_samples)
    summary["peer.host"] = peer.get("run.host")
    summary["peer.bound_by"] = peer.get("bound.by")
    summary["peer.bound_share"] = peer.get("bound.share")
    summary["peer.free_share"] = peer.get("bound.free_share")
    summary["peer.rx_missed_per_s"] = peer.get("net.rx_missed_per_s")
    summary["peer.softirq_max_core_pct"] = peer.get("net.softirq_max_core_pct")

    # Two runs that did not happen at the same time are two experiments,
    # and comparing which end was busier across them says nothing at all.
    summary["peer.overlap_pct"] = None
    ours = [_num(s, "ts") for s in samples]
    theirs = [_num(s, "ts") for s in peer_samples]
    ours = [t for t in ours if t is not None]
    theirs = [t for t in theirs if t is not None]
    if ours and theirs:
        overlap = min(max(ours), max(theirs)) - max(min(ours), min(theirs))
        shortest = min(max(ours) - min(ours), max(theirs) - min(theirs))
        if shortest > 0:
            summary["peer.overlap_pct"] = max(0.0, overlap) * 100.0 / shortest
        elif overlap >= 0:
            summary["peer.overlap_pct"] = 100.0
    return summary


def main(argv=None):
    _stdio_safe()
    global PROC, SYS
    args = build_parser().parse_args(argv)

    if args.rules:
        return cmd_rules()
    if args.explain:
        return cmd_explain(args.explain)

    PROC, SYS = args.proc_root, args.sys_root
    args.min_severity = {"info": INFO, "warn": WARN,
                         "critical": CRITICAL}[args.min_severity]

    # Strip only the leading separator: a second `--` belongs to the
    # wrapped command (`during -- git log -- path`) and must survive.
    cmd = list(args.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    child = None
    child_status = 0
    meta = {}

    if args.from_samples:
        samples = read_samples(args.from_samples)
        meta["run.wrapped"] = None
        interrupted = False
    else:
        if not cmd and not args.seconds:
            die("give a command to wrap (`%s -- ./bench.sh`) or a window "
                "(`%s --seconds 60`)" % (PROG, PROG))
        if cmd:
            try:
                child = subprocess.Popen(cmd)
            except OSError as exc:
                die("cannot run %s: %s" % (cmd[0], exc))
            meta["run.wrapped"] = cmd[0]
        samples, interrupted = watch(args, child)
        if child is not None:
            try:
                if interrupted:
                    # Collect the status if the command is going to exit
                    # promptly, but never block on it.  A benchmark that
                    # traps SIGINT -- make, a JVM, most test harnesses --
                    # would otherwise hold the report hostage for as long
                    # as it feels like running, which is exactly the
                    # twenty-minutes-lost-to-a-keystroke failure this is
                    # supposed to prevent.  It is not killed: stopping
                    # somebody's benchmark uninvited would be destructive
                    # without saying so, and saying so is cheaper.
                    try:
                        child_status = child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child_status = 130
                        meta["run.child_running"] = child.pid
                else:
                    child_status = child.wait()
            except KeyboardInterrupt:
                child_status = 130
            if child_status < 0:
                # Popen reports a signalled child as -N.  Returning that
                # gives the shell 256-N -- 254 for a plain ^C -- where
                # running the command directly would have given 128+N.
                # This is meant to be a drop-in prefix, so it reports what
                # the shell would have.
                child_status = 128 + (-child_status)
    meta["run.interrupted"] = interrupted
    # A round trip needs two machines and during watches one, so the RTT
    # arrives by flag from whatever did measure it -- netmesh, usually.
    if args.rtt_ms is not None:
        meta["net.rtt_ms"] = args.rtt_ms
    if args.expect_mbps is not None:
        meta["net.expect_mbps"] = args.expect_mbps

    if not samples:
        sys.stderr.write("%s: nothing was sampled -- the window closed "
                         "shorter than one usable interval (--interval is "
                         "%.1fs).  Give the benchmark longer, or lower "
                         "--interval.\n" % (PROG, args.interval))
        return child_status or 1

    summary = analyse(samples, meta)
    if args.baseline:
        summary = add_baseline(summary, samples, args.baseline)
    if args.peer_samples:
        summary = add_peer(summary, samples, args.peer_samples)
    findings, skipped, passed = evaluate(summary)

    if args.samples is not None:
        with _WriteGuard(args.samples or None):
            write_samples(samples, args.samples or None)
    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            render_csv(summary, findings, args.csv or None)
    if args.json is not None:
        with _WriteGuard(args.json or None):
            render_json(summary, findings, skipped, args.json or None)
    if args.csv is None and args.json is None and args.samples != "":
        use_color = (not args.no_color and not os.environ.get("NO_COLOR")
                     and sys.stdout.isatty())
        sys.stdout.write(render_human(samples, summary, findings, skipped,
                                      passed, args, Colors(use_color)) + "\n")

    # A command that failed outranks any verdict about it, even under
    # --exit-code: reporting "warn" for a benchmark that never finished
    # would hide a build failure behind a diagnosis of it, and there is
    # nothing worth reading in a run that did not complete.
    if child_status:
        return child_status
    if args.exit_code and findings:
        worst = max(SEVERITY_ORDER[f.severity] for f in findings)
        # 10/20 rather than 1/2, so a severity can never be mistaken for a
        # usage error by whatever is wrapping this.
        return {3: 20, 2: 10, 1: 0}[worst]
    # Otherwise the wrapped command's own status, so this is a drop-in
    # prefix: `during -- make bench` fails exactly when make does.
    return child_status


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
# mesh, and `agree script ./during.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
