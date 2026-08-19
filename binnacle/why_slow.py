#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""why-slow.py -- work out why a Linux box is slow, and say what to do about it.

Usage: why-slow.py                       sample for 2s, diagnose, advise
       why-slow.py --interval 10         longer sample, steadier numbers
       why-slow.py --ssh node07          diagnose a remote box over ssh
       why-slow.py --csv                 one row per finding, for fleet use
       why-slow.py --json                everything, machine-readable
       why-slow.py --facts               just the measurements, no diagnosis
       why-slow.py --rules               every rule and its thresholds
       why-slow.py --explain RULE_ID     why one rule exists and what it means

Options:
  --ssh HOST         run on HOST instead: this file is fed to `python3 -`
                     over ssh, so nothing is copied to or left on the box.
                     One host only -- a fleet is agree's job:
                     `agree script why-slow --hosts prod.txt --fleet-csv -- --csv`
  --ssh-cmd CMD      the ssh to use for --ssh                (WHY_SLOW_SSH)
  --interval S       seconds between the two counter snapshots (default 2.0;
                     0 takes a single snapshot and skips every rate rule)
  --top N            how many processes to name in the evidence block (5)
  --min-severity L   info | warn | critical -- hide findings below L
  --all              also list the rules that were skipped, and why
  --quiet            the verdict and the findings, nothing else
  --boot-window S    how far back a kernel message still counts (3600)
  --exit-code        exit 0 ok / 10 warn / 20 critical (default: always 0)
  --csv [PATH]       findings as CSV (stdout if no path)
  --json [PATH]      meta + facts + findings + skipped
  --facts            print the fact dictionary and exit
  --from-facts FILE  run the rules against a saved fact file, measuring
                     nothing -- this is how the rules are tested
  --proc-root DIR    read /proc from here instead      (WHY_SLOW_PROC)
  --sys-root DIR     read /sys from here instead       (WHY_SLOW_SYS)
  --no-exec          never run a subprocess: pure /proc and /sys mode
  --no-color         plain output (also honours NO_COLOR)

What it does
  top, atop and sar show you numbers.  This reads the numbers and tells you
  what they mean, which is the part that takes experience.  It samples
  /proc twice a couple of seconds apart, adds the things that are only
  visible once (the kernel log, cgroup limits, filesystem fullness), runs
  about thirty rules over the result, and prints the highest-severity
  finding as a verdict with the exact command to run next.

  The rules are ordered by cause, not by symptom.  A box that is swapping
  also looks CPU-busy, and reporting the CPU is how people spend an hour
  tuning the wrong thing -- so swap thrashing and disk errors outrank CPU
  saturation in the verdict even when the CPU number is larger.

  Slow is not the only way a box fails.  It can be idle and still refusing
  work because a kernel table is full: conntrack, file descriptors, task
  slots, the ephemeral port range, the neighbour table.  Those have no
  gradient to watch -- they work until abruptly they do not -- so they are
  measured as ratios against their ceilings and reported before the wall,
  and the kernel log is searched for the moment one was actually hit,
  because a table that overflowed an hour ago has since drained and no
  ratio measured now will show it.

Robustness properties
  * a fact that could not be measured is None, never 0, and any rule that
    needed it is reported as skipped *with the reason* -- a tool that
    quietly checks less when run as non-root is worse than one that says so
  * needs no root: without it you lose the kernel log (so OOM kills and I/O
    errors) and per-process I/O, and it tells you exactly that
  * needs no packages: standard library only, Python 3.6+
  * --no-exec runs without spawning anything at all
  * container-aware: in a cgroup, your own limit is checked first and the
    host's numbers are demoted, because a 4 GB container on a 256 GB host
    is out of memory while the host is fine
  * every rule is a pure function of the fact dictionary, so --from-facts
    reproduces any diagnosis exactly, with no machine in the state that
    produced it
  * exit status is 0 unless you ask for --exit-code, so it is safe in a
    pipeline by default
"""

import argparse
import csv
import glob
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections import namedtuple

VERSION = "0.3.0"
PROG = os.path.basename(sys.argv[0]) or "why-slow.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

PROC = "/proc"
SYS = "/sys"
NO_EXEC = False


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
    v = os.environ.get("WHY_SLOW_" + name)
    return v if v not in (None, "") else default


# ---------------------------------------------------------------------------
# Reading /proc and /sys
# ---------------------------------------------------------------------------
#
# Everything goes through _read/_open so --proc-root and --sys-root can
# point the whole tool at a fixture tree.  That is what makes the fact
# layer testable without root, without a slow box, and without waiting.

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


def read_stat():
    """Whole-box and per-core jiffies, plus context switches and forks."""
    txt = _read(os.path.join(PROC, "stat"))
    if txt is None:
        return None
    out = {"cores": [], "ctxt": None, "forks": None,
           "running": None, "blocked": None}
    for line in txt.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("cpu"):
            try:
                vals = [int(v) for v in parts[1:11]]
            except ValueError:
                continue
            # Old kernels have fewer fields; pad rather than drop the line.
            while len(vals) < 10:
                vals.append(0)
            total = sum(vals)
            entry = {"total": total, "idle": vals[3] + vals[4],
                     "sys": vals[2], "iowait": vals[4], "steal": vals[7],
                     "softirq": vals[6]}
            if parts[0] == "cpu":
                out["all"] = entry
            else:
                out["cores"].append(entry)
        elif parts[0] == "ctxt":
            out["ctxt"] = int(parts[1])
        elif parts[0] == "processes":
            out["forks"] = int(parts[1])
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
    """PSI avg10 for 'some' and 'full'.  Absent before kernel 4.20."""
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
        # Skip partitions and virtual devices: a partition's numbers are
        # its parent's counted twice, which would double every rate.
        if re.match(r"^(loop|ram|dm-|sr)\d*$", name):
            continue
        if re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)\d+$",
                    name):
            continue
        try:
            out[name] = {
                "reads": int(f[3]), "read_sectors": int(f[5]),
                "writes": int(f[7]), "write_sectors": int(f[9]),
                "inflight": int(f[11]), "io_ms": int(f[12]),
                "weighted_ms": int(f[13]),
            }
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
                         "rx_errs": int(f[2]), "rx_drop": int(f[3]),
                         "tx_bytes": int(f[8]), "tx_pkts": int(f[9]),
                         "tx_errs": int(f[10]), "tx_drop": int(f[11])}
        except ValueError:
            continue
    return out or None


def read_snmp():
    """TCP retransmits and accept-queue overflows."""
    out = {}
    for fname, want in (("snmp", ("RetransSegs", "OutSegs")),
                        ("netstat", ("ListenOverflows", "ListenDrops"))):
        txt = _read(os.path.join(PROC, "net", fname))
        if txt is None:
            continue
        lines = txt.splitlines()
        for i in range(0, len(lines) - 1, 2):
            keys = lines[i].split()
            vals = lines[i + 1].split()
            if len(keys) != len(vals):
                continue
            for k, v in zip(keys[1:], vals[1:]):
                if k in want:
                    try:
                        out[k] = int(v)
                    except ValueError:
                        pass
    return out or None


def read_procs(top_n=40):
    """Per-process CPU jiffies, RSS, state and name."""
    out = {}
    try:
        pids = [d for d in os.listdir(PROC) if d.isdigit()]
    except OSError:
        return None
    page_kb = 4
    for pid in pids:
        txt = _read(os.path.join(PROC, pid, "stat"))
        if not txt:
            continue
        try:
            head, rest = txt.rsplit(") ", 1)
            comm = head.split("(", 1)[1]
            f = rest.split()
            state = f[0]
            utime, stime = int(f[11]), int(f[12])
            rss_pages = int(f[21])
            ppid = int(f[1])
        except (ValueError, IndexError):
            continue
        out[pid] = {"comm": comm, "state": state, "cpu": utime + stime,
                    "rss_kb": rss_pages * page_kb, "ppid": ppid}
    return out or None


def read_cgroup():
    """cgroup v2 first, then v1.  Inside a container these paths are the
    container's own limits, which is exactly what we want."""
    cg = {"v": None, "mem_max": None, "mem_current": None, "mem_events_max": 0,
          "cpu_quota_cores": None, "nr_periods": None, "nr_throttled": None,
          "pids_current": None, "pids_max": None}
    base = os.path.join(SYS, "fs", "cgroup")
    if os.path.exists(os.path.join(base, "cgroup.controllers")) or \
            os.path.exists(os.path.join(base, "memory.max")):
        cg["v"] = 2
        cg["mem_max"] = _read_int(os.path.join(base, "memory.max"))
        cg["mem_current"] = _read_int(os.path.join(base, "memory.current"))
        ev = _read(os.path.join(base, "memory.events")) or ""
        m = re.search(r"^max (\d+)", ev, re.M)
        if m:
            cg["mem_events_max"] = int(m.group(1))
        cpumax = (_read(os.path.join(base, "cpu.max")) or "").split()
        if len(cpumax) == 2 and cpumax[0] != "max":
            try:
                cg["cpu_quota_cores"] = int(cpumax[0]) / float(cpumax[1])
            except (ValueError, ZeroDivisionError):
                pass
        stat = _read(os.path.join(base, "cpu.stat")) or ""
        for key in ("nr_periods", "nr_throttled"):
            m = re.search(r"^%s (\d+)" % key, stat, re.M)
            if m:
                cg[key] = int(m.group(1))
        cg["pids_current"] = _read_int(os.path.join(base, "pids.current"))
        cg["pids_max"] = _read_int(os.path.join(base, "pids.max"))
    else:
        mem = os.path.join(base, "memory")
        cpu = os.path.join(base, "cpu")
        if os.path.isdir(mem):
            cg["v"] = 1
            lim = _read_int(os.path.join(mem, "memory.limit_in_bytes"))
            # v1 uses a huge sentinel rather than "max" for "no limit".
            if lim is not None and lim < (1 << 62):
                cg["mem_max"] = lim
            cg["mem_current"] = _read_int(
                os.path.join(mem, "memory.usage_in_bytes"))
        if os.path.isdir(cpu):
            cg["v"] = cg["v"] or 1
            q = _read_int(os.path.join(cpu, "cpu.cfs_quota_us"))
            p = _read_int(os.path.join(cpu, "cpu.cfs_period_us"))
            if q and p and q > 0:
                cg["cpu_quota_cores"] = q / float(p)
            stat = _read(os.path.join(cpu, "cpu.stat")) or ""
            for key in ("nr_periods", "nr_throttled"):
                m = re.search(r"^%s (\d+)" % key, stat, re.M)
                if m:
                    cg[key] = int(m.group(1))
        pids = os.path.join(base, "pids")
        if os.path.isdir(pids):
            cg["v"] = cg["v"] or 1
            cg["pids_current"] = _read_int(os.path.join(pids, "pids.current"))
            # v1 writes the literal "max" for no limit, which _read_int
            # already turns into None rather than a number.
            cg["pids_max"] = _read_int(os.path.join(pids, "pids.max"))
    return cg


