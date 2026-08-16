#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""netmesh.py -- measure round-trip time, jitter, loss and path MTU between machines.

Usage: netmesh.py check HOST [HOST...]     one-shot: probe, report, clean up
       netmesh.py gen [--servers FILE]     write mesh.csv for repeatable runs
       netmesh.py start | status | stop | clean
       netmesh.py summarize [--grid DIR]   collect reports and diagnose
       netmesh.py run --for SECONDS        start, hold, summarize, stop
       netmesh.py paths [--worst N]        where does a sick pair's route diverge?
       netmesh.py doctor                   can this fleet run netmesh at all?
       netmesh.py selftest                 prove it works on this machine alone
       netmesh.py help                     every flag of every verb

Common options (each also has an environment variable):
  --mesh FILE          mesh file to use         (NETMESH_MESH, default mesh.csv)
  --user NAME          ssh user                 (NETMESH_USER, default none)
  --jobs N             hosts contacted at once  (NETMESH_JOBS, default 16)
  --remote-dir DIR     working dir on hosts     (NETMESH_REMOTE_DIR,
                                                 default /var/tmp/netmesh)
  --python PATH        remote interpreter       (NETMESH_PYTHON, default python3)
  --ssh CMD/--scp CMD  transports               (NETMESH_SSH, NETMESH_SCP)
  --reports DIR        where reports land       (NETMESH_REPORTS, default reports)
  --for SECONDS        how long to probe        (check/run, default 30)
  --pps N              probes per second/pair   (default 10)
  --size N             probe payload bytes      (default 64)
  --port N             agent UDP port           (default 5310)
  --include-self       run an agent here too, with no ssh
  --dry-run            print the ssh/scp commands instead of running them

What it measures
  Two questions get asked whenever something is slow: is it the network or
  the app, and if it is the network, which link is sick?  iperf_orchestrator
  answers "how much bandwidth under load" and mx answers "how many packets
  per second under load".  Neither says what the network does when it is
  *idle*, which is the baseline every other number needs.  This is that
  baseline, and it is deliberately cheap -- ten small packets a second per
  pair -- so it is safe to run on production during an incident.

  Probes are UDP echoes between temporary agents.  The sender's own
  monotonic clock is written into each packet and echoed back untouched,
  so the round-trip time is exact and no clock synchronisation is needed:
  only one clock is ever read.  One-way delay is deliberately NOT reported,
  because without PTP-grade sync the offset between two clocks swamps the
  microsecond differences that would make it interesting.  Instead both
  directions are measured as separate round trips -- RTT(A->B) timed by A,
  RTT(B->A) timed by B -- and the two are compared, which is what exposes
  asymmetric routing and one-sided queueing.

  Because the responder counts what actually arrived, loss is split into
  the forward and the return leg.  That is the difference between a sick
  sender and a sick receiver, and it cannot be had from ping.

  Endpoints you cannot deploy to -- a VIP, an appliance, a router -- can
  still be measured: prefix the host token with '~' and the agents reach
  it with ping instead.  Those rows are reported separately and marked
  probe=ping, because they carry no receiver-side truth, no loss split and
  no path-MTU confirmation.

