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

# skew ----------------------------------------------------------------------

t_skew_groups_hosts_by_what_their_clock_is_doing() {
    setup_three_hosts
    # node01 and node02 have working clocks; node03 cannot reach a source,
    # which is the finding that hides behind a healthy-looking daemon.
    for h in node01 node02; do
        skew_facts_json "$FAKE_ROOT/$h/clock.json" "sys.hostname=$h"
    done
    skew_facts_json "$FAKE_ROOT/node03/clock.json" sys.hostname=node03 \
        'srv.dead=["10.0.0.1","10.0.0.2","10.0.0.3"]' 'srv.alive=[]' \
        clock.offset_s=252.0 'clock.spread_s=null' 'srv.max_stratum=null'
    out="$(fleet skew.py "${FLEET_NORM[@]}" \
             -- --from-facts clock.json --csv 2>&1 || true)"
    assert_contains "$out" "2 groups"
    assert_contains "$out" "node01 node02"
    # Named by the cause, not by how far the clock had drifted.
    assert_contains "$out" "NO_SYNC_SOURCE"
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
    skew_facts_json "$TEST_TMPDIR/k.json"
    d="$("$PY" "$BINNACLE_DIR/skew.py" --from-facts "$TEST_TMPDIR/k.json" --csv | head -1)"
    assert_eq "$a" "host,ts,rule_id,severity,title,detail,fix"
    assert_eq "$b" "$a"
    assert_eq "$c" "$a"
    assert_eq "$d" "$a"
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
             "during", "skew", "binnacle", "muster"):
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

t_a_stray_utf8_byte_does_not_lose_the_run() {
    # The floor this package targets is RHEL 8: Python 3.6, often LANG=C,
    # where stdout is ASCII.  One UTF-8 name in a log, a process table, a
    # host list or a remote command used to end the run -- and in resolve's
    # case, to read a perfectly good resolv.conf as empty.
    printf 'Jan  7 04:00:00 web01 sshd[1]: Failed password for caf\xc3\xa9\n' \
        > "$TEST_TMPDIR/utf8.log"
    printf 'web01  # caf\xc3\xa9 rack\nweb02\n' > "$TEST_TMPDIR/hosts.txt"
    printf 'nameserver 10.0.0.53  # caf\xc3\xa9\n' > "$TEST_TMPDIR/resolv.conf"
    printf 'host,elapsed_s,cpu_busy_pct\ncaf\xc3\xa9,1.0,95\ncaf\xc3\xa9,2.0,95\n' \
        > "$TEST_TMPDIR/s.csv"

    # A locale with no room for the byte.  This machine's Python coerces C
    # to UTF-8 for free (PEP 538), so the coercion has to be defeated or
    # the case proves nothing.
    ascii_run() {
        env PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C LANG=C "$@" 2>&1
    }

    out="$(ascii_run "$PY" "$BINNACLE_DIR/logtriage.py" "$TEST_TMPDIR/utf8.log")"
    assert_not_contains "$out" "UnicodeEncodeError"
    assert_contains "$out" "sshd"

    out="$(ascii_run "$PY" "$BINNACLE_DIR/agree.py" hosts \
             --hosts "$TEST_TMPDIR/hosts.txt")"
    assert_not_contains "$out" "UnicodeDecodeError"
    assert_contains "$out" "web01"

    out="$(ascii_run "$PY" "$BINNACLE_DIR/reachable.py" \
             "$TEST_TMPDIR/hosts.txt" --dry-run)"
    assert_not_contains "$out" "UnicodeDecodeError"
    assert_contains "$out" "web02"

    out="$(ascii_run "$PY" "$BINNACLE_DIR/during.py" --from-samples \
             "$TEST_TMPDIR/s.csv")"
    assert_not_contains "$out" "UnicodeDecodeError"
    assert_contains "$out" "VERDICT"

    # resolve did not crash -- it read the file as empty and reported no
    # resolvers at all, which is the worse failure of the two.
    out="$(ascii_run "$PY" "$BINNACLE_DIR/resolve.py" --resolv-conf \
             "$TEST_TMPDIR/resolv.conf" --timeout 0.2 --no-stub --quiet)"
    assert_contains "$out" "1 resolver"
    assert_not_contains "$out" "0 resolvers"
}