def in_container():
    """Only positive evidence counts.

    The tempting heuristic -- "pid 1 is not systemd, so this is a
    container" -- is wrong on any machine with a non-systemd init or a
    custom pid 1, and calling a VM a container makes the whole header lie.
    A false negative just means the host-wide rules are used, which is the
    right default anyway.
    """
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    txt = _read(os.path.join(PROC, "1", "cgroup")) or ""
    return bool(re.search(r"/(docker|kubepods|lxc|containerd|crio|machine)",
                          txt))


def read_filesystems():
    """Fullness by mount point, via statvfs -- no df subprocess needed."""
    txt = _read(os.path.join(PROC, "mounts"))
    if txt is None:
        return None
    skip_types = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
                  "cgroup2", "overlay", "squashfs", "securityfs", "debugfs",
                  "tracefs", "mqueue", "hugetlbfs", "fusectl", "configfs",
                  "pstore", "bpf", "autofs", "binfmt_misc", "efivarfs",
                  "nsfs", "ramfs", "rpc_pipefs"}
    out = {}
    for line in txt.splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        dev, mount, fstype = f[0], f[1].replace("\\040", " "), f[2]
        if fstype in skip_types or not dev.startswith("/"):
            continue
        if mount in out:
            continue
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue
        used = st.f_blocks - st.f_bfree
        used_pct = used * 100.0 / st.f_blocks
        inode_pct = None
        if st.f_files:
            inode_pct = (st.f_files - st.f_ffree) * 100.0 / st.f_files
        out[mount] = {"used_pct": used_pct, "inode_pct": inode_pct,
                      "size_gb": st.f_blocks * st.f_frsize / 1e9}
    return out or None


KERNEL_PATTERNS = {
    "oom": re.compile(
        r"Out of memory: Kill(?:ed)? process (\d+) \(([^)]+)\)|"
        r"oom-kill:.*?task=([\w.-]+),pid=(\d+)|"
        r"Memory cgroup out of memory: Kill(?:ed)? process (\d+) \(([^)]+)\)"),
    "io_error": re.compile(
        r"I/O error|blk_update_request|medium error|critical medium error|"
        r"EXT4-fs error|XFS \(.*\): metadata I/O error|Buffer I/O error|"
        r"ata\d+\.\d+: failed command", re.I),
    "throttle": re.compile(
        r"temperature above threshold|clock throttled|"
        r"CPU\d+: Core temperature|thermal throttling", re.I),
    # A ceiling that was hit an hour ago is invisible to any ratio measured
    # now -- the table drains, the counter does not stay pinned, and the
    # only surviving evidence is the line the kernel wrote at the time.
    "limit": re.compile(
        r"nf_conntrack: table full, dropping packet|"
        r"neighbou?r table overflow|"
        r"VFS: file-max limit \d+ reached|"
        r"Out of socket memory|TCP: out of memory|"
        r"Possible SYN flooding on port \d+|"
        r"cgroup: fork rejected by pids controller", re.I),
}


def read_kernel_log(boot_window):
    """dmesg, then journalctl -k, then the on-disk kernel log.

    Losing this is the single biggest thing a non-root run gives up, so
    the failure is reported rather than silently treated as "nothing
    found" -- which would read as an all-clear.
    """
    text = None
    source = None
    out = _run(["dmesg", "-T", "--level=err,crit,alert,emerg,warn"])
    if out is None:
        out = _run(["dmesg", "-T"])
    if out is None:
        out = _run(["dmesg"])
    if out is not None:
        text, source = out, "dmesg"
    if text is None:
        out = _run(["journalctl", "-k", "--no-pager", "-n", "2000",
                    "--since", "-%ds" % int(boot_window)])
        if out is not None:
            text, source = out, "journalctl"
    if text is None:
        for path in ("/var/log/kern.log", "/var/log/messages"):
            out = _read(path)
            if out is not None:
                text, source = out[-500000:], path
                break
    if text is None:
        return None
    found = {"oom": [], "io_error": [], "throttle": [], "limit": [],
             "source": source}
    for line in text.splitlines():
        for key in ("oom", "io_error", "throttle", "limit"):
            if KERNEL_PATTERNS[key].search(line):
                found[key].append(line.strip()[:200])
    return found


def read_failed_units():
    out = _run(["systemctl", "--failed", "--no-legend", "--plain",
                "--no-pager"])
    if out is None:
        return None
    units = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


def read_limits():
    """Kernel tables and the ceilings they are measured against.

    These are hard edges rather than gradients: below the limit everything
    works and above it the kernel refuses outright, so each is read as a
    pair (in use, ceiling) and judged as a ratio.  A box sitting at 95% of
    its conntrack table is not slow -- it is about to start dropping
    packets while every rate in this report still looks healthy.
    """
    lim = {"fd_used": None, "fd_max": None, "tasks": None, "pid_max": None,
           "conntrack": None, "conntrack_max": None, "tw": None,
           "ephemeral": None, "arp": None, "arp_max": None}

    # file-nr is "allocated free max"; on every kernel since 2.6 the middle
    # field is 0, so the first is the count actually in use.
    nr = (_read(os.path.join(PROC, "sys", "fs", "file-nr")) or "").split()
    if len(nr) >= 3:
        try:
            lim["fd_used"], lim["fd_max"] = int(nr[0]), int(nr[2])
        except ValueError:
            pass

    # The fourth loadavg field is running/total *tasks*, and tasks is what
    # pid_max actually limits -- a threaded process consumes many.
    la = (_read(os.path.join(PROC, "loadavg")) or "").split()
    if len(la) >= 4 and "/" in la[3]:
        try:
            lim["tasks"] = int(la[3].split("/")[1])
        except ValueError:
            pass
    lim["pid_max"] = _read_int(os.path.join(PROC, "sys", "kernel", "pid_max"))

    # Absent unless connection tracking is loaded, which is the common case
    # on a box with no firewall -- absence is "no table", not "empty table".
    lim["conntrack"] = _read_int(os.path.join(
        PROC, "sys", "net", "netfilter", "nf_conntrack_count"))
    for path in (("sys", "net", "netfilter", "nf_conntrack_max"),
                 ("sys", "net", "nf_conntrack_max")):
        lim["conntrack_max"] = _read_int(os.path.join(PROC, *path))
        if lim["conntrack_max"]:
            break

    # TIME_WAIT sockets hold an ephemeral port for a minute after close, so
    # this is what exhausts the range on a busy outbound client.
    sock = _read(os.path.join(PROC, "net", "sockstat")) or ""
    m = re.search(r"^TCP:.*\btw (\d+)", sock, re.M)
    if m:
        lim["tw"] = int(m.group(1))
    rng = (_read(os.path.join(PROC, "sys", "net", "ipv4",
                              "ip_local_port_range")) or "").split()
    if len(rng) == 2:
        try:
            lim["ephemeral"] = int(rng[1]) - int(rng[0]) + 1
        except ValueError:
            pass

    arp = _read(os.path.join(PROC, "net", "arp"))
    if arp is not None:
        lim["arp"] = max(0, len(arp.splitlines()) - 1)   # minus the header
    lim["arp_max"] = _read_int(os.path.join(
        PROC, "sys", "net", "ipv4", "neigh", "default", "gc_thresh3"))
    return lim


def read_thermal():
    out = {"cur_ghz": None, "max_ghz": None, "governor": None,
           "max_temp_c": None}
    base = os.path.join(SYS, "devices", "system", "cpu")
    cur = _read_int(os.path.join(base, "cpu0", "cpufreq", "scaling_cur_freq"))
    mx = _read_int(os.path.join(base, "cpu0", "cpufreq", "cpuinfo_max_freq"))
    gov = _read(os.path.join(base, "cpu0", "cpufreq", "scaling_governor"))
    if cur:
        out["cur_ghz"] = cur / 1e6
    if mx:
        out["max_ghz"] = mx / 1e6
    if gov:
        out["governor"] = gov.strip()
    temps = []
    for zone in glob.glob(os.path.join(SYS, "class", "thermal",
                                       "thermal_zone*", "temp")):
        t = _read_int(zone)
        if t and 0 < t < 200000:
            temps.append(t / 1000.0)
    if temps:
        out["max_temp_c"] = max(temps)
    return out


# ---------------------------------------------------------------------------
# Building the fact dictionary
# ---------------------------------------------------------------------------
#
# A fact that could not be measured is None and never 0.  Every rule
# declares the facts it needs, so a missing one turns into a skip with a
# reason instead of a wrong answer.

def _delta_rate(a, b, dt):
    if a is None or b is None or not dt:
        return None
    if b < a:
        # the counter reset underneath us -- container restart, module
        # reload, wrap.  Unknown is None, never a negative rate.
        return None
    return (b - a) / dt


