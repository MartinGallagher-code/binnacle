# test_helper.bash -- shared harness for the binnacle tools.
#
# Sourced by every test_*.sh.  Provides:
#   run_test NAME BODY      run one case in a subshell with a fresh temp dir
#   assert_eq/contains/not_contains/status/file_exists/no_file/between
#   install_fake_ssh        an ssh+scp pair that run "remote" commands locally
#   fake_host NAME          create a sandbox root for one fake host
#   gen_log FILE            a synthetic log with a known planted structure
#   facts_json FILE k=v...  a why-slow fact file built from defaults
#
# Every case runs in a subshell, so one failed assertion cannot abort the
# rest of the file, and each gets its own $TEST_TMPDIR so cases cannot see
# each other's leftovers.

BINNACLE_DIR="${BINNACLE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../binnacle" && pwd)}"
PY="${PY:-python3}"

TESTS_RUN=0
TESTS_FAILED=0
FAILED_NAMES=()

setup_env() {
    TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/sharedtools-test.XXXXXX")"
    export TEST_TMPDIR
    export FAKE_BIN="$TEST_TMPDIR/bin"
    export FAKE_ROOT="$TEST_TMPDIR/hosts"
    export FAKE_SSH_LOG="$TEST_TMPDIR/ssh.log"
    mkdir -p "$FAKE_BIN" "$FAKE_ROOT"
    : > "$FAKE_SSH_LOG"
    # A stray variable from the caller's shell must never change a result.
    unset NETMESH_MESH NETMESH_USER NETMESH_JOBS NETMESH_REMOTE_DIR
    unset NETMESH_PYTHON NETMESH_SSH NETMESH_SCP NETMESH_REPORTS
    unset AGREE_HOSTS AGREE_JOBS AGREE_TIMEOUT AGREE_USER AGREE_SSH
    unset AGREE_SCP AGREE_REMOTE_DIR AGREE_LIMIT
    unset WHY_SLOW_INTERVAL WHY_SLOW_TOP WHY_SLOW_MIN_SEVERITY
    unset WHY_SLOW_BOOT_WINDOW WHY_SLOW_PROC WHY_SLOW_SYS WHY_SLOW_SSH
    unset LOGTRIAGE_TOP LOGTRIAGE_MAX_TEMPLATES LOGTRIAGE_BUCKET
    unset LOGTRIAGE_BASELINE LOGTRIAGE_WEIGHTS
    unset SKEW_SERVERS SKEW_TIMEOUT SKEW_ATTEMPTS SKEW_SAMPLES
    unset SKEW_MAX_OFFSET SKEW_WARN_OFFSET SKEW_MIN_SEVERITY
    unset SKEW_CHRONY_CONF SKEW_NTP_CONF SKEW_TIMESYNCD_CONF
    unset SKEW_PROC SKEW_RTC
    export LANG=C
}

teardown_env() {
    # A crashed case must not leave probing agents behind.
    pkill -f "[n]etmesh[.]py agent --mesh $TEST_TMPDIR" 2>/dev/null || true
    if [ -f "$TEST_TMPDIR/dns.pid" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null || true
        done < "$TEST_TMPDIR/dns.pid"
    fi
    if [ -f "$TEST_TMPDIR/ntp.pid" ]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null || true
        done < "$TEST_TMPDIR/ntp.pid"
    fi
    [ -n "$TEST_TMPDIR" ] && rm -rf "$TEST_TMPDIR"
}

run_test() {
    local name="$1"; shift
    TESTS_RUN=$((TESTS_RUN + 1))
    local out rc
    out="$(
        setup_env
        trap teardown_env EXIT
        set -e
        "$@" 2>&1
    )"
    rc=$?
    if [ $rc -eq 0 ]; then
        printf '  ok   %s\n' "$name"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        FAILED_NAMES+=("$name")
        printf '  FAIL %s\n' "$name"
        printf '%s\n' "$out" | sed 's/^/       /'
    fi
}

finish() {
    printf '\n%d run, %d failed\n' "$TESTS_RUN" "$TESTS_FAILED"
    if [ "$TESTS_FAILED" -gt 0 ]; then
        printf 'failed: %s\n' "${FAILED_NAMES[*]}"
        exit 1
    fi
    exit 0
}

# -- assertions -------------------------------------------------------------

_fail() { printf 'assertion failed: %s\n' "$1" >&2; return 1; }

assert_eq() {
    [ "$1" = "$2" ] || _fail "expected '$2', got '$1'"
}

assert_contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) _fail "expected output to contain '$2'; got: $(printf '%s' "$1" | head -c 600)" ;;
    esac
}