t_an_edited_host_file_keeps_the_bytes_it_came_with() {
    # Surviving the read is only half of it.  reachable rewrites the user's
    # own file, so a comment it does not understand has to come back out
    # exactly as it went in.  Reading under an ASCII locale replaces the
    # byte with U+FFFD, which is itself unencodable on the way back -- so
    # the tool read the list, decided correctly, and then died writing it,
    # leaving no output file at all.
    printf 'web01\n# db07 caf\xc3\xa9 rack\nweb02\n' > "$TEST_TMPDIR/h8.txt"
    # Every host fails, so every line is rewritten and the comment between
    # them has to be carried through untouched.
    cat > "$TEST_TMPDIR/ssh_down" <<'SHIM'
#!/bin/bash
exit 255
SHIM
    chmod +x "$TEST_TMPDIR/ssh_down"

    env PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 LC_ALL=C LANG=C \
        "$PY" "$BINNACLE_DIR/reachable.py" "$TEST_TMPDIR/h8.txt" \
        -o "$TEST_TMPDIR/h8.out" --ssh "$TEST_TMPDIR/ssh_down" --no-ping --quiet \
        > "$TEST_TMPDIR/h8.err" 2>&1 || true   # every host down is exit 1, not an error here

    assert_not_contains "$(cat "$TEST_TMPDIR/h8.err")" "UnicodeEncodeError"
    if [ ! -s "$TEST_TMPDIR/h8.out" ]; then
        fail "no output file was written at all"
        return 0
    fi
    # The comment is the user's, not this tool's: it must round-trip as the
    # same bytes, not as question marks and not as U+FFFD.
    if ! grep -aq "$(printf 'caf\xc3\xa9')" "$TEST_TMPDIR/h8.out"; then
        fail "the utf-8 comment did not survive the rewrite"
    fi
    if grep -aq "$(printf '\xef\xbf\xbd')" "$TEST_TMPDIR/h8.out"; then
        fail "the comment came back corrupted to U+FFFD"
    fi
    return 0
}

t_a_file_saved_by_notepad_still_means_the_same_thing() {
    # Host lists get edited wherever the person is, and resolv.confs get
    # pasted through whatever terminal is handy.  Notepad and Excel prepend
    # a UTF-8 BOM, and Windows saves CRLF.  Before this test, the BOM glued
    # itself to the first token: reachable probed <BOM>web01 -- a name that
    # cannot resolve, so a live machine got commented out of the user's
    # file -- and resolve read a healthy box as CRITICAL, no resolvers.
    printf '\xef\xbb\xbfweb01\r\nweb02\r\n' > "$TEST_TMPDIR/bom_hosts.txt"
    printf '\xef\xbb\xbfnameserver 10.0.0.53\r\n' > "$TEST_TMPDIR/bom_resolv.conf"
    printf '\xef\xbb\xbfhost,elapsed_s,cpu_busy_pct\nweb01,1.0,95\nweb01,2.0,95\n' \
        > "$TEST_TMPDIR/bom_s.csv"
    printf '\xef\xbb\xbfJan  7 04:00:00 web01 sshd[1]: error: failed\n' \
        > "$TEST_TMPDIR/bom.log"

    out="$("$PY" "$BINNACLE_DIR/reachable.py" "$TEST_TMPDIR/bom_hosts.txt" \
             --dry-run)"
    # cat -A style check: the first token must be exactly web01, no BOM, no CR
    printf '%s\n' "$out" | head -1 | od -An -c | grep -q "357 273 277" \
        && fail "the BOM is still glued to the first host"
    assert_contains "$out" "web01"

    out="$("$PY" "$BINNACLE_DIR/resolve.py" --resolv-conf \
             "$TEST_TMPDIR/bom_resolv.conf" --timeout 0.2 --no-stub --quiet)"
    assert_contains "$out" "1 resolver"
    assert_not_contains "$out" "no resolvers"

    out="$("$PY" "$BINNACLE_DIR/during.py" --from-samples "$TEST_TMPDIR/bom_s.csv")"
    # the host column used to be read as <BOM>host, and the host became "?"
    assert_contains "$out" "web01"

    out="$("$PY" "$BINNACLE_DIR/logtriage.py" "$TEST_TMPDIR/bom.log")"
    # the BOM used to hide the first line's timestamp
    assert_contains "$out" "04:00:00"

    # a pipe can carry the BOM too
    out="$(printf '\xef\xbb\xbfweb01\n' \
             | "$PY" "$BINNACLE_DIR/reachable.py" - --dry-run)"
    printf '%s\n' "$out" | od -An -c | grep -q "357 273 277" \
        && fail "the BOM survives arriving over stdin"
    assert_contains "$out" "web01"
    return 0
}

