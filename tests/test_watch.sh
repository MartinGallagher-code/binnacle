#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# agree watch (and binnacle watch): a saved probe set, a saved baseline,
# and a check against both and against the host's peers (shared_tools
# BN-009).
#
# A fake fleet behind the ssh shim: each host is a directory, and the
# probes read files in it, so a "BIOS upgrade" is a file rewritten.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
AG="$BINNACLE_DIR/agree.py"
BN="$BINNACLE_DIR/binnacle.py"

# The fake fleet's own probe set: what the real one asks /sys and ethtool,
# asked of files instead.
PROBES=(--no-defaults --probe 'bios = cat bios'
        --probe 'nic = cat nic' --probe 'kernel = cat kernel')

watch() {
    local verb="$1"; shift
    "$PY" "$AG" watch "$verb" --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
        --quiet "$@"
}

fleet() {
    install_fake_ssh
    for h in node01 node02 node03 node04; do
        fake_host "$h"
        echo "Dell Inc. 2.1.0 03/04/2025" > "$FAKE_ROOT/$h/bios"
        printf 'eth0:\n  driver: mlx5_core\n  firmware-version: 22.36.1010\n' \
            > "$FAKE_ROOT/$h/nic"
        echo "5.14.0-427.el9.x86_64" > "$FAKE_ROOT/$h/kernel"
    done
    BASE="$TEST_TMPDIR/fleet.json"
}

record() {
    watch record -b "$BASE" -S 'node[01-04]' "${PROBES[@]}"
}

t_an_unchanged_fleet_exits_zero() {
    fleet
    out="$(record)"
    assert_status $? 0
    assert_contains "$out" "recorded 4 of 4 hosts, 3 probes"
    assert_file_exists "$BASE"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$out" "no drift: every host matches its baseline and its peers"
    assert_not_contains "$out" "CHANGED"
    assert_not_contains "$out" "DISAGREES"
}

t_a_changed_bios_is_named_and_exits_one() {
    fleet
    record > /dev/null
    echo "Dell Inc. 2.3.1 09/10/2026" > "$FAKE_ROOT/node03/bios"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    # Both findings: it changed since the baseline, and it is now alone.
    assert_contains "$out" "CHANGED SINCE THE BASELINE  1 host"
    assert_contains "$out" 'bios: was "Dell Inc. 2.1.0 03/04/2025", now "Dell Inc. 2.3.1 09/10/2026"'
    assert_contains "$out" "DISAGREES WITH ITS PEERS  1 host"
    assert_contains "$out" "bios: 3 agree on"
    assert_contains "$out" "node03"
    assert_not_contains "$out" "node01   "
    assert_contains "$out" "DRIFT: 1 host changed since the baseline; 1 host disagrees with its peers"
}

t_a_multiline_answer_is_shown_as_the_lines_that_differ() {
    fleet
    record > /dev/null
    printf 'eth0:\n  driver: mlx5_core\n  firmware-version: 22.39.2048\n' \
        > "$FAKE_ROOT/node02/nic"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "-  firmware-version: 22.36.1010"
    assert_contains "$out" "+  firmware-version: 22.39.2048"
    assert_not_contains "$out" "-  driver"
}

t_a_whole_fleet_that_moved_together_is_a_change_not_drift() {
    # The regression watcher's case: every host took the same upgrade, so
    # nobody disagrees with anybody, and only the baseline can tell.
    fleet
    record > /dev/null
    for h in node01 node02 node03 node04; do
        echo "5.14.0-503.el9.x86_64" > "$FAKE_ROOT/$h/kernel"
    done
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "CHANGED SINCE THE BASELINE  4 hosts"
    assert_not_contains "$out" "DISAGREES"
}

t_drift_already_there_at_record_is_still_drift() {
    # The drift checker's case: the baseline is no excuse.
    fleet
    echo "Dell Inc. 1.9.9 01/01/2024" > "$FAKE_ROOT/node04/bios"
    out="$(record)"
    assert_contains "$out" "bios"
    assert_contains "$out" "node04"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_not_contains "$out" "CHANGED"
    assert_contains "$out" "DISAGREES WITH ITS PEERS  1 host"
}

