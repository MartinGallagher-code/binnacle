#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

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
    dr logs/app.log -S web01,web02,web03 -d out --quiet
    assert_status $? 0
    for h in web01 web02 web03; do
        assert_file_exists "out/$h~logs~app.log"
        assert_eq "$(cat "out/$h~logs~app.log")" "hello from $h"
    done
}

t_the_server_list_comes_from_a_file() {
    # The list flag had no test at all: every case here names hosts with
    # -H. A file is how a real fleet arrives -- reachable writes one --
    # so it is worth one.
    seed
    cd "$TEST_TMPDIR"
    printf '# the web tier\nweb01\nweb02\n\n' > fleet.txt
    dr logs/app.log --servers fleet.txt -d out --quiet
    assert_status $? 0
    assert_file_exists "out/web01~logs~app.log"
    assert_file_exists "out/web02~logs~app.log"
    assert_no_file "out/web03~logs~app.log"
}

t_the_old_hosts_spelling_is_gone() {
    # --hosts was this flag's name and briefly its alias. It is neither
    # now: one name for one thing, and a flag that half-exists is worse
    # than either. The refusal has to be a refusal -- exit non-zero with
    # nothing collected -- rather than a run that silently gathers no
    # hosts and reports an empty fleet.
    seed
    cd "$TEST_TMPDIR"
    printf 'web01\n' > fleet.txt
    rc=0; out="$(dr logs/app.log --hosts fleet.txt -d out --quiet 2>&1)" || rc=$?
    assert_status 2 "$rc" "an option that does not exist is a usage error"
    assert_contains "$out" "--hosts"
    assert_no_file "out/web01~logs~app.log"
    # And the name it does answer to still works.
    dr logs/app.log --servers fleet.txt -d out2 --quiet
    assert_file_exists "out2/web01~logs~app.log"
}

t_a_directory_comes_back_as_named_files() {
    # A directory comes back as its files, in one directory, told apart
    # by name. Rebuilding each host's tree locally reads well and greps
    # badly: the next command is `grep -l oom *`, not a walk.
    seed
    cd "$TEST_TMPDIR"
    dr logs -S web01 -d out --quiet
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
    dr logs/app.log -S web01 --quiet
    first="$(find . -maxdepth 1 -type d -name 'dredge-*' | head -1)"
    if [ -z "$first" ]; then
        _fail "no dredge-<stamp> directory was made"
    fi
    assert_file_exists "$first/web01~logs~app.log"
    dr logs/app.log -S web01 --quiet
    n="$(find . -maxdepth 1 -type d -name 'dredge-*' | wc -l | tr -d ' ')"
    assert_eq "$n" "2"
}

t_the_name_carries_the_host_and_the_path() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -S web01,web02 -d out --quiet
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
    out="$(dr logs/app.log -S web01,web01 -d out 2>&1)"; rc=$?
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
    dr logs/big.log -S web01 -d out --tail 3 --quiet
    assert_eq "$(cat "out/web01~logs~big.log")" \
              "$(printf 'line 4998\nline 4999\nline 5000')"
}

t_head_brings_back_only_the_start() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    printf 'one\ntwo\nthree\nfour\n' > "$FAKE_ROOT/web01/logs/a.log"
    cd "$TEST_TMPDIR"
    dr logs/a.log -S web01 -d out --head 2 --quiet
    assert_eq "$(cat "out/web01~logs~a.log")" "$(printf 'one\ntwo')"
}

t_head_and_tail_are_opposite_ends() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01 --head 2 --tail 2 2>&1)"; rc=$?
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
    dr logs/odd.log -S web01 -d out --tail 5 --quiet
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
    dr logs -S web01 -d out --since -1h --quiet
    assert_file_exists "out/web01~logs~new.log"
    assert_no_file "out/web01~logs~old.log"
}

t_the_relative_form_of_since_is_accepted_unglued() {
    # A relative time starts with '-', which argparse reads as the next
    # option -- so `--since -1h` was refused by a tool that recommends
    # exactly that spelling in its own error message.
    seed
    cd "$TEST_TMPDIR"
    out="$(dr logs/app.log -S web01 --since -1h --dry-run)"
    assert_contains "$out" "newermt"
    # ...and the joined spelling still works.
    out="$(dr logs/app.log -S web01 --since=-1h --dry-run)"
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
    out="$(dr logs -S web01 -d out --since -1h 2>&1)"; rc=$?
    set -e
    assert_status $rc 1                 # nothing came back: worth seeing
    assert_contains "$out" "EMPTY"
}

# --- collecting the same thing again ---------------------------------------

t_append_adds_to_what_is_already_here() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --quiet
    dr logs/app.log -S web01 -d out --append --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'hello from web01\nhello from web01')"
}

t_prepend_puts_it_at_the_other_end() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --quiet
    printf 'second run\n' > "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --prepend --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'second run\nhello from web01')"
}

t_a_mark_shows_where_old_meets_new() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --quiet
    dr logs/app.log -S web01 -d out --append --mark --quiet
    assert_contains "$(cat "out/web01~logs~app.log")" "web01"
    assert_contains "$(cat "out/web01~logs~app.log")" "====="
}

t_a_mark_without_a_seam_is_refused() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01 --mark 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--append"
}

t_replacing_is_the_default() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --quiet
    dr logs/app.log -S web01 -d out --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
}

# --- what went wrong -------------------------------------------------------

t_an_unreachable_host_is_named_and_the_rest_still_land() {
    seed
    fake_host_unreachable web09
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01,web09 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "web09"
    assert_file_exists "out/web01~logs~app.log"
}

t_a_missing_path_says_so() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/nope.log -S web01 -d out 2>&1)"; rc=$?
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
    out="$(dr logs -S web01 -d out --max-bytes 1000 2>&1)"
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
    dr logs -S web01 -d out --csv c.csv --quiet
    head="$(head -1 c.csv)"
    assert_eq "$head" \
        "host,source,local_path,bytes,outcome,exit_status,pass,unchanged,gap_bytes"
    assert_contains "$(cat c.csv)" "web01,logs,out/web01~logs~app.log"
}

t_a_dry_run_contacts_nothing() {
    seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_SSH_LOG"
    out="$(dr logs/app.log -S web01,web02 --dry-run)"
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
    out="$(dr many -S web01 -d out --max-files 3 2>&1)"; rc=$?
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
    out="$(dr many -S web01 -d out --max-files 3 --tail 1 2>&1)"
    set -e
    assert_contains "$out" "TRUNCATED"
    assert_not_contains "$out" "FAILED"
    assert_eq "$(find out -type f | wc -l | tr -d ' ')" "3"
}

# --- what the login says before the command runs ---------------------------

# An ssh that greets every command with a banner, the way a bastion's
# /etc/bashrc or a MOTD does: on the command's own stdout, before the
# command runs. Nothing about it is unusual, and it used to be fatal.
install_banner_ssh() {
    cat > "$FAKE_BIN/ssh" <<'SHIM'
#!/bin/bash
args=(); host=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2;; -p) shift 2;;
    *) if [ -z "$host" ]; then host="$1"; else args+=("$1"); fi; shift;;
  esac
done
host="${host#*@}"
printf 'ssh %s %s\n' "$host" "${args[*]}" >> "$FAKE_SSH_LOG"
[ -d "$FAKE_ROOT/$host" ] || { echo "ssh: no such host $host" >&2; exit 255; }
cd "$FAKE_ROOT/$host" || exit 255
echo "*** Authorised users only. All activity is monitored. ***"
echo "Last login: Tue Sep 22 09:14:02 2026 from 10.0.0.1"
exec bash -c "${args[*]}"
SHIM
    chmod +x "$FAKE_BIN/ssh"
}