assert_not_contains() {
    case "$1" in
        *"$2"*) _fail "expected output NOT to contain '$2'" ;;
        *) return 0 ;;
    esac
}

assert_status() {
    [ "$1" -eq "$2" ] || _fail "expected exit status $2, got $1"
}

assert_file_exists() {
    [ -f "$1" ] || _fail "expected file to exist: $1"
}

assert_no_file() {
    [ ! -e "$1" ] || _fail "expected no such path: $1"
}

assert_between() {
    # assert_between VALUE LOW HIGH
    "$PY" - "$1" "$2" "$3" <<'EOF' || _fail "value $1 not in [$2,$3]"
import sys
v, lo, hi = (float(x) for x in sys.argv[1:4])
sys.exit(0 if lo <= v <= hi else 1)
EOF
}

# -- fake ssh / scp ---------------------------------------------------------
#
# The shim executes the "remote" command locally inside $FAKE_ROOT/<host>,
# so a whole deploy/run/collect lifecycle can be exercised with no network
# and no second machine.  Every invocation is logged to $FAKE_SSH_LOG so a
# test can assert what was invoked as well as what came back.

install_fake_ssh() {
    cat > "$FAKE_BIN/ssh" <<'SHIM'
#!/bin/bash
args=(); host=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2;;
    -p) shift 2;;
    *) if [ -z "$host" ]; then host="$1"; else args+=("$1"); fi; shift;;
  esac
done
host="${host#*@}"
printf 'ssh %s %s\n' "$host" "${args[*]}" >> "$FAKE_SSH_LOG"
if [ -f "$FAKE_ROOT/$host/.unreachable" ]; then
  echo "ssh: connect to host $host port 22: Connection timed out" >&2
  exit 255
fi
[ -d "$FAKE_ROOT/$host" ] || { echo "ssh: no such host $host" >&2; exit 255; }
cd "$FAKE_ROOT/$host" || exit 255
exec bash -c "${args[*]}"
SHIM
    cat > "$FAKE_BIN/scp" <<'SHIM'
#!/bin/bash
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2;; -P) shift 2;; -q) shift;;
    *) args+=("$1"); shift;;
  esac
done
n=${#args[@]}; dest="${args[$((n-1))]}"; srcs=("${args[@]:0:$((n-1))}")
# Real scp splits the destination on the last colon, so an IPv6 literal
# has to arrive bracketed; strip the brackets the way scp does.
if [[ "$dest" == *"["*"]:"* ]]; then
  host="${dest#*[}"; host="${host%%]*}"; path="${dest#*]:}"
else
  host="${dest%%:*}"; path="${dest#*:}"
fi
host="${host#*@}"
printf 'scp %s %s\n' "$host" "$path" >> "$FAKE_SSH_LOG"
[ -f "$FAKE_ROOT/$host/.unreachable" ] && exit 1
# Match real scp: a destination ending in / (or naming an existing
# directory, or receiving several sources) is a directory; anything else
# names the destination file.
target="$FAKE_ROOT/$host/$path"
if [ "${path: -1}" = "/" ] || [ -d "$target" ] || [ "${#srcs[@]}" -gt 1 ]; then
  mkdir -p "$target" 2>/dev/null
  cp "${srcs[@]}" "$target/" 2>/dev/null || exit 1
else
  mkdir -p "$(dirname "$target")" 2>/dev/null
  cp "${srcs[0]}" "$target" 2>/dev/null || exit 1
fi
SHIM
    chmod +x "$FAKE_BIN/ssh" "$FAKE_BIN/scp"
}

fake_host() {
    mkdir -p "$FAKE_ROOT/$1"
}

fake_host_unreachable() {
    mkdir -p "$FAKE_ROOT/$1"
    touch "$FAKE_ROOT/$1/.unreachable"
}

ssh_calls() {
    # grep -c exits 1 on no matches, so a naive `|| echo 0` prints the
    # count AND a zero.  Count lines instead.
    local n=0
    if [ -f "$FAKE_SSH_LOG" ]; then
        n=$(grep -c '^ssh ' "$FAKE_SSH_LOG" 2>/dev/null) || n=0
    fi
    printf '%s\n' "$n"
}

# -- fixtures ---------------------------------------------------------------