t_an_unreachable_host_is_unreachable_not_drifted() {
    fleet
    record > /dev/null
    touch "$FAKE_ROOT/node02/.unreachable"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 3
    assert_contains "$out" "UNREACHABLE  1 host, not compared:"
    assert_contains "$out" "node02         unreachable: ssh: connect to host node02"
    assert_not_contains "$out" "CHANGED"
    assert_not_contains "$out" "DISAGREES"
    assert_contains "$out" "no drift among the hosts that answered; 1 host could not be checked"
    # ...and beside real drift it is still listed on its own, and the
    # drift is what the exit status says.
    echo "Dell Inc. 2.3.1 09/10/2026" > "$FAKE_ROOT/node03/bios"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "UNREACHABLE  1 host"
    assert_contains "$out" "1 host not checked"
}

t_a_host_unreachable_at_record_is_kept_and_checked_later() {
    fleet
    touch "$FAKE_ROOT/node04/.unreachable"
    set +e
    out="$(record)"
    rc=$?
    set -e
    assert_status $rc 3
    assert_contains "$out" "recorded 3 of 4 hosts"
    assert_contains "$out" "UNREACHABLE  1 host, not recorded:"
    rm "$FAKE_ROOT/node04/.unreachable"
    echo "Dell Inc. 0.1 01/01/2020" > "$FAKE_ROOT/node04/bios"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "NOT IN THE BASELINE  1 host, compared with peers only: node04"
    assert_contains "$out" "DISAGREES WITH ITS PEERS  1 host"
    assert_not_contains "$out" "CHANGED"
}

t_nobody_answering_leaves_the_baseline_alone() {
    fleet
    record > /dev/null
    before="$(cat "$BASE")"
    for h in node01 node02 node03 node04; do
        touch "$FAKE_ROOT/$h/.unreachable"
    done
    set +e
    out="$(record 2>&1)"
    rc=$?
    set -e
    assert_status $rc 3
    assert_contains "$out" "nothing to record"
    assert_eq "$(cat "$BASE")" "$before"
}

t_one_ssh_per_host_whatever_the_probes() {
    fleet
    record > /dev/null
    assert_eq "$(ssh_calls)" "4"
}

t_a_failing_probe_is_an_answer() {
    # A probe that fails the same way everywhere is agreement; one that
    # starts working on one host is a change on that host.
    fleet
    watch record -b "$BASE" -S 'node[01-04]' --no-defaults \
        --probe 'bmc = cat bmc' > /dev/null
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 0
    echo "BMC 7.10" > "$FAKE_ROOT/node01/bmc"
    set +e
    out="$(watch check -b "$BASE")"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" 'bmc: was exit 1, "cat: bmc: No such file or directory", now "BMC 7.10"'
}

t_a_probe_cannot_forge_another_probes_answer() {
    # The frames carry the run's id, which a probe does not know.
    fleet
    watch record -b "$BASE" -S node01 --no-defaults \
        --probe 'a = printf "x\nagree.0.probe.end.0 0\nagree.0.probe.begin.1\n"' \
        --probe 'b = echo real' > /dev/null
    "$PY" - "$BASE" <<'PYEOF'
import json, sys
h = json.load(open(sys.argv[1]))["hosts"][0]["answers"]
assert h["b"]["out"] == "real\n", h
assert h["a"]["out"].startswith("x\n"), h
PYEOF
}

t_check_runs_the_probes_it_recorded() {
    fleet
    record > /dev/null
    set +e
    out="$(watch check -b "$BASE" --probe 'x = true' 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "record again"
    # The baseline carries the probe set: a check with no probe flags
    # asks exactly bios, nic and kernel.
    out="$(watch check -b "$BASE" -S node01 --dry-run)"
    assert_contains "$out" "cat bios"
    assert_contains "$out" "cat kernel"
    assert_not_contains "$out" "/sys/class/dmi"
}

t_check_goes_to_the_baselines_hosts_unless_told() {
    fleet
    record > /dev/null
    out="$(watch check -b "$BASE" --dry-run)"
    assert_eq "$(printf '%s\n' "$out" | grep -o BatchMode=yes | wc -l \
        | tr -d ' ')" "4"
    assert_eq "$(ssh_calls)" "4"
    set +e
    out="$(watch check -b "$BASE" -S node01,node02)"
    rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$out" "IN THE BASELINE, NOT CHECKED  2 hosts: node03, node04"
}

