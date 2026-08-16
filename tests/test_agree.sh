#!/usr/bin/env bash
# agree: host lists, fan-out, consensus grouping, normalization, safety.
#
# Everything runs through the fake ssh/scp shim, so no network and no
# second machine are involved.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
AG="$BINNACLE_DIR/agree.py"

# The verb comes first, then flags -- so a verb-taking call passes the verb
# as $1 and this helper inserts the transports after it.
ag() { "$PY" "$AG" run --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" "$@"; }
ag_verb() {
    local verb="$1"; shift
    "$PY" "$AG" "$verb" --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" "$@"
}

seed() {
    install_fake_ssh
    for h in node01 node02 node03; do
        fake_host "$h"; echo "openssl-3.0.13" > "$FAKE_ROOT/$h/answer"
    done
    for h in node31 node47; do
        fake_host "$h"; echo "openssl-3.0.2" > "$FAKE_ROOT/$h/answer"
    done
    fake_host_unreachable node09
}

t_top_level_flags_survive_verb_defaulting() {
    # `agree -- cmd` defaults to the run verb, and that defaulting used to
    # swallow --version into a parser which has no such flag.
    out="$("$PY" "$AG" --version)"
    assert_contains "$out" "0."
    "$PY" "$AG" --help > /dev/null
    assert_status $? 0
    out="$("$PY" "$AG" help)"
    assert_contains "$out" "script"
    assert_contains "$out" "doctor"
}

t_ranges_expand() {
    out="$("$PY" "$AG" hosts -H 'node[01-03]')"
    assert_eq "$out" "$(printf 'node01\nnode02\nnode03')"
    out="$("$PY" "$AG" hosts -H 'rack[a-b]-n[1,3]')"
    assert_contains "$out" "racka-n1"
    assert_contains "$out" "rackb-n3"
}

t_host_file_comments_and_order() {
    printf '# a comment\nnode02\n\nnode01  # trailing\nnode02\n' \
        > "$TEST_TMPDIR/hosts.txt"
    out="$("$PY" "$AG" hosts --hosts "$TEST_TMPDIR/hosts.txt")"
    # Input order preserved, duplicates dropped.
    assert_eq "$out" "$(printf 'node02\nnode01')"
}

t_majority_is_baseline_and_minority_is_diffed() {
    seed
    set +e
    out="$(ag -H 'node[01-03],node31,node47' --quiet -- cat answer)"
    rc=$?
    set -e
    assert_status $rc 1                      # divergence, no failures
    assert_contains "$out" "GROUP 1"
    assert_contains "$out" "<- baseline"
    assert_contains "$out" "node01 node02 node03"
    assert_contains "$out" "-openssl-3.0.13"
    assert_contains "$out" "+openssl-3.0.2"
}

t_unreachable_is_a_group_not_an_error() {
    seed
    set +e
    out="$(ag -H 'node[01-03],node09' --quiet -- cat answer)"
    rc=$?
    set -e
    assert_status $rc 3                      # a failure outranks divergence
    assert_contains "$out" "unreachable"
    assert_contains "$out" "node09"
    assert_contains "$out" "never answered"
}

t_unanimous_exits_zero() {
    seed
    set +e
    ag -H 'node[01-03]' --quiet -- cat answer >/dev/null
    rc=$?
    set -e
    assert_status $rc 0
}

t_same_output_different_exit_codes_split() {
    # Identical stdout but a different status is not the same answer.
    install_fake_ssh
    fake_host a; fake_host b
    printf 'echo same\n' > "$FAKE_ROOT/a/run.sh"
    printf 'echo same; exit 3\n' > "$FAKE_ROOT/b/run.sh"
    set +e
    out="$(ag -H a,b --quiet -- sh run.sh)"
    set -e
    assert_contains "$out" "GROUP 1"
    assert_contains "$out" "GROUP 2"
    assert_contains "$out" "exit:3"
}

t_order_is_host_list_order_not_completion() {
    install_fake_ssh
    fake_host slow; fake_host fast
    printf 'sleep 0.4; echo x\n' > "$FAKE_ROOT/slow/run.sh"
    printf 'echo x\n'            > "$FAKE_ROOT/fast/run.sh"
    # Run it twice; only the recorded order is under test.
    ag -H slow,fast --quiet --csv "$TEST_TMPDIR/1.csv" -- sh run.sh >/dev/null
    ag -H slow,fast --quiet --csv "$TEST_TMPDIR/2.csv" -- sh run.sh >/dev/null
    assert_eq "$(cut -d, -f1 < "$TEST_TMPDIR/1.csv")" \
              "$(cut -d, -f1 < "$TEST_TMPDIR/2.csv")"
    assert_contains "$(cat "$TEST_TMPDIR/1.csv")" "slow"
}

t_mask_hosts_makes_hostname_output_agree() {
    install_fake_ssh
    for h in n1 n2; do fake_host "$h"; done
    # Each host echoes its own name: byte-exact can never agree.
    set +e
    # The single quotes are the point: $PWD must expand on the
    # far side, not here.
    # shellcheck disable=SC2016
    strict="$(ag -H n1,n2 --quiet --strict -- 'echo $(basename $PWD)')"
    set -e
    assert_contains "$strict" "GROUP 2"
    # shellcheck disable=SC2016
    loose="$(ag -H n1,n2 --quiet --mask-hosts -- 'echo $(basename $PWD)')"
    assert_not_contains "$loose" "GROUP 2"
    assert_contains "$loose" "%HOST%"
}

t_normalizations_are_disclosed() {
    seed
    out="$(ag -H node01 --quiet --loose -- cat answer)"
    assert_contains "$out" "normalized:"
    assert_contains "$out" "mask-numbers"
    # And the report warns that masking numbers can manufacture agreement.
    assert_contains "$out" "you masked numbers"
}

t_sort_lines_ignores_order() {
    install_fake_ssh
    fake_host a; fake_host b
    printf 'printf "x\\ny\\n"\n' > "$FAKE_ROOT/a/run.sh"
    printf 'printf "y\\nx\\n"\n' > "$FAKE_ROOT/b/run.sh"
    set +e
    plain="$(ag -H a,b --quiet -- sh run.sh)"
    set -e
    assert_contains "$plain" "GROUP 2"
    sorted="$(ag -H a,b --quiet --sort-lines -- sh run.sh)"
    assert_not_contains "$sorted" "GROUP 2"
}

t_danger_guard_refuses_without_yes() {
    seed
    set +e
    out="$(ag -H node01 -- rm -rf /var/log 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "refusing"
    assert_contains "$out" "--yes"
    assert_eq "$(ssh_calls)" "0"
}

t_dry_run_contacts_nobody() {
    seed
    out="$(ag -H 'node[01-03]' --dry-run -- uname -r)"
    assert_contains "$out" "BatchMode=yes"
    assert_eq "$(ssh_calls)" "0"
}

t_first_limits_the_blast_radius() {
    seed
    ag -H 'node[01-03]' --first 2 --quiet -- cat answer >/dev/null 2>&1 || true
    assert_eq "$(ssh_calls)" "2"
}

t_sudo_is_always_non_interactive() {
    seed
    ag -H node01 --sudo --dry-run -- id > "$TEST_TMPDIR/out" 2>&1
    assert_contains "$(cat "$TEST_TMPDIR/out")" "sudo -n id"
}

t_script_pushes_runs_and_cleans_up() {
    install_fake_ssh
    fake_host a
    printf '#!/bin/sh\necho hello from script\n' > "$TEST_TMPDIR/s.sh"
    chmod +x "$TEST_TMPDIR/s.sh"
    out="$(ag_verb script "$TEST_TMPDIR/s.sh" -H a --quiet --remote-dir agreetmp -- )"
    assert_contains "$out" "hello from script"
    log="$(cat "$FAKE_SSH_LOG")"
    assert_contains "$log" "scp a agreetmp"
    assert_contains "$log" "chmod +x"
    assert_contains "$log" "rm -rf agreetmp"
    assert_no_file "$FAKE_ROOT/a/agreetmp/s.sh"
}

t_merge_csv_stacks_and_prefixes_ssh_host() {
    install_fake_ssh
    for h in a b; do
        fake_host "$h"
        printf 'printf "host,rule\\n%s,OK\\n"\n' "$h" > "$FAKE_ROOT/$h/run.sh"
    done
    # Hosts a and b give different answers, so agree exits 1 by design.
    ag -H a,b --quiet --merge-csv "$TEST_TMPDIR/m.csv" -- sh run.sh \
        >/dev/null 2>&1 || true
    body="$(cat "$TEST_TMPDIR/m.csv")"
    assert_eq "$(head -1 "$TEST_TMPDIR/m.csv")" "ssh_host,host,rule"
    assert_contains "$body" "a,a,OK"
    assert_contains "$body" "b,b,OK"
    # The header must appear exactly once, not once per host.
    assert_eq "$(grep -c 'ssh_host' "$TEST_TMPDIR/m.csv")" "1"
}

t_mask_hosts_masks_names_not_substrings() {
    # A plain substring replace turns a host called "sql" into %HOST%
    # inside "postgresql", which corrupts the comparison it was meant to
    # enable -- and does it differently on each host, producing exactly the
    # spurious groups it exists to remove.
    install_fake_ssh
    for h in sql db; do
        fake_host "$h"
        printf 'echo "postgresql restarted on %s, adb loaded"\n' "$h" \
            > "$FAKE_ROOT/$h/run.sh"
    done
    out="$(ag -H sql,db --quiet --mask-hosts -- sh run.sh 2>&1 || true)"
    assert_contains "$out" "postgresql"
    assert_contains "$out" "adb"
    assert_contains "$out" "%HOST%"
}

t_mask_hosts_handles_addresses_and_fqdns() {
    # \b is defined against word characters, and half these tokens do not
    # start with one -- \b::1\b can never match, so an IPv6 address would
    # silently stop being masked.  A bare name must still mask the first
    # label of its own FQDN, since that is the part that differs.
    install_fake_ssh
    for h in node01 node02; do
        fake_host "$h"
        printf 'echo "%s.example.com bound ::1 and 10.0.0.9"\n' "$h" \
            > "$FAKE_ROOT/$h/run.sh"
    done
    out="$(ag -H node01,node02 --quiet --mask-hosts -- sh run.sh 2>&1 || true)"
    assert_contains "$out" "%HOST%.example.com"
    assert_not_contains "$out" "node01.example.com"
    # The two hosts differ only in their own name, so they must now agree.
    assert_not_contains "$out" "GROUP 2"
    # An address must not be masked where it is only a fragment of a
    # longer one: \b treats a dot as a boundary, so \b10.0.0.9\b matches
    # inside 192.10.0.0.9 and would mask a different machine's address.
    fake_host 10.0.0.9
    printf 'echo "peers 10.0.0.9 and 192.10.0.0.9"\n' \
        > "$FAKE_ROOT/10.0.0.9/run.sh"
    frag="$(ag -H 10.0.0.9 --quiet --mask-hosts -- sh run.sh 2>&1 || true)"
    assert_contains "$frag" "peers %HOST% and 192.10.0.0.9"
}

t_ipv6_is_refused_rather_than_mis_parsed() {
    # rsplit on the last colon turns "::1" into the address ":" on port 1,
    # so every v6 host in a list used to collapse to the same nonsense
    # entry and the fan-out silently contacted nothing.  netmesh already
    # refuses these; the other two now agree.
    set +e
    out="$("$PY" "$AG" hosts -H '::1' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "IPv6"
    set +e
    br="$("$PY" "$AG" hosts -H 'v6=[fe80::1]:2222' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$br" "IPv6"
    # v4 with a port is untouched, and the port survives.
    ok="$("$PY" "$AG" hosts -H '10.0.0.9:2222')"
    assert_eq "$ok" "10.0.0.9=10.0.0.9:2222"
}

t_mask_times_covers_the_machine_readable_one() {
    # Every tool in this package stamps its CSV with epoch seconds, so two
    # hosts answering either side of a tick would otherwise never agree.
    install_fake_ssh
    fake_host a
    printf 'echo "node,1786741765,OK"\n' > "$FAKE_ROOT/a/run.sh"
    fake_host b
    printf 'echo "node,1786741766,OK"\n' > "$FAKE_ROOT/b/run.sh"
    out="$(ag -H a,b --quiet --mask-times -- sh run.sh 2>&1 || true)"
    assert_contains "$out" "1 group"
    assert_contains "$out" "<TS>"
    # An ordinary ten-digit number is not a date and must survive.
    fake_host c
    printf 'echo "bytes 9876543210"\n' > "$FAKE_ROOT/c/run.sh"
    plain="$(ag -H c --quiet --mask-times -- sh run.sh 2>&1 || true)"
    assert_contains "$plain" "9876543210"
}

echo "agree"
run_test "top-level flags survive defaulting"  t_top_level_flags_survive_verb_defaulting
run_test "ranges expand"                       t_ranges_expand
run_test "host file: comments, order, dedup"   t_host_file_comments_and_order
run_test "majority baseline, minority diffed"  t_majority_is_baseline_and_minority_is_diffed
run_test "unreachable is a group"              t_unreachable_is_a_group_not_an_error
run_test "unanimous exits 0"                   t_unanimous_exits_zero
run_test "same output, different rc, splits"   t_same_output_different_exit_codes_split
run_test "output in host-list order"           t_order_is_host_list_order_not_completion
run_test "--mask-hosts makes hostnames agree"  t_mask_hosts_makes_hostname_output_agree
run_test "--mask-hosts masks names not text"   t_mask_hosts_masks_names_not_substrings
run_test "--mask-hosts handles addresses"      t_mask_hosts_handles_addresses_and_fqdns
run_test "ipv6 refused, not mis-parsed"        t_ipv6_is_refused_rather_than_mis_parsed
run_test "--mask-times covers epoch seconds"   t_mask_times_covers_the_machine_readable_one
run_test "normalizations are disclosed"        t_normalizations_are_disclosed
run_test "--sort-lines ignores order"          t_sort_lines_ignores_order
run_test "danger guard refuses without --yes"  t_danger_guard_refuses_without_yes
run_test "--dry-run contacts nobody"           t_dry_run_contacts_nobody
run_test "--first limits the blast radius"     t_first_limits_the_blast_radius
run_test "sudo is always -n"                   t_sudo_is_always_non_interactive
run_test "script pushes, runs, cleans up"      t_script_pushes_runs_and_cleans_up
run_test "--merge-csv stacks with ssh_host"    t_merge_csv_stacks_and_prefixes_ssh_host
finish