t_a_file_that_cannot_be_written_is_a_finding() {
    # Full disk, quota, a typo'd directory: before this test every tool
    # answered with a raw traceback, burying the one fact that matters --
    # which file could not be written.  A missing directory raises the
    # same OSError as ENOSPC through the same code path, and needs no
    # root to stage.
    printf 'Jan  7 04:00:00 web01 sshd[1]: error: x\n' > "$TEST_TMPDIR/w.log"
    printf 'host,elapsed_s,cpu_busy_pct\nweb01,1.0,95\nweb01,2.0,95\n' \
        > "$TEST_TMPDIR/w.csv"
    gone="$TEST_TMPDIR/no/such/dir/out.csv"

    for tool_args in \
        "logtriage.py $TEST_TMPDIR/w.log --csv" \
        "logtriage.py $TEST_TMPDIR/w.log --save-templates" \
        "during.py --from-samples $TEST_TMPDIR/w.csv --csv" \
        "during.py --from-samples $TEST_TMPDIR/w.csv --json" \
        "why_slow.py --quiet --csv" \
    ; do
        # shellcheck disable=SC2086
        set -- $tool_args
        tool="$1"; shift
        rc=0
        out="$("$PY" "$BINNACLE_DIR/$tool" "$@" "$gone" 2>&1)" || rc=$?
        if [ "$rc" -ne 2 ]; then
            fail "$tool $*: expected exit 2 on an unwritable path, got $rc"
        fi
        assert_not_contains "$out" "Traceback"
        assert_contains "$out" "cannot write"
    done

    # A failed write may not leave a stump behind: an empty CSV that
    # parses is worse than a missing one.
    mkdir -p "$TEST_TMPDIR/stump"
    ( cd "$TEST_TMPDIR/stump" && ulimit -f 0 && \
      "$PY" "$BINNACLE_DIR/logtriage.py" "$TEST_TMPDIR/w.log" \
          --csv out.csv >/dev/null 2>&1 ) || true
    if [ -e "$TEST_TMPDIR/stump/out.csv" ]; then
        fail "a failed write left an empty out.csv behind"
    fi
    return 0
}

t_declared_copies_have_not_drifted() {
    # No module imports another -- each tool is scp'd to machines that have
    # never heard of this package -- so helpers are duplicated on purpose
    # and each copy names its canonical one.  That arrangement is exactly
    # what shared_tools exists because of: "the two copies that once
    # existed drifted apart".  Nothing checked these had not.
    "$PY" - "$BINNACLE_DIR" <<'EOF'
import ast, io, os, sys

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
    ("_read", "why_slow.py", "skew.py"),
    # Duplicated into all eight instruments and into the index, so all
    # nine files are held to it.
    ("_stdio_safe", "why_slow.py", "agree.py"),
    ("_stdio_safe", "why_slow.py", "logtriage.py"),
    ("_stdio_safe", "why_slow.py", "netmesh.py"),
    ("_stdio_safe", "why_slow.py", "reachable.py"),
    ("_stdio_safe", "why_slow.py", "resolve.py"),
    ("_stdio_safe", "why_slow.py", "during.py"),
    ("_stdio_safe", "why_slow.py", "skew.py"),
    ("_stdio_safe", "why_slow.py", "binnacle.py"),
    ("_stdio_safe", "why_slow.py", "muster.py"),
    ("_WriteGuard", "why_slow.py", "agree.py"),
    ("_WriteGuard", "why_slow.py", "logtriage.py"),
    ("_WriteGuard", "why_slow.py", "netmesh.py"),
    ("_WriteGuard", "why_slow.py", "reachable.py"),
    ("_WriteGuard", "why_slow.py", "resolve.py"),
    ("_WriteGuard", "why_slow.py", "during.py"),
    ("_WriteGuard", "why_slow.py", "skew.py"),
    ("_WriteGuard", "why_slow.py", "muster.py"),
    ("expand_range", "agree.py", "reachable.py"),
    ("expand_range", "agree.py", "muster.py"),
    ("split_commas", "agree.py", "muster.py"),
    ("split_host_port", "agree.py", "reachable.py"),
    ("is_ipv6", "agree.py", "reachable.py"),
]