Output format
  reports/<host>.csv, tidy long format, one row per peer per direction per
  interval:
    ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,
    rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,
    path_mtu,mtu_state,agent_cpu_pct,note
  dir is tx (this host probed peer), rx (this host answered peer) or host
  (this host's totals).  A blank cell means "not measured" and never zero,
  so averages skip it rather than being flattered by it.

  --grid DIR additionally writes N*N grids in the mesh's own shape:
  rtt_p50, rtt_p99, jitter, loss, mtu and asym.  A dark row is a host whose
  probes go out badly; a dark column is a host that receives badly.

Robustness properties
  * needs no root, no CAP_NET_RAW, no installed packages and no inbound
    port beyond ssh -- the agent is this file, copied by scp
  * leaves nothing behind: `clean` removes the directory, `check` does it
    automatically, and there was never a package, sysctl or unit file
  * a host that cannot be reached is reported, not fatal: the rest of the
    mesh still measures
  * RTT needs no clock sync; only the sender's clock is ever read
  * loss is split into forward and return legs by the receiver's own count
  * blank means not measured, never zero
  * every run is reproducible from mesh.csv alone
  * the agent reports its own CPU, so you can see when you are measuring
    the tool rather than the network
"""

import argparse
import csv
import errno
import io
import os
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.1.0"
PROG = os.path.basename(sys.argv[0]) or "netmesh.py"

# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------
#
# Fixed 32-byte header, then zero padding out to the requested size.
#   magic | kind | flags | src index | packet len | reply len | seq | t_send
# `src index` is the sender's row number in the mesh, so accounting never
# depends on names or on reverse DNS.  `t_send` is the sender's own
# monotonic clock in microseconds, echoed back untouched -- that is what
# makes the round-trip exact with no clock sync.
MAGIC = b"NMSH"
HDR = struct.Struct("!4sBBHIIQQ")
HDR_SIZE = HDR.size                 # 32
MAX_SIZE = 65507                    # largest UDP payload over IPv4
KIND_REQ, KIND_REP = 0, 1
FLAG_PMTU = 1                       # this probe is a path-MTU search step

DEFAULT_PORT = 5310
DEFAULT_MESH = "mesh.csv"
DEFAULT_SERVERS = "servers.txt"
DEFAULT_REMOTE_DIR = "/var/tmp/netmesh"
DEFAULT_PPS = 10.0
DEFAULT_SIZE = 64
DEFAULT_FOR = 30.0
DEFAULT_INTERVAL = 5.0
DEFAULT_MTU_CEILING = 9000
DEFAULT_PMTU_EVERY = 300.0
MIN_MTU_PAYLOAD = 508               # 576-byte IPv4 minimum, less headers
IP_HDR_UDP = 28                     # IPv4 20 + UDP 8

REPORT_NAME = "report.csv"
LOG_NAME = "agent.log"
PID_NAME = "agent.pid"
MESH_NAME = "mesh.csv"

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10"]

REPORT_FIELDS = [
    "ts", "host", "dir", "peer", "probe", "size", "target_pps",
    "sent", "recv", "loss_pct",
    "rtt_min_us", "rtt_avg_us", "rtt_p50_us", "rtt_p99_us", "rtt_max_us",
    "jitter_us", "path_mtu", "mtu_state", "agent_cpu_pct", "note",
]

CONFIG_KEYS = ("size", "port", "pps", "pmtu", "mtu_ceiling", "pmtu_every")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

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
    """Exit with a usage-style status (2), as argparse does -- so a bad mesh
    file is never mistaken for a run that merely found problems (1)."""
    sys.stderr.write("%s: %s\n" % (PROG, msg))
    sys.stderr.flush()
    raise SystemExit(code)


def log(msg):
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()


def fmt_us(us):
    if us is None or us == "":
        return "-"
    us = float(us)
    if us >= 1e6:
        return "%.2fs" % (us / 1e6)
    if us >= 1000:
        return "%.1fms" % (us / 1000.0)
    return "%.0fus" % us


def fmt_secs(s):
    """'90' -> '1m30s', '7500' -> '2h05m': durations for humans."""
    s = int(round(s))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def pct(part, whole):
    return (part / whole * 100.0) if whole else 0.0


def _env(name, default=None):
    v = os.environ.get("NETMESH_" + name)
    return v if v not in (None, "") else default


def _env_num(name, default, cast=float):
    v = _env(name)
    if v is None:
        return default
    try:
        return cast(v)
    except ValueError:
        die("NETMESH_%s is not a number: %r" % (name, v))


# ---------------------------------------------------------------------------
# RTT histogram
# ---------------------------------------------------------------------------
#
# Quarter-octave buckets: constant memory, no sorting, and about 20% wide,
# which is plenty to tell 200us from 8ms -- the question actually being
# asked here.  Exact min/mean/max are tracked separately, so the only
# approximation is in the percentiles.

RTT_OCTAVES = 32
RTT_NBUCKETS = RTT_OCTAVES * 4


def rtt_bucket(us):
    us = int(us)
    if us < 4:
        return max(0, us)
    octave = us.bit_length() - 1
    frac = (us >> (octave - 2)) & 3
    return min(RTT_NBUCKETS - 1, octave * 4 + frac)


def rtt_bucket_us(i):
    """Upper edge of bucket i, in microseconds."""
    octave, frac = divmod(i, 4)
    return (1 << octave) * (1.0 + (frac + 1) / 4.0)


def rtt_percentile(hist, q):
    total = sum(hist)
    if not total:
        return None
    want = total * q
    seen = 0
    for i, n in enumerate(hist):
        seen += n
        if seen >= want:
            return rtt_bucket_us(i)
    return rtt_bucket_us(len(hist) - 1)


try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
except (ValueError, OSError, AttributeError):
    _CLK_TCK = 100


def read_self_cpu():
    """This agent's own CPU time in seconds (user + system).

    One agent is one Python process, so this caps near 100% of a single
    core.  When it reads near 100 the tool is the bottleneck, not the
    network, and every other number on the page should be distrusted.
    """
    try:
        with io.open("/proc/self/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rsplit(") ", 1)[-1].split()
        return (int(fields[11]) + int(fields[12])) / float(_CLK_TCK)
    except (OSError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The mesh file
# ---------------------------------------------------------------------------

class Mesh(object):
    def __init__(self):
        self.hosts = []             # ordered host names
        self.addrs = {}
        self.ports = {}
        self.pingonly = set()
        self.rate = {}              # rate[src][dst] = probes/sec
        self.cfg = {}

    def index(self, host):
        return self.hosts.index(host)

    def endpoint(self, host):
        return self.addrs[host], self.ports[host]

    def peers_of(self, host):
        return [(d, r) for d, r in sorted(self.rate.get(host, {}).items())
                if r > 0]

    def pairs(self):
        for s in self.hosts:
            for d, r in self.peers_of(s):
                yield s, d, r


def parse_token(tok):
    """'[~]name[=addr[:port]]' -> (name, addr, port_or_None, ping_only).

    A bare address is its own name, so a plain list of IPs just works.
    A leading '~' marks an endpoint that cannot run an agent.
    """
    tok = tok.strip()
    ping_only = tok.startswith("~")
    if ping_only:
        tok = tok[1:].strip()
    bare = "=" not in tok
    if bare:
        name = addr = tok
    else:
        name, addr = tok.split("=", 1)
        name, addr = name.strip(), addr.strip()
    # Refuse IPv6 outright rather than mis-parsing it: the bracket form
    # would otherwise be split on its own colons into a nonsense address
    # and port, and the run would silently measure the wrong thing.
    if addr.startswith("[") or addr.count(":") > 1:
        die("IPv6 addresses are not supported yet: %r" % tok)
    port = None
    if ":" in addr:
        addr, p = addr.rsplit(":", 1)
        try:
            port = int(p)
        except ValueError:
            die("bad port in host token %r" % tok)
        if bare:
            name = addr
    if not name or not addr:
        die("bad host token %r (want [~]name[=addr[:port]])" % tok)
    return name, addr, port, ping_only


def fmt_token(mesh, host):
    tok = "%s=%s" % (host, mesh.addrs[host])
    if mesh.ports[host] != int(mesh.cfg.get("port", DEFAULT_PORT)):
        tok += ":%d" % mesh.ports[host]
    if host in mesh.pingonly:
        tok = "~" + tok
    return tok


def parse_rate(cell, where):
    """A mesh cell: '' or '0' means no probing, otherwise probes/sec."""
    cell = cell.strip().lower()
    if not cell:
        return 0.0
    try:
        v = float(cell)
    except ValueError:
        die("%s: %r is not a probe rate (a number, or empty)" % (where, cell))
    if v < 0:
        die("%s: negative probe rate %r" % (where, cell))
    return v


def load_mesh(path):
    """Read a mesh CSV plus the '# key=value' run config above it."""
    if not os.path.isfile(path):
        die("mesh not found: %s  (make one with: %s gen --servers %s)"
            % (path, PROG, DEFAULT_SERVERS))
    cfg = {}
    data_lines = []
    line_numbers = []
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            if line.startswith("#"):
                for part in line[1:].split():
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k in CONFIG_KEYS:
                            cfg[k] = v
                continue
            if line.strip():
                data_lines.append(line)
                # Errors quote the line number in the file the user is
                # editing, not the index of the row within the grid.
                line_numbers.append(lineno)
    if not data_lines:
        die("%s: no grid found (just comments?)" % path)

    rows = list(csv.reader(data_lines))
    header = rows[0]
    if len(header) < 2:
        die("%s: header row needs at least one host column" % path)

    mesh = Mesh()
    mesh.cfg = cfg
    default_port = int(cfg.get("port", DEFAULT_PORT))
    for tok in header[1:]:
        if not tok.strip():
            continue
        name, addr, port, ping_only = parse_token(tok)
        if name in mesh.addrs:
            die("%s: host %r appears twice in the header row" % (path, name))
        mesh.hosts.append(name)
        mesh.addrs[name] = addr
        mesh.ports[name] = port or default_port
        if ping_only:
            mesh.pingonly.add(name)
    if not mesh.hosts:
        die("%s: header row lists no hosts" % path)

    for host in mesh.hosts:
        mesh.rate[host] = {}

    seen = set()
    for ri, row in enumerate(rows[1:], start=1):
        rn = line_numbers[ri] if ri < len(line_numbers) else ri + 1
        if not row or not row[0].strip():
            continue
        name = parse_token(row[0])[0]
        if name not in mesh.rate:
            die("%s line %d: host %r is not in the header row"
                % (path, rn, name))
        if name in seen:
            die("%s line %d: host %r appears twice" % (path, rn, name))
        seen.add(name)
        for cn, cell in enumerate(row[1:]):
            if cn >= len(mesh.hosts):
                break
            dst = mesh.hosts[cn]
            where = "%s line %d column %d (%s -> %s)" % (path, rn, cn + 2,
                                                        name, dst)
            rate = parse_rate(cell, where)
            if rate <= 0:
                continue
            if dst == name:
                die("%s: a host cannot probe itself" % where)
            if name in mesh.pingonly:
                die("%s: %r is ping-only, so it cannot originate probes"
                    % (where, name))
            mesh.rate[name][dst] = rate

    if not any(mesh.rate[h] for h in mesh.hosts):
        die("%s: every cell is empty, so there is nothing to measure" % path)
    return mesh


def write_mesh(path, hosts, addrs, ports, pingonly, rate, cfg):
    cfg_bits = " ".join("%s=%s" % (k, cfg[k]) for k in CONFIG_KEYS if k in cfg)
    default_port = int(cfg.get("port", DEFAULT_PORT))

    def tok(h):
        t = "%s=%s" % (h, addrs[h])
        if ports.get(h, default_port) != default_port:
            t += ":%d" % ports[h]
        return ("~" + t) if h in pingonly else t

    tmp = path + ".tmp"
    with io.open(tmp, "w", newline="", encoding="utf-8") as f:
        f.write("# netmesh mesh v1 -- rows probe, columns answer, "
                "cells are probes/sec\n")
        f.write("# %s\n" % cfg_bits)
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["src\\dst"] + [tok(h) for h in hosts])
        for s in hosts:
            row = [tok(s)]
            for d in hosts:
                r = rate.get(s, {}).get(d, 0.0)
                row.append("" if r <= 0 else ("%g" % r))
            w.writerow(row)
    os.replace(tmp, path)


def read_servers(path):
    if not os.path.isfile(path):
        die("server list not found: %s" % path)
    out = []
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    if not out:
        die("%s lists no hosts" % path)
    return out


def build_mesh(tokens, pps, cfg):
    """Full mesh from a list of host tokens: everyone probes everyone."""
    hosts, addrs, ports, pingonly = [], {}, {}, set()
    default_port = int(cfg.get("port", DEFAULT_PORT))
    for tok in tokens:
        name, addr, port, po = parse_token(tok)
        if name in addrs:
            die("host %r listed twice" % name)
        hosts.append(name)
        addrs[name] = addr
        ports[name] = port or default_port
        if po:
            pingonly.add(name)
    if len(hosts) < 2:
        die("need at least two hosts to measure anything (got %d)" % len(hosts))
    if len(hosts) == len(pingonly):
        die("every host is ping-only, so no agent could run anywhere")
    rate = {}
    for s in hosts:
        rate[s] = {}
        if s in pingonly:
            continue            # a ping-only endpoint never originates
        for d in hosts:
            if d != s:
                rate[s][d] = pps
    return hosts, addrs, ports, pingonly, rate


# ---------------------------------------------------------------------------
# Local-host detection (so --include-self needs no ssh)
# ---------------------------------------------------------------------------

def local_addresses():
    addrs = {"127.0.0.1", "localhost", "::1"}
    try:
        addrs.add(socket.gethostname())
        addrs.add(socket.getfqdn())
    except OSError:
        pass
    for name in list(addrs):
        try:
            for info in socket.getaddrinfo(name, None, socket.AF_INET):
                addrs.add(info[4][0])
        except (OSError, socket.gaierror):
            pass
    return addrs


_LOCAL = None


def is_local(addr):
    global _LOCAL
    if _LOCAL is None:
        _LOCAL = local_addresses()
    if addr in _LOCAL:
        return True
    return addr.startswith("127.")


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class PeerState(object):
    """Everything one direction of one pair needs, in O(1) memory."""

    def __init__(self, name, idx, addr, port, rate, ping_only=False):
        self.name = name
        self.idx = idx
        self.addr = addr
        self.port = port
        self.rate = rate
        self.ping_only = ping_only
        self.reset()
        self.seq = 0
        self.next_send = 0.0
        # Path MTU search state.
        self.mtu_lo = MIN_MTU_PAYLOAD
        self.mtu_hi = None
        self.mtu_best = None
        self.mtu_state = ""
        self.mtu_next = 0.0
        self.mtu_inflight = None
        self.mtu_deadline = 0.0
        self.mtu_tries = 0
        self.mtu_saw_small = False

    def reset(self):
        """Clear the per-interval counters; the search state survives."""
        self.sent = 0
        self.recv = 0
        self.hist = [0] * RTT_NBUCKETS
        self.rtt_min = None
        self.rtt_max = None
        self.rtt_sum = 0.0
        self.jitter = 0.0
        self.last_rtt = None
        self.note = ""

    def record(self, rtt_us):
        self.recv += 1
        self.rtt_sum += rtt_us
        if self.rtt_min is None or rtt_us < self.rtt_min:
            self.rtt_min = rtt_us
        if self.rtt_max is None or rtt_us > self.rtt_max:
            self.rtt_max = rtt_us
        self.hist[rtt_bucket(rtt_us)] += 1
        # RFC 3550 smoothed jitter: the mean deviation between consecutive
        # transit times.  One float of state, no window to keep.
        if self.last_rtt is not None:
            d = abs(rtt_us - self.last_rtt)
            self.jitter += (d - self.jitter) / 16.0
        self.last_rtt = rtt_us


class RxState(object):
    def __init__(self, name):
        self.name = name
        self.reset()

    def reset(self):
        self.recv = 0
        self.sent = 0


def _set_df(sock):
    """Ask the kernel for don't-fragment + path-MTU discovery.

    These constants are not in the socket module on every Python, and the
    call fails on non-Linux, so the caller treats failure as "PMTU is
    unsupported here" rather than as an error.
    """
    IP_MTU_DISCOVER, IP_PMTUDISC_DO = 10, 2
    sock.setsockopt(socket.IPPROTO_IP, IP_MTU_DISCOVER, IP_PMTUDISC_DO)


def _get_kernel_mtu(sock):
    IP_MTU = 14
    try:
        return sock.getsockopt(socket.IPPROTO_IP, IP_MTU)
    except OSError:
        return None


class Agent(object):
    def __init__(self, mesh, me, outdir, interval, duration, bind=""):
        self.mesh = mesh
        self.me = me
        self.outdir = outdir
        self.interval = interval
        self.duration = duration
        self.bind = bind
        self.size = int(mesh.cfg.get("size", DEFAULT_SIZE))
        self.port = mesh.ports[me]
        self.do_pmtu = str(mesh.cfg.get("pmtu", "1")) not in ("0", "no", "false")
        self.mtu_ceiling = int(mesh.cfg.get("mtu_ceiling", DEFAULT_MTU_CEILING))
        self.pmtu_every = float(mesh.cfg.get("pmtu_every", DEFAULT_PMTU_EVERY))
        self.my_idx = mesh.index(me)

        self.peers = {}
        self.ping_peers = {}
        for dst, rate in mesh.peers_of(me):
            addr, port = mesh.endpoint(dst)
            st = PeerState(dst, mesh.index(dst), addr, port, rate,
                           dst in mesh.pingonly)
            if st.ping_only:
                self.ping_peers[dst] = st
            else:
                self.peers[dst] = st
        self.rx = {}
        self.by_idx = {}
        for h in mesh.hosts:
            self.by_idx[mesh.index(h)] = h

        self.sock = None
        self.mtu_sock = None
        self.pmtu_supported = True
        self.ping_procs = {}
        self.cpu_prev = read_self_cpu()
        self.cpu_wall = time.time()

    # -- setup ------------------------------------------------------------
    def open_sockets(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.bind, self.port))
        except OSError as exc:
            die("cannot bind %s:%d -- %s" % (self.bind or "0.0.0.0",
                                             self.port, exc))
        self.sock.setblocking(False)

        if self.do_pmtu and self.peers:
            self.mtu_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                _set_df(self.mtu_sock)
                self.mtu_sock.bind((self.bind, 0))
                self.mtu_sock.setblocking(False)
            except OSError:
                self.pmtu_supported = False
                self.mtu_sock.close()
                self.mtu_sock = None
                for st in self.peers.values():
                    st.mtu_state = "unsupported"

    # -- packet plumbing --------------------------------------------------
    def _packet(self, kind, peer_idx, seq, t_send, size, flags=0, rlen=None):
        size = max(HDR_SIZE, min(size, MAX_SIZE))
        rlen = size if rlen is None else rlen
        head = HDR.pack(MAGIC, kind, flags, peer_idx, size, rlen, seq, t_send)
        return head + b"\0" * (size - HDR_SIZE)

    def _now_us(self):
        return int(time.monotonic() * 1e6)

    def send_probe(self, st, now):
        pkt = self._packet(KIND_REQ, self.my_idx, st.seq, self._now_us(),
                           self.size)
        try:
            self.sock.sendto(pkt, (st.addr, st.port))
            st.sent += 1
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.ENOBUFS, errno.EWOULDBLOCK):
                st.note = "send: %s" % exc.strerror
        st.seq += 1

    def handle_packet(self, data, src, on_mtu_sock=False):
        if len(data) < HDR_SIZE:
            return
        magic, kind, flags, idx, plen, rlen, seq, t_send = HDR.unpack(
            data[:HDR_SIZE])
        if magic != MAGIC:
            return
        if kind == KIND_REQ:
            # Echo it back to wherever it came from, keeping t_send intact.
            #
            # Path-MTU probes are echoed but never counted: they are not part
            # of the paced probe stream, and counting them here while the
            # sender counts them separately makes the receiver look like it
            # got more than was sent -- which surfaces as negative forward
            # loss, a number that cannot be true.
            peer = self.by_idx.get(idx)
            counted = peer is not None and not (flags & FLAG_PMTU)
            if counted:
                st = self.rx.get(peer)
                if st is None:
                    st = self.rx[peer] = RxState(peer)
                st.recv += 1
            reply = self._packet(KIND_REP, idx, seq, t_send, rlen, flags)
            try:
                self.sock.sendto(reply, src)
                if counted:
                    self.rx[peer].sent += 1
            except OSError:
                pass
            return

        # A reply to us.
        peer = self.by_idx.get(idx if idx != self.my_idx else self.my_idx)
        rtt = self._now_us() - t_send
        if rtt < 0:
            return
        if flags & FLAG_PMTU:
            self._pmtu_reply(src, seq, plen)
            return
        # Attribute by source address, which is authoritative for a reply.
        for st in self.peers.values():
            if st.addr == src[0] and st.port == src[1]:
                st.record(rtt)
                return

    # -- path MTU ---------------------------------------------------------
    def _pmtu_start(self, st, now):
        st.mtu_lo = MIN_MTU_PAYLOAD
        st.mtu_hi = self.mtu_ceiling - IP_HDR_UDP
        st.mtu_best = None
        st.mtu_saw_small = False
        st.mtu_tries = 0
        self._pmtu_step(st, now)

    def _pmtu_step(self, st, now):
        if st.mtu_hi is None or st.mtu_lo > st.mtu_hi:
            self._pmtu_finish(st, now)
            return
        mid = (st.mtu_lo + st.mtu_hi) // 2
        st.mtu_inflight = mid
        st.mtu_tries = 0
        self._pmtu_send(st, mid, now)

    def _pmtu_send(self, st, size, now):
        pkt = self._packet(KIND_REQ, self.my_idx, size, self._now_us(),
                           size, flags=FLAG_PMTU)
        try:
            self.mtu_sock.sendto(pkt, (st.addr, st.port))
            st.mtu_deadline = now + 1.0
        except OSError as exc:
            if exc.errno == errno.EMSGSIZE:
                # The kernel already knows a smaller PMTU: this size is out.
                st.mtu_hi = size - 1
                st.mtu_inflight = None
                self._pmtu_step(st, now)
            else:
                st.mtu_state = "unsupported"
                st.mtu_inflight = None
        return

    def _pmtu_reply(self, src, seq, plen):
        for st in self.peers.values():
            if st.addr == src[0] and st.mtu_inflight == seq:
                st.mtu_best = seq
                if seq <= 1500 - IP_HDR_UDP:
                    st.mtu_saw_small = True
                st.mtu_lo = seq + 1
                st.mtu_inflight = None
                self._pmtu_step(st, time.monotonic())
                return

    def _pmtu_timeout(self, st, now):
        st.mtu_tries += 1
        if st.mtu_tries < 3:
            self._pmtu_send(st, st.mtu_inflight, now)
            return
        # Three attempts, no echo, and no EMSGSIZE: something is eating
        # this size silently.  That is the black hole worth finding.
        size = st.mtu_inflight
        st.mtu_hi = size - 1
        st.mtu_inflight = None
        if st.mtu_saw_small or st.mtu_best is not None:
            st.mtu_state = "blackhole-pending"
        self._pmtu_step(st, now)

    def _pmtu_finish(self, st, now):
        st.mtu_inflight = None
        st.mtu_next = now + self.pmtu_every if self.pmtu_every > 0 else 1e18
        if st.mtu_best is None:
            st.mtu_state = st.mtu_state or "unsupported"
            return
        kernel = _get_kernel_mtu(self.mtu_sock)
        confirmed = st.mtu_best + IP_HDR_UDP
        if st.mtu_state == "blackhole-pending" and confirmed < self.mtu_ceiling:
            st.mtu_state = "blackhole"
        else:
            st.mtu_state = "confirmed"
        st.mtu_value = confirmed
        if kernel and kernel < confirmed:
            st.mtu_state = "cached"

    # -- ping-only endpoints ----------------------------------------------
    def _ping_start(self, st, seconds):
        count = max(1, int(st.rate * seconds))
        interval = 1.0 / st.rate if st.rate > 0 else 1.0
        cmd = ["ping", "-n", "-q", "-c", str(count), "-W", "1"]
        if interval < 1.0:
            cmd += ["-i", "%g" % interval]
        cmd.append(st.addr)
        try:
            self.ping_procs[st.name] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, universal_newlines=True,
                encoding="utf-8", errors="replace")
            st.sent = count
        except OSError:
            st.note = "no-ping-binary"
            self.ping_procs.pop(st.name, None)

    def _ping_collect(self, st):
        p = self.ping_procs.pop(st.name, None)
        if p is None:
            if not st.note:
                st.note = "no-ping-binary"
            return
        try:
            out = p.communicate(timeout=5)[0] or ""
        except subprocess.TimeoutExpired:
            p.kill()
            out = p.communicate()[0] or ""
        m = re.search(r"(\d+) packets transmitted,\s*(\d+)\s*(?:packets\s*)?received",
                      out)
        if m:
            st.sent = int(m.group(1))
            st.recv = int(m.group(2))
        m = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?\s*ms", out)
        if m:
            st.rtt_min = float(m.group(1)) * 1000.0
            avg = float(m.group(2)) * 1000.0
            st.rtt_max = float(m.group(3)) * 1000.0
            st.rtt_sum = avg * max(1, st.recv)
            if m.group(4):
                st.jitter = float(m.group(4)) * 1000.0
        elif not st.note:
            st.note = "ping-no-summary"

    # -- reporting --------------------------------------------------------
    def _cpu_pct(self):
        now_cpu, now_wall = read_self_cpu(), time.time()
        out = ""
        if now_cpu is not None and self.cpu_prev is not None:
            dt = now_wall - self.cpu_wall
            if dt > 0:
                out = "%.1f" % ((now_cpu - self.cpu_prev) / dt * 100.0)
        self.cpu_prev, self.cpu_wall = now_cpu, now_wall
        return out

    def _tx_row(self, ts, st, probe):
        avg = (st.rtt_sum / st.recv) if st.recv else None
        row = {
            "ts": ts, "host": self.me, "dir": "tx", "peer": st.name,
            "probe": probe, "size": self.size if probe == "udp" else "",
            "target_pps": "%g" % st.rate,
            "sent": st.sent, "recv": st.recv,
            "loss_pct": ("%.3f" % pct(st.sent - st.recv, st.sent)) if st.sent else "",
            "rtt_min_us": "" if st.rtt_min is None else "%.0f" % st.rtt_min,
            "rtt_avg_us": "" if avg is None else "%.0f" % avg,
            "rtt_max_us": "" if st.rtt_max is None else "%.0f" % st.rtt_max,
            "jitter_us": "%.0f" % st.jitter if st.recv else "",
            "note": st.note,
        }
        if probe == "udp":
            row["rtt_p50_us"] = _fmt_opt(rtt_percentile(st.hist, 0.50))
            row["rtt_p99_us"] = _fmt_opt(rtt_percentile(st.hist, 0.99))
        else:
            # ping gives min/avg/max/mdev only; claiming percentiles from
            # that would be inventing data.
            row["rtt_p50_us"] = ""
            row["rtt_p99_us"] = ""
        if getattr(st, "mtu_value", None) and st.mtu_state:
            row["path_mtu"] = st.mtu_value
            row["mtu_state"] = st.mtu_state
        elif st.mtu_state in ("unsupported",):
            row["mtu_state"] = st.mtu_state
        return row

    def write_interval(self, writer, ts):
        cpu = self._cpu_pct()
        for st in sorted(self.peers.values(), key=lambda s: s.name):
            writer.writerow(self._tx_row(ts, st, "udp"))
        for st in sorted(self.ping_peers.values(), key=lambda s: s.name):
            writer.writerow(self._tx_row(ts, st, "ping"))
        for name in sorted(self.rx):
            st = self.rx[name]
            writer.writerow({
                "ts": ts, "host": self.me, "dir": "rx", "peer": name,
                "probe": "udp", "sent": st.sent, "recv": st.recv,
            })
        writer.writerow({
            "ts": ts, "host": self.me, "dir": "host", "peer": "*",
            "probe": "udp", "agent_cpu_pct": cpu,
        })

    # -- the loop ---------------------------------------------------------
    def run(self):
        # `stop` sends SIGTERM, and the documented promise is that the
        # reports survive it.  Dying where we stand would throw away
        # everything measured since the last interval, so the signal just
        # sets a flag and the loop exits through its normal final write.
        self.stopping = False

        def _stop(_signum, _frame):
            self.stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

        self.open_sockets()
        os.makedirs(self.outdir, exist_ok=True)
        report_path = os.path.join(self.outdir, REPORT_NAME)
        new = not os.path.exists(report_path)
        started = time.monotonic()
        end = started + self.duration if self.duration else None

        with io.open(report_path, "a", newline="", encoding="utf-8") as fh:
            # lineterminator is explicit: csv defaults to CRLF, which would
            # put a stray \r on every field the shell tools read last.
            writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS,
                                    extrasaction="ignore", restval="",
                                    lineterminator="\n")
            if new:
                writer.writeheader()
                fh.flush()

            now = time.monotonic()
            for st in self.peers.values():
                st.next_send = now
                if self.do_pmtu and self.pmtu_supported:
                    self._pmtu_start(st, now)
            for st in self.ping_peers.values():
                self._ping_start(st, self.interval)

            next_report = now + self.interval
            socks = [self.sock] + ([self.mtu_sock] if self.mtu_sock else [])

            while not self.stopping:
                now = time.monotonic()
                if end is not None and now >= end:
                    break

                # Next thing that wants attention.
                wake = next_report
                for st in self.peers.values():
                    if st.rate > 0:
                        wake = min(wake, st.next_send)
                    if st.mtu_inflight is not None:
                        wake = min(wake, st.mtu_deadline)
                    elif self.do_pmtu and self.pmtu_supported and st.mtu_next:
                        wake = min(wake, st.mtu_next)
                timeout = max(0.0, min(wake - now, 1.0))

                try:
                    ready = select.select(socks, [], [], timeout)[0]
                except (OSError, select.error):
                    ready = []
                for s in ready:
                    on_mtu = (s is self.mtu_sock)
                    while True:
                        try:
                            data, src = s.recvfrom(MAX_SIZE)
                        except (BlockingIOError, socket.error):
                            break
                        self.handle_packet(data, src, on_mtu)

                now = time.monotonic()
                for st in self.peers.values():
                    if st.rate <= 0:
                        continue
                    step = 1.0 / st.rate
                    # Catch up at most a few probes, so a stalled loop does
                    # not fire a burst that the numbers would then blame on
                    # the network.
                    missed = 0
                    while st.next_send <= now and missed < 4:
                        self.send_probe(st, now)
                        st.next_send += step
                        missed += 1
                    if st.next_send <= now:
                        st.next_send = now + step

                    if self.do_pmtu and self.pmtu_supported:
                        if st.mtu_inflight is not None and now >= st.mtu_deadline:
                            self._pmtu_timeout(st, now)
                        elif st.mtu_inflight is None and st.mtu_next and \
                                now >= st.mtu_next and st.mtu_state:
                            self._pmtu_start(st, now)

                if now >= next_report:
                    for st in self.ping_peers.values():
                        self._ping_collect(st)
                    self.write_interval(writer, int(time.time()))
                    fh.flush()
                    for st in self.peers.values():
                        st.reset()
                    for st in self.rx.values():
                        st.reset()
                    for st in self.ping_peers.values():
                        st.reset()
                        self._ping_start(st, self.interval)
                    next_report += self.interval
                    if next_report <= now:
                        next_report = now + self.interval

            # Drain briefly before the final report.  Probes sent in the last
            # few milliseconds have replies still in flight, and counting
            # them as lost would put 2% loss on a loopback run -- an
            # artefact of stopping, reported as if it were the network.
            drain_until = time.monotonic() + 0.3
            while time.monotonic() < drain_until:
                try:
                    ready = select.select(socks, [], [], 0.05)[0]
                except (OSError, select.error):
                    break
                for s in ready:
                    while True:
                        try:
                            data, src = s.recvfrom(MAX_SIZE)
                        except (BlockingIOError, socket.error):
                            break
                        self.handle_packet(data, src, s is self.mtu_sock)

            # Final partial interval: report it rather than lose it.
            for st in self.ping_peers.values():
                self._ping_collect(st)
            self.write_interval(writer, int(time.time()))
            fh.flush()
        return 0


def _fmt_opt(v):
    return "" if v is None else "%.0f" % v


# ---------------------------------------------------------------------------
# The fleet
# ---------------------------------------------------------------------------

class Fleet(object):
    def __init__(self, mesh, args):
        self.mesh = mesh
        self.user = args.user
        self.jobs = max(1, args.jobs)
        self.dir = args.remote_dir
        self.python = args.python
        self.ssh = (args.ssh or "ssh").split()
        self.scp = (args.scp or "scp").split()
        self.dry_run = getattr(args, "dry_run", False)
        self.include_self = getattr(args, "include_self", False)
        self.local_procs = {}

    def agent_hosts(self):
        """Hosts that actually run an agent -- ping-only endpoints do not."""
        return [h for h in self.mesh.hosts if h not in self.mesh.pingonly]

    def is_local_host(self, host):
        return self.include_self and is_local(self.mesh.addrs[host])

    def target(self, host):
        addr = self.mesh.addrs[host]
        return "%s@%s" % (self.user, addr) if self.user else addr

    def dir_for(self, host):
        """Working directory for one host.

        Remote hosts each get their own machine, so one directory is
        enough.  Local hosts under --include-self might be several names
        for this same machine, and they would then share a report file and
        a pid file -- so give each its own subdirectory.
        """
        if self.is_local_host(host):
            return os.path.join(self.dir, host)
        return self.dir

    def rpath(self, host, name):
        return "%s/%s" % (self.dir_for(host), name)

    def sh(self, host, script, timeout=120):
        if self.is_local_host(host):
            return self._run(["sh", "-c", script], timeout)
        cmd = self.ssh + SSH_OPTS + [self.target(host), script]
        return self._run(cmd, timeout)

    def push(self, host, local_paths, remote_name=None, timeout=300):
        if self.is_local_host(host):
            if self.dry_run:
                return 0, "DRY-RUN copy %s" % " ".join(local_paths)
            try:
                d = self.dir_for(host)
                os.makedirs(d, exist_ok=True)
                for p in local_paths:
                    dest = os.path.join(d, remote_name or os.path.basename(p))
                    if os.path.abspath(p) != os.path.abspath(dest):
                        shutil.copy(p, dest)
                return 0, ""
            except OSError as exc:
                return 1, str(exc)
        dest = "%s:%s" % (self.target(host), self.rpath(host, remote_name or ""))
        cmd = self.scp + SSH_OPTS + ["-q"] + list(local_paths) + [dest]
        return self._run(cmd, timeout)

    def pull(self, host, remote_name, local_path, timeout=300):
        if self.is_local_host(host):
            if self.dry_run:
                return 0, "DRY-RUN fetch %s" % remote_name
            src = os.path.join(self.dir_for(host), remote_name)
            if not os.path.exists(src):
                return 1, "%s: not found" % src
            try:
                shutil.copy(src, local_path)
                return 0, ""
            except OSError as exc:
                return 1, str(exc)
        src = "%s:%s" % (self.target(host), self.rpath(host, remote_name))
        cmd = self.scp + SSH_OPTS + ["-q", src, local_path]
        return self._run(cmd, timeout)

    def _run(self, cmd, timeout):
        if self.dry_run:
            return 0, "DRY-RUN %s" % " ".join(shlex.quote(c) for c in cmd)
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 universal_newlines=True,
                                 encoding="utf-8", errors="replace")
            out, _ = p.communicate(timeout=timeout)
            return p.returncode, (out or "").strip()
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            return 124, "timed out after %ss" % timeout
        except OSError as exc:
            return 127, str(exc)

    def each(self, fn, label=None, quiet=False, hosts=None):
        """Run fn(host) on every agent host, at most --jobs at a time.

        Output is printed host-prefixed in mesh order rather than
        completion order, so two runs of the same command diff cleanly.
        """
        hosts = hosts if hosts is not None else self.agent_hosts()
        if not hosts:
            return 0
        if label:
            log("[%s] %s on %d hosts" % (PROG, label, len(hosts)))
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            results = list(pool.map(fn, hosts))
        width = max(len(h) for h in hosts)
        failed = []
        for host, (rc, text) in zip(hosts, results):
            if rc != 0:
                failed.append(host)
            if quiet and rc == 0:
                continue
            for line in (text or "").splitlines() or [""]:
                log("  %-*s  %s" % (width, host, line))
        if failed:
            log("[%s] FAILED on %d/%d hosts: %s"
                % (PROG, len(failed), len(hosts), " ".join(failed)))
        return len(failed)


def _agent_source():
    """This file, resolved through any symlink -- it is what gets copied to
    the hosts, so the fleet always runs exactly this version."""
    return os.path.realpath(os.path.abspath(__file__))


# The pgrep pattern is bracketed so the very shell running it is not itself
# a match: that shell's command line contains the literal "[n]etmesh.py
# agent", which the regex "netmesh.py agent" does not match.
PGREP = "pgrep -f '[n]etmesh[.]py agent'"
PKILL = "pkill -%s -f '[n]etmesh[.]py agent'"


def _kill_block(rdir):
    """Shell that stops the agent and leaves $status set.  Falls through in
    every case -- `clean` appends the removal to it, so it must never exit
    early."""
    return """
d=%(d)s
status=not-deployed
if [ -d "$d" ]; then
    status=not-running
    pid=""
    [ -f "$d/%(pid)s" ] && pid=$(cat "$d/%(pid)s" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null
        status=stopped
    elif %(pgrep)s >/dev/null 2>&1; then
        %(term)s 2>/dev/null
        status=stopped
    fi
    if [ "$status" = stopped ]; then
        i=0
        while [ $i -lt 20 ]; do
            %(pgrep)s >/dev/null 2>&1 || break
            sleep 0.5
            i=$((i+1))
        done
        %(kill)s >/dev/null 2>&1
    fi
    rm -f "$d/%(pid)s"
fi
""" % {"d": shlex.quote(rdir), "pid": PID_NAME, "pgrep": PGREP,
       "term": PKILL % "TERM", "kill": PKILL % "KILL"}


# ---------------------------------------------------------------------------
# Verbs: gen
# ---------------------------------------------------------------------------

def cmd_gen(args):
    tokens = list(args.hosts or [])
    if args.servers:
        tokens += read_servers(args.servers)
    if not tokens and os.path.isfile(DEFAULT_SERVERS):
        tokens += read_servers(DEFAULT_SERVERS)
    if not tokens:
        die("no hosts given (use: %s gen HOST HOST... or --servers FILE)" % PROG)
    cfg = {"size": args.size, "port": args.port, "pps": "%g" % args.pps,
           "pmtu": "0" if args.no_pmtu else "1",
           "mtu_ceiling": args.mtu_ceiling,
           "pmtu_every": "%g" % args.pmtu_every}
    hosts, addrs, ports, pingonly, rate = build_mesh(tokens, args.pps, cfg)
    write_mesh(args.mesh, hosts, addrs, ports, pingonly, rate, cfg)
    npairs = sum(len(v) for v in rate.values())
    log("[%s] wrote %s: %d hosts, %d ordered pairs, %g probes/s each"
        % (PROG, args.mesh, len(hosts), npairs, args.pps))
    if pingonly:
        log("[%s] ping-only endpoints (no agent): %s"
            % (PROG, " ".join(sorted(pingonly))))
    return 0


# ---------------------------------------------------------------------------
# Verbs: start / status / stop / clean / collect / logs
# ---------------------------------------------------------------------------

def _agent_flags(args, mesh):
    flags = ["--interval", "%g" % args.interval]
    if getattr(args, "duration", None):
        flags += ["--duration", "%g" % args.duration]
    if getattr(args, "bind", None):
        flags += ["--bind", args.bind]
    return flags


def cmd_start(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)
    src = _agent_source()
    flags = " ".join(shlex.quote(f) for f in _agent_flags(args, mesh))

    def deploy(host):
        rc, out = fleet.sh(host, "mkdir -p %s" % shlex.quote(fleet.dir_for(host)))
        if rc != 0:
            return rc, "mkdir failed: %s" % out
        rc, out = fleet.push(host, [src], os.path.basename(src))
        if rc != 0:
            return rc, "copy agent failed: %s" % out
        rc, out = fleet.push(host, [args.mesh], MESH_NAME)
        if rc != 0:
            return rc, "copy mesh failed: %s" % out
        return 0, "deployed"

    failed = fleet.each(deploy, label="deploy", quiet=True)
    if failed and not args.keep_going:
        die("deploy failed on %d hosts; nothing started" % failed, 1)

    def start(host):
        # Refuse to stack a second agent: two of them on one box would
        # interleave rows in the same report and the numbers would be junk.
        d = fleet.dir_for(host)
        script = """
d=%(d)s
if [ -f "$d/%(pid)s" ] && kill -0 "$(cat "$d/%(pid)s" 2>/dev/null)" 2>/dev/null; then
    echo already-running; exit 3
fi
cd "$d" || exit 1
rm -f %(rep)s
nohup %(py)s %(agent)s agent --mesh %(mesh)s --host %(host)s --dir . %(flags)s \
    >> %(logf)s 2>&1 &
echo $! > %(pid)s
sleep 0.3
kill -0 "$(cat %(pid)s)" 2>/dev/null || { echo failed-to-start; tail -3 %(logf)s; exit 1; }
echo started
""" % {"d": shlex.quote(d), "pid": PID_NAME, "rep": REPORT_NAME,
            "py": shlex.quote(fleet.python),
            # The script has already cd'd into $d, so the agent and the mesh
            # are named relative to it.  Joining $d back on would work for an
            # absolute --remote-dir and double the path for a relative one.
            "agent": shlex.quote(os.path.basename(src)),
            "mesh": MESH_NAME, "host": shlex.quote(host), "flags": flags,
            "logf": LOG_NAME}
        return fleet.sh(host, script)

    failed = fleet.each(start, label="start", quiet=True)
    running = len(fleet.agent_hosts()) - failed
    log("[%s] %d/%d agents running" % (PROG, running, len(fleet.agent_hosts())))
    return 1 if failed else 0


def cmd_status(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)

    def status(host):
        script = """
d=%(d)s
if [ ! -d "$d" ]; then echo NOT-DEPLOYED; exit 0; fi
pid=""
[ -f "$d/%(pid)s" ] && pid=$(cat "$d/%(pid)s" 2>/dev/null)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    printf 'RUNNING pid=%%s ' "$pid"
    tail -1 "$d/%(logf)s" 2>/dev/null || echo
else
    echo NOT-RUNNING
fi
""" % {"d": shlex.quote(fleet.dir_for(host)), "pid": PID_NAME,
            "logf": LOG_NAME}
        return fleet.sh(host, script)

    fleet.each(status)
    return 0


def cmd_stop(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)

    def stop(host):
        return fleet.sh(host, _kill_block(fleet.dir_for(host))
                        + '\necho "$status"\n')

    fleet.each(stop, label="stop")
    return 0


def cmd_clean(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)

    def clean(host):
        script = _kill_block(fleet.dir_for(host)) + """
rm -rf %s
echo "$status cleaned"
""" % shlex.quote(fleet.dir_for(host))
        return fleet.sh(host, script)

    fleet.each(clean, label="clean", quiet=args.quiet)
    return 0


def cmd_collect(args, mesh=None, fleet=None, quiet=False):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)
    outdir = args.reports
    os.makedirs(outdir, exist_ok=True)

    def fetch(host):
        dest = os.path.join(outdir, "%s.csv" % host)
        rc, out = fleet.pull(host, REPORT_NAME, dest)
        if rc != 0:
            return rc, "no report: %s" % out
        try:
            n = sum(1 for _ in io.open(dest, encoding="utf-8", errors="replace")) - 1
        except OSError:
            n = 0
        return 0, "%d rows" % max(0, n)

    failed = fleet.each(fetch, label=None if quiet else "collect", quiet=True)
    return failed


def cmd_logs(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)

    def get(host):
        return fleet.sh(host, "tail -%d %s 2>/dev/null || echo no-log"
                        % (args.lines,
                           shlex.quote(fleet.rpath(host, LOG_NAME))))

    fleet.each(get)
    return 0


# ---------------------------------------------------------------------------
# Aggregation and summarize
# ---------------------------------------------------------------------------

class Agg(object):
    """Sums that skip blanks.

    A blank means "not measured".  Counting it as zero would flatter every
    average on the page, which is the one thing a baseline tool must not do.
    """

    def __init__(self):
        self.n = 0
        self.total = 0.0
        self.min = None
        self.max = None
        self.values = []

    def add(self, v, keep=False):
        if v is None or v == "":
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        self.n += 1
        self.total += v
        if self.min is None or v < self.min:
            self.min = v
        if self.max is None or v > self.max:
            self.max = v
        if keep:
            self.values.append(v)

    @property
    def mean(self):
        return (self.total / self.n) if self.n else None

    def median(self):
        if not self.values:
            return None
        vs = sorted(self.values)
        m = len(vs) // 2
        if len(vs) % 2:
            return vs[m]
        return (vs[m - 1] + vs[m]) / 2.0


class PairStat(object):
    def __init__(self, src, dst):
        self.src, self.dst = src, dst
        self.probe = "udp"
        self.sent = 0
        self.recv = 0
        self.rx_recv = None       # what the far end says it saw
        self.p50 = Agg()
        self.p99 = Agg()
        self.avg = Agg()
        self.mx = Agg()
        self.jitter = Agg()
        self.path_mtu = None
        self.mtu_state = ""
        self.notes = set()

    @property
    def loss_pct(self):
        return pct(self.sent - self.recv, self.sent) if self.sent else None

    # The two ends flush their intervals independently, so a report
    # collected mid-run can hold one more interval of rx than of tx.  That
    # skew can make a leg look very slightly negative; loss cannot be
    # negative, so clamp it rather than print a number that cannot be true.
    @property
    def fwd_loss_pct(self):
        if self.rx_recv is None or not self.sent:
            return None
        return max(0.0, pct(self.sent - self.rx_recv, self.sent))

    @property
    def ret_loss_pct(self):
        if self.rx_recv is None or not self.rx_recv:
            return None
        return max(0.0, pct(self.rx_recv - self.recv, self.rx_recv))

    def rank(self):
        """Worst-first ordering: p99 if we have it, else mean, else loss."""
        v = self.p99.mean if self.p99.n else (self.avg.mean or 0.0)
        return (v or 0.0) + (self.loss_pct or 0.0) * 1e6


def read_reports(reports_dir):
    """All rows from reports/*.csv, oldest file first for stable ordering."""
    if not os.path.isdir(reports_dir):
        die("no reports directory: %s  (run `%s summarize` after `start`, "
            "or pass --reports)" % (reports_dir, PROG))
    rows = []
    for name in sorted(os.listdir(reports_dir)):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(reports_dir, name)
        try:
            with io.open(path, newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    rows.append(row)
        except OSError as exc:
            log("[%s] cannot read %s: %s" % (PROG, path, exc))
    return rows


def build_pairs(rows):
    pairs = {}
    hosts = set()
    cpu = Agg()
    for r in rows:
        host, peer, d = r.get("host"), r.get("peer"), r.get("dir")
        if not host:
            continue
        hosts.add(host)
        if d == "host":
            cpu.add(r.get("agent_cpu_pct"))
            continue
        if not peer or peer == "*":
            continue
        hosts.add(peer)
        if d == "tx":
            ps = pairs.setdefault((host, peer), PairStat(host, peer))
            ps.probe = r.get("probe") or ps.probe
            ps.sent += _int(r.get("sent"))
            ps.recv += _int(r.get("recv"))
            ps.p50.add(r.get("rtt_p50_us"), keep=True)
            ps.p99.add(r.get("rtt_p99_us"), keep=True)
            ps.avg.add(r.get("rtt_avg_us"), keep=True)
            ps.mx.add(r.get("rtt_max_us"))
            ps.jitter.add(r.get("jitter_us"), keep=True)
            if r.get("mtu_state"):
                ps.mtu_state = r["mtu_state"]
                if r.get("path_mtu"):
                    ps.path_mtu = _int(r["path_mtu"])
            if r.get("note"):
                ps.notes.add(r["note"])
        elif d == "rx":
            # The receiver's own count of what arrived, which is what makes
            # the forward/return loss split possible.
            ps = pairs.setdefault((peer, host), PairStat(peer, host))
            ps.rx_recv = (ps.rx_recv or 0) + _int(r.get("recv"))
    return pairs, sorted(hosts), cpu


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _pair_p50(ps):
    return ps.p50.mean if ps.p50.n else ps.avg.mean


def diagnose(pairs, bad, hosts):
    """Three cheap rules.  Nothing cleverer than this in v1 -- a wrong
    confident diagnosis is worse than none."""
    if not bad:
        return []
    out = []
    srcs = set(p.src for p in bad)
    dsts = set(p.dst for p in bad)
    healthy = [p for p in pairs.values() if p not in bad]

    if len(srcs) == 1:
        s = srcs.pop()
        inbound_ok = any(p.dst == s for p in healthy)
        line = ("Every slow pair starts at %s, so this is %s's egress or its "
                "first hop, not the fabric." % (s, s))
        if inbound_ok:
            line += "  Traffic *into* %s is fine, which narrows it further." % s
        out.append(line)
    elif len(dsts) == 1:
        d = dsts.pop()
        out.append("Every slow pair ends at %s, so this is %s's ingress -- "
                   "check its NIC, its receive queues and its CPU." % (d, d))
    else:
        subnets = set()
        for p in bad:
            subnets.add((_subnet(p.src, hosts), _subnet(p.dst, hosts)))
        if len(subnets) == 1 and len(set(subnets.pop())) == 2:
            out.append("The slow pairs all cross between two groups of hosts, "
                       "so the path between them is the suspect, not any one "
                       "machine.")
        else:
            out.append("The slow pairs do not share a single source or "
                       "destination, so this looks like the fabric rather "
                       "than one host.")
    return out


def _subnet(host, hosts):
    return host


def cmd_summarize(args, mesh=None, fleet=None):
    mesh = mesh or (load_mesh(args.mesh) if os.path.isfile(args.mesh) else None)
    if not args.no_collect and mesh is not None:
        fleet = fleet or Fleet(mesh, args)
        cmd_collect(args, mesh, fleet, quiet=True)

    rows = read_reports(args.reports)
    if not rows:
        die("no rows in %s -- did the agents run?" % args.reports, 1)
    pairs, hosts, cpu = build_pairs(rows)
    if not pairs:
        die("reports contain no probe rows", 1)

    udp = [p for p in pairs.values() if p.probe != "ping" and p.sent]
    pings = [p for p in pairs.values() if p.probe == "ping" and p.sent]
    measured = udp + pings

    # The headline is the median of pair p50s, not a mean of means: one
    # sick pair must neither move the headline nor hide inside it.
    med = Agg()
    for p in measured:
        med.add(_pair_p50(p), keep=True)
    jit = Agg()
    for p in measured:
        jit.add(p.jitter.mean, keep=True)

    sent = sum(p.sent for p in measured)
    recv = sum(p.recv for p in measured)
    fwd_known = [p for p in udp if p.fwd_loss_pct is not None]
    fwd_sent = sum(p.sent for p in fwd_known)
    fwd_seen = sum(p.rx_recv for p in fwd_known)

    worst = sorted(measured, key=lambda p: -p.rank())
    dur = ""
    ts = [_int(r.get("ts")) for r in rows if r.get("ts")]
    if ts:
        dur = fmt_secs(max(max(ts) - min(ts), 1))

    log("")
    log("%s summarize -- %d hosts, %d measured pairs, reports span %s"
        % (PROG, len(hosts), len(measured), dur or "one interval"))
    log("")
    hi = max((_pair_p50(p) or 0) for p in measured) if measured else 0
    log("  BASELINE   p50 %s across all pairs   worst pair p50 %s"
        % (fmt_us(med.median()), fmt_us(hi)))
    log("  JITTER     median %s                 worst pair %s"
        % (fmt_us(jit.median()), fmt_us(jit.max)))
    if fwd_known:
        log("  LOSS       %.2f%% overall  (%.2f%% forward, %.2f%% on the way back)"
            % (pct(sent - recv, sent), pct(fwd_sent - fwd_seen, fwd_sent),
               pct(fwd_seen - sum(p.recv for p in fwd_known), fwd_seen)))
    else:
        log("  LOSS       %.2f%% overall" % pct(sent - recv, sent))
    mtus = [p for p in udp if p.path_mtu]
    if mtus:
        black = [p for p in udp if p.mtu_state == "blackhole"]
        top = max(p.path_mtu for p in mtus)
        at_top = sum(1 for p in mtus if p.path_mtu == top)
        line = "  PATH MTU   %d on %d of %d pairs" % (top, at_top, len(mtus))
        if len(mtus) - at_top:
            line += "; %d lower" % (len(mtus) - at_top)
        if black:
            line += "; %d BLACKHOLE" % len(black)
        log(line)
    if cpu.n and (cpu.max or 0) >= 80:
        log("  AGENT CPU  %.0f%% peak -- the tool is near a full core, so treat "
            "these numbers with suspicion" % cpu.max)

    # Worst pairs.
    log("")
    log("  WORST PAIRS (of %d, ranked by p99)" % len(measured))
    for p in worst[:args.top]:
        loss = p.loss_pct or 0.0
        extra = ""
        if p.fwd_loss_pct is not None and loss > 0.005:
            extra = " (fwd %.1f%%)" % p.fwd_loss_pct
        log("    %-12s -> %-12s p50 %-8s p99 %-8s jitter %-8s loss %.2f%%%s%s"
            % (p.src, p.dst, fmt_us(_pair_p50(p)),
               fmt_us(p.p99.mean if p.p99.n else None),
               fmt_us(p.jitter.mean), loss, extra,
               "  [ping]" if p.probe == "ping" else ""))

    # Asymmetry.
    asym = []
    for (s, d), p in pairs.items():
        q = pairs.get((d, s))
        if not q or q.probe == "ping" or p.probe == "ping":
            continue
        a, b = _pair_p50(p), _pair_p50(q)
        if a is None or b is None or s > d:
            continue
        lo = min(a, b)
        if abs(a - b) >= max(args.asym_us, lo * args.asym_pct / 100.0):
            asym.append((abs(a - b), p if a > b else q, q if a > b else p))
    if asym:
        log("")
        log("  ASYMMETRY")
        for delta, slow, fast in sorted(asym, reverse=True)[:args.top]:
            ratio = (_pair_p50(slow) / _pair_p50(fast)) if _pair_p50(fast) else 0
            log("    %s -> %s is %s slower than %s -> %s (%.1fx)"
                % (slow.src, slow.dst, fmt_us(delta), fast.src, fast.dst, ratio))
            log("      -- the return path is fine, so look at %s's egress "
                "queueing, not the fabric" % slow.src)

    black = [p for p in udp if p.mtu_state == "blackhole"]
    if black:
        log("")
        log("  PATH MTU")
        for p in black:
            log("    %s -> %s  BLACKHOLE above %d bytes: small packets get "
                "through, large ones vanish and nothing reports back."
                % (p.src, p.dst, p.path_mtu or 1500))
            log("      Applications hang on big transfers and work fine on "
                "small ones.  Check for a tunnel or a firewall dropping ICMP.")

    if pings:
        log("")
        log("  ONE-SIDED (ping only -- no receiver-side truth)")
        for p in sorted(pings, key=lambda x: -x.rank())[:args.top]:
            log("    %-12s -> %-12s avg %-8s max %-8s loss %.2f%%"
                % (p.src, p.dst, fmt_us(p.avg.mean), fmt_us(p.mx.max),
                   p.loss_pct or 0.0))

    # What to do next.
    threshold = args.slow_us
    bad = [p for p in measured
           if (_pair_p50(p) or 0) >= threshold or (p.loss_pct or 0) >= 0.5]
    log("")
    log("  WHAT TO DO NEXT")
    if not bad and not black:
        log("    Every pair is under %s with no meaningful loss: the network "
            "is not your problem." % fmt_us(threshold))
        log("    If something is still slow, measure load rather than latency:")
        log("      mx run --for 60           (packets per second)")
        log("      iperf_orchestrator.sh all (TCP bandwidth)")
    else:
        for line in diagnose(pairs, bad, hosts):
            log("    %s" % line)
        if bad:
            w = bad[0] if len(bad) == 1 else max(bad, key=lambda p: p.rank())
            other = next((p for p in measured if p.src == w.src and p is not w),
                         None)
            if other is not None:
                log("      %s paths --compare %s:%s %s:%s"
                    % (PROG, w.src, w.dst, other.src, other.dst))
            else:
                log("      %s paths --worst 3" % PROG)
        log("    If the paths come back clean, the network is not the "
            "bottleneck -- measure load:")
        log("      mx run --for 60           (packets per second)")
        log("      iperf_orchestrator.sh all (TCP bandwidth)")
    log("")

    if args.grid:
        _write_grids(args.grid, pairs, hosts)
        log("[%s] grids written to %s/" % (PROG, args.grid))
    if args.json:
        _emit_json(measured, med, jit, sent, recv, black)
    return 0


def _write_grids(outdir, pairs, hosts):
    os.makedirs(outdir, exist_ok=True)

    def grid(name, fn):
        path = os.path.join(outdir, name)
        with io.open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["src\\dst"] + hosts)
            for s in hosts:
                row = [s]
                for d in hosts:
                    p = pairs.get((s, d))
                    v = fn(p) if p else None
                    row.append("" if v is None else v)
                w.writerow(row)

    grid("rtt_p50_grid.csv",
         lambda p: None if _pair_p50(p) is None else "%.0f" % _pair_p50(p))
    grid("rtt_p99_grid.csv",
         lambda p: "%.0f" % p.p99.mean if p.p99.n else None)
    grid("jitter_grid.csv",
         lambda p: "%.0f" % p.jitter.mean if p.jitter.n else None)
    grid("loss_grid.csv",
         lambda p: "%.3f" % p.loss_pct if p.loss_pct is not None else None)
    grid("mtu_grid.csv", lambda p: p.path_mtu)

    # Antisymmetric by construction: a positive band across one row is that
    # host's outbound path being slower than its inbound.
    def asym(p):
        q = pairs.get((p.dst, p.src))
        a, b = _pair_p50(p), (_pair_p50(q) if q else None)
        if a is None or b is None:
            return None
        return "%.0f" % (a - b)

    grid("asym_grid.csv", asym)


def _emit_json(measured, med, jit, sent, recv, black):
    import json
    out = {
        "pairs": len(measured),
        "p50_median_us": med.median(),
        "p50_worst_us": med.max,
        "jitter_median_us": jit.median(),
        "loss_pct": pct(sent - recv, sent),
        "blackhole_pairs": [[p.src, p.dst] for p in black],
        "detail": [
            {"src": p.src, "dst": p.dst, "probe": p.probe,
             "sent": p.sent, "recv": p.recv,
             "loss_pct": p.loss_pct,
             "fwd_loss_pct": p.fwd_loss_pct,
             "p50_us": _pair_p50(p),
             "p99_us": p.p99.mean if p.p99.n else None,
             "jitter_us": p.jitter.mean,
             "path_mtu": p.path_mtu, "mtu_state": p.mtu_state}
            for p in measured
        ],
    }
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Verbs: run / check
# ---------------------------------------------------------------------------

def cmd_run(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)
    args.duration = args.duration or args.for_secs
    rc = cmd_start(args, mesh, fleet)
    if rc and not args.keep_going:
        return rc
    log("[%s] probing for %s" % (PROG, fmt_secs(args.for_secs)))
    try:
        time.sleep(args.for_secs + 1.0)
    except KeyboardInterrupt:
        log("[%s] interrupted -- summarizing what we have" % PROG)
    cmd_summarize(args, mesh, fleet)
    cmd_stop(args, mesh, fleet)
    return 0


def cmd_check(args):
    """The headline: probe a handful of hosts and clean up after itself."""
    tokens = list(args.hosts)
    if len(tokens) < 2 and not args.include_self:
        die("check needs at least two hosts (or one plus --include-self)")
    workdir = tempfile.mkdtemp(prefix="netmesh-")
    args.mesh = os.path.join(workdir, MESH_NAME)
    args.reports = args.reports if args.reports_given else \
        os.path.join(workdir, "reports")
    cfg = {"size": args.size, "port": args.port, "pps": "%g" % args.pps,
           "pmtu": "0" if args.no_pmtu else "1",
           "mtu_ceiling": args.mtu_ceiling,
           "pmtu_every": "%g" % args.pmtu_every}
    hosts, addrs, ports, pingonly, rate = build_mesh(tokens, args.pps, cfg)
    write_mesh(args.mesh, hosts, addrs, ports, pingonly, rate, cfg)
    mesh = load_mesh(args.mesh)
    fleet = Fleet(mesh, args)

    args.duration = args.for_secs + 5.0
    args.keep_going = True
    rc = cmd_start(args, mesh, fleet)
    try:
        log("[%s] probing for %s" % (PROG, fmt_secs(args.for_secs)))
        time.sleep(args.for_secs + 1.0)
        cmd_summarize(args, mesh, fleet)
    except KeyboardInterrupt:
        log("[%s] interrupted" % PROG)
    finally:
        args.quiet = True
        cmd_clean(args, mesh, fleet)
        if args.keep_mesh:
            shutil.copy(args.mesh, args.keep_mesh)
            log("[%s] mesh kept at %s (re-run it with: %s run --mesh %s)"
                % (PROG, args.keep_mesh, PROG, args.keep_mesh))
        if not args.reports_given:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            os.path.isdir(workdir) and shutil.rmtree(workdir, ignore_errors=True)
    return rc


# ---------------------------------------------------------------------------
# Verbs: paths
# ---------------------------------------------------------------------------

HOP_RE = re.compile(r"^\s*(\d+)[:.]?\s+(.*)$")


def _trace_script(peer):
    return ("command -v tracepath >/dev/null 2>&1 && exec tracepath -n %s; "
            "command -v traceroute >/dev/null 2>&1 && exec traceroute -n -q 1 -w 2 %s; "
            "echo no-trace-tool" % (shlex.quote(peer), shlex.quote(peer)))


def parse_hops(text):
    """Parse tracepath/traceroute output into (hop, addr, rtt_us, mtu)."""
    hops = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("tracepath") or "no-trace-tool" in line:
            continue
        m = HOP_RE.match(line)
        if not m:
            continue
        n, rest = int(m.group(1)), m.group(2)
        addr = ""
        rtt = ""
        mtu = ""
        am = re.search(r"(\d+\.\d+\.\d+\.\d+)", rest)
        if am:
            addr = am.group(1)
        elif "no reply" in rest or "*" in rest:
            addr = "*"
        rm = re.search(r"([\d.]+)\s*ms", rest)
        if rm:
            rtt = "%.0f" % (float(rm.group(1)) * 1000.0)
        mm = re.search(r"pmtu\s+(\d+)", rest)
        if mm:
            mtu = mm.group(1)
        hops.append((n, addr, rtt, mtu))
    return hops


def cmd_paths(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)
    outdir = args.output
    os.makedirs(outdir, exist_ok=True)

    wanted = []
    if args.compare:
        for spec in args.compare:
            for part in spec.split(","):
                if ":" not in part:
                    die("--compare wants src:dst pairs, got %r" % part)
                s, d = part.split(":", 1)
                wanted.append((s.strip(), d.strip()))
    elif args.pairs:
        for part in args.pairs.split(","):
            if ":" not in part:
                die("--pairs wants src:dst, got %r" % part)
            s, d = part.split(":", 1)
            wanted.append((s.strip(), d.strip()))
    else:
        rows = read_reports(args.reports)
        pairs, _, _ = build_pairs(rows)
        measured = [p for p in pairs.values() if p.sent]
        if not measured:
            die("no measured pairs in %s; run summarize first" % args.reports, 1)
        for p in sorted(measured, key=lambda x: -x.rank())[:args.worst]:
            wanted.append((p.src, p.dst))

    for s, d in wanted:
        if s not in mesh.rate:
            die("unknown source host %r" % s)
        if d not in mesh.addrs:
            die("unknown destination host %r" % d)

    results = {}

    def trace(pair):
        s, d = pair
        rc, out = fleet.sh(s, _trace_script(mesh.addrs[d]), timeout=120)
        return rc, out

    with ThreadPoolExecutor(max_workers=fleet.jobs) as pool:
        outs = list(pool.map(trace, wanted))

    hops_path = os.path.join(outdir, "hops.csv")
    with io.open(hops_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["src", "dst", "hop", "addr", "rtt_us", "mtu", "note"])
        for (s, d), (rc, out) in zip(wanted, outs):
            raw = os.path.join(outdir, "%s__%s.txt" % (s, d))
            with io.open(raw, "w", encoding="utf-8") as rf:
                rf.write(out + "\n")
            hops = parse_hops(out)
            results[(s, d)] = hops
            note = "" if hops else ("no-trace-tool" if "no-trace-tool" in out
                                    else "unparsed")
            if not hops:
                w.writerow([s, d, "", "", "", "", note])
            for n, addr, rtt, mtu in hops:
                w.writerow([s, d, n, addr, rtt, mtu, ""])
            log("  %s -> %s: %d hops  (%s)" % (s, d, len(hops), raw))

    log("[%s] wrote %s" % (PROG, hops_path))

    if args.compare and len(wanted) >= 2:
        _compare_paths(wanted, results)
    return 0


def _compare_paths(wanted, results):
    """Where do two routes diverge?  That is the whole point of comparing a
    sick pair against a healthy one from the same source."""
    (s1, d1), (s2, d2) = wanted[0], wanted[1]
    a, b = results.get((s1, d1), []), results.get((s2, d2), [])
    log("")
    log("  PATH COMPARISON")
    log("    %-28s %-28s" % ("%s -> %s" % (s1, d1), "%s -> %s" % (s2, d2)))
    n = max(len(a), len(b))
    first_diff = None
    for i in range(n):
        x = a[i][1] if i < len(a) else ""
        y = b[i][1] if i < len(b) else ""
        mark = " "
        if x != y and first_diff is None and x and y:
            first_diff = i + 1
            mark = "*"
        log("    %2d %-25s %2s %-25s %s"
            % (i + 1, x or "-", "", y or "-", mark))
    if first_diff:
        log("")
        log("    The routes are identical up to hop %d and diverge there."
            % (first_diff - 1))
        log("    That hop is where to look first.")
    else:
        log("")
        log("    The two routes do not diverge, so the difference is not "
            "topological -- look at load or queueing on the shared path.")


# ---------------------------------------------------------------------------
# Verbs: doctor / selftest
# ---------------------------------------------------------------------------

DOCTOR_SCRIPT = """
printf 'python='
(command -v python3 || command -v python || echo MISSING) 2>/dev/null | tail -1
printf 'ping='; command -v ping >/dev/null 2>&1 && echo yes || echo MISSING
printf 'trace='
if command -v tracepath >/dev/null 2>&1; then echo tracepath
elif command -v traceroute >/dev/null 2>&1; then echo traceroute
else echo MISSING; fi
printf 'writable='; [ -w /var/tmp ] && echo yes || echo NO
printf 'nofile='; ulimit -n
"""


def cmd_doctor(args, mesh=None, fleet=None):
    mesh = mesh or load_mesh(args.mesh)
    fleet = fleet or Fleet(mesh, args)
    log("[%s] local checks" % PROG)
    log("  python           %s" % sys.version.split()[0])
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _set_df(s)
        log("  path-MTU probing supported (IP_MTU_DISCOVER accepted)")
    except OSError as exc:
        log("  path-MTU probing UNAVAILABLE here (%s) -- netmesh still "
            "measures RTT, jitter and loss" % exc.strerror)
    finally:
        s.close()
    log("  agent file       %s" % _agent_source())
    if mesh.pingonly:
        log("  ping-only        %s" % " ".join(sorted(mesh.pingonly)))

    def check(host):
        return fleet.sh(host, DOCTOR_SCRIPT, timeout=60)

    log("")
    fleet.each(check, label="remote checks")
    return 0


def cmd_selftest(args):
    """Two agents on loopback: no ssh, no second host, no privileges.

    This answers "is my Python OK, can I open a UDP socket here, does PMTU
    probing work on this kernel" before anyone tries a real fleet.
    """
    workdir = tempfile.mkdtemp(prefix="netmesh-selftest-")
    try:
        p1, p2 = args.port, args.port + 1
        cfg = {"size": str(DEFAULT_SIZE), "port": str(p1), "pps": "50",
               "pmtu": "0" if args.no_pmtu else "1",
               "mtu_ceiling": str(args.mtu_ceiling), "pmtu_every": "0"}
        hosts = ["a", "b"]
        addrs = {"a": "127.0.0.1", "b": "127.0.0.1"}
        ports = {"a": p1, "b": p2}
        rate = {"a": {"b": 50.0}, "b": {"a": 50.0}}
        mesh_path = os.path.join(workdir, MESH_NAME)
        write_mesh(mesh_path, hosts, addrs, ports, set(), rate, cfg)

        secs = args.for_secs
        procs = []
        for h in hosts:
            d = os.path.join(workdir, h)
            os.makedirs(d, exist_ok=True)
            cmd = [sys.executable, _agent_source(), "agent",
                   "--mesh", mesh_path, "--host", h, "--dir", d,
                   "--interval", "%g" % min(secs, 2.0),
                   "--duration", "%g" % secs, "--bind", "127.0.0.1"]
            procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT,
                                          universal_newlines=True,
                                          encoding="utf-8", errors="replace"))
        log("[%s] two agents probing over loopback for %s..."
            % (PROG, fmt_secs(secs)))
        for p in procs:
            try:
                out = p.communicate(timeout=secs + 30)[0]
            except subprocess.TimeoutExpired:
                p.kill()
                out = p.communicate()[0]
            if p.returncode != 0:
                log("  agent failed: %s" % (out or "").strip())

        reports = os.path.join(workdir, "reports")
        os.makedirs(reports, exist_ok=True)
        for h in hosts:
            src = os.path.join(workdir, h, REPORT_NAME)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(reports, "%s.csv" % h))

        rows = read_reports(reports)
        pairs, _, _ = build_pairs(rows)
        measured = [p for p in pairs.values() if p.sent]
        problems = []
        if len(measured) < 2:
            problems.append("expected 2 measured pairs, got %d" % len(measured))
        for p in measured:
            if not p.recv:
                problems.append("%s -> %s got no replies" % (p.src, p.dst))
                continue
            rtt = _pair_p50(p)
            if rtt is None or not (0 < rtt < 200000):
                problems.append("%s -> %s implausible RTT %s"
                                % (p.src, p.dst, fmt_us(rtt)))
            if (p.loss_pct or 0) > 5.0:
                problems.append("%s -> %s lost %.1f%% on loopback"
                                % (p.src, p.dst, p.loss_pct))

        log("")
        for p in measured:
            log("  %s -> %s  p50 %-8s loss %.2f%%  mtu %s (%s)"
                % (p.src, p.dst, fmt_us(_pair_p50(p)), p.loss_pct or 0.0,
                   p.path_mtu or "-", p.mtu_state or "not probed"))
        log("")
        if problems:
            for x in problems:
                log("  FAIL  %s" % x)
            log("")
            log("%s selftest: FAIL" % PROG)
            return 1
        log("%s selftest: PASS -- sockets, probing, reporting and analysis "
            "all work here." % PROG)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Verb: agent
# ---------------------------------------------------------------------------

def cmd_agent(args):
    mesh = load_mesh(args.mesh)
    if args.host not in mesh.rate:
        die("host %r is not in %s" % (args.host, args.mesh))
    if args.host in mesh.pingonly:
        die("host %r is marked ping-only, so it must not run an agent"
            % args.host)
    agent = Agent(mesh, args.host, args.dir, args.interval, args.duration,
                  args.bind)
    return agent.run()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_fleet_flags(p):
    p.add_argument("--mesh", default=_env("MESH", DEFAULT_MESH))
    p.add_argument("--user", default=_env("USER"))
    p.add_argument("--jobs", type=int, default=int(_env_num("JOBS", 16, int)))
    p.add_argument("--remote-dir", default=_env("REMOTE_DIR", DEFAULT_REMOTE_DIR))
    p.add_argument("--python", default=_env("PYTHON", "python3"))
    p.add_argument("--ssh", default=_env("SSH", "ssh"))
    p.add_argument("--scp", default=_env("SCP", "scp"))
    p.add_argument("--reports", default=_env("REPORTS", "reports"))
    p.add_argument("--include-self", action="store_true",
                   help="run an agent on this machine too, with no ssh")
    p.add_argument("--dry-run", action="store_true",
                   help="print the ssh/scp commands instead of running them")
    p.add_argument("--keep-going", action="store_true",
                   help="carry on when some hosts fail")


def _add_probe_flags(p):
    p.add_argument("--pps", type=float, default=_env_num("PPS", DEFAULT_PPS))
    p.add_argument("--size", type=int, default=int(_env_num("SIZE",
                                                            DEFAULT_SIZE, int)))
    p.add_argument("--port", type=int, default=int(_env_num("PORT",
                                                            DEFAULT_PORT, int)))
    p.add_argument("--no-pmtu", action="store_true",
                   help="skip path-MTU discovery")
    p.add_argument("--mtu-ceiling", type=int,
                   default=int(_env_num("MTU_CEILING", DEFAULT_MTU_CEILING, int)))
    p.add_argument("--pmtu-every", type=float,
                   default=_env_num("PMTU_EVERY", DEFAULT_PMTU_EVERY))


def _add_summary_flags(p):
    p.add_argument("--top", type=int, default=5,
                   help="how many rows in each ranked list")
    p.add_argument("--grid", metavar="DIR", help="also write N*N grid CSVs")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-collect", action="store_true",
                   help="analyse the reports already on disk")
    p.add_argument("--asym-us", type=float, default=50.0,
                   help="absolute asymmetry floor before flagging (us)")
    p.add_argument("--asym-pct", type=float, default=30.0,
                   help="relative asymmetry floor before flagging (%%)")
    p.add_argument("--slow-us", type=float, default=500.0,
                   help="a pair at or above this p50 counts as slow")


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version="%s %s" % (PROG, VERSION))
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="one-shot: probe a few hosts and clean up")
    c.add_argument("hosts", nargs="*", metavar="HOST")
    c.add_argument("--for", dest="for_secs", type=float, default=DEFAULT_FOR)
    c.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    c.add_argument("--keep-mesh", metavar="PATH",
                   help="save the generated mesh for repeat runs")
    _add_probe_flags(c)
    _add_fleet_flags(c)
    _add_summary_flags(c)
    c.set_defaults(func=cmd_check)

    g = sub.add_parser("gen", help="write a mesh file")
    g.add_argument("hosts", nargs="*", metavar="HOST")
    g.add_argument("--servers", metavar="FILE")
    g.add_argument("--mesh", default=_env("MESH", DEFAULT_MESH))
    _add_probe_flags(g)
    g.set_defaults(func=cmd_gen)

    s = sub.add_parser("start", help="deploy and start the agents")
    s.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    s.add_argument("--duration", type=float, default=0.0,
                   help="stop each agent after this long (0 = until stopped)")
    s.add_argument("--bind", default="")
    _add_fleet_flags(s)
    s.set_defaults(func=cmd_start)

    st = sub.add_parser("status", help="is an agent running on each host?")
    _add_fleet_flags(st)
    st.set_defaults(func=cmd_status)

    sm = sub.add_parser("summarize", help="collect reports and diagnose")
    _add_fleet_flags(sm)
    _add_summary_flags(sm)
    sm.set_defaults(func=cmd_summarize)

    sp = sub.add_parser("stop", help="stop the agents, keep the reports")
    _add_fleet_flags(sp)
    sp.set_defaults(func=cmd_stop)

    cl = sub.add_parser("clean", help="stop and remove every trace")
    cl.add_argument("--quiet", action="store_true")
    _add_fleet_flags(cl)
    cl.set_defaults(func=cmd_clean)

    r = sub.add_parser("run", help="start, hold, summarize, stop")
    r.add_argument("--for", dest="for_secs", type=float, default=DEFAULT_FOR)
    r.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    r.add_argument("--duration", type=float, default=0.0)
    r.add_argument("--bind", default="")
    _add_fleet_flags(r)
    _add_summary_flags(r)
    r.set_defaults(func=cmd_run)

    co = sub.add_parser("collect", help="fetch reports without analysing")
    _add_fleet_flags(co)
    co.set_defaults(func=cmd_collect)

    lg = sub.add_parser("logs", help="tail each agent's log")
    lg.add_argument("--lines", type=int, default=10)
    _add_fleet_flags(lg)
    lg.set_defaults(func=cmd_logs)

    pa = sub.add_parser("paths", help="trace the route for sick pairs")
    pa.add_argument("--worst", type=int, default=3)
    pa.add_argument("--pairs", help="src:dst,src:dst")
    pa.add_argument("--compare", nargs="+", metavar="SRC:DST",
                    help="two pairs to compare hop by hop")
    pa.add_argument("--output", default="paths")
    _add_fleet_flags(pa)
    pa.set_defaults(func=cmd_paths)

    d = sub.add_parser("doctor", help="can this fleet run netmesh at all?")
    _add_fleet_flags(d)
    d.set_defaults(func=cmd_doctor)

    se = sub.add_parser("selftest", help="prove it works on this machine alone")
    se.add_argument("--for", dest="for_secs", type=float, default=5.0)
    se.add_argument("--port", type=int, default=DEFAULT_PORT)
    se.add_argument("--no-pmtu", action="store_true")
    se.add_argument("--mtu-ceiling", type=int, default=DEFAULT_MTU_CEILING)
    se.set_defaults(func=cmd_selftest)

    ag = sub.add_parser("agent", help="the per-host worker (started by `start`)")
    ag.add_argument("--mesh", required=True)
    ag.add_argument("--host", required=True)
    ag.add_argument("--dir", required=True)
    ag.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ag.add_argument("--duration", type=float, default=0.0)
    ag.add_argument("--bind", default="")
    ag.set_defaults(func=cmd_agent)

    h = sub.add_parser("help", help="every flag of every verb, on one page")
    h.set_defaults(func=None)
    return p, sub


def cmd_full_help(parser, sub):
    """Generated by walking the real subparsers, so it cannot drift from
    what the tool actually accepts."""
    parser.print_help()
    for name, p in sorted(sub.choices.items()):
        if name == "help":
            continue
        log("")
        log("=" * 72)
        p.print_help()
    return 0


def main(argv=None):
    _stdio_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser, sub = build_parser()
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    if args.cmd == "help":
        return cmd_full_help(parser, sub)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    # `check` needs to know whether --reports was given, because it puts
    # reports in a temp dir and deletes them unless asked not to.
    args.reports_given = any(a == "--reports" or a.startswith("--reports=")
                             for a in argv)
    if not hasattr(args, "quiet"):
        args.quiet = False
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
