#!/usr/bin/env bash
# dredge: collecting files off a fleet, naming them apart, and the two
# transports (tar for whole files, framed base64 for a head or a tail).
#
# Everything runs through the shared fake ssh shim, which executes the
# "remote" command inside $FAKE_ROOT/<host>. The tool resolves a relative
# path against the login directory, so a relative PATH here lands in that
# sandbox and the whole pipeline -- find, tar or base64, decode, naming,
# append -- is exercised with no network and no second machine.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
DR="$BINNACLE_DIR/dredge.py"

dr() { "$PY" "$DR" --ssh "$FAKE_BIN/ssh" "$@"; }

# Three hosts, each with logs/app.log saying which host it is, plus a
# second file so directory collection has something to be a tree of.
seed() {
    install_fake_ssh
    for h in web01 web02 web03; do
        fake_host "$h"
        mkdir -p "$FAKE_ROOT/$h/logs/sub"
        printf 'hello from %s\n' "$h" > "$FAKE_ROOT/$h/logs/app.log"
        printf 'second file on %s\n' "$h" > "$FAKE_ROOT/$h/logs/sub/other.log"
    done
}

# --- the basic collection --------------------------------------------------

t_a_file_comes_back_under_the_name_of_its_host() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01,web02,web03 -d out --quiet
    assert_status $? 0
    for h in web01 web02 web03; do
        assert_file_exists "out/$h~logs~app.log"
        assert_eq "$(cat "out/$h~logs~app.log")" "hello from $h"
    done
}

t_a_directory_comes_back_as_named_files() {
    # A directory comes back as its files, in one directory, told apart
    # by name. Rebuilding each host's tree locally reads well and greps
    # badly: the next command is `grep -l oom *`, not a walk.
    seed
    cd "$TEST_TMPDIR"
    dr logs -H web01 -d out --quiet
    assert_no_file "out/web01"
    assert_file_exists "out/web01~logs~app.log"
    assert_file_exists "out/web01~logs~sub~other.log"
    assert_eq "$(cat "out/web01~logs~sub~other.log")" "second file on web01"
}

t_a_run_gets_a_directory_of_its_own() {
    # Collecting the same path twice an hour apart is the normal way to
    # use this, so without -d each run lands somewhere new rather than on
    # top of the last one.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 --quiet
    first="$(find . -maxdepth 1 -type d -name 'dredge-*' | head -1)"
    if [ -z "$first" ]; then
        _fail "no dredge-<stamp> directory was made"
    fi
    assert_file_exists "$first/web01~logs~app.log"
    dr logs/app.log -H web01 --quiet
    n="$(find . -maxdepth 1 -type d -name 'dredge-*' | wc -l | tr -d ' ')"
    assert_eq "$n" "2"
}

t_the_name_carries_the_host_and_the_path() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -H web01,web02 -d out --quiet
    assert_file_exists "out/web01~logs~app.log"
    assert_file_exists "out/web02~logs~sub~other.log"
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
    # One directory: no host subdirectory, no rebuilt tree.
    assert_eq "$(find out -type d | wc -l | tr -d ' ')" "1"
}

t_two_hosts_of_one_name_are_refused() {
    # The host name is the only thing keeping the files apart, so a list
    # naming one twice would collect the second over the first -- silently,
    # and only for the files they had in common.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -H web01,web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "named twice"
}

# --- only the part you need ------------------------------------------------

t_tail_brings_back_only_the_end() {
    # The point of doing it on the far side: the bytes never travel.
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    "$PY" -c "
import sys
with open(sys.argv[1], 'w') as fh:
    for i in range(1, 5001):
        fh.write('line %d\n' % i)" "$FAKE_ROOT/web01/logs/big.log"
    cd "$TEST_TMPDIR"
    dr logs/big.log -H web01 -d out --tail 3 --quiet
    assert_eq "$(cat "out/web01~logs~big.log")" \
              "$(printf 'line 4998\nline 4999\nline 5000')"
}

