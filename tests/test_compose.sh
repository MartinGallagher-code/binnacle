#!/usr/bin/env bash
# compose: the claim on the front page, actually run.
#
# "44 healthy, 4 agree they are swapping, 2 unreachable -- in one command"
# is the reason this package exists rather than five separate repositories,
# and until this file existed nothing tested it.  Every case here pushes a
# real tool to fake hosts through real agree, and asserts on the grouping.
#
# The hosts differ because each carries its own fact or sample file, which
# is the same seam the per-tool suites use: no load, no benchmark, no
# resolver, and hosts that disagree on demand.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"

AG="$BINNACLE_DIR/agree.py"

# The two normalizations every one of these needs, and why:
#   --mask-hosts   every row starts with the machine's own name, so without
#                  it no two hosts can ever agree about anything
#   --mask-times   every row carries an epoch, so two hosts answering either
#                  side of a tick would land in different groups
# Neither touches --merge-csv, which stacks the raw output.
FLEET_NORM=(--mask-hosts --mask-times)

fleet() {
    # fleet TOOL -- ARGS...  : push TOOL to node01..node03 and group them
    local tool="$1"; shift
    "$PY" "$AG" script "$BINNACLE_DIR/$tool" -H node01,node02,node03 \
        --quiet --remote-dir agreetmp "$@"
}

setup_three_hosts() {
    install_fake_ssh
    export PATH="$FAKE_BIN:$PATH"
    for h in node01 node02 node03; do
        fake_host "$h"
    done
}

# why-slow ------------------------------------------------------------------

seed_why_slow() {
    # node01 and node02 are swapping; node03 is healthy.
    for h in node01 node02; do
        facts_json "$FAKE_ROOT/$h/facts.json" \
            "sys.hostname=$h" mem.pswpin_per_s=14200 \
            mem.pswpout_per_s=9800 mem.available_pct=0.6
    done
    facts_json "$FAKE_ROOT/node03/facts.json" sys.hostname=node03
}

t_fleet_triage_groups_hosts_by_what_is_wrong() {
    setup_three_hosts
    seed_why_slow
    out="$(fleet why_slow.py "${FLEET_NORM[@]}" \
             -- --from-facts facts.json --csv 2>&1 || true)"
    assert_contains "$out" "2 groups"
    # The two swapping hosts are one group, and the group says what they
    # agree about rather than merely that they agree.
    assert_contains "$out" "node01 node02"
    assert_contains "$out" "SWAP_THRASH"
}

t_without_masking_every_host_is_its_own_group() {
    # Why the two flags above are not optional: the host column and the
    # epoch differ by construction, so identical diagnoses do not group.
    setup_three_hosts
    seed_why_slow
    out="$(fleet why_slow.py -- --from-facts facts.json --csv 2>&1 || true)"
    assert_contains "$out" "3 groups"
}

t_merge_csv_keeps_the_values_masking_hid() {
    # Masking is a comparison concern only: the merged artifact has to keep
    # the real host and the real timestamp, or the grouping would be bought
    # by destroying the data.
    setup_three_hosts
    seed_why_slow
    fleet why_slow.py "${FLEET_NORM[@]}" \
        --merge-csv "$TEST_TMPDIR/triage.csv" \
        -- --from-facts facts.json --csv >/dev/null 2>&1 || true
    assert_file_exists "$TEST_TMPDIR/triage.csv"
    body="$(cat "$TEST_TMPDIR/triage.csv")"
    assert_eq "$(head -1 "$TEST_TMPDIR/triage.csv")" \
        "ssh_host,host,ts,rule_id,severity,title,detail,fix"
    assert_contains "$body" "node01,node01,"
    assert_not_contains "$body" "%HOST%"
    assert_not_contains "$body" "<TS>"
    # One header for the whole file, not one per host.
    assert_eq "$(grep -c 'ssh_host' "$TEST_TMPDIR/triage.csv")" "1"
}

# resolve -------------------------------------------------------------------

