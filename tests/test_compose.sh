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

# --fleet-csv is exactly --mask-hosts --mask-times, and both are needed:
#   --mask-hosts   every row starts with the machine's own name, so without
#                  it no two hosts can ever agree about anything
#   --mask-times   every row carries an epoch, so two hosts answering either
#                  side of a tick would land in different groups
# Neither touches --merge-csv, which stacks the raw output.
FLEET_NORM=(--fleet-csv)

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

t_fleet_csv_is_the_two_flags_and_says_so() {
    # A preset that hid what it did would break the rule that every
    # normalization is disclosed with the result.
    setup_three_hosts
    seed_why_slow
    preset="$(fleet why_slow.py --fleet-csv \
                -- --from-facts facts.json --csv 2>&1 || true)"
    spelt="$(fleet why_slow.py --mask-hosts --mask-times \
               -- --from-facts facts.json --csv 2>&1 || true)"
    assert_contains "$preset" "2 groups"
    assert_contains "$preset" "mask-hosts"
    assert_contains "$preset" "mask-times"
    # Identical but for the timing line, so compare the grouping itself.
    assert_eq "$(printf '%s' "$preset" | grep -c GROUP)" \
              "$(printf '%s' "$spelt" | grep -c GROUP)"
    # It contradicts --strict, and says so rather than silently picking.
    set +e
    out="$("$PY" "$AG" hosts -H node01 --strict --fleet-csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "contradict"
}

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

t_every_flag_appears_in_its_tools_help() {
    # "Documentation cannot drift" is enforced two ways -- --help prints the
    # module docstring, and the CLI reference is generated from the live
    # parsers -- but nothing checked that the hand-written docstring lists
    # every flag the parser accepts.  --fleet-csv was added and worked and
    # was invisible to --help, and it was not the only one.
    #
    # netmesh is exempt and says so in its own words: its docstring lists
    # "Common options" and it carries a `help` verb that prints every flag
    # of every verb, which is checked separately below.
    "$PY" - "$BINNACLE_DIR" <<'EOF'
import argparse, importlib.util, os, sys

root = sys.argv[1]
SKIP = {"--help", "--version"}
bad = []


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(root, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def walk(parser, flags, seen):
    if not isinstance(parser, argparse.ArgumentParser) or id(parser) in seen:
        return
    seen.add(id(parser))
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                walk(sub, flags, seen)
            continue
        for opt in action.option_strings:
            if opt.startswith("--"):
                flags.add(opt)


for name in ("why_slow", "agree", "logtriage", "reachable", "resolve",
             "during"):
    mod = load(name)
    built = mod.build_parser()
    parsers = list(built) if isinstance(built, tuple) else [built]
    flags, seen = set(), set()
    for parser in parsers:
        walk(parser, flags, seen)
    for flag in sorted(flags - SKIP):
        if flag not in (mod.__doc__ or ""):
            bad.append("%s: %s is not in --help" % (name, flag))

if bad:
    sys.stderr.write("\n".join(bad) + "\n")
    sys.exit(1)
EOF
    assert_status $? 0
    # netmesh's escape hatch has to actually work: its help verb must name
    # the per-verb flags its docstring leaves out.
    nm="$("$PY" "$BINNACLE_DIR/netmesh.py" help)"
    assert_contains "$nm" "--pairs"
    assert_contains "$nm" "--mtu-ceiling"
}

t_declared_copies_have_not_drifted() {
    # No module imports another -- each tool is scp'd to machines that have
    # never heard of this package -- so helpers are duplicated on purpose
    # and each copy names its canonical one.  That arrangement is exactly
    # what shared_tools exists because of: "the two copies that once
    # existed drifted apart".  Nothing checked these had not.
    "$PY" - "$BINNACLE_DIR" <<'EOF'
import ast, os, sys

root = sys.argv[1]
bad = []

# (name, canonical module, copy module) for every pair declared verbatim.
VERBATIM = [
    ("_read", "why_slow.py", "during.py"),
    ("_read_int", "why_slow.py", "during.py"),
    ("read_vmstat", "why_slow.py", "during.py"),
    ("read_meminfo", "why_slow.py", "during.py"),
    ("read_pressure", "why_slow.py", "during.py"),
    ("_read", "why_slow.py", "resolve.py"),
    ("expand_range", "agree.py", "reachable.py"),
    ("split_host_port", "agree.py", "reachable.py"),
    ("is_ipv6", "agree.py", "reachable.py"),
]


def body(mod, name):
    """A function's code with its docstring dropped: the prose is allowed
    to differ per tool, the behaviour is not."""
    src = open(os.path.join(root, mod)).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
            return ast.dump(node)
    return None


for name, canon, copy in VERBATIM:
    a, b = body(canon, name), body(copy, name)
    if a is None:
        bad.append("%s: %s is gone from the canonical copy" % (canon, name))
    elif b is None:
        bad.append("%s: %s is gone" % (copy, name))
    elif a != b:
        bad.append("%s has drifted from %s in %s" % (name, canon, copy))

# Every "canonical copy: <path>" pointer must name a file that exists --
# this package moved from scripts/ to binnacle/ once already.
for fname in sorted(os.listdir(root)):
    if not fname.endswith(".py"):
        continue
    for line in open(os.path.join(root, fname)):
        if "canonical copy" not in line:
            continue
        for token in line.replace(",", " ").split():
            if token.endswith(".py") and "/" in token:
                if not os.path.exists(os.path.join(root, "..", token)):
                    bad.append("%s points at %s, which does not exist"
                               % (fname, token))

if bad:
    sys.stderr.write("\n".join(bad) + "\n")
    sys.exit(1)
EOF
    assert_status $? 0
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
run_test "--fleet-csv is the two flags"         t_fleet_csv_is_the_two_flags_and_says_so
run_test "the tools share one csv header"       t_the_diagnostic_tools_share_one_csv_header
run_test "every flag appears in --help"         t_every_flag_appears_in_its_tools_help
run_test "declared copies have not drifted"     t_declared_copies_have_not_drifted
run_test "an unreachable host is a group"       t_an_unreachable_host_is_a_group_not_an_error
finish