t_a_login_banner_does_not_break_a_collection() {
    # The tar transport has no frame to hide behind: the first byte of
    # the stream is the first byte of a header, so a banner in front of
    # it made the whole tar unreadable and the host was reported as
    # having nothing to send -- silently wrong, about a file plainly
    # there.
    seed
    install_banner_ssh
    cd "$TEST_TMPDIR"
    out="$(dr logs/app.log -S web01,web02 -d out 2>&1)"
    assert_status $? 0
    assert_eq "$(cat out/web01~logs~app.log)" "hello from web01"
    assert_eq "$(cat out/web02~logs~app.log)" "hello from web02"
    assert_not_contains "$out" "EMPTY"
    # And the banner is not mistaken for evidence and landed as one.
    assert_not_contains "$(cat out/web01~logs~app.log)" "Authorised"
}

t_a_login_banner_does_not_break_a_jump_box() {
    # The same stream, one hop further out. Here it did not even fail
    # quietly: the unreadable tar closed the pipe, the far side died of
    # it, and the SIGPIPE this side had caused came back as "the jump
    # box itself did not answer".
    relay_seed
    install_banner_ssh
    cd "$TEST_TMPDIR"
    out="$(relay --cmd 'cat logs/app.log' -S web01,web02 -d out 2>&1)"
    assert_status $? 0
    assert_eq "$(cat out/cat~web01)" "hello from web01"
    assert_eq "$(cat out/cat~web02)" "hello from web02"
    assert_not_contains "$out" "did not answer"
}

t_a_banner_big_enough_to_fill_the_pipe_is_still_only_a_banner() {
    # The failure only showed up on a collection large enough that the
    # far side was still writing when this side gave up on the stream,
    # so the size is the point of the case.
    relay_seed
    install_banner_ssh
    cd "$TEST_TMPDIR"
    out="$(relay --cmd 'head -c 2000000 /dev/zero | tr "\0" x' \
        -S web01 -d out 2>&1)"
    assert_status $? 0
    assert_eq "$(wc -c < out/head~web01 | tr -d ' ')" "2000000"
}

t_a_host_that_only_talks_is_named_for_what_it_said() {
    # A login that prints its notice and never runs what it was sent.
    # There is no collection in that and there never will be, so the
    # line it printed is the entire diagnosis and belongs in the report.
    seed
    cd "$TEST_TMPDIR"
    cat > "$FAKE_BIN/ssh-mute" <<'SHIM'
#!/bin/bash
echo "*** This bastion requires an interactive session. ***"
exit 0
SHIM
    chmod +x "$FAKE_BIN/ssh-mute"
    set +e
    out="$("$PY" "$DR" logs -S web01 -d out --ssh "$FAKE_BIN/ssh-mute" 2>&1)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "FAILED"
    assert_contains "$out" "not with a collection"
    assert_contains "$out" "interactive session"
    assert_not_contains "$out" "nothing to send"
}

t_a_jump_box_that_only_talks_is_named_for_what_it_said() {
    relay_seed
    cd "$TEST_TMPDIR"
    cat > "$FAKE_BIN/ssh-mute" <<'SHIM'
#!/bin/bash
echo "*** This bastion requires an interactive session. ***"
exit 0
SHIM
    chmod +x "$FAKE_BIN/ssh-mute"
    set +e
    out="$(dr --relay jump01 --ssh "$FAKE_BIN/ssh-mute" \
        --relay-dir "$TEST_TMPDIR/spool" --cmd 'echo hi' -S web01 -d out 2>&1)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "not with a collection"
    assert_contains "$out" "interactive session"
    # The nearer cause, not ssh's account of a pipe this side closed.
    assert_not_contains "$out" "did not answer"
}

t_a_symlinked_path_is_the_file_it_points_at() {
    # `[ -e ]` follows a symlink, so the path passed the existence check
    # and then matched no -type f: the host was reported as having
    # nothing to send for a file that is plainly there. /var/log/current
    # and friends are symlinks on plenty of boxes.
    seed
    cd "$TEST_TMPDIR"
    ln -s logs/app.log "$FAKE_ROOT/web01/current.log"
    out="$(dr current.log -S web01 -d out 2>&1)"
    assert_status $? 0
    assert_not_contains "$out" "EMPTY"
    assert_file_exists "out/web01~current.log"
    assert_eq "$(cat "out/web01~current.log")" "hello from web01"
}

t_a_link_inside_a_tree_is_still_not_collected() {
    # -S follows only what was named on the command line. A link inside
    # a collected tree is not evidence, and recreating one here is how a
    # collection directory grows a link out of itself.
    seed
    cd "$TEST_TMPDIR"
    ln -s ../app.log "$FAKE_ROOT/web01/logs/sub/link.log"
    dr logs -S web01 -d out --quiet
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
    out="$(dr logs/app.log -S web01,web02 -d out 2>&1)"; rc=$?
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
    out="$(dr t -S web01 -d out 2>&1)"; rc=$?
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
    out="$(timeout 40 "$PY" "$DR" logs -S web01 -d out --tail 1 \
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
    out="$(timeout 40 "$PY" "$DR" logs -S web01 -d out \
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
    out="$(dr logs -S web01 --max-bytes -1 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--max-bytes"
    set +e
    out="$(dr logs -S web01 --timeout 0 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--timeout"
}


# --- --cmd: the other half of what you want off a fleet --------------------

t_a_command_comes_back_as_that_hosts_artifact() {
    seed
    cd "$TEST_TMPDIR"
    for h in web01 web02; do printf 'i am %s\n' "$h" > "$FAKE_ROOT/$h/who"; done
    dr --cmd 'cat who' -S web01,web02 -d out --quiet
    assert_status $? 0
    # Named for the command, because nobody passed --tag.
    assert_file_exists "out/cat~web01"
    assert_eq "$(cat "out/cat~web01")" "i am web01"
    assert_eq "$(cat "out/cat~web02")" "i am web02"
}

t_a_command_arrives_exactly_as_typed() {
    # It travels base64'd and is decoded into a variable on the far side,
    # so no shell parses it on the way. These cases hold for careful
    # nested quoting too -- what they guard is the property, and what
    # they catch is the naive version that interpolates the command in.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd "echo 'it'\''s here'" -S web01 -d q1 --quiet
    assert_eq "$(cat "q1/echo~web01")" "it's here"

    dr --cmd 'echo "a \"quoted\" word"' -S web01 -d q2 --quiet
    assert_eq "$(cat "q2/echo~web01")" 'a "quoted" word'

    # A literal backslash in the output, not printf's own escape: the
    # point is that the backslash reaches the far side, and `printf
    # "a\b\n"` would be printf eating it there rather than us losing it
    # here.
    dr --cmd 'printf "%s\n" "a\b"' -S web01 -d q3 --quiet
    assert_eq "$(cat "q3/printf~web01")" 'a\b'

    # A command substitution is the far side's to run, not ours -- which
    # is exactly why it must not expand here.
    # shellcheck disable=SC2016
    dr --cmd 'echo "$(echo nested)"' -S web01 -d q4 --quiet
    assert_eq "$(cat "q4/echo~web01")" "nested"

    # And a command spanning lines is one command.
    dr --cmd 'echo one
echo two' -S web01 -d q5 --quiet
    assert_eq "$(printf '%s' "$(cat "q5/echo~web01")" | tr '\n' '|')" "one|two"
}

t_a_command_that_says_nothing_in_words_still_says_it() {
    # Binary through the same framed base64 the slices use: any byte the
    # command produces has to survive, or "the same information back" is
    # only true for text.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'printf "\x00\x01\xff\xfe"' -S web01 -d b --quiet
    printf '\x00\x01\xff\xfe' > want.bin
    cmp -s want.bin "b/printf~web01"
    assert_status $? 0
}

t_stderr_comes_back_with_stdout_in_order() {
    # The answer is what you would have seen on the terminal. A tool that
    # writes its headline to stderr and its table to stdout is giving one
    # answer, not two.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo out; echo err >&2; echo more' -S web01 -d e --quiet
    assert_eq "$(tr '\n' '|' < "e/echo~web01")" "out|err|more|"
}