t_resolve_groups_hosts_by_what_a_name_resolves_to() {
    setup_three_hosts
    # node01 and node02 agree about db01; node03's resolvers disagree with
    # each other, which is a different finding and so a different group.
    for h in node01 node02; do
        resolve_facts_json "$FAKE_ROOT/$h/dns.json" "sys.hostname=$h"
    done
    resolve_facts_json "$FAKE_ROOT/node03/dns.json" sys.hostname=node03 \
        'names.disagree=["db01.example.com"]' \
        'names.all={"db01.example.com": {"answers": {"10.0.0.53": ["10.0.0.7"], "10.0.0.54": ["192.0.2.9"]}, "hosts": null, "by_server": {}, "distinct": 2}}'
    out="$(fleet resolve.py "${FLEET_NORM[@]}" \
             -- --from-facts dns.json --csv 2>&1 || true)"
    assert_contains "$out" "2 groups"
    assert_contains "$out" "node01 node02"
    assert_contains "$out" "NS_DISAGREE"
}

# during --------------------------------------------------------------------

t_during_groups_hosts_by_what_limited_them() {
    setup_three_hosts
    # Two boxes were cpu bound; one was waiting on its disk the whole time.
    for h in node01 node02; do
        during_samples "$FAKE_ROOT/$h/run.csv" 30 "host=$h" \
            cpu_busy_pct=95 cpu_max_core_pct=97
    done
    during_samples "$FAKE_ROOT/node03/run.csv" 30 host=node03 \
        cpu_busy_pct=15 cpu_max_core_pct=20 disk_util_pct=98 \
        psi_io_full=40 disk_await_ms=90
    out="$(fleet during.py "${FLEET_NORM[@]}" \
             -- --from-samples run.csv --csv 2>&1 || true)"
    assert_contains "$out" "2 groups"
    assert_contains "$out" "node01 node02"
    # The odd host out is named by what bound it, not just as "different".
    assert_contains "$out" "io"
}

# the shared contract -------------------------------------------------------

t_the_diagnostic_tools_share_one_csv_header() {
    # This is what lets `agree --merge-csv` stack them into one file, so it
    # is worth asserting rather than assuming.
    setup_three_hosts
    facts_json "$TEST_TMPDIR/f.json"
    resolve_facts_json "$TEST_TMPDIR/d.json"
    during_samples "$TEST_TMPDIR/s.csv" 30
    a="$("$PY" "$BINNACLE_DIR/why_slow.py" --from-facts "$TEST_TMPDIR/f.json" --csv | head -1)"
    b="$("$PY" "$BINNACLE_DIR/resolve.py" --from-facts "$TEST_TMPDIR/d.json" --csv | head -1)"
    c="$("$PY" "$BINNACLE_DIR/during.py" --from-samples "$TEST_TMPDIR/s.csv" --csv | head -1)"
    assert_eq "$a" "host,ts,rule_id,severity,title,detail,fix"
    assert_eq "$b" "$a"
    assert_eq "$c" "$a"
}

t_an_unreachable_host_is_a_group_not_an_error() {
    # The convention that matters most in a fleet run: a host that never
    # answered has to survive as a finding rather than a line on stderr,
    # because "2 of 3 agree" means nothing if the third was never asked.
    setup_three_hosts
    seed_why_slow
    fake_host_unreachable node03
    out="$(fleet why_slow.py "${FLEET_NORM[@]}" \
             -- --from-facts facts.json --csv 2>&1 || true)"
    assert_contains "$out" "node03"
    # Under `script` the copy is what failed, and the group says so rather
    # than reporting the vaguer "unreachable".
    assert_contains "$out" "push-failed"
    assert_contains "$out" "never ran there"
    # The hosts that did answer are still grouped by what is wrong.
    assert_contains "$out" "node01 node02"
}

echo "compose"
run_test "fleet triage groups by what is wrong" t_fleet_triage_groups_hosts_by_what_is_wrong
run_test "without masking, no host can agree"   t_without_masking_every_host_is_its_own_group
run_test "--merge-csv keeps the real values"    t_merge_csv_keeps_the_values_masking_hid
run_test "resolve groups by what a name means"  t_resolve_groups_hosts_by_what_a_name_resolves_to
run_test "during groups by what limited them"   t_during_groups_hosts_by_what_limited_them
run_test "the tools share one csv header"       t_the_diagnostic_tools_share_one_csv_header
run_test "an unreachable host is a group"       t_an_unreachable_host_is_a_group_not_an_error
finish