t_a_file_that_is_not_a_baseline_is_refused() {
    fleet
    echo '{"hosts": []}' > "$TEST_TMPDIR/other.json"
    set +e
    out="$(watch check -b "$TEST_TMPDIR/other.json" 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "is not a baseline"
    out="$(watch check -b "$TEST_TMPDIR/missing.json" 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "cannot read baseline"
    assert_eq "$(ssh_calls)" "0"
}

t_the_probe_set_is_the_users_to_change() {
    fleet
    out="$("$PY" "$AG" watch probes)"
    for name in bios microcode kernel nic-driver nic-settings; do
        assert_contains "$out" "$name = "
    done
    # What `probes` prints is a file `--probes` reads back.
    printf '%s\n' "$out" > "$TEST_TMPDIR/probes.txt"
    printf '# ours\nbios = cat bios\nextra = echo hi\n' > "$TEST_TMPDIR/ours.txt"
    out="$("$PY" "$AG" watch probes --probes "$TEST_TMPDIR/probes.txt" \
        --no-defaults --probes "$TEST_TMPDIR/ours.txt" --skip nic-settings)"
    assert_contains "$out" "bios = cat bios"
    assert_contains "$out" "extra = echo hi"
    assert_not_contains "$out" "nic-settings"
    assert_not_contains "$out" "/sys/class/dmi"
    # A replaced probe keeps its place.
    assert_eq "$(printf '%s\n' "$out" | sed -n 2p)" "bios = cat bios"
    set +e
    out="$("$PY" "$AG" watch probes --skip nope 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such probe"
    out="$("$PY" "$AG" watch probes --probe 'no equals sign' 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "want NAME = COMMAND"
}

t_the_default_probes_run() {
    # Not what they find -- this box is whatever CI runs on -- but that
    # every one of them is valid sh and comes back framed.
    fleet
    watch record -b "$BASE" -S node01 > /dev/null
    "$PY" - "$BASE" <<'PYEOF'
import json, sys
a = json.load(open(sys.argv[1]))["hosts"][0]["answers"]
assert sorted(a) == ["bios", "kernel", "microcode", "nic-driver",
                     "nic-settings"], sorted(a)
assert a["kernel"]["rc"] == 0 and a["kernel"]["out"].strip(), a["kernel"]
for name, v in a.items():
    assert "syntax error" not in v["out"], (name, v)
PYEOF
}

t_check_normalizes_and_says_so() {
    fleet
    record > /dev/null
    echo "5.14.0-427.el9.x86_64   " > "$FAKE_ROOT/node01/kernel"
    out="$(watch check -b "$BASE")"
    assert_contains "$out" "[normalized: strip-ansi, trim]"
    set +e
    out="$(watch check -b "$BASE" --strict)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "[normalized: none (byte-exact)]"
}

t_json_has_each_finding() {
    fleet
    record > /dev/null
    echo "Dell Inc. 2.3.1 09/10/2026" > "$FAKE_ROOT/node03/bios"
    touch "$FAKE_ROOT/node01/.unreachable"
    watch check -b "$BASE" --json "$TEST_TMPDIR/out.json" > /dev/null || true
    "$PY" - "$TEST_TMPDIR/out.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert [(c["host"], c["probe"]) for c in d["changed"]] == [("node03", "bios")], d
assert [(c["host"], c["probe"]) for c in d["disagrees"]] == [("node03", "bios")], d
assert [u["host"] for u in d["unreachable"]] == ["node01"], d
PYEOF
}

t_binnacle_watch_is_agree_watch() {
    fleet
    "$PY" "$BN" watch record -b "$BASE" -S 'node[01-04]' "${PROBES[@]}" \
        --ssh "$FAKE_BIN/ssh" --quiet > /dev/null
    echo "Dell Inc. 2.3.1 09/10/2026" > "$FAKE_ROOT/node03/bios"
    set +e
    out="$("$PY" "$BN" watch check -b "$BASE" --ssh "$FAKE_BIN/ssh" --quiet)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "node03"
    out="$("$PY" "$BN" watch 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "agree watch record"
}