t_a_command_that_failed_is_a_finding_not_a_lost_host() {
    # The exit status is the one thing a file has no equivalent of. A
    # command that failed still has an answer worth keeping -- its error
    # text is the artifact -- so the collection is not a failure.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --cmd 'echo before; exit 7' -S web01 -d f --csv f.csv 2>&1)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "NONZERO"
    assert_contains "$out" "exit 7"
    # The output was kept either way.
    assert_eq "$(cat "f/echo~web01")" "before"
    # And the status is a column, not just a sentence.
    assert_contains "$(cat f.csv)" "exit_status"
    assert_contains "$(cat f.csv)" ",ok,7"
}

t_a_command_that_worked_is_quiet_about_it() {
    seed
    cd "$TEST_TMPDIR"
    out="$(dr --cmd 'echo fine' -S web01 -d g 2>&1)"
    assert_status $? 0
    assert_not_contains "$out" "NONZERO"
}

t_the_tag_leads_the_name_so_runs_can_share_a_directory() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo alive' -S web01 -d shared --tag probe --quiet
    dr logs/app.log -S web01 -d shared --tag before-restart --quiet
    # Two runs, one directory, told apart at a glance -- and sorted by run.
    assert_file_exists "shared/probe~web01"
    assert_file_exists "shared/before-restart~web01~logs~app.log"
}

t_a_suffix_goes_on_the_end_of_every_created_file() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -S web01 -d out --suffix .log --quiet
    assert_file_exists "out/web01~logs~app.log.log"
    assert_file_exists "out/web01~logs~sub~other.log.log"
    # A --cmd artifact is the one with no extension of its own, which is
    # half the reason this exists.
    dr --cmd 'echo hi' -S web01 -d out --suffix .txt --quiet
    assert_file_exists "out/echo~web01.txt"
    assert_eq "$(cat "out/echo~web01.txt")" "hi"
}

t_a_bare_suffix_gains_a_dot() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --suffix log --quiet
    assert_file_exists "out/web01~logs~app.log.log"
    # One that already starts with a separator is appended as typed --
    # with the joined spelling, since a bare -raw is an option to
    # argparse before it is ever a suffix.
    dr logs/app.log -S web01 -d out2 --suffix=-raw --quiet
    assert_file_exists "out2/web01~logs~app.log-raw"
    assert_no_file "out2/web01~logs~app.log.-raw"
}

t_a_suffix_cannot_smuggle_a_path() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --suffix '/../../etc/x' --quiet
    # One directory, and nothing written outside it.
    assert_eq "$(find out -type d | wc -l | tr -d ' ')" "1"
    assert_no_file "etc/x"
    # And a suffix that is only punctuation is nothing, so it is refused
    # rather than put on the end of every name in the run.
    set +e
    out="$(dr logs/app.log -S web01 -d out6 --suffix '//' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--suffix"
}

t_a_tag_cannot_smuggle_a_path_into_the_name() {
    # The tag is the one part of the name the caller writes freely.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo hi' -S web01 -d odd --tag 'a/b c:d' --quiet
    assert_file_exists "odd/a-b-c-d~web01"
    assert_eq "$(find odd -type d | wc -l | tr -d ' ')" "1"
}

t_a_command_and_a_path_are_not_both_the_artifact() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --cmd 'echo x' logs/app.log -S web01 -d r1 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "not both"

    set +e
    out="$(dr -S web01 -d r2 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "nothing to collect"

    # --since picks among files; a command has no files to pick from.
    set +e
    out="$(dr --cmd 'echo x' --since -1h -S web01 -d r3 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--since selects among files"
}

t_a_command_that_printed_nothing_is_an_empty_host() {
    # The reported bug: `dredge --cmd ls --servers hosts` over a fleet
    # whose login directories hold nothing visible landed one zero-byte
    # file per host and called it a collection. Silence is not an
    # artifact, and forty empty files look exactly like a broken
    # transport -- which is what sent somebody looking for one.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --cmd 'true' -S web01,web02 -d n1 2>&1)"; rc=$?
    set -e
    # Nothing came back, so nothing was collected.
    assert_status $rc 1
    assert_no_file "n1/true~web01"
    assert_no_file "n1/true~web02"
    assert_contains "$out" "EMPTY"
    assert_contains "$out" "the command printed nothing there"
    assert_contains "$out" "web01"
    assert_contains "$out" "web02"
}

t_a_silent_command_is_still_a_row_with_its_status() {
    # The frame still carries the exit status, and losing the host from
    # the report would lose that with it: a command that said nothing and
    # failed is a finding, not an absence.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --cmd 'exit 3' -S web01 -d n2 --csv n2.csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_no_file "n2/exit~web01"
    assert_contains "$out" "NONZERO"
    assert_contains "$out" "exit 3"
    # One row for the host, no local file, status kept.
    assert_contains "$(cat n2.csv)" "web01,exit 3,,0,ok,3"
}

t_one_silent_host_does_not_empty_the_run() {
    # A fleet where half the hosts have something to say is a successful
    # collection of that half, not a failure.
    seed
    printf 'i am web01\n' > "$FAKE_ROOT/web01/who"
    cd "$TEST_TMPDIR"
    out="$(dr --cmd 'cat who 2>/dev/null || true' -S web01,web02 -d n3 2>&1)"
    assert_status $? 0
    assert_file_exists "n3/cat~web01"
    assert_no_file "n3/cat~web02"
    assert_contains "$out" "EMPTY"
    assert_contains "$out" "web02"
}

t_a_zero_byte_file_is_still_collected() {
    # The other side of it: a file that is empty exists on the far side,
    # and a faithful copy of it is empty. Only a *command* has nothing to
    # land when it says nothing.
    seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_ROOT/web01/logs/empty.log"
    dr logs/empty.log -S web01 -d z --quiet
    assert_status $? 0
    assert_file_exists "z/web01~logs~empty.log"
    # And through the other transport, where the frame is the same one
    # --cmd uses.
    dr logs/empty.log -S web01 -d z2 --tail 5 --quiet
    assert_file_exists "z2/web01~logs~empty.log"
}

t_what_the_far_side_said_survives_an_empty_host() {
    # A host that sent nothing is the one run with no explanation in it,
    # so the stderr that used to be thrown away on a successful exit is
    # the only clue there will ever be.
    seed
    cd "$TEST_TMPDIR"
    out="$(dr --cmd 'echo mumbling >&2 1>&2; true' -S web01 -d n4 2>&1)"
    # stderr is merged into the artifact, so that one is not empty.
    assert_file_exists "n4/echo~web01"
    # But a wrapper-level complaint, outside the merged pair, is not.
    out="$(dr --cmd 'true' -S web01 -d n5 2>&1)" || true
    assert_contains "$out" "EMPTY"
}

t_head_and_tail_cut_a_commands_output_too() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'printf "a\nb\nc\nd\n"' -S web01 -d h1 --tail 2 --quiet
    assert_eq "$(tr '\n' '|' < "h1/printf~web01")" "c|d|"
    dr --cmd 'printf "a\nb\nc\nd\n"' -S web01 -d h2 --head 1 --quiet
    assert_eq "$(tr '\n' '|' < "h2/printf~web01")" "a|"
}

# --- --tsv: a table of what the fleet said ---------------------------------

t_a_table_has_a_date_a_host_and_the_variables() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "load1=0.4"; echo "procs=212"' -S web01,web02 -d out \
       --tsv m.tsv --quiet
    assert_status $? 0
    assert_eq "$(head -1 m.tsv)" "$(printf 'date\thost\tload1\tprocs')"
    # A row per host, in the order the server list named them.
    assert_eq "$(sed -n 2p m.tsv | cut -f2-)" "$(printf 'web01\t0.4\t212')"
    assert_eq "$(sed -n 3p m.tsv | cut -f2-)" "$(printf 'web02\t0.4\t212')"
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "3"
    # The date column is the one shape a spreadsheet and `sort` agree on.
    stamp="$(sed -n 2p m.tsv | cut -f1)"
    if ! printf '%s' "$stamp" | grep -Eq \
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$'; then
        fail "date column is not yyyy-mm-ddThh:mm:ss: $stamp"
    fi
}

