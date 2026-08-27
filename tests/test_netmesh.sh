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
    assert_eq "$header" "ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,path_mtu,mtu_state,agent_cpu_pct,note,rx_usecs,flow"
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

t_hostname_addresses_measure_not_100pc_loss() {
    # A mesh may carry names rather than IPs -- a bare token is its own
    # address.  Replies are attributed by source address, and recvfrom
    # only ever reports numbers, so a name left unresolved matched no
    # reply: every probe went out, every echo came back, and every pair
    # read 100% loss while the network carried every packet.  The agent
    # now resolves each peer once at start.
    cd "$TEST_TMPDIR"
    nm gen a=localhost:5440 b=localhost:5441 --mesh m.csv --pps 40 \
        --no-pmtu >/dev/null
    mkdir -p da db
    nm agent --mesh m.csv --host a --dir da --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p1=$!
    nm agent --mesh m.csv --host b --dir db --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p2=$!
    wait $p1 $p2
    got="$("$PY" - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("da/report.csv")) if r["dir"] == "tx"]
sent = sum(int(r["sent"] or 0) for r in rows)
recv = sum(int(r["recv"] or 0) for r in rows)
print("ok" if sent > 0 and recv > sent * 0.9 else
      "bad: sent=%d recv=%d" % (sent, recv))
EOF
)"
    assert_eq "$got" "ok"
}

t_a_peer_answering_from_another_address_is_measured() {
    # The same 100%-loss shape as the hostname bug, from the other end.
    # A peer answers from whichever of its addresses the route back
    # selects, which need not be the one the mesh names: a multi-homed
    # box, or a name resolving to a different interface than the return
    # route picks. Replies were attributed by source address, so every
    # one was discarded -- 100% loss on a link carrying every packet,
    # while the peer's own rx count (attributed by index) showed them all
    # arriving. Forward loss near zero against 100% total is that bug.
    #
    # Reproduced here by naming b at 127.0.0.2 and letting it answer
    # unbound, so the kernel sources its replies from 127.0.0.1.
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5460 b=127.0.0.2:5461 --mesh m.csv --pps 40 \
        --no-pmtu >/dev/null
    mkdir -p da db
    nm agent --mesh m.csv --host a --dir da --interval 2 --duration 4 &
    p1=$!
    nm agent --mesh m.csv --host b --dir db --interval 2 --duration 4 &
    p2=$!
    wait $p1 $p2
    got="$("$PY" - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("da/report.csv"))
        if r["dir"] == "tx" and not r["flow"]]
sent = sum(int(r["sent"] or 0) for r in rows)
recv = sum(int(r["recv"] or 0) for r in rows)
print("ok" if sent > 0 and recv > sent * 0.9 else
      "bad: sent=%d recv=%d" % (sent, recv))
EOF
)"
    assert_eq "$got" "ok"
}

t_path_mtu_converges_to_a_peer_on_another_address() {
    # The path-MTU echo matched on the source address too, so the same
    # peer never converged: every size read as a black hole and the pair
    # reported no path MTU at all.
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5462 b=127.0.0.2:5463 --mesh m.csv --pps 40 \
        >/dev/null
    mkdir -p da db
    nm agent --mesh m.csv --host a --dir da --interval 3 --duration 6 &
    p1=$!
    nm agent --mesh m.csv --host b --dir db --interval 3 --duration 6 &
    p2=$!
    wait $p1 $p2
    got="$("$PY" - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("da/report.csv"))
        if r["dir"] == "tx" and not r["flow"]]
states = set(r["mtu_state"] for r in rows if r["mtu_state"])
print("ok" if "confirmed" in states else "bad: %s" % (sorted(states) or "none"))
EOF
)"
    assert_eq "$got" "ok"
}

t_an_unresolvable_peer_is_a_note_not_silence() {
    # A name that does not resolve on the agent's box must say so on the
    # row, with nothing sent -- a quiet row of blanks reads as "measured
    # and clean", and 100% loss reads as the network's fault.
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5450 b=no-such-host.invalid:5451 --mesh m.csv \
        --pps 40 --no-pmtu >/dev/null
    mkdir -p da
    nm agent --mesh m.csv --host a --dir da --interval 1 --duration 2 \
        --bind 127.0.0.1 >/dev/null 2>&1
    body="$(cat da/report.csv)"
    assert_contains "$body" "cannot resolve no-such-host.invalid"
    got="$("$PY" - <<'EOF'
import csv
rows = [r for r in csv.DictReader(open("da/report.csv")) if r["dir"] == "tx"]
print("ok" if rows and all(int(r["sent"] or 0) == 0 for r in rows) else "bad")
EOF
)"
    assert_eq "$got" "ok"
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

