#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# reckon: what a run should have reached on its hardware, and where it fell
# short.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
RK="$BINNACLE_DIR/reckon.py"

rk() { "$PY" "$RK" "$@"; }

# The findings as CSV: one line each, so a case can look for a phrase
# without the human report's wrapping splitting it across two lines.
findings() { "$PY" "$RK" "$@" --csv; }

# The floor every mx case runs on: three racks of two hosts, 10 Gb/s NICs,
# one 10 Gb/s uplink per rack -- oversubscribed 2:1, so the uplinks and the
# NICs are both limits somewhere and a case can tell them apart.
#
# The expectations, worked by hand rather than by the code under test, for
# an unpaced all-to-all of 1434 B requests and 34 B replies (1500 B and
# 100 B on the wire with the 66 B of framing, so 12000 and 800 bits):
#
#   rack uplink out: 4 cross flows per host x 2 hosts send requests
#     (8 x 12000) and answer the 8 cross flows coming in (8 x 800) --
#     102400 bits per request/s, full at 10e9 / 102400 = 97656.25.
#   NIC out: 5 requests and 5 replies per request/s, 64000 bits, which
#     would allow 156250 -- so the uplinks fill first, and every cross
#     flow freezes at 97656.25.
#   The two intra-rack flows of each rack then rise alone.  A host's NIC
#     out carries its four cross flows at 4 x 12800 x 97656.25 = 5e9, and
#     its intra flow -- 12800 per request/s, its request one way and its
#     peer's reply the other -- has the other 5e9 to itself:
#     5e9 / 12800 = 390625.
CROSS=97656.25
INTRA=390625.0

write_floor() {
    cat > "$TEST_TMPDIR/floor.dc" <<'EOF'
dc T name="Test"
  room h1
    row A nic_gbps=10
      rack r01 uplinks=1 uplink_gbps=10
        node tor role=tor name={rack}tor
        node a[1..2] role=server name={id}
      rack r02 uplinks=1 uplink_gbps=10
        node tor role=tor name={rack}tor
        node b[1..2] role=server name={id}
      rack r03 uplinks=1 uplink_gbps=10
        node tor role=tor name={rack}tor
        node c[1..2] role=server name={id}
EOF
}

# mx reports with chosen efficiencies.  Every flow achieves --eff percent
# of its hand-worked expectation unless a more specific option says
# otherwise; the rates written are exactly what an mx agent would have
# written for that outcome, in mx's own columns.
write_mxgen() {
    cat > "$TEST_TMPDIR/mxgen.py" <<'EOF'
import argparse, csv, os
F = ["ts", "host", "dir", "peer", "size", "rep_size", "target_pps", "pps",
     "mbps", "rep_pps", "rep_mbps", "loss_pct", "rtt_avg_us", "rtt_p50_us",
     "rtt_p99_us", "rtt_max_us", "cpu_pct", "cpu_max_pct", "agent_cpu_pct",
     "workers", "layer"]
p = argparse.ArgumentParser()
p.add_argument("dir")
p.add_argument("--hosts", default="a1,a2,b1,b2,c1,c2")
p.add_argument("--size", type=int, default=1434)
p.add_argument("--rep", type=int, default=34)
p.add_argument("--eff", type=float, default=98.0)
p.add_argument("--host", action="append", default=[])
p.add_argument("--cross", action="append", default=[])
p.add_argument("--inside", action="append", default=[])
p.add_argument("--pair", action="append", default=[])
p.add_argument("--target", type=float)
p.add_argument("--cpu", action="append", default=[])
p.add_argument("--silent", action="append", default=[])
p.add_argument("--loss", type=float)
p.add_argument("--p50", type=float)
p.add_argument("--ts", default="1000,1005,1010")
p.add_argument("--cross-rate", type=float, default=97656.25)
p.add_argument("--intra-rate", type=float, default=390625.0)
p.add_argument("--layered", type=float, metavar="RATE")
p.add_argument("--drain", action="store_true")
p.add_argument("--append", action="store_true")
a = p.parse_args()
hosts = a.hosts.split(",")
ts = [int(x) for x in a.ts.split(",")]
kv = lambda items: dict(i.split("=", 1) for i in items)
host_pct = dict((k, float(v)) for k, v in kv(a.host).items())
cross_pct = dict((k, float(v)) for k, v in kv(a.cross).items())
inside_pct = dict((k, float(v)) for k, v in kv(a.inside).items())
pair_pct = dict((k, float(v)) for k, v in kv(a.pair).items())
cpu = dict((k, v.split(",")) for k, v in kv(a.cpu).items())
rack = lambda h: h[0]

def pct(s, d):
    if "%s:%s" % (s, d) in pair_pct:
        return pair_pct["%s:%s" % (s, d)]
    for h in (s, d):
        if h in host_pct:
            return host_pct[h]
    if rack(s) != rack(d):
        for r in (rack(s), rack(d)):
            if r in cross_pct:
                return cross_pct[r]
    elif rack(s) in inside_pct:
        return inside_pct[rack(s)]
    return a.eff

def expected(s, d):
    if a.target is not None:
        return a.target
    if a.layered is not None:
        return a.layered
    return a.intra_rate if rack(s) == rack(d) else a.cross_rate

n = len(hosts)
os.makedirs(a.dir, exist_ok=True)
for i, h in enumerate(hosts):
    if h in a.silent:
        continue
    path = os.path.join(a.dir, h + ".csv")
    fresh = not (a.append and os.path.exists(path))
    with open(path, "a" if a.append else "w", newline="") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(F)
        for j, t in enumerate(ts):
            if a.layered is not None:
                layer = j % (n - 1) + 1
                peers = [(hosts[(i + layer) % n], str(layer))]
            else:
                peers = [(x, "") for x in hosts if x != h]
            for peer, layer in peers:
                back = expected(h, peer) * pct(h, peer) / 100.0
                loss = a.loss or 0.0
                sent = back / (1 - loss / 100.0)
                w.writerow([t, h, "tx", peer, a.size, a.rep,
                            "" if a.target is None else a.target,
                            "%.1f" % sent, "", "%.1f" % back, "",
                            "" if a.loss is None else "%.3f" % loss, "",
                            "" if a.p50 is None else "%.0f" % a.p50,
                            "", "", "", "", "", "", layer])
                if a.drain:
                    # A layer switch's tail: replies only, send side blank.
                    w.writerow([t, h, "tx", peer, a.size, a.rep, "", "", "",
                                "5.0", "", "", "", "", "", "", "", "", "", "",
                                layer])
            core, agent = cpu.get(h, ("30", "20"))
            w.writerow([t, h, "host", "*", a.size, a.rep, "", "", "", "", "",
                        "", "", "", "", "", "25", core, agent, "4", ""])
EOF
}