t_a_second_run_appends_under_the_same_header() {
    # The whole value of one file is that column 4 means the same thing in
    # row 2 and in row 900.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "a=1"' -S web01 -d out --tsv m.tsv --quiet
    dr --cmd 'echo "a=2"' -S web01 -d out --tsv m.tsv --quiet
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "3"
    assert_eq "$(grep -c '^date' m.tsv)" "1"
    assert_eq "$(sed -n 3p m.tsv | cut -f3)" "2"
}

t_the_header_does_not_move_when_a_name_appears_later() {
    # A header rebuilt from each pass would renumber every column the
    # first time a host answered differently, and a week of rows behind
    # it would quietly start meaning something else.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "a=1"; echo "b=2"' -S web01 -d out --tsv m.tsv --quiet
    out="$(dr --cmd 'echo "a=9"; echo "b=8"; echo "c=7"' -S web01 -d out \
           --tsv m.tsv 2>&1)"
    assert_eq "$(head -1 m.tsv)" "$(printf 'date\thost\ta\tb')"
    assert_eq "$(sed -n 3p m.tsv | cut -f3,4)" "$(printf '9\t8')"
    assert_contains "$out" "NEWVAR"
    assert_contains "$out" "c"
}

t_a_missing_variable_is_a_blank_not_a_shift() {
    seed
    cd "$TEST_TMPDIR"
    printf 'a=1\nb=2\n' > "$FAKE_ROOT/web01/vars"
    printf 'a=3\n' > "$FAKE_ROOT/web02/vars"
    dr --cmd 'cat vars' -S web01,web02 -d out --tsv m.tsv --quiet
    assert_eq "$(head -1 m.tsv)" "$(printf 'date\thost\ta\tb')"
    # web02 has no b, so the cell is empty and `a` is still column 3.
    assert_eq "$(sed -n 3p m.tsv | cut -f3)" "3"
    assert_eq "$(sed -n 3p m.tsv | cut -f4)" ""
}

t_every_promised_shape_is_read() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "{\"a\": 1, \"b\": \"two\", \"c\": true}"' -S web01 \
       -d j --tsv j.tsv --parse json --quiet
    assert_eq "$(head -1 j.tsv)" "$(printf 'date\thost\ta\tb\tc')"
    assert_eq "$(sed -n 2p j.tsv | cut -f3-)" "$(printf '1\ttwo\ttrue')"

    dr --cmd 'printf "alpha\tbeta\n1\t2\n"' -S web01 -d r --tsv r.tsv \
       --parse row --quiet
    assert_eq "$(head -1 r.tsv)" "$(printf 'date\thost\talpha\tbeta')"
    assert_eq "$(sed -n 2p r.tsv | cut -f3-)" "$(printf '1\t2')"

    dr --cmd 'echo "3 41 0.7"' -S web01 -d v --tsv v.tsv --parse values \
       --columns q,r,s --quiet
    assert_eq "$(head -1 v.tsv)" "$(printf 'date\thost\tq\tr\ts')"
    assert_eq "$(sed -n 2p v.tsv | cut -f3-)" "$(printf '3\t41\t0.7')"
}

t_an_answer_that_will_not_parse_is_a_finding_not_a_gap() {
    # A host quietly missing from the table reads as a machine that was
    # fine. Its answer is still collected -- only its row is missing.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --cmd 'echo "not kv at all"' -S web01 -d out --tsv m.tsv 2>&1)"
    rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$out" "UNPARSED"
    assert_contains "$out" "web01"
    assert_no_file "m.tsv"
    # The raw answer is on disk either way.
    assert_eq "$(cat out/echo~web01)" "not kv at all"
}

t_quiet_still_says_a_host_has_no_row() {
    # `uptime` is prose, not one of the shapes. Under --quiet that used to
    # be no output, no table and exit 0 -- a run that looked like it
    # worked and wrote nothing.
    seed
    cd "$TEST_TMPDIR"
    prose='echo " 09:14:02 up 10 days,  3:22,  2 users,  load average: 0.41"'
    out="$(dr --cmd "$prose" -S web01,web02 -d out --tsv m.tsv --quiet 2>&1)"
    assert_contains "$out" "UNPARSED"
    assert_contains "$out" "web02"
    # Nothing read at all, so it is the command's shape, and it says so.
    assert_contains "$out" "print name=value lines"
    assert_no_file "m.tsv"
}

t_one_unreadable_host_is_not_blamed_on_the_shape() {
    # One host printing an error is that host's problem, not the command's:
    # the shape advice is for when nothing read.
    seed
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > "$FAKE_ROOT/web01/vars"
    printf 'cat: vars: Permission denied\n' > "$FAKE_ROOT/web02/vars"
    out="$(dr --cmd 'cat vars' -S web01,web02 -d out --tsv m.tsv --quiet 2>&1)"
    assert_contains "$out" "UNPARSED"
    assert_contains "$out" "web02"
    assert_not_contains "$out" "the command's shape"
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "2"
}

t_quiet_still_says_a_name_has_no_column() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "a=1"' -S web01 -d out --tsv m.tsv --quiet
    out="$(dr --cmd 'echo "a=2"; echo "b=3"' -S web01 -d out --tsv m.tsv \
           --quiet 2>&1)"
    assert_contains "$out" "NEWVAR"
    assert_contains "$out" "b"
}

t_a_value_cannot_break_the_row_it_is_in() {
    # A tab ends a column and a newline ends a row: either would turn one
    # row into two, or shift every column after it.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'printf "note=a\tb\nx=1\n"' -S web01 -d out --tsv m.tsv --quiet
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "2"
    assert_eq "$(sed -n 2p m.tsv | cut -f3)" 'a\tb'
    assert_eq "$(sed -n 2p m.tsv | cut -f4)" "1"
}

t_a_silent_host_gets_no_row() {
    # An empty line under a timestamp says the fleet reported zero, which
    # is a different and much worse claim than saying nothing.
    seed
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > "$FAKE_ROOT/web01/vars"
    dr --cmd 'cat vars 2>/dev/null || true' -S web01,web02 -d out \
       --tsv m.tsv --quiet
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "2"
    assert_eq "$(sed -n 2p m.tsv | cut -f2)" "web01"
}

t_columns_pins_the_header_up_front() {
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "b=2"; echo "a=1"' -S web01 -d out --tsv m.tsv \
       --columns a,b --quiet
    # Named order, not the order the host happened to print them in.
    assert_eq "$(head -1 m.tsv)" "$(printf 'date\thost\ta\tb')"
    assert_eq "$(sed -n 2p m.tsv | cut -f3,4)" "$(printf '1\t2')"
}

t_a_table_needs_a_command_and_a_whole_answer() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01 -d out --tsv m.tsv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "a PATH has none"

    set +e
    out="$(dr --cmd 'echo x' -S web01 -d out --tsv m.tsv --parse values 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--columns"

    # --follow brings back what was added; a row is every variable.
    set +e
    out="$(dr --cmd 'echo x' -S web01 -d out --tsv m.tsv --follow 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "whole answer"

    set +e
    out="$(dr --cmd 'echo x' -S web01 -d out --tsv m.tsv --columns host 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "already has that column"
}

t_a_daemon_building_a_table_repeats_the_command() {
    # --daemon implies --follow so a timer cannot re-fetch every file in
    # full, but a table wants the whole answer each pass -- and re-running
    # a command is not a re-transfer.
    seed
    cd "$TEST_TMPDIR"
    dr --cmd 'echo "a=1"' -S web01 -d out --tsv m.tsv --daemon --every 1s \
       --passes 3 --quiet
    assert_status $? 0
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "4"
    assert_eq "$(grep -c '^date' m.tsv)" "1"
}

# --- --relay: a jump box ---------------------------------------------------
#
# jump01 stands in for B. The shim runs its "remote" command inside
# $FAKE_ROOT/jump01, and the copy of dredge sent there fans out to web01
# and web02 through the same shim -- so the two hops are real hops, with
# a real unpack, a real inner run and a real tar coming home.

relay_seed() {
    seed
    fake_host jump01
}

