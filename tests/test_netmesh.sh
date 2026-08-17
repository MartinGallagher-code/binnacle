#!/usr/bin/env bash
# netmesh: mesh file, real agents over loopback, and the fleet lifecycle
# through the fake ssh sandbox.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
NM="$BINNACLE_DIR/netmesh.py"

nm() { "$PY" "$NM" "$@"; }

t_cli_basics() {
    out="$(nm --version)"
    assert_contains "$out" "0."
    set +e
    nm nosuchverb >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
    out="$(nm help)"
    assert_contains "$out" "check"
    assert_contains "$out" "selftest"
    assert_contains "$out" "summarize"
}

t_mesh_round_trip() {
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1 b=127.0.0.2 --mesh m.csv --pps 5 >/dev/null
    assert_file_exists m.csv
    body="$(cat m.csv)"
    assert_contains "$body" "src\\dst"
    assert_contains "$body" "a=127.0.0.1"
    assert_contains "$body" "pps=5"
    # Reloading it must not change anything the agent depends on.
    set +e
    out="$(nm summarize --mesh m.csv --no-collect --reports nowhere 2>&1)"
    set -e
    assert_contains "$out" "no reports directory"
}

t_mesh_errors_name_the_cell() {
    cd "$TEST_TMPDIR"
    printf '# netmesh mesh v1\n# port=5310\nsrc\\dst,a=1.1.1.1,b=2.2.2.2\nc=3.3.3.3,,5\n' > bad.csv
    set +e
    out="$(nm status --mesh bad.csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    # The line number must be the one in the file, not the grid row index:
    # the user is editing the file.
    assert_contains "$out" "line 4"
    assert_contains "$out" "not in the header row"
}

t_ping_only_cannot_originate() {
    cd "$TEST_TMPDIR"
    printf '# netmesh mesh v1\n# port=5310\nsrc\\dst,a=1.1.1.1,~gw=2.2.2.2\n~gw=2.2.2.2,5,\n' > po.csv
    set +e
    out="$(nm status --mesh po.csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "ping-only"
}

t_ipv6_is_refused_clearly_not_misparsed() {
    cd "$TEST_TMPDIR"
    set +e
    out="$(nm gen 'a=[::1]:5310' --mesh v6.csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "IPv6"
}

t_selftest_passes_on_loopback() {
    # Real agents, real packets, no ssh and no second host.
    out="$(nm selftest --for 4 --port 5410)"
    assert_contains "$out" "PASS"
    assert_contains "$out" "a -> b"
    assert_contains "$out" "b -> a"
}

t_agent_writes_documented_columns() {
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5420 b=127.0.0.1:5421 --mesh m.csv --pps 40 \
        --no-pmtu >/dev/null
    mkdir -p da db
    nm agent --mesh m.csv --host a --dir da --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p1=$!
    nm agent --mesh m.csv --host b --dir db --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p2=$!
    wait $p1 $p2
    assert_file_exists da/report.csv
    header="$(head -1 da/report.csv)"
    assert_eq "$header" "ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,path_mtu,mtu_state,agent_cpu_pct,note"
    body="$(cat da/report.csv)"
    assert_contains "$body" ",tx,b,"
    assert_contains "$body" ",rx,b,"
    assert_contains "$body" ",host,*,"
    # Loopback RTT must be plausible, not zero and not a second.
    rtt="$("$PY" - <<'EOF'
import csv, sys
vals = [float(r["rtt_avg_us"]) for r in csv.DictReader(open("da/report.csv"))
        if r["dir"] == "tx" and r["rtt_avg_us"]]
print(sum(vals) / len(vals) if vals else 0)
EOF
)"
    assert_between "$rtt" 1 200000
}

t_loss_columns_go_blank_not_zero_when_peer_dies() {
    # A pair whose replies dried up must write no RTT at all: averaging a
    # blank in as zero would flatter the baseline.
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5430 b=127.0.0.1:5431 --mesh m.csv --pps 40 \
        --no-pmtu >/dev/null
    mkdir -p da
    nm agent --mesh m.csv --host a --dir da --interval 1 --duration 3 \
        --bind 127.0.0.1 >/dev/null 2>&1
    # b never ran, so every probe from a is lost.
    body="$(cat da/report.csv)"
    blank="$("$PY" - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("da/report.csv")) if r["dir"] == "tx"]
sent = sum(int(r["sent"] or 0) for r in rows)
recv = sum(int(r["recv"] or 0) for r in rows)
rtts = [r["rtt_avg_us"] for r in rows]
print("ok" if sent > 0 and recv == 0 and all(v == "" for v in rtts) else "bad")
EOF
)"
    assert_eq "$blank" "ok"
}

