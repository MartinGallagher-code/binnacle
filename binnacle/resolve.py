#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""resolve.py -- work out whether it is DNS, and which resolver is wrong.

Usage: resolve.py                        check the configured resolvers
       resolve.py db01.example.com       ...and what they say about a name
       resolve.py db01 api.internal      several names at once
       resolve.py --server 10.0.0.53     ask one resolver instead
       resolve.py --csv                  one row per finding, for fleet use
       resolve.py --rules                every rule and its thresholds
       resolve.py --explain RULE_ID      why one rule exists

Options:
  --server ADDR[:PORT]  ask this resolver instead of resolv.conf (repeatable)
  --type LIST        record types to ask for            (default A,AAAA)
  --timeout S        per-query timeout, seconds         (default 2.0)
  --attempts N       tries per query before giving up   (default 1)
  --no-search        do not measure the search-domain cost
  --no-stub          do not time getaddrinfo (the path applications take)
  --min-severity L   info | warn | critical -- hide findings below L
  --all              also list the rules that were skipped, and why
  --quiet            the verdict and the findings, nothing else
  --exit-code        exit 0 ok / 10 warn / 20 critical (default: always 0)
  --csv [PATH]       findings as CSV (stdout if no path)
  --json [PATH]      meta + facts + findings + skipped
  --facts            print the fact dictionary and exit
  --from-facts FILE  run the rules against a saved fact file, resolving
                     nothing -- this is how the rules are tested
  --resolv-conf F    read resolver config from here     (RESOLVE_CONF)
  --hosts-file F     read the hosts file from here      (RESOLVE_HOSTS)
  --no-exec          never run a subprocess
  --no-color         plain output (also honours NO_COLOR)

What it does
  "Is it DNS?" is the oldest question in the book, and the usual answers --
  dig, host, nslookup -- all ask *one* resolver *one* question and print
  what came back.  That is not the shape of the problem.  A box with three
  nameservers where the first one is dead resolves everything correctly and
  slowly, for ever, and every one of those tools will tell you DNS is fine
  because they only ever spoke to the second.

  So this asks every configured resolver separately, on the wire, and
  reports where they differ:

    * each nameserver is queried directly rather than through the stub, so
      a dead or slow one is attributed to itself instead of being hidden
      behind the retry that covers for it
    * the answers are compared across resolvers, because two servers that
      disagree about a name is split horizon, a stale cache, or a resolver
      pointing somewhere you have forgotten about -- and no single-server
      query can see it
    * the search list is walked and the wasted round trips counted, since
      an unqualified name with six search domains is six NXDOMAINs before
      the query you meant
    * getaddrinfo is timed alongside, because that is the path your
      application actually takes, and the gap between it and the direct
      query is the cost of the configuration rather than the network
    * /etc/hosts is read, because an entry there silently beats DNS and is
      why a name resolves differently on one box and nowhere else

  Then it says which of those is the problem, in the same voice as the rest
  of binnacle: the verdict names the cause rather than the biggest number.

Robustness properties
  * a fact that could not be measured is None, never 0, and any rule that
    needed it is reported as skipped with the reason
  * answer sets are compared sorted, so a resolver rotating records
    round-robin -- which is normal and healthy -- never reads as
    disagreement
  * replies whose id or question does not match the query are discarded and
    the wait continues, so a late reply to an earlier query cannot be
    counted as this one's answer
  * a local stub resolver (127.0.0.53 and friends) is named as such: timing
    it says nothing about the upstreams behind it, and the report says so
    rather than reporting a healthy 0.2ms and leaving you to assume
  * needs no root, no packages and no dig: the queries are built and parsed
    here, standard library only, Python 3.6+
  * every rule is a pure function of the fact dictionary, so --from-facts
    reproduces any diagnosis with nothing resolving at the time
  * exit status is 0 unless you ask for --exit-code, so it is safe in a
    pipeline by default
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

VERSION = "0.6.0"
PROG = os.path.basename(sys.argv[0]) or "resolve.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

RESOLV_CONF = "/etc/resolv.conf"
HOSTS_FILE = "/etc/hosts"
NO_EXEC = False

# The resolvers that are a local stub rather than a real server.  Timing one
# of these measures the stub and says nothing whatever about the upstreams
# it forwards to, which is worth saying out loud.
STUB_ADDRS = {"127.0.0.53": "systemd-resolved", "127.0.0.1": "a local resolver",
              "::1": "a local resolver"}


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
    v = os.environ.get("RESOLVE_" + name)
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
# DNS on the wire
# ---------------------------------------------------------------------------
#
# Building and parsing the packets here rather than shelling out to dig is
# not purity: dig is absent on most servers, and the point of measuring is
# to time exactly one query to exactly one server, which a wrapper around
# somebody else's retry logic cannot do.

QTYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15,
          "TXT": 16, "AAAA": 28}
QTYPE_NAMES = dict((v, k) for k, v in QTYPES.items())

RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
          4: "NOTIMP", 5: "REFUSED"}


class DNSError(Exception):
    """A malformed or unusable reply.  Never a transport failure."""


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        raw = label.encode("idna") if any(ord(c) > 127 for c in label) \
            else label.encode("ascii")
        if len(raw) > 63:
            raise DNSError("label longer than 63 bytes: %s" % label)
        out += struct.pack("!B", len(raw)) + raw
    return out + b"\x00"


def decode_name(data, offset):
    """Return (name, offset_after).  Follows compression pointers.

    The jump budget is what stops a packet whose pointers form a loop from
    hanging the tool -- a malformed reply must be an error, never a spin.
    """
    labels = []
    jumps = 0
    pos = offset
    after = None
    while True:
        if pos >= len(data):
            raise DNSError("name runs past the end of the packet")
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise DNSError("truncated compression pointer")
            target = ((length & 0x3F) << 8) | data[pos + 1]
            if after is None:
                after = pos + 2
            jumps += 1
            if jumps > 32:
                raise DNSError("compression pointer loop")
            pos = target
            continue
        pos += 1
        labels.append(data[pos:pos + length].decode("ascii", "replace"))
        pos += length
    return ".".join(labels), (after if after is not None else pos)