mxgen() { "$PY" "$TEST_TMPDIR/mxgen.py" "$@"; }

# netmesh reports: every pair's idle p50, as a netmesh agent writes it --
# including the per-port bucket rows that must not be read as pairs.
write_nmgen() {
    cat > "$TEST_TMPDIR/nmgen.py" <<'EOF'
import argparse, csv, os
F = ["ts", "host", "dir", "peer", "probe", "size", "target_pps", "sent",
     "recv", "loss_pct", "rtt_min_us", "rtt_avg_us", "rtt_p50_us",
     "rtt_p99_us", "rtt_max_us", "jitter_us", "path_mtu", "mtu_state",
     "agent_cpu_pct", "note", "rx_usecs", "flow", "reply_ttl", "ttl_hops",
     "hop_ttl", "hop_addr"]
p = argparse.ArgumentParser()
p.add_argument("dir")
p.add_argument("--hosts", default="a1,a2,b1,b2,c1,c2")
p.add_argument("--p50", type=float, default=40.0)
p.add_argument("--ts", default="100,105")
p.add_argument("--mtu", type=int)
a = p.parse_args()
hosts = a.hosts.split(",")
os.makedirs(a.dir, exist_ok=True)
for h in hosts:
    with open(os.path.join(a.dir, h + ".csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=F)
        w.writeheader()
        for t in a.ts.split(","):
            for peer in hosts:
                if peer == h:
                    continue
                row = {"ts": t, "host": h, "dir": "tx", "peer": peer,
                       "probe": "udp", "size": 64, "target_pps": 10,
                       "sent": 50, "recv": 50, "loss_pct": "0.000",
                       "rtt_p50_us": "%.0f" % a.p50, "jitter_us": 3}
                if a.mtu:
                    row.update(path_mtu=a.mtu, mtu_state="confirmed")
                w.writerow(row)
                # A bucket row breaks the one above down by port; read as
                # a pair it would be a second, wildly wrong idle RTT.
                w.writerow({"ts": t, "host": h, "dir": "tx", "peer": peer,
                            "probe": "udp", "flow": 40001,
                            "rtt_p50_us": "99999"})
EOF
}

nmgen() { "$PY" "$TEST_TMPDIR/nmgen.py" "$@"; }

setup_run() {
    write_floor
    write_mxgen
    write_nmgen
    cd "$TEST_TMPDIR"
}

# One flow's row from --flows, as expected|limit|limit_on|efficiency.
# Bar-separated, because a blank expectation is the whole point of some
# cases and whitespace splitting would quietly shift every field left.
flow_row() {
    "$PY" - "$1" "$2" "$3" <<'EOF'
import csv, sys
path, src, dst = sys.argv[1:4]
for r in csv.DictReader(open(path)):
    if r["src"] == src and r["dst"] == dst:
        print("|".join((r["expected"], r["limit"], r["limit_on"],
                        r["efficiency_pct"])))
        break
EOF
}

# flow FILE SRC DST -> EXP LIM ON EFF set for the case to assert on.
flow() {
    IFS='|' read -r EXP LIM ON EFF <<<"$(flow_row "$1" "$2" "$3")"
}

# --- the model ------------------------------------------------------------

t_the_expectation_is_the_fair_share_worked_by_hand() {
    setup_run
    mxgen reports
    rk floor.dc --mx reports --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 b1
    assert_eq "$EXP" "$(printf '%.3f' "$CROSS")"
    assert_eq "$LIM" "UPLINK"
    flow flows.csv a1 a2
    assert_eq "$EXP" "$(printf '%.3f' "$INTRA")"
    assert_eq "$LIM" "NIC"
}

t_a_limit_is_charged_to_the_sending_side() {
    # Both ends of a cross-rack flow's request leg fill at the same step;
    # naming the far rack sends someone to the wrong switch.
    setup_run
    mxgen reports
    rk floor.dc --mx reports --flows flows.csv --quiet >/dev/null
    flow flows.csv b1 c2
    assert_eq "$ON" "uplink:T/h1/A/r02:out"
    flow flows.csv a2 a1
    assert_eq "$ON" "nic:a2:out"
}

t_a_target_below_the_share_is_the_expectation() {
    setup_run
    mxgen reports --target 1000
    rk floor.dc --mx reports --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 b1
    assert_eq "$EXP" "1000.000"
    assert_eq "$LIM" "TARGET"
}

t_a_layered_run_shares_only_within_a_layer() {
    # Four hosts in one rack, one peer each per layer: only that layer's
    # flows are on the wire, so each gets a whole NIC -- 1e10 / 12800 =
    # 781250 -- not a third of one, which is what reading the three
    # layers as concurrent would expect.
    setup_run
    mxgen reports --hosts a1,a2,a3,a4 --layered 781250 \
        --ts 1000,1005,1010,1015,1020,1025
    rk --nic-gbps 10 --mx reports --flows flows.csv --json r.json \
        --quiet >/dev/null
    flow flows.csv a1 a2
    assert_eq "$EXP" "781250.000"
    # The host's rate is its flows spread over its layers, not their sum.
    got="$("$PY" -c "import json;d=json.load(open('r.json'));print([h['expected'] for h in d['hosts'] if h['host']=='a1'][0])")"
    assert_eq "$got" "781250.0"
}

t_cells_that_are_not_measurements_are_skipped() {
    # NaN, infinity, a negative rate and a word, in the intervals of one
    # flow: each is skipped, as a blank is, and the one interval holding a
    # real rate decides the flow.
    setup_run
    mxgen reports --ts 1000,1005,1010,1015,1020
    "$PY" - reports/a1.csv <<'PYEOF'
import csv, sys
path = sys.argv[1]
rows = list(csv.reader(open(path)))
bad = iter(["nan", "inf", "-5", "lots"])
for r in rows[1:]:
    if r[2] == "tx" and r[3] == "a2" and r[0] != "1020":
        r[9] = next(bad)
with open(path, "w", newline="") as fh:
    csv.writer(fh).writerows(rows)
PYEOF
    rk floor.dc --mx reports --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 a2
    assert_eq "$EFF" "98.000"
}

t_a_drain_row_is_not_a_flow() {
    # The tail of a finished layer carries replies and a blank send side.
    # Read as a flow it would be one running at zero.
    setup_run
    mxgen reports --drain
    out="$(rk floor.dc --mx reports --quiet)"
    assert_contains "$out" "30 flows"
    assert_contains "$out" "Every host is within"
}

t_the_window_cuts_where_mx_summarize_cuts() {
    # An old, slow interval more than --window before the newest row is not
    # this run; with --window 0 it is.
    setup_run
    mxgen reports --eff 40 --ts 100
    mxgen reports --append --ts 1000,1005,1010
    out="$(rk floor.dc --mx reports --quiet)"
    assert_contains "$out" "Every host is within"
    out="$(rk floor.dc --mx reports --window 0 --quiet)"
    assert_contains "$out" "fleet short"
}

# --- diagnosis ------------------------------------------------------------

t_a_clean_run_says_so() {
    setup_run
    mxgen reports
    out="$(rk floor.dc --mx reports)"
    assert_status $? 0
    assert_contains "$out" "Every host is within 90% of what its hardware allows"
    assert_contains "$out" "Nothing to chase"
    assert_not_contains "$out" "WARN"
}

t_a_slow_host_is_named_and_not_its_rack() {
    setup_run
    mxgen reports --host b2=45
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "CRITICAL  host short"
    assert_contains "$out" "b2 reaches 45%"
    assert_contains "$out" "while the rest of r02 is at 98%"
    assert_contains "$out" "why-slow --ssh b2"
    assert_not_contains "$out" "rack short"
    assert_not_contains "$out" "fleet short"
}

t_a_rack_short_across_its_uplinks_names_them() {
    setup_run
    mxgen reports --cross b=60
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "rack r02: flows crossing its uplinks reach 60%"
    assert_contains "$out" "its flows inside it at 98%"
    assert_contains "$out" "carry less than the 1 x 10 Gb/s declared"
    assert_not_contains "$out" "rack r01"
    assert_not_contains "$out" "host short"
}

t_a_rack_short_inside_and_out_is_not_its_uplinks() {
    setup_run
    mxgen reports --cross b=55 --inside b=55
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "every host in rack r02 is short, inside it"
    assert_contains "$out" "Not the uplinks"
}

t_a_fleet_short_together_is_one_finding() {
    setup_run
    mxgen reports --eff 70
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "fleet short"
    assert_contains "$out" "the fleet reaches 70%"
    # Not every rack in turn, and not every host: one finding.
    assert_not_contains "$out" "rack short"
    assert_not_contains "$out" "host short"
}

t_one_short_path_is_a_path_not_a_host() {
    setup_run
    mxgen reports --pair a1:c2=40
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "path short"
    assert_contains "$out" "a1 -> c2 at 40%"
    assert_contains "$out" "netmesh check a1 c2"
    assert_not_contains "$out" "host short"
}

t_above_the_hardware_is_a_wrong_declaration_not_a_success() {
    # Never clipped to 100%: the overshoot is the finding.
    setup_run
    mxgen reports --eff 150
    out="$(rk floor.dc --mx reports --flows flows.csv)"
    assert_contains "$out" "above hardware"
    assert_contains "$out" "the declaration is wrong"
    flow flows.csv a1 b1
    assert_eq "$EFF" "150.000"
}

t_a_cpu_bound_host_is_its_cpu_not_its_link() {
    # A cause outranks its symptom: the same host is not also "short".
    setup_run
    mxgen reports --host b2=50 --cpu b2=97,40
    out="$(findings floor.dc --mx reports)"
    assert_contains "$out" "b2,1010,CPU_BOUND,CRITICAL"
    assert_contains "$out" "its busiest core at 97%"
    assert_contains "$out" "mx start --workers N"
    assert_not_contains "$out" "HOST_SHORT"
}

t_loss_with_room_to_spare_is_named() {
    setup_run
    mxgen reports --target 1000 --loss 3
    out="$(rk floor.dc --mx reports)"
    assert_contains "$out" "loss with room"
    assert_contains "$out" "lost 1% or more"
}

t_a_silent_host_is_a_finding() {
    setup_run
    mxgen reports --silent c2
    out="$(rk floor.dc --mx reports --overlay ov.tsv)"
    assert_contains "$out" "CRITICAL  never reported"
    assert_contains "$out" "c2"
    assert_contains "$(cat ov.tsv)" "$(printf 'reckon_verdict\tc2\tNO-DATA')"
}

t_a_silent_host_says_where_to_look() {
    setup_run
    mxgen reports --silent c2
    out="$(findings floor.dc --mx reports)"
    # The backticks are literal: they are what the report prints.
    # shellcheck disable=SC2016
    assert_contains "$out" '`mx status` says why they were silent; compare again once they report.'
}

t_a_silent_host_is_not_evidence_about_its_rack() {
    # b2 never reported, and the flows into it ran at 40%.  It is not a
    # rack-mate that can say what is normal for r02, so b1 at about 60% is
    # judged against the hosts that did report -- and named.  (59%, not
    # 60: b2's own flows are missing from the model, which is exactly what
    # NO_REPORT warns of, and its peers' shares come out a little higher.)
    setup_run
    mxgen reports --silent b2 --host b2=40 --host b1=60
    out="$(findings floor.dc --mx reports)"
    assert_contains "$out" "b1,1010,HOST_SHORT"
    assert_contains "$out" "while the rest of the fleet is at"
    assert_not_contains "$out" "FLEET_SHORT"
}

t_a_threshold_is_printed_as_given() {
    setup_run
    mxgen reports --eff 99.8
    out="$(rk floor.dc --mx reports --short 99.5 --quiet)"
    assert_contains "$out" "Every host is within 99.5% of what"
}

# --- what is and is not known about the hardware ---------------------------

t_a_host_with_no_speed_gets_no_expectation() {
    setup_run
    sed -i 's/node c\[1..2\] role=server/node c[1..2] role=server nic_gbps=/' floor.dc
    sed -i 's/    row A nic_gbps=10/    row A/; s/rack r01 /rack r01 nic_gbps=10 /; s/rack r02 /rack r02 nic_gbps=10 /' floor.dc
    mxgen reports
    out="$(rk floor.dc --mx reports --flows flows.csv)"
    assert_contains "$out" "not modelled"
    assert_contains "$out" "2 host(s) have no NIC speed"
    flow flows.csv a1 c1
    # Blank expectation, UNMODELLED: nothing was guessed.
    assert_eq "$EXP" ""
    assert_eq "$LIM" "UNMODELLED"
}

t_no_speed_anywhere_is_nothing_to_compare() {
    setup_run
    sed -i 's/ nic_gbps=10//' floor.dc
    mxgen reports
    set +e
    out="$(rk floor.dc --mx reports 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "nothing to compare: no host has a NIC speed"
}

t_reports_with_no_flows_are_nothing_to_compare() {
    # Host rows only: the agents ran and sent nothing.  A report, not a
    # traceback, and not a clean bill of health.
    setup_run
    mxgen reports
    for f in reports/*.csv; do
        grep -v ',tx,' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    done
    set +e
    out="$(rk floor.dc --mx reports 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "mx run, no flows"
    assert_contains "$out" "nothing to compare"
    assert_not_contains "$out" "Traceback"
}

t_nic_gbps_fills_what_the_layout_leaves_out() {
    setup_run
    sed -i 's/ nic_gbps=10//' floor.dc
    mxgen reports
    rk floor.dc --mx reports --nic-gbps 10 --json r.json --quiet >/dev/null
    src="$("$PY" -c "import json;print(json.load(open('r.json'))['hardware']['hosts']['a1']['nic_source'])")"
    assert_eq "$src" "flag"
}

t_a_value_that_is_not_a_number_is_reported_not_guessed() {
    setup_run
    sed -i 's/rack r01 uplinks=1 uplink_gbps=10/rack r01 uplinks=eight uplink_gbps=10/' floor.dc
    sed -i 's/rack r02 uplinks=1 uplink_gbps=10/rack r02 uplinks=2/' floor.dc
    sed -i 's/node c\[1..2\] role=server/node c[1..2] role=server nic_gbps=nan/' floor.dc
    mxgen reports
    out="$(findings floor.dc --mx reports)"
    assert_contains "$out" "uplinks=eight on T/h1/A/r01 is not a count"
    assert_contains "$out" "uplinks=2 on T/h1/A/r02 with no uplink_gbps="
    assert_contains "$out" "nic_gbps=nan on T/h1/A/r03/c1 is not a positive number"
}

t_adversarial_speeds_in_the_layout() {
    # Zero, negative, infinite and a unit glued on: every one is a typo,
    # and every one must be refused by name rather than graded against.
    for bad in 0 -10 inf 25g; do
        setup_run
        sed -i "s/    row A nic_gbps=10/    row A nic_gbps=$bad/" floor.dc
        mxgen reports
        set +e
        out="$(rk floor.dc --mx reports 2>&1)"; rc=$?
        set -e
        assert_status $rc 1
        assert_contains "$out" "nic_gbps=$bad on"
    done
}

t_a_link_that_came_up_slow_is_critical_and_used() {
    setup_run
    mxgen reports
    printf 'date\thost\tspeed\n2026-10-01\ta1\t10000\n2026-10-01\ta2\t1000\n' > speeds.tsv
    out="$(findings floor.dc --mx reports --speeds speeds.tsv)"
    assert_contains "$out" "a2,1010,LINK_SPEED,CRITICAL"
    assert_contains "$out" "a2 negotiated 1 Gb/s on a link the layout says is 10 Gb/s"
    rk floor.dc --mx reports --speeds speeds.tsv --json r.json >/dev/null
    used="$("$PY" -c "import json;h=json.load(open('r.json'))['hardware']['hosts']['a2'];print(h['nic_gbps'], h['nic_source'])")"
    assert_eq "$used" "1.0 measured"
}

t_a_link_faster_than_declared_and_a_link_down() {
    setup_run
    mxgen reports
    printf 'a1 25000\nb1 -1\n' > speeds.txt
    out="$(findings floor.dc --mx reports --speeds speeds.txt)"
    assert_contains "$out" "a1 runs at 25 Gb/s; the layout says 10 Gb/s"
    assert_contains "$out" "b1 reports no link speed (-1)"
}

t_a_speeds_table_must_name_its_speed_column() {
    setup_run
    mxgen reports
    printf 'host\tduplex\na1\tfull\n' > speeds.tsv
    set +e
    out="$(rk floor.dc --mx reports --speeds speeds.tsv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no speed column"
}

t_a_host_missing_from_the_layout_is_named() {
    setup_run
    mxgen reports --hosts a1,a2,b1,b2,c1,c2,x9
    out="$(rk floor.dc --mx reports --nic-gbps 10)"
    assert_contains "$out" "not in layout"
    assert_contains "$out" "x9"
}

t_names_maps_the_run_onto_the_layout() {
    setup_run
    sed -i 's/name={id}/name=srv-{id}/' floor.dc
    mxgen reports
    for h in a1 a2 b1 b2 c1 c2; do echo "$h srv-$h"; done > names.txt
    out="$(rk floor.dc --mx reports --names names.txt --overlay ov.tsv)"
    assert_contains "$out" "Every host is within"
    assert_contains "$(cat ov.tsv)" "$(printf 'reckon_verdict\tsrv-a1\tOK')"
}

t_group_kind_names_the_shared_container() {
    setup_run
    sed -i 's/rack r0/pod p0/' floor.dc
    mxgen reports
    rk floor.dc --mx reports --group-kind pod --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 b1
    assert_eq "$LIM" "UPLINK"
    # Under the default kind there are no racks, so no uplink is modelled.
    rk floor.dc --mx reports --flows flows2.csv --quiet >/dev/null
    flow flows2.csv a1 b1
    assert_eq "$LIM" "NIC"
}

# --- latency -----------------------------------------------------------------

t_queueing_where_there_is_room_is_named() {
    setup_run
    mxgen reports --target 1000 --p50 900
    nmgen idle --p50 40
    out="$(findings floor.dc --mx reports --idle idle --overlay ov.tsv)"
    assert_contains "$out" "QUEUEING,WARN"
    assert_contains "$out" "40 us idle and 900 us loaded"
    # 900 - 40, and not 900 - 99999: the bucket rows are not pairs.
    assert_contains "$(cat ov.tsv)" "$(printf 'reckon_added_rtt\ta1\t860')"
}

t_an_idle_baseline_taken_under_load_is_left_out() {
    setup_run
    mxgen reports --target 1000 --p50 900
    nmgen idle --p50 40 --ts 1005
    out="$(rk floor.dc --mx reports --idle idle)"
    assert_contains "$out" "baseline busy"
    assert_not_contains "$out" "queues building"
}

t_the_wrong_report_for_the_flag_is_refused() {
    setup_run
    mxgen reports
    nmgen idle
    set +e
    out="$(rk floor.dc --mx idle 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "is a netmesh report, not an mx one -- pass it with --idle"
    set +e
    out="$(rk floor.dc --mx reports --idle reports 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "is an mx report, not a netmesh one"
}

# --- iperf -------------------------------------------------------------------

write_iperf_overlay() {
    # $1 mode, then src:dst=mbps samples.
    mode="$1"; shift
    {
        printf '# iperf-orchestrator 2.5.0 -- overlay samples\n'
        printf '# run 20261001120000, mode %s: 3 host(s)\n' "$mode"
        printf '!test\tiperf_mbps_out\tunit=Mb/s\thigher=good\tlabel="Out"\n'
        for s in "$@"; do
            pair="${s%%=*}"; v="${s#*=}"
            printf 'iperf_mbps_out\t%s\t%s\tpeer=%s\tat=20261001120000\trun=20261001120000\n' \
                "${pair%%:*}" "$v" "${pair#*:}"
        done
    } > iperf.tsv
}

t_iperf_parallel_shares_the_nics_in_goodput() {
    # Three hosts in one rack, every pair both ways at once: two flows out
    # of each 10 Gb/s NIC, and goodput is 1448/1538 of the wire at a 1500
    # MTU -- 5000 x 1448 / 1538 = 4707.41 Mb/s each.
    setup_run
    write_iperf_overlay parallel a1:a2=4600 a2:a1=4600 a1:b1=4600 \
        b1:a1=4600 a2:b1=4600 b1:a2=4600
    rk --nic-gbps 10 --iperf iperf.tsv --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 a2
    assert_eq "$EXP" "4707.412"
    assert_eq "$LIM" "NIC"
}

t_iperf_sequential_pair_gives_each_test_the_fabric() {
    setup_run
    write_iperf_overlay sequential-pair a1:a2=9300 a2:a1=9300
    rk --nic-gbps 10 --iperf iperf.tsv --flows flows.csv --quiet >/dev/null
    flow flows.csv a1 a2
    assert_eq "$EXP" "9414.824"
}

t_iperf_mtu_comes_from_netmesh_when_measured() {
    setup_run
    write_iperf_overlay sequential-pair a1:a2=9800
    nmgen idle --hosts a1,a2 --mtu 9000
    rk --nic-gbps 10 --iperf iperf.tsv --idle idle --flows flows.csv \
        --quiet >/dev/null
    # 10000 x 8948 / 9038
    flow flows.csv a1 a2
    assert_eq "$EXP" "9900.420"
}

t_iperf_runs_reckon_cannot_model_are_refused() {
    setup_run
    write_iperf_overlay rolling a1:a2=900
    set +e
    out="$(rk --nic-gbps 10 --iperf iperf.tsv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "rolling run"
    # No mode in the file and none given: asked for, not assumed.
    grep -v '^# run' iperf.tsv > nomode.tsv
    set +e
    out="$(rk --nic-gbps 10 --iperf nomode.tsv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "pass --iperf-mode"
    out="$(rk --nic-gbps 10 --iperf nomode.tsv --iperf-mode parallel --quiet)"
    assert_contains "$out" "iperf parallel run"
    # A reduced export lost which flow was which.
    printf 'iperf_mbps_out\ta1\t900\n' > reduced.tsv
    set +e
    out="$(rk --nic-gbps 10 --iperf reduced.tsv --iperf-mode parallel 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--overlay-reduce"
}

t_iperf_reads_a_run_directory() {
    setup_run
    mkdir -p run1
    printf 'timestamp,source,target,status,protocol,mbps\n' > run1/iperf_results.csv
    printf '1,a1,a2,OK,TCP,9300\n1,a2,a1,OK,TCP,9300\n1,a1,b1,FAIL,TCP,\n' \
        >> run1/iperf_results.csv
    printf 'sequential-pair\n' > run1/.run_mode
    out="$(rk --nic-gbps 10 --iperf run1 --quiet)"
    assert_contains "$out" "iperf sequential-pair run"
    out="$(findings --nic-gbps 10 --iperf run1)"
    assert_contains "$out" "1 test(s) failed and carried nothing -- a1 -> b1 (FAIL)"
}

# --- outputs -----------------------------------------------------------------

# --- since a baseline --------------------------------------------------------
#
# The baseline run is every flow at 104% -- inside the 105% that would make
# it ABOVE_HARDWARE -- so a fall of 12 points lands at 92%, still above
# --short.  That is the case the comparison exists for: a host going wrong
# that no shortfall rule can see yet.

# The human report with its wrapping undone, for phrases a line break
# would otherwise split.
flat() { "$PY" "$RK" "$@" | tr -s ' \n' '  '; }

baseline() {
    mxgen before --eff 104
    rk floor.dc --mx before --run tue --json before.json --overlay before.tsv \
        --quiet >/dev/null
}

t_a_host_that_fell_is_named_before_it_is_short() {
    setup_run
    baseline
    mxgen now --eff 104 --host a1=92
    out="$(findings floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "a1,1010,HOST_REGRESSED,WARN"
    assert_contains "$out" "a1 fell from 104% to 92% of what its hardware allows since run tue, while the rest of r01 held"
    assert_not_contains "$out" "HOST_SHORT"
    # Nobody else moved, so nobody else is named.
    assert_eq "$(grep -c REGRESSED <<<"$out")" "1"
    # Without a baseline the same run is clean: 92% is not short.
    out="$(findings floor.dc --mx now)"
    assert_not_contains "$out" "a1,"
}

t_an_overlay_is_a_baseline_as_good_as_the_json() {
    setup_run
    baseline
    mxgen now --eff 104 --host a1=92
    findings floor.dc --mx now --baseline before.json > from-json
    findings floor.dc --mx now --baseline before.tsv > from-overlay
    cmp -s from-json from-overlay || fail "the two baselines disagree"
    RECKON_BASELINE=before.tsv findings floor.dc --mx now > from-env
    cmp -s from-json from-env || fail "RECKON_BASELINE is not --baseline"
}

t_a_rack_that_fell_is_one_finding_on_its_uplinks() {
    setup_run
    baseline
    mxgen now --eff 104 --cross a=92
    out="$(findings floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "GROUP_REGRESSED,WARN"
    assert_contains "$out" "rack r01: 2 of its 2 hosts fell since run tue, median 104% to 92%, while the other racks held"
    assert_contains "$out" "the flows crossing its boundary moved -12 points, the flows inside it held"
    assert_contains "$out" "Capacity out of r01 went down between the runs"
    assert_not_contains "$out" "HOST_REGRESSED"
}

t_a_rack_that_fell_inside_too_is_not_its_uplinks() {
    setup_run
    baseline
    mxgen now --eff 104 --host a1=92 --host a2=92
    out="$(findings floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "inside it (-12 points) and across its boundary (-12 points) alike"
    assert_contains "$out" "Not its uplinks"
}

t_a_fleet_that_fell_is_one_finding() {
    setup_run
    baseline
    mxgen now --eff 92
    out="$(findings floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "FLEET_REGRESSED,WARN"
    assert_contains "$out" "the fleet fell from 104% to 92% of what its hardware allows since run tue (median host; 6 of 6 hosts down 5 points or more)"
    assert_eq "$(grep -c REGRESSED <<<"$out")" "1"
}

t_a_shortfall_says_what_it_was_before() {
    # Below --short the shortfall rule has it already: one finding, with
    # the old figure in it, not a second finding saying it fell.
    setup_run
    baseline
    mxgen now --eff 104 --host a1=60
    out="$(findings floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "a1,1010,HOST_SHORT,WARN"
    assert_contains "$out" "; it was 104% in run tue"
    assert_not_contains "$out" "REGRESSED"
    mxgen low --eff 70
    out="$(findings floor.dc --mx low --baseline before.json)"
    assert_contains "$out" "FLEET_SHORT"
    assert_contains "$out" "mostly against the uplinks; it was 104% in run tue"
    assert_not_contains "$out" "FLEET_REGRESSED"
}

t_a_run_that_held_says_so() {
    setup_run
    baseline
    mxgen now --eff 100
    out="$(rk floor.dc --mx now --baseline before.json --quiet)"
    assert_contains "$out" "and no host fell 5 points since run tue"
    out="$(flat floor.dc --mx now --baseline before.json)"
    assert_contains "$out" "run tue: 6 hosts in both; median host 104% then, 100% now; 0 fell 5 points or more"
    # --drop moves the line: four points is a fall when 3 is the threshold.
    out="$(findings floor.dc --mx now --baseline before.json --drop 3)"
    assert_contains "$out" "FLEET_REGRESSED"
}

t_the_change_reaches_every_output() {
    setup_run
    baseline
    mxgen now --eff 104 --host a1=92
    rk floor.dc --mx now --baseline before.json --overlay ov.tsv --json out.json \
        --flows flows.csv --quiet >/dev/null
    "$PY" - ov.tsv out.json flows.csv <<'EOF'
import csv, json, sys
ov, js, fl = sys.argv[1:4]
declared, change = set(), {}
for line in open(ov):
    cells = line.rstrip("\n").split("\t")
    if cells[0] == "!test":
        declared.add(cells[1])
        if cells[1] == "reckon_change":
            assert 'label="Efficiency change since run tue"' in cells, cells
            assert "min=-50" in cells and "max=50" in cells, cells
    elif cells[0] == "reckon_change":
        change[cells[1]] = (float(cells[2]), cells[3])
assert {"reckon_change", "reckon_peer_change"} <= declared, declared
assert change["a1"] == (-12.0, "then=104.0"), change["a1"]
assert change["b1"][0] == 0.0, change["b1"]
doc = json.load(open(js))
b = doc["baseline"]
assert b["since"] == "run tue" and b["fell"] == ["a1"], b
assert b["hosts_in_both"] == 6 and b["drop_pts"] == 5.0, b
a1 = [h for h in doc["hosts"] if h["host"] == "a1"][0]
assert round(a1["change_pts"]) == -12 and a1["baseline_efficiency_pct"] == 104.0
rows = list(csv.DictReader(open(fl)))
assert rows and "change_pts" in rows[0], rows[0].keys()
assert any(r["src"] == "a1" and r["change_pts"].startswith("-12") for r in rows)
EOF
    assert_status $? 0
    # Its --run label is in the JSON for the next run to name it by.
    assert_contains "$(cat before.json)" '"label": "tue"'
}

t_a_baseline_that_cannot_be_compared_is_refused() {
    setup_run
    baseline
    mxgen now --eff 104
    "$PY" - before.json <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
d["meta"]["unit"] = "Mb/s"
json.dump(d, open("iperf.json", "w"))
d["meta"]["tool"] = "something-else"
json.dump(d, open("other.json", "w"))
for h in d["hosts"]:
    h["efficiency_pct"] = None
d["meta"]["tool"] = "reckon"
d["meta"]["unit"] = "pps"
json.dump(d, open("empty.json", "w"))
EOF
    printf 'host,pps\na1,1\n' > reports.csv
    rk floor.dc --mx before --run mon --overlay mon.tsv --quiet >/dev/null
    grep -v '^[#!]' mon.tsv | cat before.tsv - > two-runs.tsv
    for case in "iperf.json|grade different workloads" \
                "other.json|not reckon --json output" \
                "empty.json|no host in it was compared" \
                "reports.csv|not reckon --json or --overlay output" \
                "missing.json|cannot read missing.json" \
                "two-runs.tsv|holds 2 runs (mon, tue)"; do
        set +e
        out="$(rk floor.dc --mx now --baseline "${case%%|*}" 2>&1)"; rc=$?
        set -e
        assert_status $rc 2
        assert_contains "$out" "${case#*|}"
    done
}

t_a_tampered_baseline_is_counted_not_believed() {
    # A hand-edited baseline: NaN, a string, a bool, a negative.  Each of
    # those hosts has no history -- not a fall to nothing, not a crash --
    # and the report says how many values it could not read.
    setup_run
    baseline
    mxgen now --eff 104 --host a1=92 --host b1=92
    "$PY" - before.json <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
bad = {"a1": "NaN", "a2": True, "b1": -5, "b2": "fast"}
for h in d["hosts"]:
    if h["host"] in bad:
        h["efficiency_pct"] = bad[h["host"]]
d["hardware"]["hosts"]["c1"]["nic_gbps"] = 25
d["hosts"].append({"host": "z9", "efficiency_pct": 99.0})
d["hosts"].append({"efficiency_pct": 99.0})
open("tampered.json", "w").write(json.dumps(d))
EOF
    out="$(flat floor.dc --mx now --baseline tampered.json)"
    assert_contains "$out" "5 value(s) in the baseline are not numbers"
    assert_contains "$out" "1 host(s) the baseline compared are not in this run: z9"
    assert_contains "$out" "modelled at another NIC speed than in the baseline, so their change is partly the declaration: c1"
    out="$(findings floor.dc --mx now --baseline tampered.json)"
    assert_not_contains "$out" "a1,1010,HOST_REGRESSED"
    assert_not_contains "$out" "b1,1010,HOST_REGRESSED"
}

t_the_overlay_declares_every_test_it_uses() {
    setup_run
    mxgen reports --host b2=45
    rk floor.dc --mx reports --overlay ov.tsv --run r7 --quiet >/dev/null
    "$PY" - ov.tsv <<'EOF'
import sys
declared, used, peer = set(), set(), 0
for line in open(sys.argv[1]):
    if line.startswith("#") or not line.strip():
        continue
    cells = line.rstrip("\n").split("\t")
    if cells[0] == "!test":
        declared.add(cells[1])
        continue
    used.add(cells[0])
    assert "run=r7" in cells, cells
    if cells[0] == "reckon_peer_efficiency":
        peer += 1
        assert any(c.startswith("peer=") for c in cells), cells
assert used <= declared, used - declared
assert peer == 30, peer
EOF
    assert_status $? 0
}

t_the_findings_csv_has_the_house_header() {
    setup_run
    mxgen reports --host b2=45
    head="$(rk floor.dc --mx reports --csv | head -1)"
    assert_eq "$head" "host,ts,rule_id,severity,title,detail,fix"
    out="$(rk floor.dc --mx reports --csv)"
    assert_contains "$out" "b2,1010,HOST_SHORT,CRITICAL"
}

t_two_reckonings_of_one_run_are_identical() {
    setup_run
    mxgen reports --host b2=45 --cross c=70
    rk floor.dc --mx reports --overlay o1 --json j1 --csv c1 --flows f1 >/dev/null
    rk floor.dc --mx reports --overlay o2 --json j2 --csv c2 --flows f2 >/dev/null
    for x in o j c f; do
        cmp -s "${x}1" "${x}2" || fail "$x differs between two runs"
    done
}

t_a_dash_is_stdout_not_a_file() {
    setup_run
    mxgen reports
    out="$(rk floor.dc --mx reports --overlay -)"
    assert_contains "$out" "!test"
    assert_not_contains "$out" "VERDICT"
    assert_no_file "$TEST_TMPDIR/-"
}

t_exit_code_follows_the_worst_finding() {
    setup_run
    mxgen reports
    rk floor.dc --mx reports --exit-code >/dev/null
    assert_status $? 0
    mxgen reports --host b2=70
    set +e
    rk floor.dc --mx reports --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 10
    mxgen reports --host b2=30
    set +e
    rk floor.dc --mx reports --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 20
}

# --- refusals ---------------------------------------------------------------

t_numbers_that_cannot_mean_anything_are_refused() {
    setup_run
    mxgen reports
    for args in "--mtu 0" "--mtu 70000" "--nic-gbps 0" "--nic-gbps -1" \
                "--nic-gbps nan" "--window -1" "--short -5" \
                "--fail 95" "--top -1" "--drop 0" "--drop -3" \
                "--drop nan"; do
        set +e
        # Unquoted on purpose: each string is a whole argument list.
        # shellcheck disable=SC2086
        out="$(rk floor.dc --mx reports $args 2>&1)"; rc=$?
        set -e
        assert_status $rc 2
    done
    set +e
    out="$(RECKON_SHORT=ninety rk floor.dc --mx reports 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "RECKON_SHORT='ninety' is not a number"
}

t_one_run_at_a_time() {
    setup_run
    mxgen reports
    write_iperf_overlay parallel a1:a2=1
    for args in "" "--mx reports --iperf iperf.tsv"; do
        set +e
        # shellcheck disable=SC2086
        out="$(rk floor.dc $args 2>&1)"; rc=$?
        set -e
        assert_status $rc 2
        assert_contains "$out" "give one run to compare"
    done
}

t_rules_and_explain() {
    out="$(rk --rules)"
    for r in LINK_SPEED NO_REPORT HOST_SHORT GROUP_SHORT FLEET_SHORT \
             FLOW_SHORT ABOVE_HARDWARE QUEUEING; do
        assert_contains "$out" "$r"
    done
    out="$(rk --explain host_short)"
    assert_contains "$out" "HOST_SHORT -- host short"
    set +e
    rk --explain NOPE >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_a_hostile_locale_loses_nothing() {
    # LANG=C with a rack named outside ASCII: the report is degraded a
    # character at a time, never lost to an encode error.
    setup_run
    sed -i 's/rack r02 /rack r02 name="Bâtiment 2" /' floor.dc
    mxgen reports --cross b=60
    out="$(LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 rk floor.dc --mx reports 2>&1)"
    assert_contains "$out" "rack r02"
}

echo "reckon"
run_test "the fair share, worked by hand"       t_the_expectation_is_the_fair_share_worked_by_hand
run_test "a limit is charged to the sender"     t_a_limit_is_charged_to_the_sending_side
run_test "a target below the share is expected" t_a_target_below_the_share_is_the_expectation
run_test "a layer shares only with its layer"   t_a_layered_run_shares_only_within_a_layer
run_test "non-measurements are skipped"         t_cells_that_are_not_measurements_are_skipped
run_test "a drain row is not a flow"            t_a_drain_row_is_not_a_flow
run_test "--window cuts where mx cuts"          t_the_window_cuts_where_mx_summarize_cuts
run_test "a clean run says so"                  t_a_clean_run_says_so
run_test "a slow host, not its rack"            t_a_slow_host_is_named_and_not_its_rack
run_test "a rack short across its uplinks"      t_a_rack_short_across_its_uplinks_names_them
run_test "a rack short inside and out"          t_a_rack_short_inside_and_out_is_not_its_uplinks
run_test "a fleet short together"               t_a_fleet_short_together_is_one_finding
run_test "one short path is a path"             t_one_short_path_is_a_path_not_a_host
run_test "above the hardware is a bad layout"   t_above_the_hardware_is_a_wrong_declaration_not_a_success
run_test "a cpu-bound host is its cpu"          t_a_cpu_bound_host_is_its_cpu_not_its_link
run_test "loss with room to spare"              t_loss_with_room_to_spare_is_named
run_test "a silent host is a finding"           t_a_silent_host_is_a_finding
run_test "a silent host says where to look"   t_a_silent_host_says_where_to_look
run_test "a silent host is not evidence"     t_a_silent_host_is_not_evidence_about_its_rack
run_test "a threshold prints as given"      t_a_threshold_is_printed_as_given
run_test "no speed, no expectation"             t_a_host_with_no_speed_gets_no_expectation
run_test "no speed anywhere exits 1"            t_no_speed_anywhere_is_nothing_to_compare
run_test "no flows is nothing to compare"       t_reports_with_no_flows_are_nothing_to_compare
run_test "--nic-gbps fills the gaps"            t_nic_gbps_fills_what_the_layout_leaves_out
run_test "a non-number is reported"             t_a_value_that_is_not_a_number_is_reported_not_guessed
run_test "adversarial speeds are refused"       t_adversarial_speeds_in_the_layout
run_test "a link that came up slow"             t_a_link_that_came_up_slow_is_critical_and_used
run_test "a fast link and a link down"          t_a_link_faster_than_declared_and_a_link_down
run_test "a speeds table names its column"      t_a_speeds_table_must_name_its_speed_column
run_test "a host missing from the layout"       t_a_host_missing_from_the_layout_is_named
run_test "--names maps onto the layout"         t_names_maps_the_run_onto_the_layout
run_test "--group-kind names the container"     t_group_kind_names_the_shared_container
run_test "queueing with room is named"          t_queueing_where_there_is_room_is_named
run_test "a busy baseline is left out"          t_an_idle_baseline_taken_under_load_is_left_out
run_test "the wrong report is refused"          t_the_wrong_report_for_the_flag_is_refused
run_test "iperf parallel, in goodput"           t_iperf_parallel_shares_the_nics_in_goodput
run_test "iperf sequential-pair alone"          t_iperf_sequential_pair_gives_each_test_the_fabric
run_test "iperf mtu from netmesh"               t_iperf_mtu_comes_from_netmesh_when_measured
run_test "unmodellable iperf runs refused"      t_iperf_runs_reckon_cannot_model_are_refused
run_test "iperf reads a run directory"          t_iperf_reads_a_run_directory
run_test "a host that fell is named"         t_a_host_that_fell_is_named_before_it_is_short
run_test "an overlay is a baseline too"         t_an_overlay_is_a_baseline_as_good_as_the_json
run_test "a rack that fell, on its uplinks"     t_a_rack_that_fell_is_one_finding_on_its_uplinks
run_test "a rack that fell inside too"          t_a_rack_that_fell_inside_too_is_not_its_uplinks
run_test "a fleet that fell is one finding"     t_a_fleet_that_fell_is_one_finding
run_test "a shortfall says what it was"         t_a_shortfall_says_what_it_was_before
run_test "a run that held says so"              t_a_run_that_held_says_so
run_test "the change reaches every output"      t_the_change_reaches_every_output
run_test "an incomparable baseline refused"     t_a_baseline_that_cannot_be_compared_is_refused
run_test "a tampered baseline is counted"       t_a_tampered_baseline_is_counted_not_believed
run_test "the overlay declares its tests"       t_the_overlay_declares_every_test_it_uses
run_test "the findings csv header"              t_the_findings_csv_has_the_house_header
run_test "two reckonings are identical"         t_two_reckonings_of_one_run_are_identical
run_test "a dash is stdout"                     t_a_dash_is_stdout_not_a_file
run_test "--exit-code follows severity"         t_exit_code_follows_the_worst_finding
run_test "meaningless numbers refused"          t_numbers_that_cannot_mean_anything_are_refused
run_test "one run at a time"                    t_one_run_at_a_time
run_test "--rules and --explain"                t_rules_and_explain
run_test "a hostile locale loses nothing"       t_a_hostile_locale_loses_nothing
finish
