#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# check_real_sshd.sh -- agree against a real OpenSSH server, on this machine.
#
# Usage: check_real_sshd.sh
#
# Starts an sshd of its own on 127.0.0.1 (a free port, a throwaway host key
# and client key, as this user: nothing of this machine's ssh setup is read
# or changed), then checks what the fake ssh in the suites cannot prove:
# that a command agree gives up on, past --timeout or --max-output, is
# stopped on the host with its children (BN-004), a pushed script too,
# and that a command that finishes in time answers exactly as before.
# Each command would leave a mark a few seconds on; a stopped one never
# does.  Needs sshd and ssh-keygen (openssh-server); skips, exit 0, where
# there are none.  Not run by CI.  Exit status: 0 when every check passed,
# 1 when one failed.  After quarterdeck's tests/check_real_sshd.sh (QD-026).

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
AGREE="$PWD/binnacle/agree.py"
PY="${PY:-python3}"

case "${1:-}" in
    -h|--help) sed -n '5,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

SSHD="$(command -v sshd || echo /usr/sbin/sshd)"
if [ ! -x "$SSHD" ] || ! command -v ssh-keygen > /dev/null || ! command -v ssh > /dev/null; then
    echo "check_real_sshd: skipped: no sshd, ssh or ssh-keygen here (openssh-server)"
    exit 0
fi

d="$(mktemp -d "${TMPDIR:-/tmp}/agree-sshd.XXXXXX")"
d="$(cd "$d" && pwd -P)"
chmod 700 "$d"
cleanup() {
    [ -f "$d/sshd.pid" ] && kill "$(cat "$d/sshd.pid")" 2> /dev/null
    rm -rf "$d"
}
trap cleanup EXIT

port="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
ssh-keygen -q -t ed25519 -N '' -f "$d/hostkey" < /dev/null
ssh-keygen -q -t ed25519 -N '' -f "$d/clientkey" < /dev/null
cp "$d/clientkey.pub" "$d/authorized_keys"
chmod 600 "$d/authorized_keys"
cat > "$d/sshd_config" <<CONF
Port $port
ListenAddress 127.0.0.1
HostKey $d/hostkey
AuthorizedKeysFile $d/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
UsePAM no
StrictModes no
PidFile $d/sshd.pid
Subsystem sftp internal-sftp
CONF
cat > "$d/ssh_config" <<CONF
Host *
  IdentityFile $d/clientkey
  IdentitiesOnly yes
  UserKnownHostsFile $d/known_hosts
  StrictHostKeyChecking accept-new
  Port $port
CONF
[ "$(id -u)" = 0 ] && mkdir -p /run/sshd
if ! "$SSHD" -f "$d/sshd_config" -E "$d/sshd.log"; then
    echo "check_real_sshd: sshd would not start:" >&2
    cat "$d/sshd.log" >&2
    exit 1
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$d/sshd.pid" ] && break
    sleep 0.2
done

SSH="ssh -F $d/ssh_config"
failed=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }
# agree VERB ARGS...: the verb, then this sshd, then the rest
agree() {
    local verb=$1
    shift
    "$PY" "$AGREE" "$verb" --ssh "$SSH" --scp "scp -F $d/ssh_config" \
        -S 127.0.0.1 "$@" 2>&1
}

if ! $SSH -o BatchMode=yes 127.0.0.1 true 2> "$d/err"; then
    echo "check_real_sshd: cannot log in to the sshd started here:" >&2
    cat "$d/err" "$d/sshd.log" >&2
    exit 1
fi

# check NAME OUTPUT MARK... : agree said it stopped the command, and none
# of the marks is there 4 s on
stopped() {
    local name=$1 out=$2 m
    shift 2
    sleep 4
    for m in "$@"; do
        if [ -e "$m" ]; then
            fail "$name: $m was written: the command ran on after agree"
            return
        fi
    done
    case "$out" in
        *"stopped it on the host"*) pass "$name" ;;
        *) fail "$name: agree did not say it stopped it: $out" ;;
    esac
}

out="$(agree run --timeout 1 -- "(sleep 2; touch $d/child) & sleep 2; touch $d/mark")"
stopped "past --timeout, stopped with its children" "$out" "$d/mark" "$d/child"

out="$(agree run --max-output 1000 -- "head -c 5000 /dev/zero; sleep 2; touch $d/flood")"
stopped "past --max-output, stopped" "$out" "$d/flood"

# shellcheck disable=SC2016  # a script for the far side to expand
printf '#!/bin/sh\nsleep 2\ntouch "$1"\n' > "$d/slow.sh"
out="$(agree script --timeout 1 --remote-dir "$d/rd" "$d/slow.sh" -- "$d/script")"
if ls -d "$d"/rd/agree.* > /dev/null 2>&1; then
    fail "script: its directory was left: $(ls "$d/rd")"
else
    stopped "script, stopped, then its directory removed" "$out" "$d/script"
fi

# in time: the answer as plain ssh gives it, byte for byte
cmd='printf "a\tb\n\nc"; printf "e1\ne2\n" >&2; [ -t 0 ] || [ -t 1 ] && echo tty; exit 3'
got="$("$PY" - "$PWD/binnacle" "$SSH" "$cmd" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[1])
import agree
args = agree.build_parser()[0].parse_args(["run", "--ssh", sys.argv[2]])
x = agree.Runner(args).run_one(agree.Host("h", "127.0.0.1"), sys.argv[3])
print(json.dumps([x.rc, x.stdout, x.stderr]))
PYEOF
)"
want="$("$PY" - "$SSH" "$cmd" <<'PYEOF'
import json, shlex, subprocess, sys
p = subprocess.run(shlex.split(sys.argv[1]) + ["127.0.0.1", sys.argv[2]],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   stdin=subprocess.DEVNULL)
print(json.dumps([p.returncode, p.stdout.decode(), p.stderr.decode()]))
PYEOF
)"
if [ "$got" = "$want" ]; then
    pass "in time, the same answer as plain ssh: $got"
else
    fail "in time: agree said $got, plain ssh $want"
fi

if [ "$failed" -gt 0 ]; then
    echo "check_real_sshd: $failed check(s) failed"
    exit 1
fi
echo "check_real_sshd: every check passed, against $("$SSHD" -V 2>&1 | head -1 || true)"