def build_query(qid, name, qtype, want_edns=True, bufsize=1232):
    flags = 0x0100                      # standard query, recursion desired
    header = struct.pack("!HHHHHH", qid, flags, 1, 0, 0, 1 if want_edns else 0)
    body = encode_name(name) + struct.pack("!HH", qtype, 1)
    if want_edns:
        # OPT pseudo-record: root name, type 41, class = the UDP size we can
        # accept.  Without it a server may answer with a truncated reply
        # where a larger one would have fitted.
        body += struct.pack("!BHHBBHH", 0, 41, bufsize, 0, 0, 0, 0)
    return header + body


def parse_response(data, qid, name, qtype):
    """Unpack a reply, or raise DNSError if it is not ours or not sane."""
    if len(data) < 12:
        raise DNSError("reply shorter than a header")
    rid, flags, qdcount, ancount = struct.unpack("!HHHH", data[:8])
    if rid != qid:
        raise DNSError("id mismatch")
    if not flags & 0x8000:
        raise DNSError("not a response")
    pos = 12
    for _ in range(qdcount):
        qname, pos = decode_name(data, pos)
        if pos + 4 > len(data):
            raise DNSError("truncated question")
        qt, _qc = struct.unpack("!HH", data[pos:pos + 4])
        pos += 4
        # A reply carrying a different question is an answer to something
        # else -- a late one from a previous query, or worse.
        if qname.lower() != name.rstrip(".").lower() or qt != qtype:
            raise DNSError("question mismatch")
    answers = []
    for _ in range(ancount):
        try:
            rname, pos = decode_name(data, pos)
            if pos + 10 > len(data):
                raise DNSError("truncated record")
            rtype, _rclass, ttl, rdlen = struct.unpack("!HHIH",
                                                       data[pos:pos + 10])
            pos += 10
            rdata_at = pos
            if pos + rdlen > len(data):
                # slicing would quietly hand back fewer bytes than the
                # record claims, and a truncated address must not become
                # an answer the cross-server comparison then acts on
                raise DNSError("truncated record")
            rdata = data[pos:pos + rdlen]
            pos += rdlen
        except struct.error:
            raise DNSError("malformed answer section")
        answers.append((QTYPE_NAMES.get(rtype, str(rtype)),
                        _rdata_str(rtype, rdata, data, rdata_at), ttl))
    return {"rcode": RCODES.get(flags & 0xF, "RCODE%d" % (flags & 0xF)),
            "truncated": bool(flags & 0x0200),
            "authoritative": bool(flags & 0x0400),
            "recursion_available": bool(flags & 0x0080),
            "answers": answers}


def _rdata_str(rtype, rdata, packet, offset):
    try:
        if rtype == 1 and len(rdata) == 4:
            return socket.inet_ntop(socket.AF_INET, rdata)
        if rtype == 28 and len(rdata) == 16:
            return socket.inet_ntop(socket.AF_INET6, rdata)
        if rtype in (2, 5, 12):
            # NS, CNAME and PTR carry a name, and that name may be
            # compressed against any earlier point in the packet -- so it
            # has to be decoded from the whole packet at its real offset,
            # never from the rdata slice on its own.
            return decode_name(packet, offset)[0]
    except (ValueError, OSError, DNSError):
        pass
    return rdata.hex() if hasattr(rdata, "hex") else repr(rdata)


def _family_of(addr):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, addr)
            return fam
        except (OSError, ValueError):
            continue
    return None


Reply = namedtuple("Reply", "ok rtt_ms rcode answers truncated error")


def _query_id():
    """An unguessable query id, from the OS rather than a seeded PRNG.

    The id is half of what makes an off-path reply distinguishable from a
    real one, and a predictable sequence would make this tool easier to lie
    to than the resolver it is checking.
    """
    return struct.unpack("!H", os.urandom(2))[0]