def collect_facts(interval, top_n, boot_window):
    f = {}
    f["sys.hostname"] = socket.gethostname()
    f["sys.kernel"] = (_read(os.path.join(PROC, "sys", "kernel", "osrelease"))
                       or "").strip() or None
    up = _read(os.path.join(PROC, "uptime"))
    f["sys.uptime_s"] = float(up.split()[0]) if up else None
    f["sys.container"] = in_container()
    f["sys.euid"] = os.geteuid() if hasattr(os, "geteuid") else None

    stat1 = read_stat()
    vm1 = read_vmstat()
    disk1 = read_diskstats()
    net1 = read_netdev()
    snmp1 = read_snmp()
    proc1 = read_procs(top_n)
    t1 = time.monotonic()

    ncpu = len(stat1["cores"]) if stat1 and stat1["cores"] else (
        os.cpu_count() or 1)
    f["cpu.count"] = ncpu

    if interval > 0:
        time.sleep(interval)
    stat2 = read_stat() if interval > 0 else stat1
    vm2 = read_vmstat() if interval > 0 else vm1
    disk2 = read_diskstats() if interval > 0 else disk1
    net2 = read_netdev() if interval > 0 else net1
    snmp2 = read_snmp() if interval > 0 else snmp1
    proc2 = read_procs(top_n) if interval > 0 else proc1
    dt = time.monotonic() - t1
    f["sample.interval_s"] = dt if interval > 0 else 0.0

    # --- load ---
    la = _read(os.path.join(PROC, "loadavg"))
    if la:
        p = la.split()
        try:
            f["cpu.load1"], f["cpu.load5"], f["cpu.load15"] = (
                float(p[0]), float(p[1]), float(p[2]))
        except (ValueError, IndexError):
            pass
    f["cpu.runnable"] = stat1.get("running") if stat1 else None
    f["cpu.blocked"] = stat1.get("blocked") if stat1 else None

    # --- cpu percentages ---
    for key in ("busy_pct", "sys_pct", "iowait_pct", "steal_pct",
                "softirq_pct", "ctxt_per_s", "forks_per_s"):
        f["cpu." + key] = None
    if stat1 and stat2 and interval > 0:
        a, b = stat1["all"], stat2["all"]
        dtot = b["total"] - a["total"]
        didle = b["idle"] - a["idle"]
        deltas = dict((k, b[k] - a[k])
                      for k in ("sys", "iowait", "steal", "softirq"))
        # any component going backwards means the pair is inconsistent
        # (reset between reads); a missing measurement is None, never
        # a negative percentage
        if dtot > 0 and didle >= 0 and min(deltas.values()) >= 0:
            f["cpu.busy_pct"] = (dtot - didle) * 100.0 / dtot
            f["cpu.sys_pct"] = deltas["sys"] * 100.0 / dtot
            f["cpu.iowait_pct"] = deltas["iowait"] * 100.0 / dtot
            f["cpu.steal_pct"] = deltas["steal"] * 100.0 / dtot
            f["cpu.softirq_pct"] = deltas["softirq"] * 100.0 / dtot
        f["cpu.ctxt_per_s"] = _delta_rate(stat1["ctxt"], stat2["ctxt"], dt)
        f["cpu.forks_per_s"] = _delta_rate(stat1["forks"], stat2["forks"], dt)

    # --- pressure ---
    for kind, prefix in (("cpu", "psi.cpu"), ("io", "psi.io"),
                         ("memory", "psi.mem")):
        some, full = read_pressure(kind)
        f[prefix + ".some10"] = some
        f[prefix + ".full10"] = full

    # --- memory ---
    mi = read_meminfo()
    f["mem.total_kb"] = mi.get("MemTotal") if mi else None
    f["mem.available_kb"] = mi.get("MemAvailable") if mi else None
    f["mem.swap_total_kb"] = mi.get("SwapTotal") if mi else None
    f["mem.swap_free_kb"] = mi.get("SwapFree") if mi else None
    f["mem.available_pct"] = None
    if mi and mi.get("MemTotal"):
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = (mi.get("MemFree", 0) + mi.get("Cached", 0)
                     + mi.get("Buffers", 0))
        f["mem.available_pct"] = avail * 100.0 / mi["MemTotal"]
    for key, src in (("mem.pswpin_per_s", "pswpin"),
                     ("mem.pswpout_per_s", "pswpout"),
                     ("mem.pgmajfault_per_s", "pgmajfault")):
        f[key] = None
        if vm1 and vm2 and interval > 0:
            f[key] = _delta_rate(vm1.get(src), vm2.get(src), dt)

    # --- disks ---
    f["disk.busiest"] = None
    f["disk.busiest_util_pct"] = None
    f["disk.busiest_await_ms"] = None
    f["disk.busiest_iops"] = None
    f["disk.busiest_inflight"] = None
    f["disk.all"] = {}
    if disk1 and disk2 and interval > 0:
        for name, b in disk2.items():
            a = disk1.get(name)
            if not a:
                continue
            ios = (b["reads"] + b["writes"]) - (a["reads"] + a["writes"])
            io_ms = b["io_ms"] - a["io_ms"]
            wt_ms = b["weighted_ms"] - a["weighted_ms"]
            util = min(100.0, io_ms / (dt * 1000.0) * 100.0) if dt else None
            await_ms = (wt_ms / ios) if ios > 0 else 0.0
            mb = ((b["read_sectors"] + b["write_sectors"])
                  - (a["read_sectors"] + a["write_sectors"])) * 512 / 1e6 / dt
            f["disk.all"][name] = {"util_pct": util, "await_ms": await_ms,
                                   "iops": ios / dt if dt else None,
                                   "mbps": mb, "inflight": b["inflight"]}
        if f["disk.all"]:
            worst = max(f["disk.all"].items(),
                        key=lambda kv: (kv[1]["util_pct"] or 0,
                                        kv[1]["await_ms"] or 0))
            f["disk.busiest"] = worst[0]
            f["disk.busiest_util_pct"] = worst[1]["util_pct"]
            f["disk.busiest_await_ms"] = worst[1]["await_ms"]
            f["disk.busiest_iops"] = worst[1]["iops"]
            f["disk.busiest_inflight"] = worst[1]["inflight"]

    # --- filesystems ---
    fs = read_filesystems()
    f["fs.all"] = fs or {}
    f["fs.fullest"] = None
    f["fs.fullest_pct"] = None
    f["fs.worst_inode"] = None
    f["fs.worst_inode_pct"] = None
    if fs:
        m, d = max(fs.items(), key=lambda kv: kv[1]["used_pct"])
        f["fs.fullest"], f["fs.fullest_pct"] = m, d["used_pct"]
        withi = [(k, v) for k, v in fs.items() if v["inode_pct"] is not None]
        if withi:
            m, d = max(withi, key=lambda kv: kv[1]["inode_pct"])
            f["fs.worst_inode"], f["fs.worst_inode_pct"] = m, d["inode_pct"]

    # --- network ---
    f["net.worst_if"] = None
    f["net.worst_drop_pct"] = None
    f["net.worst_drop_per_s"] = None
    f["net.tcp_retrans_pct"] = None
    f["net.listen_overflows_per_s"] = None
    if net1 and net2 and interval > 0:
        worst = (None, -1.0, 0.0)
        for name, b in net2.items():
            a = net1.get(name)
            if not a:
                continue
            dp = b["rx_pkts"] - a["rx_pkts"]
            dd = (b["rx_drop"] - a["rx_drop"]) + (b["rx_errs"] - a["rx_errs"])
            if dp + dd <= 0:
                continue
            pctdrop = dd * 100.0 / (dp + dd)
            if pctdrop > worst[1]:
                worst = (name, pctdrop, dd / dt if dt else 0.0)
        if worst[0]:
            f["net.worst_if"] = worst[0]
            f["net.worst_drop_pct"] = worst[1]
            f["net.worst_drop_per_s"] = worst[2]
    if snmp1 and snmp2 and interval > 0:
        dout = snmp2.get("OutSegs", 0) - snmp1.get("OutSegs", 0)
        dre = snmp2.get("RetransSegs", 0) - snmp1.get("RetransSegs", 0)
        if dout > 0:
            f["net.tcp_retrans_pct"] = dre * 100.0 / dout
        f["net.listen_overflows_per_s"] = _delta_rate(
            snmp1.get("ListenOverflows"), snmp2.get("ListenOverflows"), dt)

    # --- processes ---
    f["proc.count"] = len(proc2) if proc2 else None
    f["proc.top_cpu"] = []
    f["proc.top_rss"] = []
    f["proc.dstate"] = []
    if proc2:
        if proc1 and interval > 0 and dt:
            hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            deltas = []
            for pid, b in proc2.items():
                a = proc1.get(pid)
                if not a or a["comm"] != b["comm"]:
                    continue
                d = (b["cpu"] - a["cpu"]) / float(hz) / dt * 100.0
                if d > 0.5:
                    deltas.append((d, pid, b["comm"]))
            deltas.sort(reverse=True)
            f["proc.top_cpu"] = [{"pid": p, "comm": c, "cpu_pct": round(d, 1)}
                                 for d, p, c in deltas[:top_n]]
        rss = sorted(((v["rss_kb"], k, v["comm"]) for k, v in proc2.items()),
                     reverse=True)
        f["proc.top_rss"] = [{"pid": p, "comm": c, "rss_kb": r}
                             for r, p, c in rss[:top_n] if r > 0]
        f["proc.dstate"] = [{"pid": k, "comm": v["comm"]}
                            for k, v in sorted(proc2.items())
                            if v["state"] == "D"]

    # --- cgroup ---
    cg = read_cgroup()
    f["cg.v"] = cg["v"]
    f["cg.mem_max"] = cg["mem_max"]
    f["cg.mem_current"] = cg["mem_current"]
    f["cg.mem_events_max"] = cg["mem_events_max"]
    f["cg.mem_pct"] = None
    if cg["mem_max"] and cg["mem_current"]:
        f["cg.mem_pct"] = cg["mem_current"] * 100.0 / cg["mem_max"]
    f["cg.cpu_quota_cores"] = cg["cpu_quota_cores"]
    f["cg.throttled_pct"] = None
    if cg["nr_periods"]:
        f["cg.throttled_pct"] = (cg["nr_throttled"] or 0) * 100.0 / cg["nr_periods"]
    f["cg.pids_current"] = cg["pids_current"]
    f["cg.pids_max"] = cg["pids_max"]
    f["cg.pids_pct"] = None
    if cg["pids_max"] and cg["pids_current"] is not None:
        f["cg.pids_pct"] = cg["pids_current"] * 100.0 / cg["pids_max"]

    # --- kernel tables and their ceilings ---
    lim = read_limits()
    for key in ("fd_used", "fd_max", "tasks", "pid_max", "conntrack",
                "conntrack_max", "tw", "ephemeral", "arp", "arp_max"):
        f["lim." + key] = lim[key]
    for pct, used, ceiling in (("lim.fd_pct", "fd_used", "fd_max"),
                               ("lim.pid_pct", "tasks", "pid_max"),
                               ("lim.conntrack_pct", "conntrack",
                                "conntrack_max"),
                               ("lim.ephemeral_pct", "tw", "ephemeral"),
                               ("lim.arp_pct", "arp", "arp_max")):
        f[pct] = None
        if lim[used] is not None and lim[ceiling]:
            f[pct] = lim[used] * 100.0 / lim[ceiling]

    # --- kernel log, units, thermal ---
    klog = read_kernel_log(boot_window)
    f["kern.available"] = klog is not None
    f["kern.source"] = klog["source"] if klog else None
    f["kern.oom"] = klog["oom"] if klog else None
    f["kern.io_errors"] = klog["io_error"] if klog else None
    f["kern.throttle"] = klog["throttle"] if klog else None
    f["kern.limit_hits"] = klog["limit"] if klog else None

    units = read_failed_units()
    f["sys.failed_units"] = units

    th = read_thermal()
    f["therm.cur_ghz"] = th["cur_ghz"]
    f["therm.max_ghz"] = th["max_ghz"]
    f["therm.governor"] = th["governor"]
    f["therm.max_temp_c"] = th["max_temp_c"]

    ent = _read_int(os.path.join(PROC, "sys", "kernel", "random",
                                 "entropy_avail"))
    f["sys.entropy"] = ent
    return f


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
#
# A rule is a pure function of the fact dict.  `needs` lists the facts it
# cannot work without; if any is None the rule is skipped and says why.
# `level` returns a severity or None (None means the rule looked and found
# nothing wrong).  That split is what lets --from-facts reproduce any
# diagnosis exactly.