# The inner dredge needs the shim to reach the fleet, and a spool that
# does not litter the machine running the tests.
relay() {
    dr --relay jump01 --ssh "$FAKE_BIN/ssh" \
       --relay-dir "$TEST_TMPDIR/spool" "$@"
}

t_a_jump_box_runs_the_collection_and_sends_it_back() {
    relay_seed
    cd "$TEST_TMPDIR"
    relay --cmd 'cat logs/app.log' -S web01,web02 -d out --quiet
    assert_status $? 0
    # The names are the ones this side would have built without a jump
    # box in the way, so the next command reads the same either way.
    assert_eq "$(cat out/cat~web01)" "hello from web01"
    assert_eq "$(cat out/cat~web02)" "hello from web02"
}

t_a_jump_box_keeps_nothing_afterwards() {
    # The whole point of sending the file rather than installing it.
    relay_seed
    cd "$TEST_TMPDIR"
    relay --cmd 'echo hi' -S web01 -d out --quiet
    assert_no_file "spool"
    assert_no_file "spool/dredge.py"
}

t_keep_relay_leaves_the_spool_to_look_at() {
    relay_seed
    cd "$TEST_TMPDIR"
    relay --cmd 'echo hi' -S web01 -d out --keep-relay --quiet
    assert_file_exists "spool/dredge.py"
    assert_file_exists "spool/servers"
    assert_file_exists "spool/report"
}

t_relay_refuses_a_directory_it_did_not_make() {
    # The spool is dredge's to clear and remove. A --relay-dir pointed at
    # a tree dredge did not make is somebody's own directory handed over by
    # mistake, and clearing it would delete their files -- the report that
    # started this. dredge must refuse and delete nothing.
    relay_seed
    cd "$TEST_TMPDIR"
    mkdir -p spool/src
    printf 'months of work\n' > spool/src/main.c
    printf 'keep me\n'        > spool/keepme
    set +e
    out="$(relay --cmd 'echo hi' -S web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "dredge did not make it"
    # Nothing in the tree was touched -- not by the far side's guard, and
    # not by the sweep that tidies up afterwards.
    assert_eq "$(cat spool/src/main.c)" "months of work"
    assert_file_exists "spool/keepme"
}

t_relay_still_owns_and_clears_its_own_spool() {
    # The marker is what tells dredge's own scratch directory from the
    # caller's tree, so a genuine leftover spool -- marker inside -- is
    # still cleared and reused, and the run collects as normal.
    relay_seed
    cd "$TEST_TMPDIR"
    mkdir -p spool
    : > "spool/.dredge-relay-spool"
    printf 'stale\n' > spool/leftover
    relay --cmd 'echo hi' -S web01 -d out --quiet
    assert_status $? 0
    assert_eq "$(cat out/echo~web01)" "hi"
    # dredge owned this spool, so it cleared the stale contents and then
    # (no --keep-relay) removed the whole thing.
    assert_no_file "spool"
}

t_relay_refuses_a_root_or_home_before_contacting_anything() {
    # A relay-dir that resolves to a root or a home is a request to
    # delete one, and the far side would refuse it -- but these few are
    # worth stopping here, before a single host is contacted.
    relay_seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr --relay jump01 --relay-dir / --cmd 'echo hi' -S web01 \
        -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "cannot be a root or a home"
    assert_eq "$(ssh_calls)" "0"
}

t_the_jump_box_gets_the_fleet_already_expanded() {
    # `web[01-02]` is expanded here, by this side's idea of the syntax,
    # so the far side collects from exactly the fleet that was asked for.
    relay_seed
    cd "$TEST_TMPDIR"
    relay --cmd 'echo hi' -S 'web[01-02]' -d out --keep-relay --quiet
    assert_contains "$(cat spool/servers)" "web01"
    assert_contains "$(cat spool/servers)" "web02"
    assert_not_contains "$(cat spool/servers)" "["
}

t_the_far_sides_report_and_status_come_home() {
    # The run that actually happened is the one over there, so its report
    # is what is shown -- rewriting it here would be one more place for
    # the two to disagree.
    relay_seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(relay --cmd 'echo before; exit 7' -S web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "via jump01"
    assert_contains "$out" "NONZERO"
    assert_contains "$out" "exit 7"
    assert_eq "$(cat out/echo~web01)" "before"
}

t_a_table_built_over_there_lands_here() {
    relay_seed
    cd "$TEST_TMPDIR"
    relay --cmd 'echo "a=1"; echo "b=2"' -S web01 -d out --tsv m.tsv --quiet
    assert_eq "$(head -1 m.tsv)" "$(printf 'date\thost\ta\tb')"
    assert_eq "$(sed -n 2p m.tsv | cut -f2-)" "$(printf 'web01\t1\t2')"
    # A second pass appends to this side's table, header and all -- one
    # header, not one per pass.
    relay --cmd 'echo "a=3"; echo "b=4"' -S web01 -d out --tsv m.tsv --quiet
    assert_eq "$(grep -c '^date' m.tsv)" "1"
    assert_eq "$(wc -l < m.tsv | tr -d ' ')" "3"
    # And the table is not left in the collection directory.
    assert_no_file "out/relay.tsv"
}

t_a_jump_box_that_cannot_be_reached_says_so() {
    relay_seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_ROOT/jump01/.unreachable"
    set +e
    out="$(relay --cmd 'echo hi' -S web01 -d out 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "the jump box itself did not answer"
}

t_a_relay_cannot_resume_what_it_does_not_keep() {
    relay_seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(relay logs/app.log -S web01 -d out --follow 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "marks"

    set +e
    out="$(relay --cmd 'echo hi' -S web01 -d out --daemon 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--tsv"
}

t_a_dry_run_through_a_jump_box_contacts_nothing() {
    relay_seed
    cd "$TEST_TMPDIR"
    out="$(relay --cmd 'echo hi' -S web01 -d out --dry-run 2>&1)"
    assert_status $? 0
    assert_contains "$out" "via jump01"
    assert_contains "$out" "base64 -d | tar -xf -"
    assert_no_file "out"
    assert_eq "$(wc -l < "$FAKE_SSH_LOG" | tr -d ' ')" "0"
}

t_a_file_comes_back_through_a_jump_box_too() {
    # Not just --cmd: the tar transport works the same way over there.
    relay_seed
    cd "$TEST_TMPDIR"
    relay logs -S web01 -d out --quiet
    assert_status $? 0
    assert_file_exists "out/web01~logs~app.log"
    assert_file_exists "out/web01~logs~sub~other.log"
}

echo "dredge"
# --- a remote tail ---------------------------------------------------------
#
# --follow brings back what was *added*, which means the far side has to
# decide -- per file, against a mark carried over from the last pass --
# between new, longer, rotated and untouched.  These cases are about the
# four decisions and about the mark surviving the pass that made it.

t_follow_brings_back_only_what_was_added() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
    printf 'and a second line\n' >> "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow)"
    # The local copy is the whole log; what crossed the wire is the line
    # that was added to it, which is the entire point.
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'hello from web01\nand a second line')"
    assert_contains "$out" "18B"
}

t_nothing_new_is_an_answer_not_a_failure() {
    # A tail spends most of its life with nothing to say, and a tail that
    # exits 1 every quiet minute is a tail nothing can be built on.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    set +e
    out="$(dr logs/app.log -S web01 -d out --follow)"; rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$out" "UNCHANGED"
    assert_contains "$out" "nothing new"
}

t_a_follow_without_a_directory_is_refused() {
    # The marks live in the collection directory, and without -d every run
    # gets a new one -- so there would be nothing to resume from, and the
    # "tail" would silently be a whole-file copy every time.
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01 --follow 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "-d DIR"
}

t_the_marks_are_kept_where_you_said() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    assert_file_exists "out/dredge-state.json"
    assert_contains "$(cat out/dredge-state.json)" '"offset"'
    dr logs/app.log -S web01 -d out2 --follow --state marks.json --quiet
    assert_file_exists "marks.json"
    assert_no_file "out2/dredge-state.json"
}