t_a_p50_that_is_really_the_cards_timer_says_so() {
    # The one setting that can make netmesh's own answer wrong with
    # nothing looking wrong: a card told to wait 200us cannot report a
    # round trip faster than that.
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 210 400 210 400 0 0 200
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_contains "$out" "MEASUREMENT"
    assert_contains "$out" "coalescing timer"
    assert_contains "$out" "ethtool -C"
}

t_coalescing_well_under_the_measurement_is_not_mentioned() {
    # 10us against a 210us p50 is not what you are measuring, and saying
    # so anyway would be noise on every healthy run.
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 210 400 210 400 0 0 10
    assert_not_contains "$(nm summarize --reports "$TEST_TMPDIR/reports" \
        --no-collect --mesh /nonexistent)" "coalescing timer"
}

t_without_the_setting_the_note_is_absent_not_zero() {
    # Blank means not measured: a report from an agent that could not run
    # ethtool must not read as "coalescing is off".
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 210 400 210 400
    assert_not_contains "$(nm summarize --reports "$TEST_TMPDIR/reports" \
        --no-collect --mesh /nonexistent)" "MEASUREMENT"
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

t_two_pairs_tied_on_asymmetry_do_not_crash() {
    # Ranked lists must never sort on tuples that carry objects.  Two pairs
    # with the same asymmetry delta made the sort fall through to comparing
    # PairStat against PairStat, which has no ordering: summarize died with
    # a TypeError on a report it had already finished measuring.  Rounding
    # p50s to whole microseconds makes the tie ordinary, not exotic.
    cd "$TEST_TMPDIR"
    mkdir -p tied
    hdr="ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,path_mtu,mtu_state,agent_cpu_pct,note"
    {
        echo "$hdr"
        # a <-> b and c <-> d, both asymmetric by exactly 200us.
        echo "100,a,tx,b,udp,64,10,100,100,0.000,100,100,100,200,200,10,,,,"
        echo "100,b,tx,a,udp,64,10,100,100,0.000,300,300,300,600,600,10,,,,"
        echo "100,c,tx,d,udp,64,10,100,100,0.000,500,500,500,1000,1000,10,,,,"
        echo "100,d,tx,c,udp,64,10,100,100,0.000,700,700,700,1400,1400,10,,,,"
    } > tied/r.csv
    rc=0
    out="$(nm summarize --no-collect --reports tied --mesh /nonexistent 2>&1)" \
        || rc=$?
    assert_status $rc 0
    assert_not_contains "$out" "TypeError"
    assert_contains "$out" "ASYMMETRY"
    # Both tied pairs survive the cut, and the order is stable by name.
    assert_contains "$out" "b -> a is"
    assert_contains "$out" "d -> c is"
}

t_flow_buckets_break_the_rate_down_they_do_not_multiply_it() {
    # The one property that matters for trusting these numbers: turning
    # flow spreading on must not put more traffic on the fabric being
    # measured.  The buckets are a breakdown of the same probes.
    cd "$TEST_TMPDIR"
    nm gen a=127.0.0.1:5430 b=127.0.0.1:5431 --mesh mf.csv --pps 40 \
        --no-pmtu --flows 4 >/dev/null
    assert_contains "$(cat mf.csv)" "flows=4"
    mkdir -p fa fb
    nm agent --mesh mf.csv --host a --dir fa --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p1=$!
    nm agent --mesh mf.csv --host b --dir fb --interval 2 --duration 4 \
        --bind 127.0.0.1 &
    p2=$!
    wait $p1 $p2
    assert_file_exists fa/report.csv
    # Four distinct source ports, and each interval's buckets add up to
    # exactly the aggregate row above them -- never to more.
    check="$("$PY" - <<'EOF'
import collections, csv
agg, flows = collections.Counter(), collections.defaultdict(dict)
for r in csv.DictReader(open("fa/report.csv")):
    if r["dir"] != "tx" or r["peer"] != "b":
        continue
    if r["flow"]:
        flows[r["ts"]][r["flow"]] = int(r["sent"])
    else:
        agg[r["ts"]] = int(r["sent"])
ports = set()
for ts in flows:
    ports |= set(flows[ts])
sums_match = all(sum(flows[ts].values()) == agg[ts] for ts in flows)
print("%d %s" % (len(ports), "match" if sums_match else "mismatch"))
EOF
)"
    assert_eq "$check" "4 match"
    # The peer's own rate is untouched: the split is across the buckets.
    assert_contains "$(grep ',tx,b,' fa/report.csv | head -1)" ",udp,64,40,"
}

