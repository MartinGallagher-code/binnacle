#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# A command agree gives up on -- past --timeout or --max-output -- is
# stopped on the host, not only its local ssh killed (BN-004, after
# quarterdeck's QD-026).  Real OpenSSH runs the command apart from the
# client, so killing ssh leaves it running; FAKE_SSH_DETACH=1 makes the
# fake ssh do the same, and the stop cases run that way.  Each command
# would leave a mark (MARK, CHILD) a few seconds on; a stopped one never
# does.  The first case shows the fake keeps a command running when
# nothing stops it, so the others prove something.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
AG="$BINNACLE_DIR/agree.py"

ag() { "$PY" "$AG" run --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" "$@"; }
ag_rc() { AG_RC=0; ag "$@" || AG_RC=$?; }
ag_script_rc() {
    AG_RC=0
    "$PY" "$AG" script --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" "$@" \
        || AG_RC=$?
}

later() { sleep 3.5; }

# agree.Runner on one fake host, for what the CLI does not show: the
# answer byte for byte, and a run with the stop switched off.
# argv: PKGDIR CASE; prints what came back as JSON.
write_driver() {
    cat > "$TEST_TMPDIR/drive.py" <<'PYEOF'
import json, os, sys
sys.path.insert(0, sys.argv[1])
import agree

args = agree.build_parser()[0].parse_args(
    ["run", "--ssh", os.environ["FAKE_BIN"] + "/ssh",
     "--scp", os.environ["FAKE_BIN"] + "/scp", "--timeout", "1",
     "--max-output", os.environ.get("CASE_MAX", "4194304")])
r = agree.Runner(args)
if sys.argv[2] == "nostop":
    r.stop = lambda host, group: ""
x = r.run_one(agree.Host("n01", "n01"), os.environ["CASE_CMD"])
print(json.dumps({"outcome": x.outcome, "rc": int(x.rc), "out": x.stdout,
                  "err": x.stderr, "left": x.left_running}))
PYEOF
}

drive() { "$PY" "$TEST_TMPDIR/drive.py" "$BINNACLE_DIR" "$@"; }

setup_detached() {
    cd "$TEST_TMPDIR"
    install_fake_ssh
    fake_host n01
    export FAKE_SSH_DETACH=1
}

t_the_fake_keeps_an_unstopped_command_running() {
    setup_detached
    write_driver
    CASE_CMD='sleep 2; touch MARK' drive nostop > out
    assert_contains "$(cat out)" '"outcome": "timeout"'
    later
    assert_file_exists "$FAKE_ROOT/n01/MARK"
}

t_a_timed_out_command_is_stopped_with_its_children() {
    setup_detached
    ag_rc -S n01 --timeout 1 -- \
        '(sleep 3; touch CHILD) & sleep 3; touch MARK' > out 2>&1
    assert_status "$AG_RC" 3
    assert_contains "$(cat out)" "timed out after 1.0s; agree.py stopped it on the host"
    assert_not_contains "$(cat out)" "NOT STOPPED"
    later
    assert_no_file "$FAKE_ROOT/n01/MARK"
    assert_no_file "$FAKE_ROOT/n01/CHILD"
}

t_a_flooding_command_is_stopped() {
    setup_detached
    # silent once past the cap, so no broken pipe ends it
    ag_rc -S n01 --max-output 1000 -- \
        'head -c 5000 /dev/zero; sleep 3; touch MARK' > out 2>&1
    assert_status "$AG_RC" 3
    assert_contains "$(cat out)" "output exceeded --max-output (1000 bytes); agree.py stopped it on the host"
    assert_not_contains "$(cat out)" "host killed"
    later
    assert_no_file "$FAKE_ROOT/n01/MARK"
}

t_a_script_is_stopped_before_its_directory_goes() {
    setup_detached
    # shellcheck disable=SC2016  # a script for the far side to expand
    printf '#!/bin/sh\nsleep 3\ntouch "$1"\n' > slow.sh
    ag_script_rc -S n01 --timeout 1 --remote-dir rd slow.sh -- \
        "$FAKE_ROOT/n01/MARK" > out 2>&1
    assert_contains "$(cat out)" "stopped it on the host"
    assert_not_contains "$(cat out)" "NOT CLEANED UP"
    assert_eq "$(ls "$FAKE_ROOT/n01/rd" 2>/dev/null)" ""
    later
    assert_no_file "$FAKE_ROOT/n01/MARK"
}

# the host cannot be reached again to stop it: the error and the report
# say it may still be running, and a pushed directory is left with it
t_a_stop_that_cannot_be_confirmed_is_said() {
    setup_detached
    printf '#!/bin/sh\ntouch .unreachable\nsleep 3\n' > cut.sh
    ag_script_rc -S n01 --timeout 1 --remote-dir rd cut.sh > out 2>&1
    rm -f "$FAKE_ROOT/n01/.unreachable"
    assert_status "$AG_RC" 3
    assert_contains "$(cat out)" "NOT STOPPED  the command may still be running on 1 host, where agree.py could not stop it:"
    assert_contains "$(cat out)" "timed out after 1.0s, and may still be running on the host: agree.py could not stop it ("
    assert_contains "$(cat out)" "NOT CLEANED UP"
    assert_contains "$(cat out)" "since the command in it may still be running"
    assert_contains "$(cat out)" "could not confirm it stopped there"
    [ -d "$(echo "$FAKE_ROOT"/n01/rd/agree.*)" ] \
        || fail "the run's directory was removed while its command may run"
    later
}

# a command that exits 124 or 125 itself was not given up on: it is not
# stopped, and no second ssh goes to the host
t_a_command_exiting_124_is_not_stopped() {
    setup_detached
    ag_rc -S n01 -- 'exit 124' > out 2>&1
    assert_not_contains "$(cat out)" "stopped it"
    assert_eq "$(ssh_calls)" 1
    ag_rc -S n01 -- 'exit 125' > out 2>&1
    assert_not_contains "$(cat out)" "stopped it"
    assert_eq "$(ssh_calls)" 2
}

# what finishes in time comes back exactly as before: no terminal, no
# CRLF, stderr apart, and the line naming the group taken off again and
# not counted against --max-output
t_an_answer_in_time_is_unchanged() {
    cd "$TEST_TMPDIR"
    for detach in "" 1; do
        install_fake_ssh
        fake_host n01
        export FAKE_SSH_DETACH="$detach"
        write_driver
        # shellcheck disable=SC2016  # for the far side
        CASE_CMD='printf "a\tb\n\nc"; printf "e1\ne2\n" >&2; [ -t 0 ] || [ -t 1 ] && echo tty; exit 3' \
            drive plain > out
        assert_eq "$(cat out)" '{"outcome": "failed", "rc": 3, "out": "a\tb\n\nc", "err": "e1\ne2\n", "left": false}'
        # stderr of exactly --max-output bytes is not past it
        CASE_MAX=10 CASE_CMD='printf 0123456789 >&2' drive plain > out
        assert_eq "$(cat out)" '{"outcome": "ok", "rc": 0, "out": "", "err": "0123456789", "left": false}'
        CASE_MAX=10 CASE_CMD='printf 0123456789x >&2' drive plain > out
        assert_contains "$(cat out)" '"rc": 125'
    done
}

t_dry_run_shows_the_command_without_the_mark() {
    install_fake_ssh
    fake_host n01
    out="$(ag -S n01 --dry-run -- uname -r)"
    assert_contains "$out" "uname -r"
    assert_not_contains "$out" "pgid"
    assert_eq "$(ssh_calls)" 0
}

run_test "the fake keeps an unstopped command running" t_the_fake_keeps_an_unstopped_command_running
run_test "a timed-out command is stopped, children too" t_a_timed_out_command_is_stopped_with_its_children
run_test "a flooding command is stopped"               t_a_flooding_command_is_stopped
run_test "a script stops before its directory goes"    t_a_script_is_stopped_before_its_directory_goes
run_test "a stop not confirmed is said"                t_a_stop_that_cannot_be_confirmed_is_said
run_test "a command exiting 124 or 125 is not stopped" t_a_command_exiting_124_is_not_stopped
run_test "an answer in time is unchanged"              t_an_answer_in_time_is_unchanged
run_test "--dry-run leaves the mark out"               t_dry_run_shows_the_command_without_the_mark
finish