Rule = namedtuple("Rule", "id title needs level say fix why")

MISSING_REASONS = {
    "psi.cpu.some10": "no /proc/pressure -- kernel < 4.20 or CONFIG_PSI=n",
    "psi.io.some10": "no /proc/pressure -- kernel < 4.20 or CONFIG_PSI=n",
    "psi.io.full10": "no /proc/pressure -- kernel < 4.20 or CONFIG_PSI=n",
    "psi.mem.full10": "no /proc/pressure -- kernel < 4.20 or CONFIG_PSI=n",
    "cpu.busy_pct": "needs two snapshots -- rerun without --interval 0",
    "cpu.steal_pct": "needs two snapshots -- rerun without --interval 0",
    "cpu.forks_per_s": "needs two snapshots -- rerun without --interval 0",
    "mem.pswpin_per_s": "needs two snapshots -- rerun without --interval 0",
    "disk.busiest_util_pct": "needs two snapshots -- rerun without --interval 0",
    "net.worst_drop_pct": "needs two snapshots -- rerun without --interval 0",
    "net.tcp_retrans_pct": "needs two snapshots -- rerun without --interval 0",
    "kern.oom": "kernel log unreadable -- rerun with sudo to check for OOM "
                "kills and I/O errors",
    "kern.io_errors": "kernel log unreadable -- rerun with sudo to check for "
                      "OOM kills and I/O errors",
    "kern.throttle": "kernel log unreadable -- rerun with sudo",
    "kern.limit_hits": "kernel log unreadable -- rerun with sudo to see "
                       "whether a kernel table has already overflowed",
    "sys.failed_units": "systemctl unavailable (not systemd, or --no-exec)",
    "lim.fd_pct": "no /proc/sys/fs/file-nr on this machine",
    "lim.pid_pct": "no task count or pid_max -- /proc/loadavg or "
                   "/proc/sys/kernel/pid_max unreadable",
    "lim.conntrack_pct": "no conntrack table here -- connection tracking is "
                         "not loaded, so there is nothing to fill",
    "lim.ephemeral_pct": "no /proc/net/sockstat or local port range",
    "lim.arp_pct": "no /proc/net/arp or neighbour gc_thresh3 here",
    "cg.pids_pct": "not in a task-limited cgroup",
    "cg.mem_pct": "not in a memory-limited cgroup",
    "cg.throttled_pct": "not in a CPU-limited cgroup",
    "cg.cpu_quota_cores": "not in a CPU-limited cgroup",
    "therm.cur_ghz": "no cpufreq information on this machine",
    "therm.governor": "no cpufreq information on this machine",
    "sys.entropy": "entropy pool not readable",
}


def _g(f, key, default=None):
    v = f.get(key)
    return default if v is None else v


def _top_cpu_str(f, n=3):
    tops = _g(f, "proc.top_cpu", [])[:n]
    if not tops:
        return "no per-process CPU (single snapshot)"
    return "  ".join("%s(%s) %.0f%%" % (p["comm"], p["pid"], p["cpu_pct"])
                     for p in tops)


def _top_rss_str(f, n=3):
    tops = _g(f, "proc.top_rss", [])[:n]
    if not tops:
        return "unknown"
    return "  ".join("%s(%s) %s" % (p["comm"], p["pid"], _kb(p["rss_kb"]))
                     for p in tops)


def _kb(kb):
    if kb is None:
        return "-"
    if kb >= 1048576:
        return "%.1fG" % (kb / 1048576.0)
    if kb >= 1024:
        return "%.0fM" % (kb / 1024.0)
    return "%dK" % kb


def _biggest_proc(f):
    tops = _g(f, "proc.top_rss", [])
    return tops[0] if tops else None


RULES = []


def rule(rid, title, needs, why):
    def deco(fn):
        level, say, fix = fn()
        RULES.append(Rule(rid, title, needs, level, say, fix, why))
        return fn
    return deco


# 1 ------------------------------------------------------------------------
@rule("CPU_SATURATED", "cpu saturated",
      ("cpu.load1", "cpu.count", "cpu.busy_pct"),
      "Load per core with the CPU actually busy and not waiting on disk: "
      "real demand for compute rather than a queue behind something else.")
def _cpu_saturated():
    def level(f):
        per = f["cpu.load1"] / max(1, f["cpu.count"])
        if f["cpu.busy_pct"] < 85 or _g(f, "cpu.iowait_pct", 0) >= 15:
            return None
        if per >= 4:
            return CRITICAL
        if per >= 1.5:
            return WARN
        return None

    def say(f):
        return ("load %.1f over %d cores (%.1f per core), %.0f%% busy, "
                "%.0f%% of it system time"
                % (f["cpu.load1"], f["cpu.count"],
                   f["cpu.load1"] / max(1, f["cpu.count"]),
                   f["cpu.busy_pct"], _g(f, "cpu.sys_pct", 0)))

    def fix(f):
        return ("This is real CPU demand, not a queue behind something else. "
                "Busiest: %s.  Renice it, move the work, or add cores -- "
                "nothing else on this box will be fast until the run queue "
                "drops." % _top_cpu_str(f))
    return level, say, fix


# 2 ------------------------------------------------------------------------
@rule("CPU_STEAL", "hypervisor stealing cpu", ("cpu.steal_pct",),
      "Steal time is CPU the hypervisor gave to someone else. No amount of "
      "tuning inside the guest can recover it.")
def _cpu_steal():
    def level(f):
        if f["cpu.steal_pct"] >= 15:
            return CRITICAL
        if f["cpu.steal_pct"] >= 5:
            return WARN
        return None

    def say(f):
        return "%.1f%% of CPU time was taken by the hypervisor" % f["cpu.steal_pct"]

    def fix(f):
        return ("Nothing on this box can fix this -- the CPU is being given "
                "to another tenant.  Move off a burstable or shared instance "
                "type (t3/t4g, e2-micro and friends), or take this number to "
                "your provider: it is the evidence.")
    return level, say, fix


# 3 ------------------------------------------------------------------------
@rule("PSI_CPU", "tasks waiting for cpu", ("psi.cpu.some10",),
      "Pressure stall information counts real contention directly, unlike "
      "load average which also counts uninterruptible sleepers.")
def _psi_cpu():
    def level(f):
        if f["psi.cpu.some10"] >= 50:
            return CRITICAL
        if f["psi.cpu.some10"] >= 20:
            return WARN
        return None

    def say(f):
        return ("tasks were runnable but waiting %.0f%% of the last 10s"
                % f["psi.cpu.some10"])

    def fix(f):
        return ("Unlike load average this counts only real contention, so it "
                "is the number to trust.  Cut concurrency (worker counts, "
                "thread pools, parallel make) or add cores.")
    return level, say, fix


# 4 ------------------------------------------------------------------------
@rule("IO_STALL", "stalled on disk", ("psi.io.full10",),
      "psi.io.full means every task was blocked on I/O at once -- the whole "
      "box stopped, not just one process.")
def _io_stall():
    def level(f):
        if f["psi.io.full10"] >= 10:
            return CRITICAL
        if _g(f, "psi.io.some10", 0) >= 20:
            return WARN
        return None

    def say(f):
        return ("everything stalled on I/O %.0f%% of the last 10s "
                "(some %.0f%%)" % (f["psi.io.full10"],
                                   _g(f, "psi.io.some10", 0)))

    def fix(f):
        d = f.get("disk.busiest")
        bits = []
        if d:
            bits.append("Busiest device: %s at %.0f%% util, %.0fms average "
                        "wait." % (d, _g(f, "disk.busiest_util_pct", 0),
                                   _g(f, "disk.busiest_await_ms", 0)))
        ds = _g(f, "proc.dstate", [])
        if ds:
            bits.append("Waiting on it: %s."
                        % ", ".join("%s(%s)" % (p["comm"], p["pid"])
                                    for p in ds[:4]))
        bits.append("Find the writer with `iotop -o` or "
                    "`pidstat -d 1`, then decide whether the device or the "
                    "workload is wrong.")
        return "  ".join(bits)
    return level, say, fix


# 5 ------------------------------------------------------------------------
@rule("DISK_SATURATED", "disk at its limit",
      ("disk.busiest_util_pct", "disk.busiest_await_ms"),
      "A device at high utilisation with a long service time is at its "
      "physical limit; that is not a tuning problem.")
def _disk_saturated():
    def level(f):
        u, a = f["disk.busiest_util_pct"], f["disk.busiest_await_ms"]
        if u >= 90 and a >= 100:
            return CRITICAL
        if u >= 90 and a >= 20:
            return WARN
        return None

    def say(f):
        return ("%s at %.0f%% util, %.0f IOPS, %.1fms average wait, "
                "queue depth %d"
                % (f.get("disk.busiest"), f["disk.busiest_util_pct"],
                   _g(f, "disk.busiest_iops", 0), f["disk.busiest_await_ms"],
                   _g(f, "disk.busiest_inflight", 0)))

    def fix(f):
        return ("That is a device at its limit, not a knob you have not "
                "found yet.  Move the hot dataset, or check it is not a "
                "single spindle or a throttled cloud volume whose burst "
                "balance has run out.")
    return level, say, fix


# 6 ------------------------------------------------------------------------
@rule("MEM_EXHAUSTED", "memory exhausted", ("mem.available_pct",),
      "MemAvailable is the honest figure: it accounts for reclaimable cache, "
      "unlike the 'free' column which always looks alarming and never is.")