t_head_brings_back_only_the_start() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    printf 'one\ntwo\nthree\nfour\n' > "$FAKE_ROOT/web01/logs/a.log"
    cd "$TEST_TMPDIR"
    dr logs/a.log -H web01 -d out --head 2 --quiet
    assert_eq "$(cat "out/web01~logs~a.log")" "$(printf 'one\ntwo')"
}

t_head_and_tail_are_opposite_ends() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -H web01 --head 2 --tail 2 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "pick one"
}

t_a_slice_survives_a_binary_file() {
    # The framed stream is base64 with a token drawn fresh each run, so no
    # byte in the payload can be mistaken for the frame.
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    "$PY" -c "
import sys
open(sys.argv[1], 'wb').write(b'===dredge FILE\n\x00\x01\x02binary\xff\n')" \
        "$FAKE_ROOT/web01/logs/odd.log"
    cd "$TEST_TMPDIR"
    dr logs/odd.log -H web01 -d out --tail 5 --quiet
    assert_file_exists "out/web01~logs~odd.log"
    got="$("$PY" -c "
import sys; sys.stdout.write(repr(open(sys.argv[1],'rb').read()))" \
        "out/web01~logs~odd.log")"
    assert_contains "$got" "binary"
    assert_contains "$got" "===dredge FILE"
}

# --- only if it changed ----------------------------------------------------

t_since_selects_by_modification_time() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    printf 'old\n' > "$FAKE_ROOT/web01/logs/old.log"
    printf 'new\n' > "$FAKE_ROOT/web01/logs/new.log"
    touch -d "2001-01-01 00:00:00" "$FAKE_ROOT/web01/logs/old.log"
    cd "$TEST_TMPDIR"
    dr logs -H web01 -d out --since -1h --quiet
    assert_file_exists "out/web01~logs~new.log"
    assert_no_file "out/web01~logs~old.log"
}

t_the_relative_form_of_since_is_accepted_unglued() {
    # A relative time starts with '-', which argparse reads as the next
    # option -- so `--since -1h` was refused by a tool that recommends
    # exactly that spelling in its own error message.
    seed
    cd "$TEST_TMPDIR"
    out="$(dr logs/app.log -H web01 --since -1h --dry-run)"
    assert_contains "$out" "newermt"
    # ...and the joined spelling still works.
    out="$(dr logs/app.log -H web01 --since=-1h --dry-run)"
    assert_contains "$out" "newermt"
}

t_a_host_with_nothing_new_is_named_not_silent() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    printf 'old\n' > "$FAKE_ROOT/web01/logs/old.log"
    touch -d "2001-01-01 00:00:00" "$FAKE_ROOT/web01/logs/old.log"
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs -H web01 -d out --since -1h 2>&1)"; rc=$?
    set -e
    assert_status $rc 1                 # nothing came back: worth seeing
    assert_contains "$out" "EMPTY"
}

# --- collecting the same thing again ---------------------------------------

t_append_adds_to_what_is_already_here() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    dr logs/app.log -H web01 -d out --append --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'hello from web01\nhello from web01')"
}

t_prepend_puts_it_at_the_other_end() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    printf 'second run\n' > "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -H web01 -d out --prepend --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'second run\nhello from web01')"
}

t_a_mark_shows_where_old_meets_new() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    dr logs/app.log -H web01 -d out --append --mark --quiet
    assert_contains "$(cat "out/web01~logs~app.log")" "web01"
    assert_contains "$(cat "out/web01~logs~app.log")" "====="
}

t_a_mark_without_a_seam_is_refused() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -H web01 --mark 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--append"
}

t_replacing_is_the_default() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    dr logs/app.log -H web01 -d out --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
}

# --- what went wrong -------------------------------------------------------

t_an_unreachable_host_is_named_and_the_rest_still_land() {
    seed
    fake_host_unreachable web09
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -H web01,web09 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "web09"
    assert_file_exists "out/web01~logs~app.log"
}

t_a_missing_path_says_so() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/nope.log -H web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "MISSING"
}