# gen_log FILE -- a synthetic syslog with a KNOWN planted structure:
#   3000 steady sshd auth failures across the whole window
#   1200 novel kernel EXT4 errors, only in the last third
#      3 novel critical OOM kills
#     40 java tracebacks (4 lines each) -- must collapse to ONE template
gen_log() {
    "$PY" - "$1" <<'EOF'
import sys
from datetime import datetime, timedelta
path = sys.argv[1]
base = datetime(2026, 1, 7, 4, 0, 0)
rows = []
for i in range(3000):
    t = base + timedelta(seconds=i * 12.0)
    rows.append((t, "%s web01 sshd[%d]: Failed password for invalid user admin "
                    "from 10.0.%d.%d port %d ssh2"
                 % (t.strftime("%b %e %H:%M:%S"), 1000 + i % 500,
                    i % 255, i % 254, 40000 + i % 20000)))
start = base + timedelta(hours=7)
for i in range(1200):
    t = start + timedelta(seconds=i * 3.0)
    rows.append((t, "%s web01 kernel: EXT4-fs error (device sda1): "
                    "ext4_find_entry:1455: inode #%d: comm postgres"
                 % (t.strftime("%b %e %H:%M:%S"), 131072 + i)))
for i in range(3):
    t = start + timedelta(minutes=2, seconds=i * 40)
    rows.append((t, "%s web01 kernel: Out of memory: Killed process %d (java)"
                 % (t.strftime("%b %e %H:%M:%S"), 1842 + i)))
for i in range(40):
    t = start + timedelta(minutes=5, seconds=i * 3)
    ts = t.strftime("%b %e %H:%M:%S")
    rows.append((t, "%s web01 app[9911]: java.lang.NullPointerException: boom" % ts))
    rows.append((t, "\tat com.example.Foo.bar(Foo.java:42)"))
    rows.append((t, "\tat com.example.Baz.qux(Baz.java:17)"))
    rows.append((t, "\t... 14 more"))
rows.sort(key=lambda r: r[0])
with open(path, "w") as f:
    f.write("\n".join(r[1] for r in rows) + "\n")
EOF
}

# start_fake_dns PORT JSON -- a UDP (and optionally TCP) DNS responder on
# 127.0.0.1, so resolve's wire code can be exercised for real without a
# network or a resolver.  The responder builds its replies independently of
# the tool's own packet code, which is the point: a shared bug would cancel
# out.  JSON keys:
#   records     {"name": {"A": ["10.0.0.7"], "AAAA": [...]}}, "." is served
#               automatically so a liveness probe answers
#   nxdomain    ["name", ...]        answer NXDOMAIN
#   drop        ["AAAA"]             never answer these types (a black hole)
#   truncate    ["name"]             reply with TC set, and refuse TCP
#   delay_ms    N                    wait this long before answering
#   tcp         true/false           listen on TCP 53 as well (default true)
# Writes the pid to $TEST_TMPDIR/dns.pid; teardown_env kills it.
start_fake_dns() {
    local port="$1" cfg="$2"
    cat > "$TEST_TMPDIR/dnsd.py" <<'EOF'
import json, socket, struct, sys, threading, time

cfg = json.loads(sys.argv[2])
port = int(sys.argv[1])
records = cfg.get("records", {})
nxdomain = set(n.lower() for n in cfg.get("nxdomain", []))
drop = set(cfg.get("drop", []))
truncate = set(n.lower() for n in cfg.get("truncate", []))
delay = cfg.get("delay_ms", 0) / 1000.0
TYPES = {1: "A", 28: "AAAA", 2: "NS"}


def decode_name(data, pos):
    labels = []
    while True:
        n = data[pos]
        pos += 1
        if n == 0:
            break
        labels.append(data[pos:pos + n].decode())
        pos += n
    return ".".join(labels), pos


def encode_name(name):
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode()
    return out + b"\x00"


def build_reply(data):
    qid, flags, qd = struct.unpack("!HHH", data[:6])
    name, pos = decode_name(data, 12)
    qtype, qclass = struct.unpack("!HH", data[pos:pos + 4])
    pos += 4
    tname = TYPES.get(qtype, str(qtype))
    if tname in drop:
        return None
    question = data[12:pos]
    if name.lower() in truncate:
        head = struct.pack("!HHHHHH", qid, 0x8380, 1, 0, 0, 0)
        return head + question
    if name.lower() in nxdomain:
        head = struct.pack("!HHHHHH", qid, 0x8183, 1, 0, 0, 0)
        return head + question
    if name == "":
        vals = ["a.root-servers.net"] if tname == "NS" else []
    else:
        vals = records.get(name.lower(), {}).get(tname, [])
    body = b""
    for v in vals:
        if tname == "A":
            rd = socket.inet_pton(socket.AF_INET, v)
        elif tname == "AAAA":
            rd = socket.inet_pton(socket.AF_INET6, v)
        else:
            rd = encode_name(v)
        body += b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 300, len(rd)) + rd
    head = struct.pack("!HHHHHH", qid, 0x8180, 1, len(vals), 0, 0)
    return head + question + body