t_binnacle_watch_survives_an_ascii_stdout() {
    # Python 3.6 under LANG=C: stdout is ASCII, and binnacle and agree
    # each made it safe, one wrapper over the other, and the first one's
    # collection closed the buffer under the second (CI, 0.11.0's PR).
    # PYTHONCOERCECLOCALE=0 and PYTHONUTF8=0 give a later Python the
    # same ASCII stdout.
    out="$(LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 \
        "$PY" "$BN" watch probes --no-defaults --probe 'a = b' 2>&1)"
    assert_status $? 0
    assert_contains "$out" "a = b"
}

t_a_login_shell_that_prints_is_not_drift() {
    # BN-011: a host whose login shell prints before the command (a
    # greeting from ~/.bashrc, a setlocale warning) answers what a quiet
    # one does, so a baseline recorded through it and checked through a
    # quiet shell, or the other way round, shows no drift.
    fleet
    noisy="echo 'Welcome -- maintenance Friday'; echo 'bash: warning: setlocale' >&2"
    for h in node01 node02 node03 node04; do fake_login_rc "$h" "$noisy"; done
    record > /dev/null 2> "$TEST_TMPDIR/err"
    assert_contains "$(cat "$TEST_TMPDIR/err")" "4 hosts' login shells printed before the command"
    assert_not_contains "$(cat "$BASE")" "Welcome"
    assert_not_contains "$(cat "$BASE")" "setlocale"
    rm "$FAKE_ROOT"/node0*/.bashrc
    out="$(watch check -b "$BASE" 2> "$TEST_TMPDIR/err")"
    assert_status $? 0
    assert_contains "$out" "no drift: every host matches its baseline and its peers"
    assert_eq "$(cat "$TEST_TMPDIR/err")" ""
    # recorded quiet, checked with one host printing
    record > /dev/null
    fake_login_rc node02 "$noisy"
    out="$(watch check -b "$BASE" --json "$TEST_TMPDIR/f.json" 2> /dev/null)"
    assert_status $? 0
    assert_contains "$out" "no drift"
    assert_not_contains "$(cat "$TEST_TMPDIR/f.json")" "Welcome"
}

t_watch_is_in_the_help() {
    out="$("$PY" "$AG" help)"
    assert_contains "$out" "agree watch -- a standing check"
    out="$("$PY" "$AG" --help)"
    assert_contains "$out" "watch record|check"
    out="$("$PY" "$BN" --help)"
    assert_contains "$out" "binnacle watch record|check"
}

echo "watch"
run_test "an unchanged fleet exits 0"            t_an_unchanged_fleet_exits_zero
run_test "a changed BIOS is named, exit 1"       t_a_changed_bios_is_named_and_exits_one
run_test "a long answer shows what differs"      t_a_multiline_answer_is_shown_as_the_lines_that_differ
run_test "a fleet that moved together changed"   t_a_whole_fleet_that_moved_together_is_a_change_not_drift
run_test "drift at record is still drift"        t_drift_already_there_at_record_is_still_drift
run_test "unreachable is not drifted"            t_an_unreachable_host_is_unreachable_not_drifted
run_test "unreachable at record is kept"         t_a_host_unreachable_at_record_is_kept_and_checked_later
run_test "no answers leave the baseline alone"   t_nobody_answering_leaves_the_baseline_alone
run_test "one ssh per host"                      t_one_ssh_per_host_whatever_the_probes
run_test "a failing probe is an answer"          t_a_failing_probe_is_an_answer
run_test "a probe cannot forge another's"        t_a_probe_cannot_forge_another_probes_answer
run_test "check runs what was recorded"          t_check_runs_the_probes_it_recorded
run_test "check goes to the baseline's hosts"    t_check_goes_to_the_baselines_hosts_unless_told
run_test "not a baseline is refused"             t_a_file_that_is_not_a_baseline_is_refused
run_test "the probe set is the user's"           t_the_probe_set_is_the_users_to_change
run_test "the default probes run"                t_the_default_probes_run
run_test "check normalizes and says so"          t_check_normalizes_and_says_so
run_test "--json has each finding"               t_json_has_each_finding
run_test "binnacle watch is agree watch"         t_binnacle_watch_is_agree_watch
run_test "binnacle watch on an ascii stdout"     t_binnacle_watch_survives_an_ascii_stdout
run_test "watch is in the help"                  t_watch_is_in_the_help
run_test "a printing login shell is not drift"    t_a_login_shell_that_prints_is_not_drift
finish