t_fleet_lifecycle_through_fake_ssh() {
    install_fake_ssh
    for h in h1 h2; do fake_host "$h"; done
    cd "$TEST_TMPDIR"
    nm gen h1=h1 h2=h2 --mesh m.csv --pps 20 --no-pmtu >/dev/null

    set +e
    nm start --mesh m.csv --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
        --remote-dir nmdir --interval 2 --duration 6 --python "$PY" \
        >/dev/null 2>&1
    set -e
    log="$(cat "$FAKE_SSH_LOG")"
    assert_contains "$log" "scp h1 nmdir"
    assert_contains "$log" "scp h2 nmdir"
    assert_file_exists "$FAKE_ROOT/h1/nmdir/agent.pid"

    out="$(nm status --mesh m.csv --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
             --remote-dir nmdir 2>&1)"
    assert_contains "$out" "RUNNING"

    # Starting twice must be refused, not silently stacked.
    set +e
    out="$(nm start --mesh m.csv --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
             --remote-dir nmdir --python "$PY" --keep-going 2>&1)"
    set -e
    assert_contains "$out" "already-running"

    nm stop --mesh m.csv --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
        --remote-dir nmdir >/dev/null 2>&1
    assert_file_exists "$FAKE_ROOT/h1/nmdir/report.csv"
    assert_no_file "$FAKE_ROOT/h1/nmdir/agent.pid"

    nm clean --mesh m.csv --ssh "$FAKE_BIN/ssh" --scp "$FAKE_BIN/scp" \
        --remote-dir nmdir --quiet >/dev/null 2>&1
    assert_no_file "$FAKE_ROOT/h1/nmdir"
}

t_summarize_reads_fixtures_without_a_network() {
    # Fixture-driven: this is what catches regressions in the analysis.
    cd "$TEST_TMPDIR"
    mkdir -p reports
    hdr="ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,path_mtu,mtu_state,agent_cpu_pct,note"
    {
        echo "$hdr"
        echo "100,a,tx,b,udp,64,10,100,99,1.000,200,220,220,400,900,15,9000,confirmed,,"
        echo "100,a,rx,b,udp,,,99,100,,,,,,,,,,,"
    } > reports/a.csv
    {
        echo "$hdr"
        echo "100,b,tx,a,udp,64,10,100,100,0.000,1800,1900,1900,8200,9000,600,1500,blackhole,,"
        echo "100,b,rx,a,udp,,,100,100,,,,,,,,,,,"
    } > reports/b.csv
    out="$(nm summarize --no-collect --reports reports --mesh nonexistent.csv)"
    assert_contains "$out" "BASELINE"
    assert_contains "$out" "WORST PAIRS"
    assert_contains "$out" "ASYMMETRY"
    assert_contains "$out" "BLACKHOLE"
    assert_contains "$out" "WHAT TO DO NEXT"
    # b -> a is the slow direction, so it must be named as the egress side.
    assert_contains "$out" "b -> a is"

    nm summarize --no-collect --reports reports --mesh nonexistent.csv \
        --grid grids >/dev/null
    assert_file_exists grids/asym_grid.csv
    # The asymmetry grid is antisymmetric by construction.
    anti="$("$PY" - <<'EOF'
import csv
rows = list(csv.reader(open("grids/asym_grid.csv")))
hdr = rows[0][1:]
m = {}
for r in rows[1:]:
    for h, v in zip(hdr, r[1:]):
        if v:
            m[(r[0], h)] = float(v)
print("ok" if all(abs(m[(a, b)] + m.get((b, a), 0)) < 1e-6
                  for (a, b) in m if (b, a) in m) else "bad")
EOF
)"
    assert_eq "$anti" "ok"
}