def serve_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    while True:
        data, peer = s.recvfrom(4096)
        reply = build_reply(data)
        if reply is None:
            continue
        if delay:
            time.sleep(delay)
        s.sendto(reply, peer)


def serve_tcp():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    while True:
        conn, _ = s.accept()
        try:
            head = conn.recv(2)
            length = struct.unpack("!H", head)[0]
            data = conn.recv(length)
            reply = build_reply(data)
            if reply is not None:
                conn.sendall(struct.pack("!H", len(reply)) + reply)
        except Exception:
            pass
        finally:
            conn.close()


if cfg.get("tcp", True):
    threading.Thread(target=serve_tcp, daemon=True).start()
serve_udp()
EOF
    "$PY" "$TEST_TMPDIR/dnsd.py" "$port" "$cfg" &
    echo $! >> "$TEST_TMPDIR/dns.pid"
    # Wait for the socket rather than sleeping a guessed interval.
    local i=0
    while [ $i -lt 50 ]; do
        if "$PY" - "$port" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(0.2)
try:
    s.sendto(b"\x00\x00\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x01",
             ("127.0.0.1", int(sys.argv[1])))
    s.recvfrom(4096)
except Exception:
    sys.exit(1)
EOF
        then
            return 0
        fi
        i=$((i + 1))
    done
    echo "fake dns on port $port never came up" >&2
    return 1
}

# start_fake_ntp PORT CFG -- an SNTP responder on 127.0.0.1.
#
# Written against RFC 5905 rather than against skew.py: it packs its own
# replies and reads the client's transmit timestamp out of the request by
# byte offset, so a bug in the tool's packet code cannot cancel itself out
# against the same bug here.  That is the same reason the DNS responder
# above builds its own answers.
#
# CFG is JSON:
#   offset      seconds to add to real time, i.e. how wrong this source
#               claims the world is relative to us (default 0)
#   stratum     stratum to report                   (default 2)
#   leap        leap indicator, 3 = alarm/unsynced  (default 0)
#   drop        answer nothing at all               (default false)
#   bad_origin  echo the wrong originate timestamp, which is what a stale
#               or forged reply looks like          (default false)
start_fake_ntp() {
    local port="$1" cfg="$2"
    cat > "$TEST_TMPDIR/ntpd.py" <<'EOF'
import json, socket, struct, sys, time

port = int(sys.argv[1])
cfg = json.loads(sys.argv[2])
offset = float(cfg.get("offset", 0.0))
stratum = int(cfg.get("stratum", 2))
leap = int(cfg.get("leap", 0))
drop = bool(cfg.get("drop", False))
bad_origin = bool(cfg.get("bad_origin", False))

NTP_EPOCH = 2208988800


def stamp(t):
    return int(t) + NTP_EPOCH, int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", port))
while True:
    try:
        data, peer = sock.recvfrom(512)
    except OSError:
        break
    if len(data) < 48 or drop:
        continue
    # The client's transmit timestamp lives at bytes 40..48; it has to come
    # back as the originate timestamp or the reply is not an answer.
    orig_sec, orig_frac = struct.unpack("!II", data[40:48])
    if bad_origin:
        orig_sec, orig_frac = orig_sec + 7, 0
    recv_sec, recv_frac = stamp(time.time() + offset)
    tx_sec, tx_frac = stamp(time.time() + offset)
    reply = struct.pack(
        "!BBBb11I",
        (leap << 6) | (4 << 3) | 4,   # leap, version 4, mode 4 (server)
        stratum, 6, -20,
        0, 0, 0x7F7F0101,
        recv_sec, recv_frac,
        orig_sec, orig_frac,
        recv_sec, recv_frac,
        tx_sec, tx_frac)
    try:
        sock.sendto(reply, peer)
    except OSError:
        pass
EOF
    "$PY" "$TEST_TMPDIR/ntpd.py" "$port" "$cfg" &
    echo $! >> "$TEST_TMPDIR/ntp.pid"
    # Wait for the socket rather than sleeping a guessed interval.  A
    # dropping responder never answers, so its readiness is the bind
    # itself: the port stops being free.
    local i=0
    while [ $i -lt 50 ]; do
        if ! "$PY" - "$port" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)   # taken: the responder has it
s.close()
EOF
        then
            return 0
        fi
        i=$((i + 1))
    done
    echo "fake ntp on port $port never came up" >&2
    return 1
}

