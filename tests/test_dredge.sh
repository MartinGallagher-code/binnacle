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
        assert_file_exists "out/$h/logs/app.log"
        assert_eq "$(cat "out/$h/logs/app.log")" "hello from $h"
    done
}

t_a_directory_comes_back_as_a_tree() {
    seed
    cd "$TEST_TMPDIR"
    dr logs -H web01 -d out --quiet
    assert_file_exists "out/web01/logs/app.log"
    assert_file_exists "out/web01/logs/sub/other.log"
    assert_eq "$(cat out/web01/logs/sub/other.log)" "second file on web01"
}

t_flat_folds_the_path_into_the_name() {
    # One directory, so the next step can be a glob rather than a walk.
    seed
    cd "$TEST_TMPDIR"
    dr logs -H web01,web02 -d out --flat --quiet
    assert_file_exists "out/web01~logs~app.log"
    assert_file_exists "out/web02~logs~sub~other.log"
    assert_eq "$(cat "out/web01~logs~app.log")" "hello from web01"
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
    assert_eq "$(cat out/web01/logs/big.log)" \
              "$(printf 'line 4998\nline 4999\nline 5000')"
}

t_head_brings_back_only_the_start() {
    install_fake_ssh
    fake_host web01
    mkdir -p "$FAKE_ROOT/web01/logs"
    printf 'one\ntwo\nthree\nfour\n' > "$FAKE_ROOT/web01/logs/a.log"
    cd "$TEST_TMPDIR"
    dr logs/a.log -H web01 -d out --head 2 --quiet
    assert_eq "$(cat out/web01/logs/a.log)" "$(printf 'one\ntwo')"
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
    assert_file_exists "out/web01/logs/odd.log"
    got="$("$PY" -c "
import sys; sys.stdout.write(repr(open(sys.argv[1],'rb').read()))" \
        out/web01/logs/odd.log)"
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
    assert_file_exists "out/web01/logs/new.log"
    assert_no_file "out/web01/logs/old.log"
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
    assert_eq "$(cat out/web01/logs/app.log)" \
              "$(printf 'hello from web01\nhello from web01')"
}

t_prepend_puts_it_at_the_other_end() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    printf 'second run\n' > "$FAKE_ROOT/web01/logs/app.log"
    dr logs/app.log -H web01 -d out --prepend --quiet
    assert_eq "$(cat out/web01/logs/app.log)" \
              "$(printf 'second run\nhello from web01')"
}

t_a_mark_shows_where_old_meets_new() {
    seed
    cd "$TEST_TMPDIR"
    dr logs/app.log -H web01 -d out --quiet
    dr logs/app.log -H web01 -d out --append --mark --quiet
    assert_contains "$(cat out/web01/logs/app.log)" "web01"
    assert_contains "$(cat out/web01/logs/app.log)" "====="
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
    assert_eq "$(cat out/web01/logs/app.log)" "hello from web01"
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
    assert_file_exists "out/web01/logs/app.log"
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
    assert_file_exists "out/web01/logs/small.log"
    assert_no_file "out/web01/logs/big.log"
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
    assert_contains "$(cat c.csv)" "web01,logs,out/web01/logs/app.log"
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

echo "dredge"
run_test "a file comes back under its host"    t_a_file_comes_back_under_the_name_of_its_host
run_test "a directory comes back as a tree"    t_a_directory_comes_back_as_a_tree
run_test "flat folds the path into the name"   t_flat_folds_the_path_into_the_name
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
finish