t_a_file_over_the_ceiling_is_named_not_carried() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    "$PY" -c "
import sys; open(sys.argv[1],'wb').write(b'x' * 20000)" \
        "$FAKE_ROOT/web01/logs/big.log"
    printf 'small\n' > "$FAKE_ROOT/web01/logs/small.log"
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs -H web01 -d out --max-bytes 1000 2>&1)"
    set -e
    assert_contains "$out" "OVERSIZE"
    assert_contains "$out" "big.log"
    assert_file_exists "out/web01~logs~small.log"
    assert_no_file "out/web01~logs~big.log"
}

t_nothing_a_host_says_becomes_a_local_path() {
    # The names are rebuilt here, so a host answering with ../../etc/x
    # writes inside the collection directory or not at all.
    got="$("$PY" - "$DR" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dredge", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name in ("../../etc/cron.d/x", "/etc/shadow", "a/../../b", "./ok/f",
             "..", "/"):
    sys.stdout.write("%s -> %s\n" % (name, mod._clean_relpath(name)))
EOF
)"
    assert_contains "$got" "../../etc/cron.d/x -> etc/cron.d/x"
    assert_contains "$got" "/etc/shadow -> etc/shadow"
    assert_contains "$got" "a/../../b -> a/b"
    assert_not_contains "$got" "-> .."
}

# --- reporting -------------------------------------------------------------

t_csv_carries_a_row_per_file() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -H web01 -d out --csv c.csv --quiet
    head="$(head -1 c.csv)"
    assert_eq "$head" "host,remote_path,local_path,bytes,outcome"
    assert_contains "$(cat c.csv)" "web01,logs,out/web01~logs~app.log"
}

t_a_dry_run_contacts_nothing() {
    seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_SSH_LOG"
    out="$(dr logs/app.log -H web01,web02 --dry-run)"
    assert_contains "$out" "2 host(s)"
    assert_contains "$out" "tar"
    assert_eq "$(wc -l < "$FAKE_SSH_LOG" | tr -d ' ')" "0"
}

t_version_matches_the_house_format() {
    out="$("$PY" "$DR" --version)"
    assert_contains "$out" "0."
    assert_contains "$out" "GPL-3.0-or-later"
}


# --- ceilings, stalls and names that clash ---------------------------------

t_max_files_stops_without_calling_it_a_failure() {
    # Stopping on purpose is not the far side failing. Closing the stream
    # early kills it with SIGPIPE, and reading that status as the outcome
    # turned a deliberate partial collection into FAILED, with the host
    # counted as one that did not answer.
    seed
    cd "$TEST_TMPDIR"
    # Big enough that tar is still writing when we stop reading: eight
    # tiny files fit in the pipe buffer and tar exits cleanly before the
    # ceiling is even reached, which exercises none of this.
    mkdir -p "$FAKE_ROOT/web01/many"
    for i in 1 2 3 4 5 6 7 8; do
        head -c 100000 /dev/zero | tr '\0' "$i" > "$FAKE_ROOT/web01/many/f$i.log"
    done
    set +e
    out="$(dr many -H web01 -d out --max-files 3 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "3 files from 1 of 1 host"
    assert_contains "$out" "TRUNCATED"
    assert_not_contains "$out" "FAILED"
    assert_eq "$(find out -type f | wc -l | tr -d ' ')" "3"
}

t_max_files_is_not_a_failure_on_a_slice_either() {
    # The framed transport stops the same way and used to report the
    # xargs death -- "terminated by signal 13" -- as the host's outcome.
    seed
    cd "$TEST_TMPDIR"
    mkdir -p "$FAKE_ROOT/web01/many"
    for i in 1 2 3 4 5 6 7 8; do
        printf 'file %s\n' "$i" > "$FAKE_ROOT/web01/many/f$i.log"
    done
    set +e
    out="$(dr many -H web01 -d out --max-files 3 --tail 1 2>&1)"
    set -e
    assert_contains "$out" "TRUNCATED"
    assert_not_contains "$out" "FAILED"
    assert_eq "$(find out -type f | wc -l | tr -d ' ')" "3"
}