# skew_facts_json FILE [key=value ...] -- a healthy skew fact dict with
# overrides applied, so a rule test states only what it cares about.
skew_facts_json() {
    local path="$1"; shift
    "$PY" - "$path" "$@" <<'EOF'
import json, sys
path, overrides = sys.argv[1], sys.argv[2:]
f = {
  "sys.hostname": "testbox",
  "tz.name": "UTC", "tz.utc_offset_s": 0,
  "sync.daemon": "chronyd", "sync.daemon_pid": 941,
  "sync.daemon_running": True,
  "sync.ntp_enabled": True, "sync.synchronized": True,
  "cfg.path": "/etc/chrony.conf", "cfg.kind": "chrony",
  "cfg.servers": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
  "probe.timeout_s": 2.0, "probe.attempts": 2, "probe.samples": 2,
  "srv.list": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
  "srv.all": {
      "10.0.0.1": {"reachable": True, "error": None, "offset_s": 0.004,
                   "delay_ms": 1.2, "stratum": 2, "leap": 0,
                   "root_dispersion_s": 0.01},
      "10.0.0.2": {"reachable": True, "error": None, "offset_s": 0.006,
                   "delay_ms": 1.4, "stratum": 2, "leap": 0,
                   "root_dispersion_s": 0.01},
      "10.0.0.3": {"reachable": True, "error": None, "offset_s": 0.005,
                   "delay_ms": 1.9, "stratum": 3, "leap": 0,
                   "root_dispersion_s": 0.01}},
  "srv.count": 3, "srv.dead": [],
  "srv.alive": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
  "srv.unusable": [], "srv.max_stratum": 3,
  "clock.offset_s": 0.005, "clock.spread_s": 0.002,
  "rtc.path": "/sys/class/rtc/rtc0/since_epoch",
  "rtc.since_epoch": 1786741765, "rtc.delta_s": 0.4,
  "thresh.max_offset_s": 300.0, "thresh.warn_offset_s": 1.0,
}
for o in overrides:
    k, v = o.split("=", 1)
    try:
        f[k] = json.loads(v)
    except ValueError:
        f[k] = v
with open(path, "w") as fh:
    json.dump(f, fh)
EOF
}

# netmesh_reports DIR SPLIT -- a two-window report file for one pair.
#
# Rows before SPLIT are the idle baseline and rows at or after it are the
# loaded window, which is exactly the split `summarize --load-split` makes.
# Written by hand rather than by running agents: the arithmetic of idle
# versus loaded is what is under test, and real agents on an idle loopback
# cannot be made to bufferbloat on demand.
#
# Usage: netmesh_reports DIR SPLIT IDLE_P50 IDLE_P99 LOAD_P50 LOAD_P99 \
#                        [IDLE_LOSS LOAD_LOSS] [RX_USECS]
#
# RX_USECS, when given, is written on the agent's own "host" row, which is
# where a real agent records the card's coalescing timer.
netmesh_reports() {
    "$PY" - "$@" <<'EOF'
import csv, io, os, sys
d, split = sys.argv[1], int(sys.argv[2])
ip50, ip99, lp50, lp99 = (float(x) for x in sys.argv[3:7])
iloss, lloss = (float(x) for x in (sys.argv[7:9] or ["0", "0"]))
rx_usecs = sys.argv[9] if len(sys.argv) > 9 else ""
FIELDS = ["ts", "host", "dir", "peer", "probe", "size", "target_pps",
          "sent", "recv", "loss_pct", "rtt_min_us", "rtt_avg_us",
          "rtt_p50_us", "rtt_p99_us", "rtt_max_us", "jitter_us",
          "path_mtu", "mtu_state", "agent_cpu_pct", "note", "rx_usecs"]
os.makedirs(d, exist_ok=True)
with io.open(os.path.join(d, "web03.csv"), "w", newline="",
             encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS, restval="",
                       lineterminator="\n")
    w.writeheader()
    for i in range(10):
        loaded = i >= 5
        ts = split + (i - 5) * 10 if loaded else split - (5 - i) * 10
        p50, p99 = (lp50, lp99) if loaded else (ip50, ip99)
        loss = lloss if loaded else iloss
        sent = 1000
        recv = int(sent * (100.0 - loss) / 100.0)
        w.writerow({"ts": ts, "host": "web03", "dir": "tx", "peer": "db01",
                    "probe": "udp", "sent": sent, "recv": recv,
                    "rtt_avg_us": p50, "rtt_p50_us": p50, "rtt_p99_us": p99,
                    "rtt_max_us": p99, "jitter_us": 10})
        w.writerow({"ts": ts, "host": "db01", "dir": "rx", "peer": "web03",
                    "probe": "udp", "sent": 0, "recv": recv})
        if rx_usecs:
            w.writerow({"ts": ts, "host": "web03", "dir": "host",
                        "peer": "*", "probe": "udp", "agent_cpu_pct": "1.0",
                        "rx_usecs": rx_usecs})
EOF
}

