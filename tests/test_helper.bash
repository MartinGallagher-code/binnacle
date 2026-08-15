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
CURRENT_TEST=""
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
    unset WHY_SLOW_BOOT_WINDOW WHY_SLOW_PROC WHY_SLOW_SYS
    unset LOGTRIAGE_TOP LOGTRIAGE_MAX_TEMPLATES LOGTRIAGE_BUCKET
    unset LOGTRIAGE_BASELINE LOGTRIAGE_WEIGHTS
    export LANG=C
}

teardown_env() {
    # A crashed case must not leave probing agents behind.
    pkill -f "[n]etmesh[.]py agent --mesh $TEST_TMPDIR" 2>/dev/null || true
    [ -n "$TEST_TMPDIR" ] && rm -rf "$TEST_TMPDIR"
}

run_test() {
    local name="$1"; shift
    TESTS_RUN=$((TESTS_RUN + 1))
    CURRENT_TEST="$name"
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
host="${dest%%:*}"; host="${host#*@}"; path="${dest#*:}"
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
    if [ -f "$FAKE_SSH_LOG" ]; then
        grep '^ssh ' "$FAKE_SSH_LOG" 2>/dev/null | wc -l | tr -d ' '
    else
        echo 0
    fi
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
  "kern.io_errors": [], "kern.throttle": [],
  "sys.failed_units": [], "sys.entropy": 3000,
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
