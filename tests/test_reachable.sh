#!/usr/bin/env bash
# reachable: probing, file preservation, and the restore-on-recovery loop.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
RE="$BINNACLE_DIR/reachable.py"

# Fake ssh/ping driven by two files the test writes: $TEST_TMPDIR/ssh_ok and
# ping_ok list the hosts that answer, so a case states the world it wants.
install_fake_probes() {
    cat > "$FAKE_BIN/ssh" <<'SHIM'
#!/bin/bash
host=""
while [ $# -gt 0 ]; do
  case "$1" in -o) shift 2;; -p) shift 2;;
    *) if [ -z "$host" ]; then host="${1#*@}"; fi; shift;;
  esac
done
if grep -qx "$host" "$TEST_TMPDIR/ssh_ok" 2>/dev/null; then exit 0; fi
if grep -qx "$host" "$TEST_TMPDIR/ssh_auth" 2>/dev/null; then
  echo "$host: Permission denied (publickey)." >&2; exit 255
fi
if grep -qx "$host" "$TEST_TMPDIR/ssh_refused" 2>/dev/null; then
  echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255
fi
if grep -qx "$host" "$TEST_TMPDIR/ssh_dns" 2>/dev/null; then
  echo "ssh: Could not resolve hostname $host: Name or service not known" >&2
  exit 255
fi
echo "ssh: connect to host $host port 22: Connection timed out" >&2
exit 255
SHIM
    cat > "$FAKE_BIN/ping" <<'SHIM'
#!/bin/bash
for a in "$@"; do host="$a"; done
grep -qx "$host" "$TEST_TMPDIR/ping_ok" 2>/dev/null && exit 0
exit 1
SHIM
    chmod +x "$FAKE_BIN/ssh" "$FAKE_BIN/ping"
    : > "$TEST_TMPDIR/ssh_ok"; : > "$TEST_TMPDIR/ping_ok"
    : > "$TEST_TMPDIR/ssh_auth"; : > "$TEST_TMPDIR/ssh_refused"
    : > "$TEST_TMPDIR/ssh_dns"
}

re() {
    "$PY" "$RE" --ssh "$FAKE_BIN/ssh" --ping "$FAKE_BIN/ping" "$@"
}

t_comments_out_only_the_failures() {
    install_fake_probes
    printf 'web01\nweb02\ndb07\n' > "$TEST_TMPDIR/h.txt"
    printf 'web01\nweb02\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    rc=$?
    set -e
    assert_status $rc 1                 # some host failed
    body="$(cat "$TEST_TMPDIR/out.txt")"
    assert_contains "$body" "web01"
    assert_contains "$body" "# db07"
    assert_contains "$body" "#[unreachable]"
    assert_not_contains "$body" "# web01"
}

t_all_reachable_exits_zero_and_changes_nothing() {
    install_fake_probes
    printf '# a note\n\nweb01\nweb02  # inline\n' > "$TEST_TMPDIR/h.txt"
    printf 'web01\nweb02\n' > "$TEST_TMPDIR/ssh_ok"
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    assert_status $? 0
    assert_eq "$(cat "$TEST_TMPDIR/out.txt")" "$(cat "$TEST_TMPDIR/h.txt")"
}

t_preserves_comments_blanks_and_inline_notes() {
    install_fake_probes
    printf '# production tier\n\nweb01  # the fast one\ndb07\n\n# trailing note\n' \
        > "$TEST_TMPDIR/h.txt"
    printf 'web01\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    set -e
    body="$(cat "$TEST_TMPDIR/out.txt")"
    assert_contains "$body" "# production tier"
    assert_contains "$body" "web01  # the fast one"
    assert_contains "$body" "# trailing note"
    # The blank lines survive, so the file still reads the way it was written.
    assert_eq "$(grep -c '^$' "$TEST_TMPDIR/out.txt")" "2"
}

t_ssh_beats_ping_when_icmp_is_blocked() {
    # A host answering ssh must be kept even with no ping: dropping it would
    # discard a working machine on any network that filters ICMP.
    install_fake_probes
    printf 'noicmp\n' > "$TEST_TMPDIR/h.txt"
    printf 'noicmp\n' > "$TEST_TMPDIR/ssh_ok"
    : > "$TEST_TMPDIR/ping_ok"
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    assert_status $? 0
    assert_eq "$(cat "$TEST_TMPDIR/out.txt")" "noicmp"
}