# netmesh_flow_reports DIR NFLOWS SICK_IDX GOOD_P50 SICK_P50 \
#                       [PER_FLOW_RECV] [SICK_LOSS] [SPLIT]
#
# One pair, web03 -> db01, probed over NFLOWS source ports, where bucket
# SICK_IDX (0-based, or -1 for none) is the sick member of the bundle.
# Written by hand for the same reason as netmesh_reports: no loopback can
# be made to hash one flow onto a bad optic on demand.
#
# The aggregate tx row carries no flow and the bucket rows do, which is
# the distinction summarize relies on to avoid counting both.  PER_FLOW_RECV
# defaults high enough to clear the minimum a bucket needs before its p50
# is compared; pass a small value to exercise the thin case.  With SPLIT,
# the first half of the rows land before it and the second half at or
# after it, so --load-split has two windows to compare.
netmesh_flow_reports() {
    "$PY" - "$@" <<'EOF'
import csv, io, os, sys
d = sys.argv[1]
nflows, sick_idx = int(sys.argv[2]), int(sys.argv[3])
good_p50, sick_p50 = float(sys.argv[4]), float(sys.argv[5])
per_flow = int(sys.argv[6]) if len(sys.argv) > 6 else 50
sick_loss = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
split = int(sys.argv[8]) if len(sys.argv) > 8 else 0
FIELDS = ["ts", "host", "dir", "peer", "probe", "size", "target_pps",
          "sent", "recv", "loss_pct", "rtt_min_us", "rtt_avg_us",
          "rtt_p50_us", "rtt_p99_us", "rtt_max_us", "jitter_us",
          "path_mtu", "mtu_state", "agent_cpu_pct", "note", "rx_usecs",
          "flow"]
os.makedirs(d, exist_ok=True)
with io.open(os.path.join(d, "web03.csv"), "w", newline="",
             encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS, restval="",
                       lineterminator="\n")
    w.writeheader()
    for i in range(6):
        # Without a split every row is simply one interval apart; with one,
        # rows 0-2 are idle and rows 3-5 are loaded.
        ts = (split + (i - 3) * 10 if i >= 3 else split - (3 - i) * 10) \
            if split else 1700000000 + i * 5
        agg_sent = per_flow * nflows
        agg_recv = agg_sent
        rows = []
        for f in range(nflows):
            sick = (f == sick_idx)
            recv = int(round(per_flow * (100.0 - (sick_loss if sick else 0.0))
                             / 100.0))
            agg_recv -= (per_flow - recv)
            rows.append({
                "ts": ts, "host": "web03", "dir": "tx", "peer": "db01",
                "probe": "udp", "size": 64, "target_pps": 10,
                "sent": per_flow, "recv": recv,
                "loss_pct": "%.3f" % (sick_loss if sick else 0.0),
                "rtt_avg_us": sick_p50 if sick else good_p50,
                "rtt_p50_us": sick_p50 if sick else good_p50,
                "rtt_p99_us": (sick_p50 if sick else good_p50) * 2,
                "rtt_max_us": (sick_p50 if sick else good_p50) * 3,
                "jitter_us": 10, "flow": 40001 + f})
        w.writerow({"ts": ts, "host": "web03", "dir": "tx", "peer": "db01",
                    "probe": "udp", "size": 64, "target_pps": 10 * nflows,
                    "sent": agg_sent, "recv": agg_recv,
                    "loss_pct": "%.3f" % (100.0 * (agg_sent - agg_recv)
                                          / agg_sent),
                    "rtt_avg_us": good_p50, "rtt_p50_us": good_p50,
                    "rtt_p99_us": good_p50 * 2, "rtt_max_us": good_p50 * 3,
                    "jitter_us": 10})
        for r in rows:
            w.writerow(r)
        w.writerow({"ts": ts, "host": "db01", "dir": "rx", "peer": "web03",
                    "probe": "udp", "sent": 0, "recv": agg_recv})
        w.writerow({"ts": ts, "host": "web03", "dir": "host", "peer": "*",
                    "probe": "udp", "agent_cpu_pct": "1.0"})
EOF
}

# free_port -- a UDP port nothing is listening on, for the responder.
free_port() {
    "$PY" - <<'EOF'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
EOF
}