def _mem_exhausted():
    def level(f):
        if f["mem.available_pct"] <= 5:
            return CRITICAL
        if f["mem.available_pct"] <= 10:
            return WARN
        return None

    def say(f):
        return ("MemAvailable %s of %s (%.1f%%)"
                % (_kb(f.get("mem.available_kb")), _kb(f.get("mem.total_kb")),
                   f["mem.available_pct"]))

    def fix(f):
        big = _biggest_proc(f)
        s = "Ignore the 'free' column -- MemAvailable is the number.  "
        if big:
            s += ("%s (pid %s) holds %s.  Cap it and most of this report goes "
                  "away: `systemd-run --scope -p MemoryMax=...` on the next "
                  "restart, or the application's own heap limit.  "
                  % (big["comm"], big["pid"], _kb(big["rss_kb"])))
        s += "If it is meant to hold that much, this box needs RAM, not a knob."
        return s
    return level, say, fix


# 7 ------------------------------------------------------------------------
@rule("SWAP_THRASH", "swap thrashing",
      ("mem.pswpin_per_s", "mem.pswpout_per_s"),
      "Sustained paging makes everything slow, including processes that "
      "never allocate -- so it must outrank the CPU number it causes.")
def _swap_thrash():
    def level(f):
        rate = f["mem.pswpin_per_s"] + f["mem.pswpout_per_s"]
        if rate >= 10000 or _g(f, "psi.mem.full10", 0) >= 10:
            return CRITICAL
        if rate >= 1000:
            return WARN
        return None

    def say(f):
        rate = f["mem.pswpin_per_s"] + f["mem.pswpout_per_s"]
        return ("%.0f pages/s in+out (~%.0f MB/s), memory pressure full %.0f%%"
                % (rate, rate * 4 / 1024.0, _g(f, "psi.mem.full10", 0)))

    def fix(f):
        big = _biggest_proc(f)
        s = ("This is why *everything* is slow, including processes that "
             "never allocate.  ")
        if big:
            s += ("Cap the biggest consumer (%s, %s) or give the box more "
                  "RAM.  " % (big["comm"], _kb(big["rss_kb"])))
        s += ("While it swaps, any CPU number above is queueing on page "
              "faults rather than doing work -- fix this first and re-measure. "
              "vm.swappiness is a knob, not a cure.")
        return s
    return level, say, fix


# 8 ------------------------------------------------------------------------
@rule("OOM_KILLS", "kernel killed something", ("kern.oom",),
      "An OOM kill removes a process without the application knowing why; "
      "the failures it causes look unrelated and are debugged for hours.")
def _oom_kills():
    def level(f):
        return CRITICAL if f["kern.oom"] else None

    def say(f):
        n = len(f["kern.oom"])
        return "%d OOM kill%s in the kernel log" % (n, "" if n == 1 else "s")

    def fix(f):
        first = f["kern.oom"][-1] if f["kern.oom"] else ""
        return ("Most recent: %s.  That process is gone, and whatever "
                "depended on it now fails for reasons that look unrelated. "
                "Check the service is back up, then set a MemoryMax= so the "
                "next one is throttled rather than shot." % first[:160])
    return level, say, fix


# 9 ------------------------------------------------------------------------
@rule("CGROUP_MEM", "at your cgroup memory limit", ("cg.mem_pct",),
      "A container at its own limit is out of memory even when the host has "
      "hundreds of free gigabytes. The host's numbers are a distraction.")