t_a_rotated_file_comes_back_whole() {
    # A new inode where the mark says the old one was: resuming at the old
    # offset would hand you the middle of a different file.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    printf 'after the rotation\n' > "$FAKE_ROOT/web01/logs/app.log.new"
    mv "$FAKE_ROOT/web01/logs/app.log.new" "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --replace)"
    assert_contains "$out" "ROTATED"
    assert_eq "$(cat "out/web01~logs~app.log")" "after the rotation"
}

t_a_truncated_file_is_rotated_too() {
    # copytruncate keeps the inode and puts the size back to zero, so the
    # size going *backwards* has to be the second half of the test.
    seed
    cd "$TEST_TMPDIR"
    printf 'aaaa\nbbbb\ncccc\n' > "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --quiet
    printf 'zz\n' > "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --replace)"
    assert_contains "$out" "ROTATED"
    assert_eq "$(cat "out/web01~logs~app.log")" "zz"
}

t_a_directory_follows_what_grew_and_what_appeared() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -S web01 -d out --follow --quiet
    printf 'a new line\n' >> "$FAKE_ROOT/web01/logs/app.log"
    printf 'a new file\n' > "$FAKE_ROOT/web01/logs/third.log"
    dr logs -S web01 -d out --follow --quiet
    assert_eq "$(cat "out/web01~logs~third.log")" "a new file"
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'hello from web01\na new line')"
    # The file nobody touched was not carried again.
    assert_eq "$(cat "out/web01~logs~sub~other.log")" "second file on web01"
}

t_tail_says_where_a_first_sight_starts() {
    # tail -n N -f, exactly: the last N lines to begin with, and byte for
    # byte from there.
    seed
    cd "$TEST_TMPDIR"
    printf 'a\nb\nc\nd\ne\n' > "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --tail 2 --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "$(printf 'd\ne')"
    printf 'f\n' >> "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --tail 2 --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "$(printf 'd\ne\nf')"
}

t_head_and_follow_are_opposite_ideas() {
    seed
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01 -d out --follow --head 5 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--tail N"
}

t_a_commands_answer_follows_too() {
    # No offset to seek to, so the far side compares a checksum of the
    # bytes we already have against the same prefix of this answer.
    seed
    cd "$TEST_TMPDIR"
    printf 'one\n' > "$FAKE_ROOT/web01/logs/growing"
    dr --cmd 'cat logs/growing' -S web01 -d out --follow --quiet
    assert_eq "$(cat "out/cat~web01")" "one"
    set +e
    out="$(dr --cmd 'cat logs/growing' -S web01 -d out --follow)"; rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$out" "UNCHANGED"
    printf 'two\n' >> "$FAKE_ROOT/web01/logs/growing"
    out="$(dr --cmd 'cat logs/growing' -S web01 -d out --follow)"
    assert_eq "$(cat "out/cat~web01")" "$(printf 'one\ntwo')"
    # Four bytes of answer, not eight: the part that was added.
    assert_contains "$out" "4B"
}

t_a_silent_command_under_follow_is_still_a_stream() {
    # The silence rule is for a one-shot collection, where a zero-byte
    # artifact is noise with nothing behind it. Under --follow it would
    # be a different claim: an empty first pass is what UNCHANGED already
    # means on every pass after it, and the mark has to be kept either
    # way or the next pass has nothing to resume from. A stream that is
    # empty the first time it is looked at is a stream, not a failed
    # collection.
    seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_ROOT/web01/logs/growing"
    dr --cmd 'cat logs/growing' -S web01 -d out --follow --quiet
    assert_status $? 0
    assert_file_exists "out/cat~web01"
    # And the follow resumes from it rather than starting over.
    printf 'late\n' >> "$FAKE_ROOT/web01/logs/growing"
    dr --cmd 'cat logs/growing' -S web01 -d out --follow --quiet
    assert_eq "$(cat "out/cat~web01")" "late"
}

t_an_answer_that_changed_from_the_start_comes_back_whole() {
    seed
    cd "$TEST_TMPDIR"
    printf 'first answer\n' > "$FAKE_ROOT/web01/logs/growing"
    dr --cmd 'cat logs/growing' -S web01 -d out --follow --quiet
    printf 'a different answer\n' > "$FAKE_ROOT/web01/logs/growing"
    dr --cmd 'cat logs/growing' -S web01 -d out --follow --replace --quiet
    assert_eq "$(cat "out/cat~web01")" "a different answer"
}

t_the_local_copy_going_missing_drops_the_mark() {
    # "Nothing new" about a file that is no longer here is the most
    # confidently wrong thing this could say.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    rm "out/web01~logs~app.log"
    out="$(dr logs/app.log -S web01 -d out --follow)"
    assert_contains "$out" "RESYNC"
    dr logs/app.log -S web01 -d out --follow --quiet
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
}

t_where_the_new_bytes_land_is_still_yours_to_say() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --replace --quiet
    printf 'the new line\n' >> "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --replace --quiet
    # --replace under --follow: the local file holds the last slice only.
    assert_eq "$(cat "out/web01~logs~app.log")" "the new line"
}

t_two_collections_share_a_directory_without_sharing_marks() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --tag one --quiet
    dr logs/sub/other.log -S web01 -d out --follow --tag two --quiet
    assert_eq "$(cat "out/one~web01~logs~app.log")" "hello from web01"
    assert_eq "$(cat "out/two~web01~logs~sub~other.log")" \
              "second file on web01"
    # Each stream kept its own place: neither pass reported the other's
    # file as new, and neither lost its own.
    out="$(dr logs/app.log -S web01 -d out --follow --tag one)"
    assert_contains "$out" "UNCHANGED"
}

t_marks_that_cannot_be_read_are_refused() {
    # Starting over means every host sending every file again, which on
    # the fleet this is pointed at is the event --follow exists to avoid.
    seed
    cd "$TEST_TMPDIR"
    mkdir -p out
    printf 'not json at all\n' > out/dredge-state.json
    set +e
    out="$(dr logs/app.log -S web01 -d out --follow 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "delete it"
    assert_no_file "out/web01~logs~app.log"
}

t_a_mark_that_makes_no_sense_is_dropped_not_obeyed() {
    # The marks are JSON in a directory you named, so they will be edited
    # by hand. A mark that is not a number is not a mark: the artifact
    # comes back whole rather than the host coming back mysteriously
    # failed from inside a worker thread.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    "$PY" - out/dredge-state.json <<'EOF'
import json, sys
path = sys.argv[1]
doc = json.load(open(path))
key = list(doc["streams"])[0]
doc["streams"][key]["hosts"]["web01"]["logs/app.log"]["offset"] = "banana"
doc["streams"]["junk"] = ["not", "a", "stream"]
json.dump(doc, open(path, "w"))
EOF
    dr logs/app.log -S web01 -d out --follow --replace --quiet
    assert_status $? 0
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
}

t_a_dry_run_of_a_follow_shows_the_resume_table() {
    seed
    cd "$TEST_TMPDIR"
    : > "$FAKE_SSH_LOG"
    out="$(dr logs/app.log -S web01 -d out --follow --dry-run)"
    assert_contains "$out" "META"
    assert_contains "$out" "rotated"
    assert_eq "$(wc -l < "$FAKE_SSH_LOG" | tr -d ' ')" "0"
}

t_the_ceiling_of_a_follow_is_the_new_part() {
    # A log that grows past --max-bytes is still a log being followed;
    # what the ceiling catches is one pass carrying more than it.
    seed
    cd "$TEST_TMPDIR"
    "$PY" -c "
import sys; open(sys.argv[1],'w').write('x' * 3000 + '\n')" \
        "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --max-bytes 5000 --quiet
    assert_file_exists "out/web01~logs~app.log"
    # Now 3000 more bytes on a 4000-byte ceiling: the file is over it,
    # the new part is not, and it still comes back.
    "$PY" -c "
import sys; open(sys.argv[1],'a').write('y' * 2000 + '\n')" \
        "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --max-bytes 4000)"
    assert_not_contains "$out" "OVERSIZE"
    assert_contains "$(cat "out/web01~logs~app.log")" "yyy"
}