# facts_json FILE [key=value ...] -- a healthy why-slow fact dict with
# overrides applied, so a rule test states only what it cares about.
facts_json() {
    local path="$1"; shift
    "$PY" - "$path" "$@" <<'EOF'
import json, sys
path, overrides = sys.argv[1], sys.argv[2:]
f = {
  "sys.hostname": "testbox", "sys.kernel": "6.1.0", "sys.container": False,
  "sample.interval_s": 2.0, "cpu.count": 16, "cpu.load1": 0.4,
  "cpu.busy_pct": 8.0, "cpu.sys_pct": 1.0, "cpu.iowait_pct": 0.5,
  "cpu.steal_pct": 0.0, "cpu.softirq_pct": 0.2, "cpu.forks_per_s": 3.0,
  "cpu.runnable": 1, "cpu.blocked": 0,
  "psi.cpu.some10": 1.0, "psi.io.some10": 0.0, "psi.io.full10": 0.0,
  "psi.mem.some10": 0.0, "psi.mem.full10": 0.0,
  "mem.total_kb": 65748992, "mem.available_kb": 48000000,
  "mem.available_pct": 73.0, "mem.pswpin_per_s": 0.0,
  "mem.pswpout_per_s": 0.0, "mem.pgmajfault_per_s": 0.0,
  "disk.busiest": "sda", "disk.busiest_util_pct": 4.0,
  "disk.busiest_await_ms": 1.0, "disk.busiest_iops": 30.0,
  "disk.busiest_inflight": 0, "disk.all": {},
  "fs.fullest": "/", "fs.fullest_pct": 31.0, "fs.all": {},
  "fs.worst_inode": "/", "fs.worst_inode_pct": 12.0,
  "net.worst_if": "eth0", "net.worst_drop_pct": 0.0,
  "net.worst_drop_per_s": 0.0, "net.tcp_retrans_pct": 0.1,
  "net.listen_overflows_per_s": 0.0,
  "proc.count": 210, "proc.top_cpu": [], "proc.dstate": [],
  "proc.top_rss": [{"pid": "1", "comm": "init", "rss_kb": 9000}],
  "cg.v": None, "cg.mem_max": None, "cg.mem_current": None,
  "cg.mem_events_max": 0, "cg.mem_pct": None,
  "cg.cpu_quota_cores": None, "cg.throttled_pct": None,
  "kern.available": True, "kern.source": "dmesg", "kern.oom": [],
  "kern.io_errors": [], "kern.throttle": [], "kern.limit_hits": [],
  "sys.failed_units": [], "sys.entropy": 3000,
  "lim.fd_used": 4800, "lim.fd_max": 9223372036854775807,
  "lim.fd_pct": 0.1, "lim.tasks": 210, "lim.pid_max": 4194304,
  "lim.pid_pct": 0.01, "lim.conntrack": 812, "lim.conntrack_max": 262144,
  "lim.conntrack_pct": 0.3, "lim.tw": 61, "lim.ephemeral": 28232,
  "lim.ephemeral_pct": 0.2, "lim.arp": 14, "lim.arp_max": 1024,
  "lim.arp_pct": 1.4, "cg.pids_current": None, "cg.pids_max": None,
  "cg.pids_pct": None,
  "therm.cur_ghz": 3.4, "therm.max_ghz": 3.6, "therm.governor": "performance",
  "therm.max_temp_c": 45.0,
}
for o in overrides:
    k, v = o.split("=", 1)
    try:
        f[k] = json.loads(v)
    except ValueError:
        f[k] = v
with open(path, "w") as fh:
    json.dump(f, fh)
EOF
}