t_a_symlinked_path_is_the_file_it_points_at() {
    # `[ -e ]` follows a symlink, so the path passed the existence check
    # and then matched no -type f: the host was reported as having
    # nothing to send for a file that is plainly there. /var/log/current
    # and friends are symlinks on plenty of boxes.
    seed
    cd "$TEST_TMPDIR"
    ln -s logs/app.log "$FAKE_ROOT/web01/current.log"
    out="$(dr current.log -H web01 -d out 2>&1)"
    assert_status $? 0
    assert_not_contains "$out" "EMPTY"
    assert_file_exists "out/web01~current.log"
    assert_eq "$(cat "out/web01~current.log")" "hello from web01"
}

t_a_link_inside_a_tree_is_still_not_collected() {
    # -H follows only what was named on the command line. A link inside
    # a collected tree is not evidence, and recreating one here is how a
    # collection directory grows a link out of itself.
    seed
    cd "$TEST_TMPDIR"
    ln -s ../app.log "$FAKE_ROOT/web01/logs/sub/link.log"
    dr logs -H web01 -d out --quiet
    assert_file_exists "out/web01~logs~app.log"
    assert_no_file "out/web01~logs~sub~link.log"
}

t_a_local_write_failure_is_one_hosts_failure() {
    # It happens in a worker thread, where a SystemExit does not end the
    # process it was raised in: it travelled up through the pool and
    # ended the whole run with a usage exit code, no report at all, and
    # every other host's collection thrown away.
    seed
    cd "$TEST_TMPDIR"
    # A directory where web01's file has to land, and nothing in web02's way.
    mkdir -p "out/web01~logs~app.log"
    set +e
    out="$(dr logs/app.log -H web01,web02 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "FAILED"
    assert_contains "$out" "cannot write"
    # The other host still landed, and the run still reported.
    assert_file_exists "out/web02~logs~app.log"
    assert_contains "$out" "1 of 2 hosts"
}

t_two_paths_folding_onto_one_name_are_not_silently_merged() {
    # `a~b/c` and `a/b/c` both fold to `a~b~c`, and the second replacing
    # the first looks exactly like a successful collection.
    seed
    cd "$TEST_TMPDIR"
    mkdir -p "$FAKE_ROOT/web01/t/a~b" "$FAKE_ROOT/web01/t/a/b"
    printf 'first\n' > "$FAKE_ROOT/web01/t/a~b/c"
    printf 'second\n' > "$FAKE_ROOT/web01/t/a/b/c"
    set +e
    out="$(dr t -H web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "COLLISION"
    assert_eq "$(find out -type f | wc -l | tr -d ' ')" "1"
}

# A fake ssh that answers with PAYLOAD and then goes quiet for ever,
# without ever closing the connection.  $1 is what to send first.
stall_ssh() {
    cat > "$FAKE_BIN/ssh-stall" <<STALL
#!/bin/bash
printf '%s' '$1'
sleep 120
STALL
    chmod +x "$FAKE_BIN/ssh-stall"
}

t_a_transfer_that_stalls_mid_stream_is_bounded() {
    # The wait on the child only started once the stream had been read to
    # its end, so a far side that goes quiet mid-transfer -- the failure a
    # saturated link produces -- was bounded by nothing at all and the run
    # hung on that host for ever. Half a frame and then silence is what
    # that looks like from here.
    seed
    cd "$TEST_TMPDIR"
    stall_ssh "half a line and then nothing"
    start="$(date +%s)"
    set +e
    out="$(timeout 40 "$PY" "$DR" logs -H web01 -d out --tail 1 \
        --ssh "$FAKE_BIN/ssh-stall" --timeout 3 2>&1)"
    rc=$?
    set -e
    took=$(( $(date +%s) - start ))
    assert_status $rc 1
    assert_contains "$out" "TIMEOUT"
    if [ "$took" -gt 30 ]; then
        _fail "the timeout did not bound the transfer: took ${took}s"
    fi
}