t_more_than_one_pass_can_carry_is_a_hole_not_a_stop() {
    # The ceiling bounds a pass; it does not end one. Refusing the pass
    # would leave the mark where it was, the next pass would have *more*
    # to carry and would be refused for the same reason, and that file
    # would never be collected again -- the whole of the rest of the log
    # lost to protect the part of it that did not fit.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    "$PY" -c "
import sys; open(sys.argv[1],'a').write('x' * 9000 + '\n')" \
        "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --max-bytes 500)"
    # Losing bytes is a finding, and it is said out loud with its size --
    # but not with the exit status. A log busy enough to outrun its
    # ceiling does it on most passes, and a daemon that called that
    # failure would have an exit status nobody reads.
    assert_status $? 0
    assert_contains "$out" "GAP"
    assert_contains "$out" "not carried"
    # The newest 500 bytes came back, so they are in the local copy.
    assert_contains "$(cat "out/web01~logs~app.log")" "xxx"
    # And the follow is at the end of the file rather than stuck: the
    # next pass carries what was added after it, and nothing else.
    printf 'after the hole\n' >> "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --max-bytes 500)"
    assert_status $? 0
    assert_not_contains "$out" "GAP"
    assert_contains "$out" "15B"
}

t_a_gap_is_in_the_csv_because_it_is_not_in_the_status() {
    # The exit status says nothing about a hole, so the CSV has to: this
    # is the only thing automation can read to find out that part of a
    # log is missing.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    "$PY" -c "
import sys; open(sys.argv[1],'a').write('x' * 9000 + '\n')" \
        "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --max-bytes 500 --csv c.csv \
       --quiet
    assert_status $? 0
    # 9001 bytes added, 500 carried: the rest is the hole, on the row of
    # the file it is a hole in.
    row="$(grep 'web01~logs~app.log' c.csv)"
    assert_contains "$row" ",8501"
    # And a pass with no hole says 0 rather than leaving it to be guessed.
    printf 'small\n' >> "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -S web01 -d out --follow --max-bytes 500 --csv c2.csv \
       --quiet
    assert_contains "$(grep 'web01~logs~app.log' c2.csv)" ",0"
}

t_a_rotation_is_a_seam_not_a_stop() {
    # The complaint this came from: a file that rotates has to go on
    # being followed. It always came back; what could stop it was the
    # ceiling, because after a rotation the whole new file is the new
    # part -- so this drives a rotation *through* the ceiling and then
    # asks for the pass after it.
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --quiet
    # Rotated aside, and what replaces it is over the ceiling.
    mv "$FAKE_ROOT/web01/logs/app.log" "$FAKE_ROOT/web01/logs/app.log.1"
    "$PY" -c "
import sys; open(sys.argv[1],'w').write('n' * 4000 + '\n')" \
        "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --max-bytes 900)"
    assert_status $? 0
    assert_contains "$out" "ROTATED"
    assert_contains "$out" "seam, not a stop"
    # Still being read: the newest bytes of the new file are here.
    assert_contains "$(cat "out/web01~logs~app.log")" "nnn"
    # And still being read on the next pass, from the new file.
    printf 'still following\n' >> "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --max-bytes 900)"
    assert_status $? 0
    assert_not_contains "$out" "ROTATED"
    assert_contains "$(cat "out/web01~logs~app.log")" "still following"
}

t_a_rotation_under_no_ceiling_brings_the_whole_new_file() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --follow --replace --quiet
    mv "$FAKE_ROOT/web01/logs/app.log" "$FAKE_ROOT/web01/logs/app.log.1"
    printf 'brand new\nsecond line\n' > "$FAKE_ROOT/web01/logs/app.log"
    out="$(dr logs/app.log -S web01 -d out --follow --replace)"
    assert_contains "$out" "ROTATED"
    assert_eq "$(cat "out/web01~logs~app.log")" \
              "$(printf 'brand new\nsecond line')"
    assert_not_contains "$out" "GAP"
}

# --- daemon mode -----------------------------------------------------------

t_a_daemon_stops_after_the_passes_it_was_given() {
    seed
    cd "$TEST_TMPDIR"
    out="$(dr logs/app.log -S web01 -d out --daemon --every 1s --passes 2)"
    assert_contains "$out" "pass 1"
    assert_contains "$out" "pass 2"
    assert_not_contains "$out" "pass 3"
}

t_a_daemon_picks_up_what_appeared_between_passes() {
    seed
    cd "$TEST_TMPDIR"
    ( sleep 1; printf 'written between passes\n' \
        >> "$FAKE_ROOT/web01/logs/app.log" ) &
    dr logs/app.log -S web01 -d out --daemon --every 1s --passes 3 --quiet
    wait
    assert_contains "$(cat "out/web01~logs~app.log")" "between passes"
}

t_a_daemon_that_was_asked_to_stop_stops_cleanly() {
    seed
    cd "$TEST_TMPDIR"
    # Not through dr(): backgrounding a shell *function* backgrounds the
    # subshell that runs it, so $! is the subshell and the signal never
    # reaches the tool -- which comes back as 143 from a daemon that
    # exits 0 when you drive it by hand.
    "$PY" "$DR" --ssh "$FAKE_BIN/ssh" logs/app.log -S web01 -d out \
        --daemon --every 30s > d.out 2>&1 &
    pid=$!
    # Long enough for the first pass to have finished and the wait to
    # have started.
    sleep 2
    kill -TERM "$pid"
    set +e
    wait "$pid"; rc=$?
    set -e
    assert_status $rc 0
    assert_contains "$(cat d.out)" "stopped after 1 pass"
    assert_file_exists "out/web01~logs~app.log"
}

t_a_daemon_keeps_going_when_a_host_does_not() {
    seed
    fake_host_unreachable web09
    cd "$TEST_TMPDIR"
    set +e
    out="$(dr logs/app.log -S web01,web09 -d out --daemon --every 1s \
             --passes 2)"; rc=$?
    set -e
    # The failure is a finding in every pass, and the run still made both.
    assert_status $rc 1
    assert_contains "$out" "pass 2"
    assert_contains "$out" "web09"
    assert_file_exists "out/web01~logs~app.log"
}

t_the_csv_of_a_daemon_carries_a_row_per_pass() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -S web01 -d out --daemon --every 1s --passes 2 \
       --csv c.csv --quiet
    assert_eq "$(grep -c '^host,' c.csv)" "1"
    assert_contains "$(cat c.csv)" ",1,0"
    assert_contains "$(cat c.csv)" ",2,1"
}

t_an_interval_that_cannot_mean_anything_is_refused() {
    seed
    cd "$TEST_TMPDIR"
    for bad in 0 -1 wat 5x; do
        set +e
        out="$(dr logs/app.log -S web01 -d out --daemon --every "$bad" 2>&1)"
        rc=$?
        set -e
        assert_status $rc 2 "--every $bad should be a usage error"
    done
    # And a count of passes with nothing to count.
    set +e
    out="$(dr logs/app.log -S web01 -d out --passes 2 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--daemon"
}