# during_samples FILE COUNT [col=spec ...] -- a synthetic sample series for
# during's analysis, which is a pure function of one.  A spec is:
#     7          constant
#     10..90     linear ramp from first sample to last
#     10:90      step change at the halfway point
#     a,b,c      an explicit list, cycled to COUNT
# Defaults describe an idle but healthy box, so a case states only the
# series it is about.
during_samples() {
    local path="$1" count="$2"; shift 2
    "$PY" - "$path" "$count" "$@" <<'EOF'
import csv, sys
path, count, overrides = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
FIELDS = ["host", "ts", "elapsed_s", "cpu_busy_pct", "cpu_sys_pct",
          "cpu_iowait_pct", "cpu_steal_pct", "cpu_max_core_pct", "cpu_cores",
          "runnable", "blocked", "psi_cpu_some", "psi_io_some", "psi_io_full",
          "psi_mem_full", "mem_available_pct", "swap_per_s", "disk",
          "disk_util_pct", "disk_await_ms", "disk_mbps", "net_if", "net_mbps",
          "net_link_mbps", "net_drop_per_s", "tcp_retrans_pct",
          "cg_throttled_pct", "cpu_ghz", "softirq_max_core_pct",
          "softnet_drop_per_s", "time_squeeze_per_s",
          "net_rx_missed_per_s", "net_cc", "net_rmem_max_kb", "net_numa",
          "top_comm", "top_cpu_pct"]
base = {"host": "testbox", "ts": 1786741765, "cpu_busy_pct": 5.0,
        "cpu_sys_pct": 1.0, "cpu_iowait_pct": 0.2, "cpu_steal_pct": 0.0,
        "cpu_max_core_pct": 8.0, "cpu_cores": 8, "runnable": 1, "blocked": 0,
        "psi_cpu_some": 0.0, "psi_io_some": 0.0, "psi_io_full": 0.0,
        "psi_mem_full": 0.0, "mem_available_pct": 80.0, "swap_per_s": 0.0,
        "disk": "sda", "disk_util_pct": 2.0, "disk_await_ms": 1.0,
        "disk_mbps": 1.0, "net_if": "eth0", "net_mbps": 1.0,
        "net_link_mbps": 10000, "net_drop_per_s": 0.0,
        "tcp_retrans_pct": 0.0, "cg_throttled_pct": "", "cpu_ghz": 3.4,
        "softirq_max_core_pct": 3.0, "softnet_drop_per_s": 0.0,
        "time_squeeze_per_s": 0.0, "net_rx_missed_per_s": 0.0,
        "net_cc": "cubic", "net_rmem_max_kb": 6144, "net_numa": "",
        "top_comm": "", "top_cpu_pct": ""}


def series(spec, n):
    if ".." in spec:
        a, b = (float(x) for x in spec.split("..", 1))
        if n == 1:
            return [a]
        return [a + (b - a) * i / (n - 1.0) for i in range(n)]
    if ":" in spec:
        a, b = (float(x) for x in spec.split(":", 1))
        return [a if i < n // 2 else b for i in range(n)]
    if "," in spec:
        vals = spec.split(",")
        return [vals[i % len(vals)] for i in range(n)]
    return [spec] * n


cols = {}
for o in overrides:
    k, v = o.split("=", 1)
    cols[k] = series(v, count)

with open(path, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for i in range(count):
        row = dict(base)
        row["elapsed_s"] = round((i + 1) * 1.0, 2)
        for k, vals in cols.items():
            row[k] = vals[i]
        w.writerow(dict((k, row.get(k, "")) for k in FIELDS))
EOF
}

# resolve_facts_json FILE [key=value ...] -- the same idea for resolve: a
# box whose DNS is healthy, with overrides applied.
resolve_facts_json() {
    local path="$1"; shift
    "$PY" - "$path" "$@" <<'EOF'
import json, sys
path, overrides = sys.argv[1], sys.argv[2:]
f = {
  "sys.hostname": "testbox", "res.path": "/etc/resolv.conf",
  "res.readable": True, "res.nameservers": ["10.0.0.53", "10.0.0.54"],
  "res.search": ["example.com"], "res.options": [], "res.ndots": 1,
  "res.timeout": 1, "res.attempts": 2, "res.rotate": False,
  "res.single_request": False, "res.stub": None, "res.upstreams": None,
  "probe.source": "/etc/resolv.conf", "probe.names": ["db01.example.com"],
  "probe.types": ["A", "AAAA"], "probe.timeout_s": 2.0, "probe.attempts": 1,
  "srv.list": ["10.0.0.53", "10.0.0.54"],
  "srv.all": {"10.0.0.53": {"alive": True, "rtt_ms": 1.2, "rcode": "NOERROR",
                            "error": None, "names": {}},
              "10.0.0.54": {"alive": True, "rtt_ms": 1.9, "rcode": "NOERROR",
                            "error": None, "names": {}}},
  "srv.count": 2, "srv.dead": [], "srv.alive": ["10.0.0.53", "10.0.0.54"],
  "srv.first_dead": False, "srv.slowest": "10.0.0.54",
  "srv.slowest_ms": 1.9, "srv.fastest_ms": 1.2,
  "names.all": {"db01.example.com": {
      "by_server": {}, "hosts": None, "distinct": 1,
      "answers": {"10.0.0.53": ["10.0.0.7"], "10.0.0.54": ["10.0.0.7"]}}},
  "names.disagree": [], "names.failed": [], "names.partial": [],
  "names.aaaa_stall": [], "names.hosts_shadow": [], "tcp.blocked": [],
  "hosts.readable": True,
  "search.count": 1, "search.wasted": 0, "search.wasted_ms": 0.0,
  "stub.all": {"db01.example.com": {"ms": 2.4, "addrs": ["10.0.0.7"],
                                    "error": None}},
  "stub.slowest_ms": 2.4, "budget.worst_s": 4,
}
for o in overrides:
    k, v = o.split("=", 1)
    try:
        f[k] = json.loads(v)
    except ValueError:
        f[k] = v
with open(path, "w") as fh:
    json.dump(f, fh)
EOF
}