t_something_still_holding_the_pipe_does_not_hang_the_run() {
    # Ending the transfer has to end the whole session. A remote command
    # that leaves something behind holding the connection keeps the pipe
    # open, so the thread draining stderr never sees EOF -- and closing
    # that stream under it takes the lock that thread is holding, which
    # is a deadlock the join's own timeout was there to prevent.
    seed
    cd "$TEST_TMPDIR"
    cat > "$FAKE_BIN/ssh-linger" <<'LINGER'
#!/bin/bash
# A child in a session of its own -- what a remote command that
# daemonises leaves behind -- so ending ours does not end it, and it
# goes on holding both pipes open.
setsid sleep 120 &
head -c 512 /dev/zero
sleep 120
LINGER
    chmod +x "$FAKE_BIN/ssh-linger"
    start="$(date +%s)"
    set +e
    out="$(timeout 40 "$PY" "$DR" logs -H web01 -d out \
        --ssh "$FAKE_BIN/ssh-linger" --timeout 3 2>&1)"
    rc=$?
    set -e
    took=$(( $(date +%s) - start ))
    assert_status $rc 1
    assert_contains "$out" "TIMEOUT"
    if [ "$took" -gt 30 ]; then
        _fail "the run did not let go of the host: took ${took}s"
    fi
}

t_ceilings_that_cannot_mean_anything_are_refused() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs -H web01 --max-bytes -1 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--max-bytes"
    set +e
    out="$(dr logs -H web01 --timeout 0 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--timeout"
}

echo "dredge"
run_test "a file comes back under its host"    t_a_file_comes_back_under_the_name_of_its_host
run_test "a directory comes back as names"     t_a_directory_comes_back_as_named_files
run_test "a run gets a directory of its own"   t_a_run_gets_a_directory_of_its_own
run_test "the name carries host and path"      t_the_name_carries_the_host_and_the_path
run_test "two hosts of one name are refused"   t_two_hosts_of_one_name_are_refused
run_test "tail brings back only the end"       t_tail_brings_back_only_the_end
run_test "head brings back only the start"     t_head_brings_back_only_the_start
run_test "head and tail are opposite ends"     t_head_and_tail_are_opposite_ends
run_test "a slice survives a binary file"      t_a_slice_survives_a_binary_file
run_test "since selects by mtime"              t_since_selects_by_modification_time
run_test "the relative form of since works"    t_the_relative_form_of_since_is_accepted_unglued
run_test "a host with nothing new is named"    t_a_host_with_nothing_new_is_named_not_silent
run_test "append adds to what is here"         t_append_adds_to_what_is_already_here
run_test "prepend puts it at the other end"    t_prepend_puts_it_at_the_other_end
run_test "a mark shows where old meets new"    t_a_mark_shows_where_old_meets_new
run_test "a mark without a seam is refused"    t_a_mark_without_a_seam_is_refused
run_test "replacing is the default"            t_replacing_is_the_default
run_test "an unreachable host is named"        t_an_unreachable_host_is_named_and_the_rest_still_land
run_test "a missing path says so"              t_a_missing_path_says_so
run_test "an oversize file is named"           t_a_file_over_the_ceiling_is_named_not_carried
run_test "no remote name becomes a path"       t_nothing_a_host_says_becomes_a_local_path
run_test "csv carries a row per file"          t_csv_carries_a_row_per_file
run_test "a dry run contacts nothing"          t_a_dry_run_contacts_nothing
run_test "--version matches the house format"  t_version_matches_the_house_format
run_test "--max-files is not a failure"        t_max_files_stops_without_calling_it_a_failure
run_test "nor on a slice"                      t_max_files_is_not_a_failure_on_a_slice_either
run_test "a symlinked path is followed"        t_a_symlinked_path_is_the_file_it_points_at
run_test "a link inside a tree is not"         t_a_link_inside_a_tree_is_still_not_collected
run_test "a write failure is one host's"       t_a_local_write_failure_is_one_hosts_failure
run_test "two paths on one name are caught"    t_two_paths_folding_onto_one_name_are_not_silently_merged
run_test "a stalled transfer is bounded"       t_a_transfer_that_stalls_mid_stream_is_bounded
run_test "a lingering pipe is let go of"       t_something_still_holding_the_pipe_does_not_hang_the_run
run_test "impossible ceilings are refused"     t_ceilings_that_cannot_mean_anything_are_refused
finish