def query_udp(server, port, name, qtype, timeout, attempts):
    """One question to one server.  Returns a Reply, never raises.

    Late replies to an earlier query arrive with the wrong id and are
    discarded rather than counted -- the wait continues on the remaining
    budget, because treating a stale packet as this answer would report a
    working resolver as fast when it never answered at all.
    """
    fam = _family_of(server)
    if fam is None:
        return Reply(False, None, None, [], False, "not an IP address")
    last = "no reply"
    for _attempt in range(max(1, attempts)):
        qid = _query_id()
        packet = build_query(qid, name, qtype)
        sock = None
        try:
            sock = socket.socket(fam, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            t0 = time.monotonic()
            sock.sendto(packet, (server, port))
            deadline = t0 + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    last = "timed out after %.1fs" % timeout
                    break
                sock.settimeout(left)
                data, _peer = sock.recvfrom(65535)
                try:
                    parsed = parse_response(data, qid, name, qtype)
                except DNSError as exc:
                    last = str(exc)
                    continue          # not ours: keep waiting on the budget
                return Reply(True, (time.monotonic() - t0) * 1000.0,
                             parsed["rcode"], parsed["answers"],
                             parsed["truncated"], None)
        except socket.timeout:
            last = "timed out after %.1fs" % timeout
        except OSError as exc:
            last = str(exc)
        finally:
            if sock is not None:
                sock.close()
    return Reply(False, None, None, [], False, last)


def query_tcp(server, port, name, qtype, timeout):
    """The fallback path, which is a separate question: plenty of firewalls
    pass UDP 53 and drop TCP 53, and everything works until an answer is
    too big to fit in a datagram."""
    fam = _family_of(server)
    if fam is None:
        return Reply(False, None, None, [], False, "not an IP address")
    qid = _query_id()
    packet = build_query(qid, name, qtype)
    framed = struct.pack("!H", len(packet)) + packet
    sock = None
    try:
        sock = socket.socket(fam, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        t0 = time.monotonic()
        sock.connect((server, port))
        sock.sendall(framed)
        head = _recv_exactly(sock, 2)
        length = struct.unpack("!H", head)[0]
        data = _recv_exactly(sock, length)
        parsed = parse_response(data, qid, name, qtype)
        return Reply(True, (time.monotonic() - t0) * 1000.0, parsed["rcode"],
                     parsed["answers"], False, None)
    except (OSError, DNSError, struct.error) as exc:
        return Reply(False, None, None, [], False,
                     "timed out" if isinstance(exc, socket.timeout) else str(exc))
    finally:
        if sock is not None:
            sock.close()


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise DNSError("connection closed mid-message")
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# What this box is configured to do
# ---------------------------------------------------------------------------

def read_resolv_conf(path):
    """nameserver / search / domain / options, in the order they appear.

    Order matters more than anything else in the file: the stub tries the
    nameservers in sequence, so a dead first entry costs every single
    lookup a full timeout before the working second one is reached.
    """
    text = _read(path)
    if text is None:
        return None
    conf = {"nameservers": [], "search": [], "options": [], "ndots": 1,
            "timeout": 5, "attempts": 2, "rotate": False,
            "single_request": False}
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        key = parts[0].lower()
        if key == "nameserver" and len(parts) > 1:
            conf["nameservers"].append(parts[1].split("%")[0])
        elif key == "search" and len(parts) > 1:
            conf["search"] = parts[1:]
        elif key == "domain" and len(parts) > 1:
            # 'domain' is the one-entry form of 'search', and the last of
            # the two in the file wins.
            conf["search"] = [parts[1]]
        elif key == "options":
            for opt in parts[1:]:
                conf["options"].append(opt)
                if opt.startswith("ndots:"):
                    conf["ndots"] = _int_or(opt.split(":", 1)[1], 1)
                elif opt.startswith("timeout:"):
                    conf["timeout"] = _int_or(opt.split(":", 1)[1], 5)
                elif opt.startswith("attempts:"):
                    conf["attempts"] = _int_or(opt.split(":", 1)[1], 2)
                elif opt == "rotate":
                    conf["rotate"] = True
                elif opt.startswith("single-request"):
                    conf["single_request"] = True
    return conf


def _int_or(text, default):
    try:
        return int(text)
    except ValueError:
        return default


def read_hosts(path):
    """name -> [addresses].  An entry here beats DNS and leaves no trace."""
    text = _read(path)
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        for nm in parts[1:]:
            out.setdefault(nm.lower(), []).append(parts[0])
    return out


def read_resolved_upstreams():
    """The real servers behind a systemd-resolved stub, if it will say."""
    out = _run(["resolvectl", "status"])
    if out is None:
        out = _run(["systemd-resolve", "--status"])
    if out is None:
        return None
    servers = []
    for line in out.splitlines():
        line = line.strip()
        for prefix in ("Current DNS Server:", "DNS Servers:"):
            if line.startswith(prefix):
                for tok in line[len(prefix):].split():
                    if _family_of(tok) is not None and tok not in servers:
                        servers.append(tok)
    return servers or None


# ---------------------------------------------------------------------------
# Building the fact dictionary
# ---------------------------------------------------------------------------
#
# Same contract as the rest of binnacle: a fact that could not be measured
# is None, and every rule declares what it needs, so a missing measurement
# becomes a skip with a reason rather than a wrong answer.

def _answer_key(reply, types):
    """The comparable shape of an answer.

    Sorted, because a resolver rotating its records round-robin is normal
    and healthy; comparing unsorted would report every load-balanced name
    as a disagreement and bury the real ones.
    """
    vals = sorted(v for (t, v, _ttl) in reply.answers if t in types)
    return vals


def collect_facts(args, names):
    f = {}
    f["sys.hostname"] = socket.gethostname()
    f["probe.timeout_s"] = args.timeout
    f["probe.attempts"] = args.attempts
    f["probe.names"] = list(names)
    f["probe.types"] = list(args.types)

    conf = read_resolv_conf(args.resolv_conf)
    f["res.path"] = args.resolv_conf
    f["res.readable"] = conf is not None
    f["res.nameservers"] = conf["nameservers"] if conf else None
    f["res.search"] = conf["search"] if conf else None
    f["res.options"] = conf["options"] if conf else None
    f["res.ndots"] = conf["ndots"] if conf else None
    f["res.timeout"] = conf["timeout"] if conf else None
    f["res.attempts"] = conf["attempts"] if conf else None
    f["res.rotate"] = conf["rotate"] if conf else None
    f["res.single_request"] = conf["single_request"] if conf else None

    if args.servers:
        configured = [_split_server(s) for s in args.servers]
        for spec, (addr, _port) in zip(args.servers, configured):
            if not _is_ip(addr):
                # A name here would have to be resolved by the very stub
                # this tool is diagnosing, and a typo'd address reported
                # as "no resolver answered" blames the fleet for a bad
                # argument.  resolv.conf only ever holds addresses; so
                # does --server.
                die("--server %s: give the resolver as an IP address"
                    % spec)
        f["probe.source"] = "--server"
    else:
        configured = [_split_server(s) for s in (f["res.nameservers"] or [])]
        f["probe.source"] = args.resolv_conf
    # Probe each distinct server once, keeping the configured order: the
    # results are held in a dict keyed by server, so a repeated nameserver
    # line would otherwise leave srv.count larger than the number of
    # results and "all of them are dead" could never be true.
    servers = []
    for entry in configured:
        if entry not in servers:
            servers.append(entry)
    f["res.duplicates"] = len(configured) - len(servers)
    f["srv.list"] = ["%s:%d" % (a, p) if p != 53 else a for a, p in servers]

    # A stub is not the resolver you think you are measuring -- but only
    # when it is what the box is configured to use.  A --server you typed
    # yourself is a deliberate choice and needs no warning about it.
    stubs = {}
    if not args.servers:
        for addr, _port in servers:
            if addr in STUB_ADDRS:
                stubs[addr] = STUB_ADDRS[addr]
    f["res.stub"] = stubs or None
    f["res.upstreams"] = read_resolved_upstreams() if stubs else None

    # --- liveness and latency, one server at a time -----------------------
    #
    # Sequentially rather than in parallel: these are latency measurements,
    # and probes racing each other on one interface measure each other.
    f["srv.all"] = {}
    for addr, port in servers:
        key = "%s:%d" % (addr, port) if port != 53 else addr
        r = query_udp(addr, port, ".", QTYPES["NS"], args.timeout,
                      args.attempts)
        f["srv.all"][key] = {"alive": r.ok, "rtt_ms": r.rtt_ms,
                             "rcode": r.rcode, "error": r.error,
                             "names": {}}
    f["srv.count"] = len(servers)
    # The stub works down the file as written, duplicates and all, so the
    # wait an application faces is counted from the configured list.
    f["res.nameserver_count"] = len(configured)
    f["srv.dead"] = sorted(k for k, v in f["srv.all"].items()
                           if not v["alive"]) if servers else None
    f["srv.alive"] = sorted(k for k, v in f["srv.all"].items()
                            if v["alive"]) if servers else None
    f["srv.first_dead"] = None
    if servers:
        first = f["srv.list"][0]
        f["srv.first_dead"] = not f["srv.all"][first]["alive"]
    rtts = [v["rtt_ms"] for v in f["srv.all"].values()
            if v["rtt_ms"] is not None]
    f["srv.slowest_ms"] = max(rtts) if rtts else None
    f["srv.fastest_ms"] = min(rtts) if rtts else None
    f["srv.slowest"] = None
    if rtts:
        f["srv.slowest"] = max((v["rtt_ms"], k)
                               for k, v in f["srv.all"].items()
                               if v["rtt_ms"] is not None)[1]

    # --- the names, on every server ---------------------------------------
    hosts = read_hosts(args.hosts_file)
    f["hosts.readable"] = hosts is not None
    f["names.all"] = {}
    f["names.disagree"] = [] if names else None
    f["names.failed"] = [] if names else None
    f["names.partial"] = [] if names else None
    f["names.aaaa_stall"] = [] if names else None
    f["names.hosts_shadow"] = [] if names else None
    f["tcp.blocked"] = [] if names else None

    live = [(a, p) for (a, p) in servers
            if f["srv.all"]["%s:%d" % (a, p) if p != 53 else a]["alive"]]

    for name in names:
        entry = {"by_server": {}, "answers": {}, "hosts": None}
        if hosts is not None:
            entry["hosts"] = hosts.get(name.lower())
        for addr, port in live:
            key = "%s:%d" % (addr, port) if port != 53 else addr
            per_type = {}
            for tname in args.types:
                qtype = QTYPES[tname]
                r = query_udp(addr, port, name, qtype, args.timeout,
                              args.attempts)
                per_type[tname] = {
                    "ok": r.ok, "rtt_ms": r.rtt_ms, "rcode": r.rcode,
                    "answers": _answer_key(r, (tname, "CNAME")) if r.ok else [],
                    "truncated": r.truncated, "error": r.error}
                if r.ok and r.truncated:
                    t = query_tcp(addr, port, name, qtype, args.timeout)
                    per_type[tname]["tcp_ok"] = t.ok
                    if not t.ok and key not in f["tcp.blocked"]:
                        f["tcp.blocked"].append(key)
            entry["by_server"][key] = per_type
            # One comparable answer set per server: the union across the
            # types asked for, so a server answering A but not AAAA differs
            # from one answering both.
            merged = []
            for tname in args.types:
                merged.extend(per_type[tname]["answers"])
            entry["answers"][key] = sorted(set(merged))

        # AAAA lost while A answers is the classic v6 black hole: glibc
        # asks for both and waits for the one that never comes.
        for key, per_type in entry["by_server"].items():
            if "AAAA" in per_type and "A" in per_type:
                if per_type["A"]["ok"] and not per_type["AAAA"]["ok"]:
                    if name not in f["names.aaaa_stall"]:
                        f["names.aaaa_stall"].append(name)

        answered = [k for k, v in entry["answers"].items() if v]
        empty = [k for k, v in entry["answers"].items() if not v]
        distinct = set(tuple(v) for v in entry["answers"].values() if v)
        entry["distinct"] = len(distinct)
        if entry["answers"] and not answered:
            f["names.failed"].append(name)
        elif empty and answered:
            f["names.partial"].append(name)
        if len(distinct) > 1:
            f["names.disagree"].append(name)
        if entry["hosts"] and answered:
            dns_set = set()
            for v in entry["answers"].values():
                dns_set.update(v)
            if dns_set and not dns_set.intersection(set(entry["hosts"])):
                f["names.hosts_shadow"].append(name)
        f["names.all"][name] = entry

    # --- the search list, measured rather than assumed ---------------------
    f["search.count"] = len(f["res.search"]) if f["res.search"] is not None \
        else None
    f["search.wasted"] = None
    f["search.wasted_ms"] = None
    unqualified = [n for n in names
                   if n.count(".") < (f["res.ndots"] or 1)
                   and not n.endswith(".")]
    if unqualified and live and f["res.search"] and not args.no_search:
        addr, port = live[0]
        wasted = 0
        wasted_ms = 0.0
        for name in unqualified:
            for suffix in f["res.search"]:
                r = query_udp(addr, port, "%s.%s" % (name, suffix),
                              QTYPES["A"], args.timeout, args.attempts)
                if r.ok and r.rcode == "NOERROR" and r.answers:
                    break
                wasted += 1
                wasted_ms += r.rtt_ms or 0.0
        f["search.wasted"] = wasted
        f["search.wasted_ms"] = wasted_ms

    # --- what the application actually does --------------------------------
    f["stub.all"] = {}
    f["stub.slowest_ms"] = None
    if names and not args.no_stub:
        for name in names:
            t0 = time.monotonic()
            try:
                infos = socket.getaddrinfo(name, None)
                addrs = sorted(set(i[4][0] for i in infos))
                err = None
            except socket.gaierror as exc:
                addrs, err = [], str(exc)
            except (OSError, UnicodeError) as exc:
                addrs, err = [], str(exc)
            f["stub.all"][name] = {"ms": (time.monotonic() - t0) * 1000.0,
                                   "addrs": addrs, "error": err}
        ms = [v["ms"] for v in f["stub.all"].values()]
        f["stub.slowest_ms"] = max(ms) if ms else None

    # The worst case an application waits before every resolver has been
    # tried, straight out of the configuration.  Meaningless when --server
    # replaced the list: the configured timeout would be multiplied by a
    # server count the stub knows nothing about.
    f["budget.worst_s"] = None
    if not args.servers and f["res.timeout"] and f["res.attempts"] \
            and f["res.nameserver_count"]:
        f["budget.worst_s"] = (f["res.timeout"] * f["res.attempts"]
                               * f["res.nameserver_count"])
    return f


DATA_SUFFIXES = ("csv", "json", "tsv", "txt", "out", "log", "dat", "tmp")


def _looks_like_a_hostname(value):
    """A dotted name with no path separator and no data-file suffix."""
    if os.sep in value or value.startswith("."):
        return False
    if "." not in value:
        return False
    return value.rsplit(".", 1)[1].lower() not in DATA_SUFFIXES


def _is_ip(addr):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, addr)
            return True
        except (OSError, ValueError):
            pass
    return False


def _split_server(spec):
    """'10.0.0.53', '10.0.0.53:5353', '[::1]:5353' -> (addr, port)."""
    spec = spec.strip()
    if spec.startswith("["):
        addr, _, rest = spec[1:].partition("]")
        port = 53
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return addr, port
    if spec.count(":") == 1:
        addr, _, p = spec.partition(":")
        if p.isdigit():
            return addr, int(p)
    return spec, 53


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

Rule = namedtuple("Rule", "id title needs level say fix why")

MISSING_REASONS = {
    "res.nameservers": "resolv.conf could not be read",
    "res.search": "resolv.conf could not be read",
    "res.ndots": "resolv.conf could not be read",
    "srv.dead": "no nameservers to probe",
    "srv.first_dead": "no nameservers to probe",
    "srv.slowest_ms": "no nameserver answered, so there is no latency to "
                      "report",
    "names.disagree": "no names given -- pass a name your application "
                      "actually looks up",
    "names.failed": "no names given -- pass a name your application "
                    "actually looks up",
    "names.partial": "no names given -- pass a name your application "
                     "actually looks up",
    "names.aaaa_stall": "no names given -- pass a name your application "
                        "actually looks up",
    "names.hosts_shadow": "no names given, or no hosts file to compare "
                          "against",
    "tcp.blocked": "no names given, so no answer was large enough to need "
                   "TCP",
    "search.wasted": "nothing to measure: no unqualified name, no search "
                     "list, or --no-search",
    "stub.slowest_ms": "getaddrinfo not timed (--no-stub, or no names "
                       "given)",
    "budget.worst_s": "resolv.conf could not be read, or --server replaced "
                      "the configured list",
    "res.stub": "no local stub resolver in use",
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


def _join(items, n=4):
    items = list(items)
    shown = " ".join(str(i) for i in items[:n])
    return shown + ("" if len(items) <= n else " (+%d)" % (len(items) - n))


# 1 ------------------------------------------------------------------------
@rule("NO_NAMESERVERS", "no resolvers configured", ("res.nameservers",),
      "With no nameserver line every lookup fails immediately, and the "
      "error the application prints names the host it wanted rather than "
      "the file that is empty.")
def _no_nameservers():
    def level(f):
        return CRITICAL if not f["res.nameservers"] else None

    def say(f):
        return "%s lists no nameserver" % f.get("res.path", RESOLV_CONF)

    def fix(f):
        return ("Nothing on this box can resolve anything.  If this is a "
                "container, its resolv.conf is written at start-up and the "
                "runtime failed to do it; if it is a VM, check whether the "
                "DHCP client or systemd-resolved owns this file before "
                "editing it by hand, or your edit will be overwritten at "
                "the next lease.")
    return level, say, fix


# 2 ------------------------------------------------------------------------
@rule("NS_ALL_DEAD", "no resolver answered", ("srv.dead", "srv.count"),
      "Every configured resolver failing is a different problem from a slow "
      "one: nothing resolves at all, and every timeout downstream is a "
      "consequence rather than a separate fault.")
def _ns_all_dead():
    def level(f):
        if f["srv.count"] and len(f["srv.dead"]) == f["srv.count"]:
            return CRITICAL
        return None

    def say(f):
        return ("all %d configured resolver%s failed to answer: %s"
                % (f["srv.count"], "" if f["srv.count"] == 1 else "s",
                   _join(f["srv.dead"])))

    def fix(f):
        srv = f["srv.dead"][0] if f["srv.dead"] else "the server"
        return ("Everything that resolves a name on this box is failing, so "
                "expect unrelated-looking timeouts everywhere.  Check the "
                "path before the servers: `%s` is either gone, or UDP 53 to "
                "it is being dropped -- those look identical from here and "
                "are fixed in different places." % srv)
    return level, say, fix


# 3 ------------------------------------------------------------------------
@rule("NS_DEAD", "a resolver not answering",
      ("srv.dead", "srv.count", "srv.first_dead"),
      "The stub tries the nameservers in order, so a dead one earlier in "
      "the list costs every single lookup a full timeout before the working "
      "one is reached -- and every dig against the working one says DNS is "
      "fine.")
def _ns_dead():
    def level(f):
        if not f["srv.dead"] or len(f["srv.dead"]) == f["srv.count"]:
            return None                 # none, or NS_ALL_DEAD's business
        return CRITICAL if f["srv.first_dead"] else WARN

    def say(f):
        return ("%d of %d resolvers did not answer: %s%s"
                % (len(f["srv.dead"]), f["srv.count"], _join(f["srv.dead"]),
                   " -- including the first one tried"
                   if f["srv.first_dead"] else ""))

    def fix(f):
        s = ("Resolution still works, which is exactly why this survives "
             "for months.  ")
        if f["srv.first_dead"]:
            wait = _g(f, "res.timeout", 5)
            s += ("The dead server is first in the list, so every lookup on "
                  "this box waits %s second%s for it before asking the one "
                  "that works.  That is your latency, and no query against "
                  "the working server will ever show it.  "
                  % (wait, "" if wait == 1 else "s"))
        s += ("Remove it from %s, or fix it -- and if that file is generated, "
              "fix whatever generates it." % f.get("res.path", RESOLV_CONF))
        return s
    return level, say, fix


# 4 ------------------------------------------------------------------------
@rule("NS_DISAGREE", "resolvers disagree", ("names.disagree",),
      "Two resolvers giving different answers for one name is split "
      "horizon, a stale cache, or a resolver pointing somewhere forgotten "
      "-- and which answer you get depends on which server replied first.")
def _ns_disagree():
    def level(f):
        return CRITICAL if f["names.disagree"] else None

    def say(f):
        return ("%d name%s answered differently by different resolvers: %s"
                % (len(f["names.disagree"]),
                   "" if len(f["names.disagree"]) == 1 else "s",
                   _join(f["names.disagree"])))

    def fix(f):
        name = f["names.disagree"][0]
        per = _g(f, "names.all", {}).get(name, {}).get("answers", {})
        detail = "; ".join("%s -> %s" % (k, ",".join(v) or "nothing")
                           for k, v in sorted(per.items()))
        return ("%s: %s.  Which of those you get depends on which server "
                "answers first, so this box works intermittently and the "
                "failures look random.  Usually split horizon (an internal "
                "and an external view of the same zone) or one resolver "
                "holding a stale record -- compare the SOA serials before "
                "assuming which is right." % (name, detail))
    return level, say, fix


# 5 ------------------------------------------------------------------------
@rule("NAME_FAILS", "a name does not resolve",
      ("names.failed", "names.partial"),
      "A name that resolves nowhere is a different fault from one that "
      "resolves on some resolvers and not others, and the second is the "
      "one that produces intermittent failures nobody can reproduce.")
def _name_fails():
    def level(f):
        if f["names.failed"]:
            return CRITICAL
        if f["names.partial"]:
            return WARN
        return None

    def say(f):
        if f["names.failed"]:
            return ("%s resolved on no server at all"
                    % _join(f["names.failed"]))
        return ("%s resolved on some resolvers and not others"
                % _join(f["names.partial"]))

    def fix(f):
        if f["names.failed"]:
            return ("Every resolver was asked and none had an answer, so "
                    "this is the zone rather than this box: check the name "
                    "is spelled as the application spells it, and that the "
                    "record exists in the zone you think it does.")
        name = f["names.partial"][0]
        return ("%s answers on some resolvers and not others, which is the "
                "worst shape this fault takes: the application fails "
                "roughly half the time and retries succeed, so it reads as "
                "flakiness rather than DNS.  Fix the resolver that is "
                "missing the zone, or stop listing it." % name)
    return level, say, fix


# 6 ------------------------------------------------------------------------
@rule("TCP53_BLOCKED", "tcp 53 blocked", ("tcp.blocked",),
      "A firewall passing UDP 53 and dropping TCP 53 works perfectly until "
      "an answer outgrows a datagram, and then one name fails while every "
      "other name on the same server is fine.")
def _tcp_blocked():
    def level(f):
        return WARN if f["tcp.blocked"] else None

    def say(f):
        return ("%s truncated a reply and then refused the TCP retry"
                % _join(f["tcp.blocked"]))

    def fix(f):
        return ("The server asked the query to be repeated over TCP, which "
                "is normal for a large answer, and TCP 53 did not connect.  "
                "That is a firewall rule that has been wrong since it was "
                "written and has never mattered until now -- open TCP 53 "
                "alongside UDP 53.  Until then, exactly the names with big "
                "answers fail and everything else works, which is why this "
                "gets blamed on the application.")
    return level, say, fix


# 7 ------------------------------------------------------------------------
@rule("AAAA_STALL", "aaaa queries dropped", ("names.aaaa_stall",),
      "glibc asks for A and AAAA together and waits for both, so a dropped "
      "AAAA adds the full resolver timeout to a lookup that then succeeds "
      "-- the answer is right and slow, which is the hardest kind to see.")
def _aaaa_stall():
    def level(f):
        return WARN if f["names.aaaa_stall"] else None

    def say(f):
        return ("A answered but AAAA never did for %s"
                % _join(f["names.aaaa_stall"]))

    def fix(f):
        return ("Almost always a middlebox dropping the AAAA query rather "
                "than answering it -- an honest NODATA costs nothing, a "
                "silent drop costs the whole timeout on every lookup.  Fix "
                "the middlebox; `options single-request-reopen` in %s is "
                "the workaround if you cannot, and `no-aaaa` on glibc 2.36 "
                "and newer if this box genuinely has no v6."
                % f.get("res.path", RESOLV_CONF))
    return level, say, fix


# 8 ------------------------------------------------------------------------
@rule("NS_SLOW", "a resolver is slow", ("srv.slowest_ms",),
      "Every request an application makes starts with a lookup, so resolver "
      "latency is added to everything and attributed to nothing.")
def _ns_slow():
    def level(f):
        if f["srv.slowest_ms"] >= 500:
            return CRITICAL
        if f["srv.slowest_ms"] >= 100:
            return WARN
        return None

    def say(f):
        return ("%s answered in %.0fms%s"
                % (f.get("srv.slowest"), f["srv.slowest_ms"],
                   " (fastest was %.0fms)" % f["srv.fastest_ms"]
                   if f.get("srv.fastest_ms") is not None else ""))

    def fix(f):
        return ("That latency is paid by every request this box makes, "
                "before any of them start.  A resolver on the far side of a "
                "WAN link is the usual cause; a local caching resolver in "
                "front of it turns a repeated lookup into a memory read.  "
                "Confirm the path itself with `netmesh check <this host> "
                "%s` before moving anything." % f.get("srv.slowest", ""))
    return level, say, fix


# 9 ------------------------------------------------------------------------
@rule("TIMEOUT_BUDGET", "the timeout budget is huge", ("budget.worst_s",),
      "timeout x attempts x servers is what an application waits before DNS "
      "gives up, and the default arithmetic exceeds every client timeout it "
      "sits behind.")
def _timeout_budget():
    def level(f):
        # The stock defaults are timeout:5 attempts:2, so one resolver is
        # already 10s and two are 20s.  Firing there would mean firing on
        # nearly every box in existence, which is noise rather than a
        # finding -- and when a server really is dead, NS_DEAD says so with
        # the wait spelled out.  These thresholds are the point past which
        # somebody has configured something unusual.
        if f["budget.worst_s"] >= 30:
            return WARN
        if f["budget.worst_s"] >= 20:
            return INFO
        return None

    def say(f):
        return ("worst case %ds before resolution fails (timeout:%s x "
                "attempts:%s x %d servers)"
                % (f["budget.worst_s"], _g(f, "res.timeout", 5),
                   _g(f, "res.attempts", 2), _g(f, "srv.count", 0)))

    def fix(f):
        return ("Nothing waits that long: the health check, the load "
                "balancer and the client library all give up first, so a "
                "DNS problem surfaces as an application timeout with no DNS "
                "error anywhere.  `options timeout:1 attempts:2` in %s "
                "bounds it, and is nearly always right on a local network."
                % f.get("res.path", RESOLV_CONF))
    return level, say, fix


# 10 -----------------------------------------------------------------------
@rule("SEARCH_COST", "the search list costs",
      ("search.wasted",),
      "An unqualified name is tried against every search domain in turn, so "
      "a long list is several NXDOMAINs before the query you meant -- paid "
      "on every lookup, for ever.")
def _search_cost():
    def level(f):
        if f["search.wasted"] >= 3:
            return WARN
        if f["search.wasted"] >= 1:
            return INFO
        return None

    def say(f):
        return ("%d wasted quer%s before the real one%s"
                % (f["search.wasted"], "y" if f["search.wasted"] == 1
                   else "ies",
                   ", costing %.0fms" % f["search.wasted_ms"]
                   if f.get("search.wasted_ms") else ""))

    def fix(f):
        return ("Each of those is a full round trip to a resolver, on every "
                "lookup of that name, and it is invisible in every trace "
                "that starts after the connection.  Use fully qualified "
                "names in configuration -- a trailing dot stops the search "
                "list dead -- or shorten `search` in %s.  Kubernetes ships "
                "ndots:5, which turns every external name into five of "
                "these." % f.get("res.path", RESOLV_CONF))
    return level, say, fix


# 11 -----------------------------------------------------------------------
@rule("HOSTS_SHADOW", "pinned in the hosts file",
      ("names.hosts_shadow",),
      "An /etc/hosts entry silently beats DNS on this box alone, so the "
      "name resolves one way here and another way everywhere else, and "
      "nothing in any log says why.")
def _hosts_shadow():
    def level(f):
        return WARN if f["names.hosts_shadow"] else None

    def say(f):
        name = f["names.hosts_shadow"][0]
        entry = _g(f, "names.all", {}).get(name, {}).get("hosts") or []
        return ("%s is %s in the hosts file and something else in DNS"
                % (name, ",".join(entry)))

    def fix(f):
        name = f["names.hosts_shadow"][0]
        return ("The hosts file wins, so this box ignores DNS for %s "
                "entirely -- including any change made since the entry was "
                "written.  Someone added it during an incident and it "
                "outlived the incident, which is how this always happens.  "
                "Remove it, or make DNS agree with it deliberately."
                % name)
    return level, say, fix


# 12 -----------------------------------------------------------------------
@rule("STUB_SLOW", "getaddrinfo is the slow part",
      ("stub.slowest_ms", "srv.fastest_ms"),
      "The gap between what a resolver answers in and what the C library "
      "takes is the cost of the configuration -- search domains, a dead "
      "server, a v6 query nobody answers -- rather than of the network.")
def _stub_slow():
    def level(f):
        gap = f["stub.slowest_ms"] - f["srv.fastest_ms"]
        if gap < 50:
            return None
        if f["stub.slowest_ms"] >= 5 * max(1.0, f["srv.fastest_ms"]):
            return WARN
        return INFO if gap >= 200 else None

    def say(f):
        return ("getaddrinfo took %.0fms where the fastest resolver "
                "answered in %.0fms"
                % (f["stub.slowest_ms"], f["srv.fastest_ms"]))

    def fix(f):
        return ("The resolver is not the slow part -- the path to it is "
                "fine and the library is still slow, so the cost is in the "
                "configuration.  The findings above name the specific one: "
                "a dead server earlier in the list, the search-domain walk, "
                "or an unanswered AAAA.  This line is the total your "
                "application actually pays.")
    return level, say, fix


# 13 -----------------------------------------------------------------------
@rule("RESOLV_STUB", "measuring a local stub",
      ("res.stub",),
      "Measuring 127.0.0.53 measures a cache on this box: it says nothing "
      "about the servers behind it, and reporting its healthy 0.2ms without "
      "saying so would be the most misleading line in the report.")
def _resolv_stub():
    def level(f):
        return INFO if f["res.stub"] else None

    def say(f):
        return ("%s is %s -- the upstreams behind it were not measured%s"
                % (", ".join(sorted(f["res.stub"])),
                   sorted(f["res.stub"].values())[0],
                   "; it forwards to %s" % " ".join(f["res.upstreams"])
                   if f.get("res.upstreams") else ""))

    def fix(f):
        up = f.get("res.upstreams")
        if up:
            return ("Everything above describes the local cache.  To "
                    "measure what it forwards to, ask them directly: "
                    "`%s --server %s`.  A fast stub in front of a sick "
                    "upstream looks perfect here until the cache misses."
                    % (PROG, up[0]))
        return ("Everything above describes the local cache rather than the "
                "servers it forwards to, and a fast stub in front of a sick "
                "upstream looks perfect until the cache misses.  "
                "`resolvectl status` names the real servers; pass one to "
                "--server to measure it.")
    return level, say, fix


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------
#
# Same discipline as why-slow: causes first.  A dead resolver explains the
# slow getaddrinfo it causes, so naming the latency would send the reader
# looking for a network problem that is not there.
VERDICT_PRECEDENCE = [
    "NO_NAMESERVERS", "NS_ALL_DEAD", "NS_DISAGREE", "NAME_FAILS", "NS_DEAD",
    "TCP53_BLOCKED", "AAAA_STALL", "NS_SLOW", "SEARCH_COST",
    "TIMEOUT_BUDGET", "HOSTS_SHADOW", "STUB_SLOW", "RESOLV_STUB",
]

Finding = namedtuple("Finding", "rule severity say fix")

# Not in RULES: it is not a rule about DNS, it is a statement that no rule
# could be evaluated at all.
NOTHING_CHECKED = Rule("NOTHING_CHECKED", "nothing could be checked", (),
                       None, None, None,
                       "Every rule was skipped for want of the facts it "
                       "needs, so the run says nothing about resolution.")


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
        if not facts.get("probe.names"):
            return ("The resolvers on this box answer, and quickly.  That is "
                    "all this run checked -- pass a name your application "
                    "looks up to compare what they say about it.")
        return ("DNS is not the problem here: every resolver answered, they "
                "agree, and the library is no slower than they are.")
    top = findings[0]
    consequences = {
        "NO_NAMESERVERS": "Nothing on this box can resolve anything -- "
                          "there is no resolver configured at all.",
        "NS_ALL_DEAD": "No resolver on this box is answering.  Every "
                       "timeout you are chasing downstream starts here.",
        "NS_DISAGREE": "Your resolvers disagree about a name, so this box "
                       "gets a different answer depending on which one "
                       "replies first.  That is the intermittency.",
        "NAME_FAILS": "A name your application needs does not resolve.",
        "NS_DEAD": "A configured resolver is dead and the others are "
                   "covering for it.  Everything still works, and every "
                   "lookup pays a timeout to find that out.",
        "AAAA_STALL": "AAAA queries are being dropped rather than answered, "
                      "so every lookup here is correct and slow.",
        "TCP53_BLOCKED": "Large DNS answers cannot get through, so exactly "
                         "the names with big answers fail and every other "
                         "name works.",
        "NS_SLOW": "Your resolvers answer, slowly.  That latency is paid "
                   "before every request this box makes.",
        "NOTHING_CHECKED": "Nothing about resolution was measured here, so "
                           "this says nothing about whether DNS works.  It "
                           "is not a clean bill of health -- it is an empty "
                           "one.",
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
        bits.append("%d resolver%s" % (facts["srv.count"],
                                       "" if facts["srv.count"] == 1 else "s"))
    if facts.get("probe.names"):
        bits.append("%d name%s" % (len(facts["probe.names"]),
                                   "" if len(facts["probe.names"]) == 1
                                   else "s"))
    if facts.get("probe.source"):
        bits.append("from %s" % facts["probe.source"])
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

    if not args.quiet:
        out.append("  %s" % C.bold("RESOLVERS"))
        for name in facts.get("srv.list") or []:
            d = (facts.get("srv.all") or {}).get(name) or {}
            if d.get("alive"):
                state = "%6.1fms  %s" % (d.get("rtt_ms") or 0.0,
                                         d.get("rcode") or "")
            else:
                state = "no answer  (%s)" % (d.get("error") or "unknown")
            first = " <- tried first" if facts.get("srv.list", [None])[0] \
                == name else ""
            out.append("    %-22s %s%s" % (name, state, first))
        if facts.get("res.search"):
            out.append("    %-22s search %s  ndots:%s"
                       % ("", " ".join(facts["res.search"]),
                          facts.get("res.ndots")))
        out.append("")

        names_all = facts.get("names.all") or {}
        if names_all:
            out.append("  %s" % C.bold("NAMES"))
            for name in facts.get("probe.names") or []:
                entry = names_all.get(name) or {}
                for srv, answers in sorted((entry.get("answers") or {}).items()):
                    out.append("    %-22s %-18s %s"
                               % (name[:22], srv[:18],
                                  ", ".join(answers) if answers
                                  else "no answer"))
                if entry.get("hosts"):
                    out.append("    %-22s %-18s %s"
                               % (name[:22], "/etc/hosts",
                                  ", ".join(entry["hosts"])))
                stub = (facts.get("stub.all") or {}).get(name)
                if stub:
                    out.append("    %-22s %-18s %s  (%.0fms)"
                               % (name[:22], "getaddrinfo",
                                  ", ".join(stub["addrs"]) or stub.get("error")
                                  or "no answer", stub["ms"]))
            out.append("")

    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    if not shown:
        if not facts.get("probe.names"):
            out.append("    %s" % _wrap(
                "Every configured resolver answered. That is a liveness "
                "check, not a verdict on your zone -- run this again with "
                "the name your application actually looks up, and it will "
                "compare what each resolver says about it.", 4))
            out.append("")
            out.append("    %s %s" % (PROG, "db01.example.com"))
        else:
            out.append("    %s" % _wrap(
                "DNS is not your problem. Every resolver answered, they "
                "agree on every name asked about, and getaddrinfo is no "
                "slower than the resolvers are. Look at the box and the "
                "network next.", 4))
            out.append("")
            out.append("    %s" % _wrap(
                "why-slow                               (is it the box?)", 4))
            out.append("    %s" % _wrap(
                "netmesh check <this host> <the peer>   (is it the network?)",
                4))
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
    p.add_argument("names", nargs="*", metavar="NAME",
                   help="names to look up; with none, the resolvers are "
                        "checked for liveness and latency only")
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("--server", action="append", dest="servers",
                   metavar="ADDR[:PORT]",
                   default=(_env("SERVERS").split(",") if _env("SERVERS")
                            else None))
    p.add_argument("--type", dest="types", default=_env("TYPES", "A,AAAA"))
    p.add_argument("--timeout", type=float, default=float(_env("TIMEOUT", 2.0)))
    p.add_argument("--attempts", type=int, default=int(_env("ATTEMPTS", 1)))
    p.add_argument("--no-search", action="store_true")
    p.add_argument("--no-stub", action="store_true")
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
    p.add_argument("--resolv-conf", default=_env("CONF", RESOLV_CONF))
    p.add_argument("--hosts-file", default=_env("HOSTS", HOSTS_FILE))
    p.add_argument("--no-exec", action="store_true")
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
    args.types = [t.strip().upper() for t in args.types.split(",")
                  if t.strip()]
    for t in args.types:
        if t not in QTYPES:
            die("unknown record type %r (known: %s)"
                % (t, ", ".join(sorted(QTYPES))))

    # `resolve --csv db01.example.com` is the same trap: --csv takes an
    # optional path, so it swallows the name and the run silently resolves
    # nothing while writing a file called db01.example.com.  Only a value
    # that reads as a hostname is refused -- report.csv and run.json are
    # ordinary output paths and pass straight through.
    for flag, value in (("--csv", args.csv), ("--json", args.json)):
        if value and not args.names and _looks_like_a_hostname(value):
            die("%s was given %r, which reads as a name rather than a file, "
                "so nothing would be looked up.  The name comes first: "
                "`%s %s %s`.  If it really is a filename, write it as "
                "%s=./%s." % (flag, value, PROG, value, flag, flag, value))

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
        facts = collect_facts(args, args.names)

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
                            "so this says nothing about whether DNS works.  "
                            "Run %s where the resolvers are, or pass a file "
                            "written by --facts." % PROG)]

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
# mesh, and `agree script ./resolve.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