t_one_slow_flow_is_named_as_a_bundle_member() {
    # A single sick member of a LAG: seven flows fine, one 30x worse.
    # Nothing else in the package can see this, because every other
    # measurement takes one path and hits it or misses it by luck.
    netmesh_flow_reports "$TEST_TMPDIR/reports" 8 7 140 4200 50 3.0
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_contains "$out" "PATH SPREAD"
    assert_contains "$out" ":40008"
    # Short fragments: the finding is wrapped at 76 columns.
    assert_contains "$out" "5-tuple"
    assert_contains "$out" "bundle"
}

t_flows_that_agree_are_not_a_finding() {
    # Every bucket the same: a single-link path spreads nowhere, and
    # saying so is not the same as reporting a fault.
    netmesh_flow_reports "$TEST_TMPDIR/reports" 8 -1 140 140 50
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_contains "$out" "PATH SPREAD"
    assert_contains "$out" "agrees:"
    assert_not_contains "$out" "5-tuple"
}

t_the_flow_threshold_moves() {
    # 140us against 266us is 1.9x, which is what two buckets of the same
    # healthy loopback measured on a busy machine: a bucket's p50 rests on
    # a fraction of the samples a pair's does, so this much spread is
    # noise and the default must not call it a finding.
    netmesh_flow_reports "$TEST_TMPDIR/reports" 8 7 140 266 50
    quiet="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
               --mesh /nonexistent)"
    assert_not_contains "$quiet" "5-tuple"
    loud="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
              --mesh /nonexistent --flow-factor 1.5)"
    assert_contains "$loud" "5-tuple"
}

t_flows_too_thin_to_compare_are_not_ranked() {
    # Blank means not measured, and so does thin: the rate is split across
    # the buckets, so a short run leaves each one with too few replies to
    # rank.  Saying nothing would read as "the flows agree".
    netmesh_flow_reports "$TEST_TMPDIR/reports" 8 7 140 4200 4
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_contains "$out" "PATH SPREAD"
    assert_contains "$out" "compared."
    assert_not_contains "$out" "5-tuple"
}

t_a_sick_member_only_under_load_is_a_different_finding() {
    # Composes with --baseline: a member that only misbehaves when the
    # link is busy is congested, not broken, and the two want different
    # things done about them.
    netmesh_flow_reports "$TEST_TMPDIR/reports" 8 7 140 4200 50 0 2000000
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent --load-split 2000000)"
    assert_contains "$out" "PATH SPREAD"
    # Sick in both windows, so it is the member itself, not congestion.
    assert_contains "$out" "faulty"
}

t_without_flows_there_is_no_spread_section() {
    # The feature is opt-in, and a report with no flow rows must look
    # exactly as it did before it existed.
    netmesh_reports "$TEST_TMPDIR/reports" 2000000 139 210 8400 42100
    out="$(nm summarize --reports "$TEST_TMPDIR/reports" --no-collect \
             --mesh /nonexistent)"
    assert_not_contains "$out" "PATH SPREAD"
}


echo "netmesh"
run_test "cli basics and generated help"       t_cli_basics
run_test "mesh file round trip"                t_mesh_round_trip
run_test "mesh errors name the cell"           t_mesh_errors_name_the_cell
run_test "ping-only cannot originate"          t_ping_only_cannot_originate
run_test "ipv6 refused, not misparsed"         t_ipv6_is_refused_clearly_not_misparsed
run_test "selftest passes on loopback"         t_selftest_passes_on_loopback
run_test "agent writes documented columns"     t_agent_writes_documented_columns
run_test "hostname mesh measures, not 100% loss" t_hostname_addresses_measure_not_100pc_loss
run_test "unresolvable peer is a note"         t_an_unresolvable_peer_is_a_note_not_silence
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
run_test "a p50 that is really the timer"      t_a_p50_that_is_really_the_cards_timer_says_so
run_test "small coalescing is not mentioned"   t_coalescing_well_under_the_measurement_is_not_mentioned
run_test "no setting means absent, not zero"   t_without_the_setting_the_note_is_absent_not_zero
run_test "tied asymmetry does not crash"     t_two_pairs_tied_on_asymmetry_do_not_crash
run_test "flow buckets split, not multiply"    t_flow_buckets_break_the_rate_down_they_do_not_multiply_it
run_test "one slow flow names the member"      t_one_slow_flow_is_named_as_a_bundle_member
run_test "flows that agree are not a finding"  t_flows_that_agree_are_not_a_finding
run_test "the flow threshold moves"            t_the_flow_threshold_moves
run_test "thin flows are not ranked"           t_flows_too_thin_to_compare_are_not_ranked
run_test "sick only under load differs"        t_a_sick_member_only_under_load_is_a_different_finding
run_test "without flows, no spread section"    t_without_flows_there_is_no_spread_section
run_test "a peer on another address measures"  t_a_peer_answering_from_another_address_is_measured
run_test "path mtu converges on that peer"     t_path_mtu_converges_to_a_peer_on_another_address
finish