t_the_interval_is_spelled_the_way_since_is() {
    got="$("$PY" - "$DR" <<'EOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dredge", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for text in ("45", "45s", "5m", "2h", "1d"):
    sys.stdout.write("%s=%g\n" % (text, mod.parse_interval(text)))
EOF
)"
    assert_contains "$got" "45=45"
    assert_contains "$got" "45s=45"
    assert_contains "$got" "5m=300"
    assert_contains "$got" "2h=7200"
    assert_contains "$got" "1d=86400"
}

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
run_test "a banner does not break a tar"       t_a_login_banner_does_not_break_a_collection
run_test "a banner does not break a relay"     t_a_login_banner_does_not_break_a_jump_box
run_test "a big answer past a banner"          t_a_banner_big_enough_to_fill_the_pipe_is_still_only_a_banner
run_test "a host that only talks is named"     t_a_host_that_only_talks_is_named_for_what_it_said
run_test "a jump box that only talks is named" t_a_jump_box_that_only_talks_is_named_for_what_it_said
run_test "a symlinked path is followed"        t_a_symlinked_path_is_the_file_it_points_at
run_test "a link inside a tree is not"         t_a_link_inside_a_tree_is_still_not_collected
run_test "a write failure is one host's"       t_a_local_write_failure_is_one_hosts_failure
run_test "two paths on one name are caught"    t_two_paths_folding_onto_one_name_are_not_silently_merged
run_test "a stalled transfer is bounded"       t_a_transfer_that_stalls_mid_stream_is_bounded
run_test "a lingering pipe is let go of"       t_something_still_holding_the_pipe_does_not_hang_the_run
run_test "impossible ceilings are refused"     t_ceilings_that_cannot_mean_anything_are_refused
run_test "a command is that host's artifact"   t_a_command_comes_back_as_that_hosts_artifact
run_test "a command arrives as typed"          t_a_command_arrives_exactly_as_typed
run_test "binary output survives"              t_a_command_that_says_nothing_in_words_still_says_it
run_test "stderr comes back in order"          t_stderr_comes_back_with_stdout_in_order
run_test "a failed command is a finding"       t_a_command_that_failed_is_a_finding_not_a_lost_host
run_test "a command that worked is quiet"      t_a_command_that_worked_is_quiet_about_it
run_test "the tag leads the name"              t_the_tag_leads_the_name_so_runs_can_share_a_directory
run_test "a tag cannot smuggle a path"         t_a_tag_cannot_smuggle_a_path_into_the_name
run_test "a suffix goes on every file"         t_a_suffix_goes_on_the_end_of_every_created_file
run_test "a bare suffix gains a dot"           t_a_bare_suffix_gains_a_dot
run_test "a suffix cannot smuggle a path"      t_a_suffix_cannot_smuggle_a_path
run_test "a command and a path are not both"   t_a_command_and_a_path_are_not_both_the_artifact
run_test "a silent command is an empty host"   t_a_command_that_printed_nothing_is_an_empty_host
run_test "a silent command keeps its status"  t_a_silent_command_is_still_a_row_with_its_status
run_test "one silent host is not the run"     t_one_silent_host_does_not_empty_the_run
run_test "a zero-byte file is still a file"   t_a_zero_byte_file_is_still_collected
run_test "an empty host's stderr survives"    t_what_the_far_side_said_survives_an_empty_host
run_test "head and tail cut a command too"     t_head_and_tail_cut_a_commands_output_too
run_test "the server list comes from a file"   t_the_server_list_comes_from_a_file
run_test "the old --hosts is gone"             t_the_old_hosts_spelling_is_gone
run_test "follow carries only what was added"  t_follow_brings_back_only_what_was_added
run_test "nothing new is not a failure"        t_nothing_new_is_an_answer_not_a_failure
run_test "a follow needs a directory"          t_a_follow_without_a_directory_is_refused
run_test "the marks are kept where you said"   t_the_marks_are_kept_where_you_said
run_test "a rotated file comes back whole"     t_a_rotated_file_comes_back_whole
run_test "a truncated file is rotated too"     t_a_truncated_file_is_rotated_too
run_test "a directory follows growth"          t_a_directory_follows_what_grew_and_what_appeared
run_test "tail says where a first sight starts" t_tail_says_where_a_first_sight_starts
run_test "head and follow are opposites"       t_head_and_follow_are_opposite_ideas
run_test "a command's answer follows too"      t_a_commands_answer_follows_too
run_test "a silent follow is a stream"         t_a_silent_command_under_follow_is_still_a_stream
run_test "a changed answer comes back whole"   t_an_answer_that_changed_from_the_start_comes_back_whole
run_test "a missing local copy drops the mark" t_the_local_copy_going_missing_drops_the_mark
run_test "where new bytes land is yours"       t_where_the_new_bytes_land_is_still_yours_to_say
run_test "two follows share a directory"       t_two_collections_share_a_directory_without_sharing_marks
run_test "unreadable marks are refused"        t_marks_that_cannot_be_read_are_refused
run_test "a nonsense mark is dropped"          t_a_mark_that_makes_no_sense_is_dropped_not_obeyed
run_test "a dry run shows the resume table"    t_a_dry_run_of_a_follow_shows_the_resume_table
run_test "a follow's ceiling is the new part"  t_the_ceiling_of_a_follow_is_the_new_part
run_test "too much for one pass is a hole"     t_more_than_one_pass_can_carry_is_a_hole_not_a_stop
run_test "a gap is in the csv, not the status" t_a_gap_is_in_the_csv_because_it_is_not_in_the_status
run_test "a rotation is a seam not a stop"     t_a_rotation_is_a_seam_not_a_stop
run_test "a rotation brings the new file"      t_a_rotation_under_no_ceiling_brings_the_whole_new_file
run_test "a daemon stops after its passes"     t_a_daemon_stops_after_the_passes_it_was_given
run_test "a daemon picks up what appeared"     t_a_daemon_picks_up_what_appeared_between_passes
run_test "a daemon asked to stop stops"        t_a_daemon_that_was_asked_to_stop_stops_cleanly
run_test "a daemon outlives a dead host"       t_a_daemon_keeps_going_when_a_host_does_not
run_test "a daemon's csv has a row per pass"   t_the_csv_of_a_daemon_carries_a_row_per_pass
run_test "an impossible interval is refused"   t_an_interval_that_cannot_mean_anything_is_refused
run_test "an interval is spelled like --since" t_the_interval_is_spelled_the_way_since_is
run_test "a table has date, host, variables"   t_a_table_has_a_date_a_host_and_the_variables
run_test "a second run appends to it"         t_a_second_run_appends_under_the_same_header
run_test "the header does not move"           t_the_header_does_not_move_when_a_name_appears_later
run_test "a missing variable is a blank"      t_a_missing_variable_is_a_blank_not_a_shift
run_test "every promised shape is read"       t_every_promised_shape_is_read
run_test "an unreadable answer is a finding"  t_an_answer_that_will_not_parse_is_a_finding_not_a_gap
run_test "--quiet still says a row is missing" t_quiet_still_says_a_host_has_no_row
run_test "one bad host is not the shape"      t_one_unreadable_host_is_not_blamed_on_the_shape
run_test "--quiet still says a column is new" t_quiet_still_says_a_name_has_no_column
run_test "a value cannot break its row"       t_a_value_cannot_break_the_row_it_is_in
run_test "a silent host gets no row"          t_a_silent_host_gets_no_row
run_test "--columns pins the header"          t_columns_pins_the_header_up_front
run_test "a table needs a whole answer"       t_a_table_needs_a_command_and_a_whole_answer
run_test "a daemon table repeats the command" t_a_daemon_building_a_table_repeats_the_command
run_test "a jump box runs the collection"     t_a_jump_box_runs_the_collection_and_sends_it_back
run_test "a jump box keeps nothing"           t_a_jump_box_keeps_nothing_afterwards
run_test "--keep-relay leaves the spool"      t_keep_relay_leaves_the_spool_to_look_at
run_test "a relay will not wipe your tree"    t_relay_refuses_a_directory_it_did_not_make
run_test "a relay clears its own spool"       t_relay_still_owns_and_clears_its_own_spool
run_test "a relay refuses root or home"       t_relay_refuses_a_root_or_home_before_contacting_anything
run_test "the fleet goes over expanded"       t_the_jump_box_gets_the_fleet_already_expanded
run_test "the far report and status come home" t_the_far_sides_report_and_status_come_home
run_test "a table built over there lands here" t_a_table_built_over_there_lands_here
run_test "an unreachable jump box says so"    t_a_jump_box_that_cannot_be_reached_says_so
run_test "a relay cannot resume"              t_a_relay_cannot_resume_what_it_does_not_keep
run_test "a dry run via a jump box is dry"    t_a_dry_run_through_a_jump_box_contacts_nothing
run_test "a file comes back through it too"   t_a_file_comes_back_through_a_jump_box_too
finish