t_restores_hosts_that_come_back() {
    install_fake_probes
    printf 'web01\ndb07\n' > "$TEST_TMPDIR/h.txt"
    printf 'web01\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    set -e
    assert_contains "$(cat "$TEST_TMPDIR/h.txt")" "# db07"

    # db07 comes back.
    printf 'web01\ndb07\n' > "$TEST_TMPDIR/ssh_ok"
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    assert_status $? 0
    # Restored to exactly the original line, marker and all removed.
    assert_eq "$(cat "$TEST_TMPDIR/h.txt")" "$(printf 'web01\ndb07')"
}

t_restore_keeps_the_inline_comment() {
    install_fake_probes
    printf 'db07  # the important one\n' > "$TEST_TMPDIR/h.txt"
    : > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    set -e
    printf 'db07\n' > "$TEST_TMPDIR/ssh_ok"
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    assert_eq "$(cat "$TEST_TMPDIR/h.txt")" "db07  # the important one"
}

t_is_idempotent() {
    install_fake_probes
    printf 'web01\ndb07\nghost\n' > "$TEST_TMPDIR/h.txt"
    printf 'web01\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    cp "$TEST_TMPDIR/h.txt" "$TEST_TMPDIR/first.txt"
    re "$TEST_TMPDIR/h.txt" -i --no-backup --quiet
    set -e
    assert_eq "$(cat "$TEST_TMPDIR/h.txt")" "$(cat "$TEST_TMPDIR/first.txt")"
}

t_never_touches_a_hand_written_comment() {
    install_fake_probes
    # A commented host WITHOUT our marker is someone's deliberate note and
    # must be left exactly alone, never re-checked or uncommented.
    printf '# db07 is decommissioned, do not re-add\nweb01\n' \
        > "$TEST_TMPDIR/h.txt"
    printf 'web01\ndb07\n' > "$TEST_TMPDIR/ssh_ok"
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    assert_contains "$(cat "$TEST_TMPDIR/out.txt")" \
        "# db07 is decommissioned, do not re-add"
    assert_not_contains "$(cat "$TEST_TMPDIR/out.txt")" "#[unreachable]"
}

t_outcomes_are_distinguished() {
    install_fake_probes
    printf 'auth01\nrefused1\nghost\ndown1\n' > "$TEST_TMPDIR/h.txt"
    printf 'auth01\n' > "$TEST_TMPDIR/ssh_auth"
    printf 'refused1\n' > "$TEST_TMPDIR/ssh_refused"
    printf 'ghost\n' > "$TEST_TMPDIR/ssh_dns"
    set +e
    re "$TEST_TMPDIR/h.txt" --csv "$TEST_TMPDIR/r.csv" --quiet \
        -o "$TEST_TMPDIR/out.txt"
    set -e
    body="$(cat "$TEST_TMPDIR/r.csv")"
    assert_contains "$body" "auth01,auth01,no,no,no,auth"
    assert_contains "$body" "refused"
    assert_contains "$body" "dns"
    # The reason is written into the file, so a later reader knows why.
    assert_contains "$(cat "$TEST_TMPDIR/out.txt")" "ssh refused your key"
    assert_contains "$(cat "$TEST_TMPDIR/out.txt")" "name does not resolve"
}

t_keep_auth_keeps_provisioning_gaps() {
    install_fake_probes
    printf 'auth01\n' > "$TEST_TMPDIR/h.txt"
    printf 'auth01\n' > "$TEST_TMPDIR/ssh_auth"
    printf 'auth01\n' > "$TEST_TMPDIR/ping_ok"
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet --keep-auth
    assert_status $? 0
    assert_eq "$(cat "$TEST_TMPDIR/out.txt")" "auth01"
}

t_require_modes() {
    install_fake_probes
    printf 'h1\n' > "$TEST_TMPDIR/h.txt"
    printf 'h1\n' > "$TEST_TMPDIR/ssh_ok"     # ssh yes, ping no
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/a.txt" --quiet --require ssh
    assert_status $? 0
    set +e
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/b.txt" --quiet --require ping
    rc=$?
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/c.txt" --quiet --require both
    rc2=$?
    set -e
    assert_status $rc 1
    assert_status $rc2 1
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/d.txt" --quiet --require any
    assert_status $? 0
}