t_clean_run_says_the_network_is_fine() {
    cd "$TEST_TMPDIR"
    mkdir -p reports
    hdr="ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,path_mtu,mtu_state,agent_cpu_pct,note"
    {
        echo "$hdr"
        echo "100,a,tx,b,udp,64,10,100,100,0.000,90,100,100,140,200,5,9000,confirmed,,"
        echo "100,a,rx,b,udp,,,100,100,,,,,,,,,,,"
    } > reports/a.csv
    out="$(nm summarize --no-collect --reports reports --mesh nonexistent.csv)"
    assert_contains "$out" "the network is not your problem"
    assert_contains "$out" "mx run"
    assert_contains "$out" "iperf_orchestrator"
}

# --- latency under load ----------------------------------------------------
#
# The measurement neither half of the toolchain had: iperf_orchestrator says
# what the link carried and netmesh says what it costs idle, and until now
# nothing said what the carrying did to the latency.

t_latency_under_load_is_reported_against_the_idle_baseline() {
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 139 210 8400 42100
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent --load-split 2000000)"
    assert_contains "$out" "UNDER LOAD"
    assert_contains "$out" "139us"
    assert_contains "$out" "42.1ms"
    assert_contains "$out" "(200x)"
    assert_contains "$out" "queue in front of the bottleneck"
}

t_a_link_that_held_up_says_so_plainly() {
    # A clean run must not read like breakage: the same rule the rest of
    # the package follows.
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 139 210 145 240
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent --load-split 2000000)"
    assert_contains "$out" "Latency held up under load"
    assert_not_contains "$out" "queue in front of the bottleneck"
}

t_the_bloat_threshold_moves() {
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 100 100 100 300
    # 3x is under the default factor of 4 and over a factor of 2.
    assert_contains "$(nm summarize --reports "$TEST_TMPDIR/reports" \
        --no-collect --mesh /nonexistent --load-split 2000000)" \
        "Latency held up"
    assert_contains "$(nm summarize --reports "$TEST_TMPDIR/reports" \
        --no-collect --mesh /nonexistent --load-split 2000000 \
        --bloat-factor 2)" "rose 3x"
}

t_loss_that_only_appears_under_load_is_its_own_finding() {
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 100 200 110 220 0 4
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent --load-split 2000000)"
    assert_contains "$out" "Loss appeared under load"
    # Short fragment: the finding is wrapped at 76 columns.
    assert_contains "$out" "a queue running out"
}

t_a_split_with_nothing_on_one_side_is_not_a_comparison() {
    # Blank means not measured: a one-sided split must say so rather than
    # report an idle baseline of zero and a huge regression against it.
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 139 210 8400 42100
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent --load-split 1000000)"
    assert_contains "$out" "leaves one side of it empty"
    assert_not_contains "$out" "queue in front of the bottleneck"
}

t_without_a_split_the_report_is_unchanged() {
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 139 210 8400 42100
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_not_contains "$out" "UNDER LOAD"
}


echo "netmesh"
run_test "cli basics and generated help"       t_cli_basics
run_test "mesh file round trip"                t_mesh_round_trip
run_test "mesh errors name the cell"           t_mesh_errors_name_the_cell
run_test "ping-only cannot originate"          t_ping_only_cannot_originate
run_test "ipv6 refused, not misparsed"         t_ipv6_is_refused_clearly_not_misparsed
run_test "selftest passes on loopback"         t_selftest_passes_on_loopback
run_test "agent writes documented columns"     t_agent_writes_documented_columns
run_test "dead peer leaves rtt blank not 0"    t_loss_columns_go_blank_not_zero_when_peer_dies
run_test "fleet lifecycle through fake ssh"    t_fleet_lifecycle_through_fake_ssh
run_test "summarize runs from fixtures"        t_summarize_reads_fixtures_without_a_network
run_test "clean run says so plainly"           t_clean_run_says_the_network_is_fine
run_test "latency under load vs the baseline"   t_latency_under_load_is_reported_against_the_idle_baseline
run_test "a link that held up says so"         t_a_link_that_held_up_says_so_plainly
run_test "the bloat threshold moves"           t_the_bloat_threshold_moves
run_test "load-only loss is its own finding"   t_loss_that_only_appears_under_load_is_its_own_finding
run_test "a one-sided split is not compared"   t_a_split_with_nothing_on_one_side_is_not_a_comparison
run_test "without a split nothing changes"     t_without_a_split_the_report_is_unchanged
finish