def _cgroup_mem():
    def level(f):
        if f["cg.mem_pct"] >= 98 or _g(f, "cg.mem_events_max", 0) > 0:
            return CRITICAL
        if f["cg.mem_pct"] >= 90:
            return WARN
        return None

    def say(f):
        return ("this cgroup is at %.0f%% of its own limit (%s of %s)%s"
                % (f["cg.mem_pct"],
                   _kb((f.get("cg.mem_current") or 0) // 1024),
                   _kb((f.get("cg.mem_max") or 0) // 1024),
                   ", and has hit it %d times" % f["cg.mem_events_max"]
                   if _g(f, "cg.mem_events_max", 0) > 0 else ""))

    def fix(f):
        return ("The host may have plenty of memory free -- that is not the "
                "constraint you are hitting.  Raise the limit (`--memory` on "
                "docker, `MemoryMax=` on systemd, `resources.limits.memory` "
                "on Kubernetes) or make the workload fit it.")
    return level, say, fix


# 10 -----------------------------------------------------------------------
@rule("CGROUP_THROTTLED", "cpu throttled by quota", ("cg.throttled_pct",),
      "CFS throttling stops the cgroup dead until the next period. It shows "
      "up as latency spikes on a box that looks idle.")
def _cgroup_throttled():
    def level(f):
        if f["cg.throttled_pct"] >= 40:
            return CRITICAL
        if f["cg.throttled_pct"] >= 10:
            return WARN
        return None

    def say(f):
        return ("throttled in %.0f%% of scheduling periods; quota is %.1f cores"
                % (f["cg.throttled_pct"], _g(f, "cg.cpu_quota_cores", 0)))

    def fix(f):
        return ("Every throttle stops the whole cgroup until the next period, "
                "which reads as latency spikes on an apparently idle box.  "
                "Raise the quota, or lower the thread count: a small quota "
                "with a big thread pool throttles hard at every period "
                "boundary.")
    return level, say, fix


# 11 -----------------------------------------------------------------------
@rule("CPU_QUOTA_VS_NPROC", "thread pools sized from the wrong number",
      ("cg.cpu_quota_cores", "cpu.count"),
      "Runtimes size their pools from nproc, which reports the host's cores, "
      "not the cgroup's quota. The mismatch causes constant throttling.")
def _quota_vs_nproc():
    def level(f):
        q = f["cg.cpu_quota_cores"]
        if q > 0 and f["cpu.count"] / q >= 4:
            return WARN
        return None

    def say(f):
        return ("nproc reports %d cores but the quota is %.1f"
                % (f["cpu.count"], f["cg.cpu_quota_cores"]))

    def fix(f):
        q = int(max(1, round(f["cg.cpu_quota_cores"])))
        return ("Anything sizing a pool from nproc -- Go's GOMAXPROCS, the "
                "JVM before 8u191, OpenBLAS, `make -j$(nproc)` -- is building "
                "a %d-way pool for a %.1f-core budget.  Set GOMAXPROCS=%d, "
                "-XX:ActiveProcessorCount=%d, OMP_NUM_THREADS=%d."
                % (f["cpu.count"], f["cg.cpu_quota_cores"], q, q, q))
    return level, say, fix


# 12 -----------------------------------------------------------------------
@rule("FS_FULL", "filesystem full", ("fs.fullest_pct",),
      "A full filesystem makes everything that logs or caches fail at once, "
      "which presents as a hundred unrelated bugs.")
def _fs_full():
    def level(f):
        if f["fs.fullest_pct"] >= 99:
            return CRITICAL
        if f["fs.fullest_pct"] >= 95:
            return WARN
        return None

    def say(f):
        return "%s is %.1f%% full" % (f.get("fs.fullest"), f["fs.fullest_pct"])

    def fix(f):
        m = f.get("fs.fullest")
        return ("Everything that writes there is now failing, which surfaces "
                "as unrelated errors all over the box.  `du -xh %s | sort -h "
                "| tail -20`, and `journalctl --vacuum-size=200M` if it is "
                "/var." % m)
    return level, say, fix


# 13 -----------------------------------------------------------------------
@rule("FS_INODES", "out of inodes", ("fs.worst_inode_pct",),
      "Inode exhaustion looks like a full disk to applications while df "
      "shows plenty of free space, so it is missed for hours.")
def _fs_inodes():
    def level(f):
        if f["fs.worst_inode_pct"] >= 95:
            return CRITICAL
        if f["fs.worst_inode_pct"] >= 90:
            return WARN
        return None

    def say(f):
        m = f.get("fs.worst_inode")
        blocks = _g(f, "fs.all", {}).get(m, {}).get("used_pct", 0)
        return ("%s has used %.0f%% of its inodes but only %.0f%% of its "
                "blocks" % (m, f["fs.worst_inode_pct"], blocks))

    def fix(f):
        return ("Millions of tiny files -- almost always a spool, session or "
                "cache directory.  Writes fail with ENOSPC while df shows "
                "free space, which is why this gets missed.  Find it with "
                "`find %s -xdev -type f | head -1000000 | "
                "sed 's|/[^/]*$||' | sort | uniq -c | sort -n | tail`."
                % f.get("fs.worst_inode"))
    return level, say, fix


# 14 -----------------------------------------------------------------------
@rule("IO_ERRORS", "disk hardware errors", ("kern.io_errors",),
      "I/O errors in the kernel log mean failing hardware or a disappearing "
      "remote filesystem. No tuning applies.")
def _io_errors():
    def level(f):
        return CRITICAL if f["kern.io_errors"] else None

    def say(f):
        return ("%d I/O error lines in the kernel log"
                % len(f["kern.io_errors"]))

    def fix(f):
        return ("This is not tuning -- it is failing storage.  Most recent: "
                "%s.  Run `smartctl -a` on the device now and get the data "
                "off before you touch anything else."
                % (f["kern.io_errors"][-1][:160]))
    return level, say, fix


# 15 -----------------------------------------------------------------------
@rule("DSTATE_PROCS", "processes stuck in uninterruptible sleep",
      ("proc.dstate", "cpu.count"),
      "D-state processes cannot be killed and do not consume CPU; they are "
      "waiting on a device or a mount that stopped answering.")
def _dstate():
    def level(f):
        n = len(f["proc.dstate"])
        if n >= max(4, f["cpu.count"]):
            return CRITICAL
        if n >= 3:
            return WARN
        return None

    def say(f):
        ps = f["proc.dstate"]
        return ("%d processes in uninterruptible sleep: %s"
                % (len(ps), ", ".join("%s(%s)" % (p["comm"], p["pid"])
                                      for p in ps[:5])))

    def fix(f):
        return ("They cannot be killed, only unblocked -- kill -9 will not "
                "touch them.  Usually NFS, a dying disk, or a device that "
                "stopped answering.  Check your mounts and the kernel log; "
                "`cat /proc/<pid>/stack` names the exact wait if you have "
                "root.")
    return level, say, fix


# 16 -----------------------------------------------------------------------
@rule("THERMAL_THROTTLE", "cpu clocked down",
      ("therm.cur_ghz", "therm.max_ghz", "cpu.busy_pct"),
      "A busy CPU running far below its rated clock is throttling; you are "
      "paying for silicon you are not getting.")
def _thermal():
    def level(f):
        if f["cpu.busy_pct"] < 70:
            return None
        if f["therm.cur_ghz"] < 0.7 * f["therm.max_ghz"]:
            return WARN
        return None

    def say(f):
        return ("running at %.1f GHz of a possible %.1f while %.0f%% busy%s"
                % (f["therm.cur_ghz"], f["therm.max_ghz"], f["cpu.busy_pct"],
                   ", %.0f C" % f["therm.max_temp_c"]
                   if f.get("therm.max_temp_c") else ""))

    def fix(f):
        return ("You are getting a fraction of the CPU you have.  Check "
                "airflow and dust, the BIOS power profile, and "
                "`cpupower frequency-info` to see what is capping it.")
    return level, say, fix


# 17 -----------------------------------------------------------------------
@rule("NET_DROPS", "network interface dropping packets",
      ("net.worst_drop_pct",),
      "Drops at the interface mean the ring buffer or the CPU draining it "
      "cannot keep up; TCP hides this as latency.")
def _net_drops():
    def level(f):
        if f["net.worst_drop_pct"] >= 1:
            return CRITICAL
        if f["net.worst_drop_pct"] >= 0.01:
            return WARN
        return None

    def say(f):
        return ("%s dropping %.0f pkt/s (%.2f%% of receive)"
                % (f.get("net.worst_if"), _g(f, "net.worst_drop_per_s", 0),
                   f["net.worst_drop_pct"]))

    def fix(f):
        return ("Either the ring buffer is too small (`ethtool -g %s`, then "
                "`ethtool -G %s rx 4096`) or the box cannot drain the queue "
                "-- check softirq time (%.0f%% here) and whether one core is "
                "taking every interrupt."
                % (f.get("net.worst_if"), f.get("net.worst_if"),
                   _g(f, "cpu.softirq_pct", 0)))
    return level, say, fix


# 18 -----------------------------------------------------------------------
@rule("LISTEN_OVERFLOW", "accept queue overflowing",
      ("net.listen_overflows_per_s",),
      "An overflowing accept queue drops new connections silently; the "
      "client sees a timeout with nothing in any server log.")
def _listen_overflow():
    def level(f):
        return WARN if f["net.listen_overflows_per_s"] > 0 else None

    def say(f):
        return ("%.1f connection%s/s dropped at the accept queue"
                % (f["net.listen_overflows_per_s"],
                   "" if f["net.listen_overflows_per_s"] == 1 else "s"))

    def fix(f):
        return ("New connections are being dropped silently -- the client "
                "sees a timeout and your logs show nothing.  Raise "
                "net.core.somaxconn *and* the application's own listen() "
                "backlog: raising one without the other changes nothing.")
    return level, say, fix


# 19 -----------------------------------------------------------------------
@rule("TCP_RETRANS", "tcp retransmitting", ("net.tcp_retrans_pct",),
      "Retransmits mean segments are being lost between here and the peer; "
      "throughput collapses long before the loss rate looks alarming.")
def _tcp_retrans():
    def level(f):
        if f["net.tcp_retrans_pct"] >= 5:
            return CRITICAL
        if f["net.tcp_retrans_pct"] >= 2:
            return WARN
        return None

    def say(f):
        return "%.1f%% of sent segments were retransmitted" % f["net.tcp_retrans_pct"]

    def fix(f):
        return ("Segments are being lost on the path, and TCP's backoff turns "
                "a small loss rate into a large slowdown.  Measure the "
                "network itself rather than guessing: "
                "`netmesh check <this host> <the peer>`.")
    return level, say, fix


# 20 -----------------------------------------------------------------------
@rule("FORK_STORM", "fork storm", ("cpu.forks_per_s",),
      "A high process creation rate is almost always a retry loop or "
      "overlapping cron jobs, and it starves everything else.")
def _fork_storm():
    def level(f):
        if f["cpu.forks_per_s"] >= 1000:
            return CRITICAL
        if f["cpu.forks_per_s"] >= 200:
            return WARN
        return None

    def say(f):
        return "%.0f new processes per second" % f["cpu.forks_per_s"]

    def fix(f):
        return ("Something is in a fork loop -- usually a shell script "
                "retrying without a backoff, or cron jobs overlapping because "
                "each run takes longer than the interval.  `pidstat -w 1` or "
                "`execsnoop` will name the parent within seconds.")
    return level, say, fix


# 21 -----------------------------------------------------------------------
@rule("SYSTEMD_FAILED", "failed units", ("sys.failed_units",),
      "A failed unit is a service that is not doing its job; a failed time "
      "sync in particular makes every timestamp on the box a lie.")
def _failed_units():
    def level(f):
        return WARN if f["sys.failed_units"] else None

    def say(f):
        u = f["sys.failed_units"]
        return "%d failed unit%s: %s" % (len(u), "" if len(u) == 1 else "s",
                                         " ".join(u[:5]))

    def fix(f):
        u = f["sys.failed_units"]
        s = "`systemctl status %s -l --no-pager` for the reason." % u[0]
        if any("chrony" in x or "ntp" in x or "timesyncd" in x for x in u):
            s += ("  Note the clock sync unit is among them, which makes "
                  "every timestamp on this box untrustworthy -- including "
                  "the ones you are about to correlate against.")
        return s
    return level, say, fix


# 22 -----------------------------------------------------------------------
@rule("GOVERNOR", "powersave governor on a busy box",
      ("therm.governor", "cpu.busy_pct"),
      "The powersave governor costs 20-30% on server workloads and is a "
      "common default on machines that were never tuned.")
def _governor():
    def level(f):
        if f["therm.governor"] in ("powersave", "conservative") and \
                f["cpu.busy_pct"] >= 30:
            return INFO
        return None

    def say(f):
        return ("scaling governor is '%s' while the box is %.0f%% busy"
                % (f["therm.governor"], f["cpu.busy_pct"]))

    def fix(f):
        return ("Usually a free 20-30% on server workloads: "
                "`cpupower frequency-set -g performance` (and make it stick "
                "across reboots).")
    return level, say, fix


# 23 -----------------------------------------------------------------------
@rule("ENTROPY", "entropy pool low", ("sys.entropy",),
      "A drained entropy pool blocks getrandom(), which presents only as "
      "'TLS handshakes are slow' and nothing else.")
def _entropy():
    def level(f):
        kern = _g(f, "sys.kernel", "") or ""
        m = re.match(r"(\d+)\.(\d+)", kern)
        if m and (int(m.group(1)), int(m.group(2))) >= (5, 6):
            return None       # getrandom stopped blocking in 5.6
        return WARN if f["sys.entropy"] < 200 else None

    def say(f):
        return "entropy_avail is %d" % f["sys.entropy"]

    def fix(f):
        return ("Anything calling getrandom() blocks, which shows up as slow "
                "TLS handshakes and nothing else.  Install rng-tools or "
                "haveged, or move to a kernel 5.6 or newer where this stopped "
                "being a problem.")
    return level, say, fix


# ---------------------------------------------------------------------------
# Ceilings rather than rates
# ---------------------------------------------------------------------------
#
# Everything above this point measures how hard the box is working.  The
# rules below measure how close it is to a wall, which is a different
# failure and reads nothing like slowness: the load is normal, the disks
# are quiet, memory is fine, and connections are being refused anyway.
# Each is a ratio against a kernel ceiling, because below the ceiling there
# is no gradient to see -- it works, until abruptly it does not.

# 24 -----------------------------------------------------------------------
@rule("LIMIT_HITS", "a kernel limit was actually hit", ("kern.limit_hits",),
      "A table that filled an hour ago is invisible to any ratio measured "
      "now: it drains, and the only evidence left is the line the kernel "
      "wrote at the time.")
def _limit_hits():
    def level(f):
        return CRITICAL if f["kern.limit_hits"] else None

    def say(f):
        n = len(f["kern.limit_hits"])
        return ("%d line%s in the kernel log where a limit was reached"
                % (n, "" if n == 1 else "s"))

    def fix(f):
        return ("The kernel has already refused work here -- most recent: "
                "%s.  Whatever failed at that moment failed for this reason "
                "and not the one in its own logs.  The ratios below are "
                "measured now and will look healthy if the table has since "
                "drained, so trust this line over them."
                % f["kern.limit_hits"][-1][:160])
    return level, say, fix


# 25 -----------------------------------------------------------------------
@rule("CONNTRACK_FULL", "connection tracking table filling",
      ("lim.conntrack_pct",),
      "A full conntrack table makes the kernel drop new connections while "
      "every rate on the box still looks healthy, and the only symptom is "
      "clients timing out.")
def _conntrack_full():
    def level(f):
        if f["lim.conntrack_pct"] >= 90:
            return CRITICAL
        if f["lim.conntrack_pct"] >= 75:
            return WARN
        return None

    def say(f):
        return ("%d of %d conntrack entries in use (%.0f%%)"
                % (_g(f, "lim.conntrack", 0), _g(f, "lim.conntrack_max", 0),
                   f["lim.conntrack_pct"]))

    def fix(f):
        return ("At the ceiling the kernel drops new connections and logs "
                "'nf_conntrack: table full' -- clients see timeouts and your "
                "application logs show nothing at all.  Raise "
                "net.netfilter.nf_conntrack_max (and nf_conntrack_buckets "
                "with it, or the hash degrades into a list), or shorten "
                "nf_conntrack_tcp_timeout_established, which is 5 days by "
                "default and is why the table fills with connections that "
                "ended long ago.")
    return level, say, fix


# 26 -----------------------------------------------------------------------
@rule("FD_EXHAUSTION", "file descriptors running out",
      ("lim.fd_pct",),
      "Out of descriptors, every accept() and open() fails at once, across "
      "every process on the box, and the errors name the victim rather than "
      "the cause.")
def _fd_exhaustion():
    def level(f):
        if f["lim.fd_pct"] >= 90:
            return CRITICAL
        if f["lim.fd_pct"] >= 75:
            return WARN
        return None

    def say(f):
        return ("%d of %d file descriptors allocated system-wide (%.0f%%)"
                % (_g(f, "lim.fd_used", 0), _g(f, "lim.fd_max", 0),
                   f["lim.fd_pct"]))

    def fix(f):
        return ("Raise fs.file-max for the box, but check the per-process "
                "limit first: it is far more often the binding one, and it "
                "fails identically.  `cat /proc/<pid>/limits | grep files` "
                "for the process that is failing, then LimitNOFILE= in its "
                "unit -- ulimit in a shell profile does not reach a systemd "
                "service.")
    return level, say, fix


# 27 -----------------------------------------------------------------------
@rule("PID_EXHAUSTION", "task table running out",
      ("lim.pid_pct",),
      "At pid_max, fork() fails for everything including the shell you "
      "would use to fix it, so this is worth seeing while it is still a "
      "ratio.")
def _pid_exhaustion():
    def level(f):
        if f["lim.pid_pct"] >= 90:
            return CRITICAL
        if f["lim.pid_pct"] >= 75:
            return WARN
        return None

    def say(f):
        return ("%d tasks against a pid_max of %d (%.0f%%)"
                % (_g(f, "lim.tasks", 0), _g(f, "lim.pid_max", 0),
                   f["lim.pid_pct"]))

    def fix(f):
        return ("Threads count here, not just processes, so this is usually "
                "one runtime with an unbounded pool rather than a fork loop. "
                " `ps -eLf | awk '{print $1}' | sort | uniq -c | sort -n | "
                "tail` names the owner.  Raising kernel.pid_max buys room; "
                "it does not fix a pool with no ceiling.")
    return level, say, fix


# 28 -----------------------------------------------------------------------
@rule("CGROUP_PIDS", "at your cgroup task limit", ("cg.pids_pct",),
      "A container at its own TasksMax cannot fork while the host has "
      "thousands of free slots -- the host's numbers say nothing about it.")
def _cgroup_pids():
    def level(f):
        if f["cg.pids_pct"] >= 95:
            return CRITICAL
        if f["cg.pids_pct"] >= 85:
            return WARN
        return None

    def say(f):
        return ("this cgroup holds %d of its %d permitted tasks (%.0f%%)"
                % (_g(f, "cg.pids_current", 0), _g(f, "cg.pids_max", 0),
                   f["cg.pids_pct"]))

    def fix(f):
        return ("At the limit every fork fails with 'Resource temporarily "
                "unavailable' -- which reads as a memory problem and is not. "
                " Raise TasksMax= on the unit, --pids-limit on docker, or "
                "pids.max on the cgroup directly.  The host being fine is "
                "not evidence against this.")
    return level, say, fix


# 29 -----------------------------------------------------------------------
@rule("EPHEMERAL_PORTS", "ephemeral ports running out",
      ("lim.ephemeral_pct",),
      "A client that has consumed its local port range cannot open an "
      "outbound connection at all, and the failure surfaces as 'cannot "
      "assign requested address' rather than anything about ports.")
def _ephemeral_ports():
    def level(f):
        if f["lim.ephemeral_pct"] >= 90:
            return CRITICAL
        if f["lim.ephemeral_pct"] >= 70:
            return WARN
        return None

    def say(f):
        return ("%d sockets in TIME_WAIT against a local port range of %d "
                "(%.0f%%)" % (_g(f, "lim.tw", 0), _g(f, "lim.ephemeral", 0),
                              f["lim.ephemeral_pct"]))

    def fix(f):
        return ("Every closed outbound connection holds its port for a "
                "minute, so this is a connection *rate* problem wearing a "
                "capacity mask -- almost always a client that opens a fresh "
                "connection per request.  Turn on keepalive or pooling in "
                "that client, widen net.ipv4.ip_local_port_range, and set "
                "net.ipv4.tcp_tw_reuse=1.  Do not reach for tcp_tw_recycle: "
                "it breaks NAT and was removed in 4.12.")
    return level, say, fix


# 30 -----------------------------------------------------------------------
@rule("ARP_TABLE_FULL", "neighbour table filling", ("lim.arp_pct",),
      "Past gc_thresh3 the kernel drops entries it still needs, so hosts on "
      "the local network become intermittently unreachable in a pattern "
      "that looks exactly like a flapping switch.")
def _arp_table():
    def level(f):
        if f["lim.arp_pct"] >= 90:
            return CRITICAL
        if f["lim.arp_pct"] >= 75:
            return WARN
        return None

    def say(f):
        return ("%d neighbour entries against a gc_thresh3 of %d (%.0f%%)"
                % (_g(f, "lim.arp", 0), _g(f, "lim.arp_max", 0),
                   f["lim.arp_pct"]))

    def fix(f):
        return ("The default ceiling is 1024, which a flat /16 or a busy "
                "container host passes without anything being wrong.  Raise "
                "net.ipv4.neigh.default.gc_thresh1/2/3 together -- raising "
                "only gc_thresh3 leaves the garbage collector working "
                "against you.  The symptom is intermittent unreachability "
                "that looks like a network fault, so check this before "
                "blaming the switch.")
    return level, say, fix


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------

# A swapping box also looks CPU-busy, and a box with dying disks also looks
# I/O-bound.  Reporting the symptom over the cause is the exact failure this
# tool exists to prevent, so causes are pulled to the front of the verdict.
#
# The exhaustion rules sit above the network symptoms they cause and below
# the memory ones that cause them: a full conntrack table produces drops
# and retransmits, so naming the drops would be the same mistake as naming
# the CPU on a swapping box.  LIMIT_HITS ranks with OOM_KILLS because both
# are the kernel reporting that it already refused work, which outranks any
# ratio measured afterwards.
VERDICT_PRECEDENCE = [
    "IO_ERRORS", "OOM_KILLS", "LIMIT_HITS", "SWAP_THRASH", "MEM_EXHAUSTED",
    "CGROUP_MEM", "FS_FULL", "FS_INODES", "CONNTRACK_FULL", "FD_EXHAUSTION",
    "PID_EXHAUSTION", "CGROUP_PIDS", "EPHEMERAL_PORTS", "ARP_TABLE_FULL",
    "CPU_STEAL", "DISK_SATURATED", "IO_STALL",
    "DSTATE_PROCS", "CGROUP_THROTTLED", "NET_DROPS", "LISTEN_OVERFLOW",
    "TCP_RETRANS", "CPU_SATURATED", "PSI_CPU", "FORK_STORM",
    "THERMAL_THROTTLE", "CPU_QUOTA_VS_NPROC", "SYSTEMD_FAILED",
    "GOVERNOR", "ENTROPY",
]

Finding = namedtuple("Finding", "rule severity say fix")

# Not in RULES: it is not a rule about the machine, it is a statement that
# no rule could be evaluated at all.
NOTHING_CHECKED = Rule("NOTHING_CHECKED", "nothing could be checked", (),
                       None, None, None,
                       "Every rule was skipped for want of the facts it "
                       "needs, so the run says nothing about the machine.")


def missing_reason(f, key):
    if key in MISSING_REASONS:
        return MISSING_REASONS[key]
    return "%s not available on this machine" % key


def evaluate(facts):
    findings, skipped, passed = [], [], []
    for r in RULES:
        missing = [k for k in r.needs
                   if facts.get(k) is None
                   or (isinstance(facts.get(k), (list, dict))
                       and k in ("proc.dstate",) and False)]
        if missing:
            skipped.append((r, missing_reason(facts, missing[0])))
            continue
        try:
            sev = r.level(facts)
        except (TypeError, ValueError, ZeroDivisionError, KeyError) as exc:
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
        return ("This box is not slow.  If something feels slow, the problem "
                "is off-box: DNS, a dependency, or the network.")
    top = findings[0]
    consequences = {
        "SWAP_THRASH": "This box is memory-starved and swapping.  Everything "
                       "else you see is a consequence of that.",
        "MEM_EXHAUSTED": "This box is out of memory.  Any CPU or I/O number "
                         "below is a symptom of that, not a separate problem.",
        "IO_ERRORS": "The storage on this box is failing.  Stop tuning and "
                     "start copying data off.",
        "OOM_KILLS": "The kernel has been killing processes here.  Whatever "
                     "is broken downstream probably starts with that.",
        "CGROUP_MEM": "This container is at its own memory limit.  The host "
                      "is likely fine; your limit is not.",
        "CPU_STEAL": "The hypervisor is taking this machine's CPU.  Nothing "
                     "you change inside the guest will fix it.",
        "DISK_SATURATED": "The disk is at its physical limit.  Everything "
                          "queueing behind it looks like a separate problem "
                          "and is not.",
        "FS_FULL": "A filesystem is full.  Expect unrelated-looking failures "
                   "everywhere until it is not.",
        "CGROUP_THROTTLED": "This cgroup is being throttled against its CPU "
                            "quota, which is why it stalls on an idle box.",
        "NOTHING_CHECKED": "Nothing here was measured, so this report says "
                           "nothing about the machine.  It is not a clean "
                           "bill of health -- it is an empty one.",
        # These read differently from everything above: the box is not slow,
        # it is refusing.  Saying so plainly stops the reader from going
        # looking for a bottleneck that is not there.
        "LIMIT_HITS": "The kernel has already refused work on this box "
                      "because a table was full.  Whatever failed at that "
                      "moment failed for that reason, not the one in its "
                      "own logs.",
        "CONNTRACK_FULL": "This box is running out of connection tracking "
                          "entries.  It is not slow -- past the ceiling it "
                          "silently drops new connections.",
        "FD_EXHAUSTION": "This box is running out of file descriptors.  "
                         "Nothing here is overloaded; it is about to start "
                         "failing every open() and accept() at once.",
        "PID_EXHAUSTION": "This box is running out of task slots.  At the "
                          "limit fork() fails for everything, including the "
                          "shell you would fix it from.",
        "CGROUP_PIDS": "This container is at its own task limit.  The host "
                       "has slots free; that is not the constraint you are "
                       "hitting.",
        "EPHEMERAL_PORTS": "This box is running out of local ports for "
                           "outbound connections.  It is a connection rate "
                           "problem wearing a capacity mask.",
        "ARP_TABLE_FULL": "The neighbour table is filling, so hosts on the "
                          "local network will drop in and out in a way that "
                          "looks exactly like a flapping switch.",
    }
    return consequences.get(top.rule.id,
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


def render_human(facts, findings, skipped, passed, args, C):
    out = []
    ncpu = facts.get("cpu.count")
    head = "%s -- %s" % (PROG, facts.get("sys.hostname") or "?")
    bits = []
    if facts.get("sample.interval_s"):
        bits.append("%.1fs sample" % facts["sample.interval_s"])
    else:
        bits.append("single snapshot")
    if facts.get("sys.kernel"):
        bits.append("Linux %s" % facts["sys.kernel"])
    if ncpu:
        bits.append("%d cores" % ncpu)
    if facts.get("sys.container"):
        lim = []
        if facts.get("cg.mem_max"):
            lim.append("%s" % _kb(facts["cg.mem_max"] // 1024))
        if facts.get("cg.cpu_quota_cores"):
            lim.append("%.1f cores" % facts["cg.cpu_quota_cores"])
        bits.append("container%s" % (" limit %s" % " / ".join(lim)
                                     if lim else ""))
    out.append("%s, %s" % (head, ", ".join(bits)))
    out.append("")

    v = verdict_line(findings, facts)
    out.append("  %s   %s" % (C.bold("VERDICT"), _wrap(v, 12)))
    out.append("")

    shown = [f for f in findings
             if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[args.min_severity]]
    for f in shown:
        tag = {CRITICAL: C.crit("CRITICAL"), WARN: C.warn("WARN    "),
               INFO: C.info("INFO    ")}[f.severity]
        out.append("  %s  %-22s %s" % (tag, f.rule.title, f.say))
    if passed and not args.quiet:
        titles = sorted(set(r.title for r in passed))
        names = ", ".join(titles[:6])
        if len(titles) > 6:
            names += " (+%d)" % (len(titles) - 6)
        out.append("  %s        %s" % (C.dim("ok"), C.dim(_wrap(names, 12))))
    if skipped and (args.all or not facts.get("kern.available", True)):
        for r, why in skipped if args.all else []:
            out.append("  %s   %-22s %s" % (C.dim("skipped"), r.title,
                                            C.dim(why)))
        if not args.all:
            out.append("  %s   %s"
                       % (C.dim("skipped"),
                          C.dim(_wrap(MISSING_REASONS["kern.oom"], 12))))
    out.append("")

    if not args.quiet:
        out.append("  %s" % C.bold("EVIDENCE"))
        if facts.get("proc.top_rss"):
            out.append("    top rss    %s" % _top_rss_str(facts, args.top))
        if facts.get("proc.top_cpu"):
            out.append("    top cpu    %s" % _top_cpu_str(facts, args.top))
        psi = []
        for label, key in (("cpu some", "psi.cpu.some10"),
                           ("io some", "psi.io.some10"),
                           ("io full", "psi.io.full10"),
                           ("mem full", "psi.mem.full10")):
            if facts.get(key) is not None:
                psi.append("%s %.0f%%" % (label, facts[key]))
        if psi:
            out.append("    pressure   %s  (avg10)" % "  ".join(psi))
        if facts.get("disk.busiest"):
            out.append("    disks      %s  %.0f%% busy  %.0f IOPS  "
                       "await %.1fms"
                       % (facts["disk.busiest"],
                          _g(facts, "disk.busiest_util_pct", 0),
                          _g(facts, "disk.busiest_iops", 0),
                          _g(facts, "disk.busiest_await_ms", 0)))
        if facts.get("mem.available_pct") is not None:
            out.append("    memory     %s available of %s (%.1f%%)"
                       % (_kb(facts.get("mem.available_kb")),
                          _kb(facts.get("mem.total_kb")),
                          facts["mem.available_pct"]))
        # How close the box is to each wall, whether or not a rule fired --
        # a table at 60% is worth seeing before the day it is at 100%.
        lims = []
        for label, key in (("fds", "lim.fd_pct"), ("tasks", "lim.pid_pct"),
                           ("conntrack", "lim.conntrack_pct"),
                           ("ports", "lim.ephemeral_pct"),
                           ("arp", "lim.arp_pct"),
                           ("cgroup tasks", "cg.pids_pct")):
            if facts.get(key) is not None:
                lims.append("%s %.0f%%" % (label, facts[key]))
        if lims:
            out.append("    limits     %s  (of ceiling)" % "  ".join(lims))
        out.append("")

    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    if not shown:
        out.append("    %s" % _wrap(
            "Nothing here is wrong.  Load %s over %d cores, no pressure, no "
            "swapping, disks quiet.  If it still feels slow the problem is "
            "not on this box -- look at DNS, a dependency it calls, or the "
            "network between them."
            % (facts.get("cpu.load1", "?"), ncpu or 0), 4))
        out.append("")
        out.append("    %s" % _wrap(
            "resolve <the name it looks up>         (is it DNS?)", 4))
        out.append("    %s" % _wrap(
            "netmesh check <this host> <the peer>   (is it the network?)",
            4))
        out.append("    %s" % _wrap(
            "logtriage /var/log/syslog              (what changed?)", 4))
    else:
        for f in shown[:4]:
            out.append("    * %s" % _wrap(f.fix, 6))
        if facts.get("sample.interval_s", 0) and \
                facts["sample.interval_s"] < 5:
            out.append("    * Confirm before acting: %s --interval 10"
                       % PROG)
    out.append("")
    return "\n".join(out)


def _wrap(text, indent, width=76):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width - indent:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    pad = " " * indent
    return ("\n" + pad).join(lines)


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
        "facts": {k: v for k, v in sorted(facts.items())},
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
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("--ssh", metavar="HOST")
    p.add_argument("--ssh-cmd", default=_env("SSH", "ssh"), metavar="CMD")
    p.add_argument("--interval", type=float,
                   default=float(_env("INTERVAL", 2.0)))
    p.add_argument("--top", type=int, default=int(_env("TOP", 3)))
    p.add_argument("--min-severity", default=_env("MIN_SEVERITY", "info"),
                   choices=["info", "warn", "critical"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--boot-window", type=float,
                   default=float(_env("BOOT_WINDOW", 3600)))
    p.add_argument("--exit-code", action="store_true")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--facts", action="store_true")
    p.add_argument("--from-facts", metavar="FILE")
    p.add_argument("--rules", action="store_true")
    p.add_argument("--explain", metavar="RULE_ID")
    p.add_argument("--proc-root", default=_env("PROC", "/proc"))
    p.add_argument("--sys-root", default=_env("SYS", "/sys"))
    p.add_argument("--no-exec", action="store_true")
    p.add_argument("--no-color", action="store_true")
    return p


def cmd_rules():
    log_out = []
    for r in RULES:
        log_out.append("%-22s %s" % (r.id, r.title))
        log_out.append("    needs: %s" % ", ".join(r.needs))
        log_out.append("    %s" % _wrap(r.why, 4))
        log_out.append("")
    sys.stdout.write("\n".join(log_out))
    return 0


def cmd_explain(rid):
    for r in RULES:
        if r.id == rid.upper():
            sys.stdout.write("%s -- %s\n\n%s\n\nneeds: %s\n"
                             % (r.id, r.title, _wrap(r.why, 0),
                                ", ".join(r.needs)))
            return 0
    die("no such rule: %s  (try --rules)" % rid)


def _strip_flag(argv, flag):
    """ARGV without FLAG and its value, in both `--x v` and `--x=v` forms."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == flag:
            skip = True
        elif not a.startswith(flag + "="):
            out.append(a)
    return out


def run_remote(host, argv, ssh_cmd):
    """Run this same file on HOST by feeding it to `python3 -` over ssh.

    Nothing is copied to the box and nothing is left on it: the source
    travels on stdin and the report comes back on stdout, so this works on
    a machine that has never heard of binnacle.  The remote exit code is
    passed through, so --exit-code keeps its meaning.  One host only,
    deliberately -- fanning out and grouping the answers is agree's job.
    """
    fwd = _strip_flag(_strip_flag(argv, "--ssh"), "--ssh-cmd")
    try:
        with io.open(__file__, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        die("cannot read my own source to send it to %s: %s" % (host, exc))
    # ssh joins its trailing words with spaces and hands them to the remote
    # shell, so each forwarded argument is quoted for that shell.
    cmd = shlex.split(ssh_cmd) + [host, "python3", "-"] \
        + [shlex.quote(a) for a in fwd]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    except OSError as exc:
        die("cannot run %s: %s" % (cmd[0], exc))
    proc.communicate(source.encode("utf-8"))
    if proc.returncode == 255:
        # 255 is ssh's own failure, never this tool's: the report above (if
        # any) is ssh's stderr, not a diagnosis.
        sys.stderr.write("%s: ssh to %s failed (exit 255) -- the box was "
                         "never diagnosed\n" % (PROG, host))
    return proc.returncode


def main(argv=None):
    _stdio_safe()
    global PROC, SYS, NO_EXEC
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    if args.rules:
        return cmd_rules()
    if args.explain:
        return cmd_explain(args.explain)

    if args.ssh:
        if args.no_exec:
            die("--ssh has to run ssh, which --no-exec forbids -- drop one")
        if args.csv or args.json:
            # A PATH would be opened by the remote side, and a report file
            # quietly landing on the other box is exactly the kind of
            # surprise the rest of this tool exists to avoid.
            flag, ext = ("--csv", "csv") if args.csv else ("--json", "json")
            die("%s with a PATH would write the file on %s, not here -- "
                "drop the path and redirect instead:  %s --ssh %s %s > "
                "out.%s" % (flag, args.ssh, PROG, args.ssh, flag, ext))
        return run_remote(args.ssh, raw_argv, args.ssh_cmd)

    PROC, SYS = args.proc_root, args.sys_root
    NO_EXEC = args.no_exec
    args.min_severity = {"info": INFO, "warn": WARN,
                         "critical": CRITICAL}[args.min_severity]

    if args.from_facts:
        try:
            with io.open(args.from_facts, encoding="utf-8-sig", errors="replace") as fh:
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
        if not os.path.isdir(PROC):
            die("%s is not readable -- this tool needs /proc (Linux only)"
                % PROC)
        facts = collect_facts(args.interval, max(args.top, 10),
                              args.boot_window)

    if args.facts:
        sys.stdout.write(json.dumps(facts, indent=2, sort_keys=True,
                                    default=str) + "\n")
        return 0

    findings, skipped, passed = evaluate(facts)
    if not findings and not passed:
        # Nothing was measurable, so nothing was checked.  Saying "this box
        # is not slow" here would be the exact failure the skip-with-a-
        # reason convention exists to prevent, one step further along: not
        # a report that quietly checked less, but one that checked nothing
        # at all and still came back clean.
        findings = [Finding(NOTHING_CHECKED, WARN,
                            "none of the %d rules could run: every fact they "
                            "need is missing" % len(RULES),
                            "The fact dictionary is empty or unrecognisable, "
                            "so this is not a healthy box -- it is no box at "
                            "all.  Run %s on the machine itself, or pass a "
                            "file written by --facts.  `--all` lists every "
                            "rule and the fact it wanted." % PROG)]

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
# mesh, and `agree script ./why_slow.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