t_range_with_mixed_results_expands() {
    # A range line cannot be half-commented, so it is expanded and each host
    # judged on its own -- anything else keeps dead hosts live or drops good
    # ones.
    install_fake_probes
    printf 'node[01-03]\n' > "$TEST_TMPDIR/h.txt"
    printf 'node02\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    set -e
    body="$(cat "$TEST_TMPDIR/out.txt")"
    assert_contains "$body" "# node01"
    assert_contains "$body" "# node03"
    assert_eq "$(grep -cx 'node02' "$TEST_TMPDIR/out.txt")" "1"
}

t_range_all_good_stays_compact() {
    install_fake_probes
    printf 'node[01-03]\n' > "$TEST_TMPDIR/h.txt"
    printf 'node01\nnode02\nnode03\n' > "$TEST_TMPDIR/ssh_ok"
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    assert_eq "$(cat "$TEST_TMPDIR/out.txt")" "node[01-03]"
}

t_in_place_keeps_a_backup() {
    install_fake_probes
    printf 'web01\ndb07\n' > "$TEST_TMPDIR/h.txt"
    printf 'web01\n' > "$TEST_TMPDIR/ssh_ok"
    set +e
    re "$TEST_TMPDIR/h.txt" -i --quiet
    set -e
    assert_file_exists "$TEST_TMPDIR/h.txt.bak"
    assert_eq "$(cat "$TEST_TMPDIR/h.txt.bak")" "$(printf 'web01\ndb07')"
}

t_dry_run_contacts_nobody() {
    install_fake_probes
    printf 'web01\nnode[01-02]\n' > "$TEST_TMPDIR/h.txt"
    out="$(re "$TEST_TMPDIR/h.txt" --dry-run)"
    assert_eq "$out" "$(printf 'web01\nnode01\nnode02')"
}

t_name_equals_address_form() {
    install_fake_probes
    printf 'web01=10.0.0.5\n' > "$TEST_TMPDIR/h.txt"
    printf '10.0.0.5\n' > "$TEST_TMPDIR/ssh_ok"   # the address is contacted
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet
    assert_status $? 0
    assert_eq "$(cat "$TEST_TMPDIR/out.txt")" "web01=10.0.0.5"
}

t_recheck_only_touches_commented_lines() {
    install_fake_probes
    printf 'web01\n# db07  #[unreachable] no ping, no ssh - 2026-01-01\n' \
        > "$TEST_TMPDIR/h.txt"
    printf 'db07\n' > "$TEST_TMPDIR/ssh_ok"    # web01 would fail if checked
    re "$TEST_TMPDIR/h.txt" -o "$TEST_TMPDIR/out.txt" --quiet --recheck-only
    body="$(cat "$TEST_TMPDIR/out.txt")"
    assert_contains "$body" "db07"
    assert_not_contains "$body" "#[unreachable]"
    # web01 was never contacted, so it stays exactly as written.
    assert_contains "$body" "web01"
    assert_not_contains "$body" "# web01"
}

echo "reachable"
run_test "comments out only the failures"      t_comments_out_only_the_failures
run_test "all reachable: exit 0, no change"    t_all_reachable_exits_zero_and_changes_nothing
run_test "preserves comments, blanks, notes"   t_preserves_comments_blanks_and_inline_notes
run_test "ssh beats ping when icmp is blocked" t_ssh_beats_ping_when_icmp_is_blocked
run_test "restores hosts that come back"       t_restores_hosts_that_come_back
run_test "restore keeps the inline comment"    t_restore_keeps_the_inline_comment
run_test "is idempotent"                       t_is_idempotent
run_test "never touches a hand-written note"   t_never_touches_a_hand_written_comment
run_test "outcomes are distinguished"          t_outcomes_are_distinguished
run_test "--keep-auth keeps provisioning gaps" t_keep_auth_keeps_provisioning_gaps
run_test "--require modes"                     t_require_modes
run_test "mixed range expands"                 t_range_with_mixed_results_expands
run_test "all-good range stays compact"        t_range_all_good_stays_compact
run_test "-i keeps a backup"                   t_in_place_keeps_a_backup
run_test "--dry-run contacts nobody"           t_dry_run_contacts_nobody
run_test "name=address form"                   t_name_equals_address_form
run_test "--recheck-only is narrow"            t_recheck_only_touches_commented_lines
finish