def body(mod, name):
    """A function's code with its docstring dropped: the prose is allowed
    to differ per tool, the behaviour is not."""
    # Explicit encoding: under the C locale -- which is what the Python 3.6
    # container runs with -- a bare open() decodes as ASCII, and
    # logtriage's sparkline block is not.
    src = io.open(os.path.join(root, mod), encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) \
                and node.name == name:
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
            for sub in getattr(node, "body", []):
                if isinstance(sub, ast.FunctionDef) \
                        and ast.get_docstring(sub) is not None:
                    sub.body = sub.body[1:]
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
    for line in io.open(os.path.join(root, fname), encoding="utf-8"):
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

t_every_declared_version_agrees() {
    # The version is written in nine places and nothing checked they match.
    # PUBLISHING.md named only two of them, so cutting 0.2.0 meant noticing
    # the other seven by hand -- and the seven are the ones that matter in
    # the field: a tool scp'd to a bare machine answers --version from its
    # own constant, and `agree` groups a fleet by what comes back. A module
    # left at the old number therefore reads as version skew across the
    # fleet, which is a finding people act on, rather than as a slip.
    "$PY" - "$BINNACLE_DIR" <<'EOF'
import io, os, re, sys

root = sys.argv[1]
repo = os.path.dirname(root)
found = {}


def declared(path, pattern):
    # Explicit encoding for the same reason the drift test above gives:
    # the 3.6 container runs under the C locale.
    for line in io.open(path, encoding="utf-8"):
        m = re.match(pattern, line)
        if m:
            return m.group(1)
    return None


found["pyproject.toml"] = declared(
    os.path.join(repo, "pyproject.toml"), r'^version = "([^"]+)"')
for fname in sorted(os.listdir(root)):
    if fname.endswith(".py"):
        found[fname] = declared(
            os.path.join(root, fname), r'^VERSION = "([^"]+)"')

missing = sorted(k for k, v in found.items() if v is None)
if missing:
    sys.stderr.write("no version declared in: %s\n" % ", ".join(missing))
    sys.exit(1)

# __init__.py is the canonical one: it is what docs/conf.py renders and
# what `import binnacle` reports.
canon = found["__init__.py"]
bad = sorted(k for k, v in found.items() if v != canon)
if bad:
    for name in bad:
        sys.stderr.write("%s says %s, __init__.py says %s\n"
                         % (name, found[name], canon))
    sys.exit(1)
EOF
    assert_status $? 0
}

echo "compose"
run_test "fleet triage groups by what is wrong" t_fleet_triage_groups_hosts_by_what_is_wrong
run_test "without masking, no host can agree"   t_without_masking_every_host_is_its_own_group
run_test "--merge-csv keeps the real values"    t_merge_csv_keeps_the_values_masking_hid
run_test "resolve groups by what a name means"  t_resolve_groups_hosts_by_what_a_name_resolves_to
run_test "during groups by what limited them"   t_during_groups_hosts_by_what_limited_them
run_test "skew groups by what clocks are doing" t_skew_groups_hosts_by_what_their_clock_is_doing
run_test "--fleet-csv is the two flags"         t_fleet_csv_is_the_two_flags_and_says_so
run_test "the tools share one csv header"       t_the_diagnostic_tools_share_one_csv_header
run_test "every flag appears in --help"         t_every_flag_appears_in_its_tools_help
run_test "declared copies have not drifted"     t_declared_copies_have_not_drifted
run_test "every declared version agrees"        t_every_declared_version_agrees
run_test "a stray utf8 byte is survivable"      t_a_stray_utf8_byte_does_not_lose_the_run
run_test "an edited host file keeps its bytes"  t_an_edited_host_file_keeps_the_bytes_it_came_with
run_test "a notepad file still means the same"  t_a_file_saved_by_notepad_still_means_the_same_thing
run_test "an unwritable file is a finding"      t_a_file_that_cannot_be_written_is_a_finding
run_test "an unreachable host is a group"       t_an_unreachable_host_is_a_group_not_an_error
finish
