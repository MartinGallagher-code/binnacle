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

# --- a ramp: a model fitted to several runs ---------------------------------
#
# mx reports of a ramp, written to a model with known parameters so the fit
# can be held to them: every host delivers min(asked, its ceiling) per flow,
# and its p99 is r0 + b * u / (1 - u) with u = delivered / ceiling -- 20x
# r0 once a step is at the ceiling.  With the defaults (ceiling 70000, r0
# 40 us, b 10 us) the knee at --bloat 4 is worked by hand: u/(1-u) =
# 3 * 40 / 10 = 12, so u = 12/13 and the knee is 70000 * 12/13 = 64615.4.
# One history whose rate steps up back to back (an `mx reload` ramp) unless
# --gap, --dirs or --stagger say otherwise.
write_rampgen() {
    cat > "$TEST_TMPDIR/rampgen.py" <<'EOF'
import argparse, csv, os, random
F = ["ts", "host", "dir", "peer", "size", "rep_size", "target_pps", "pps",
     "mbps", "rep_pps", "rep_mbps", "loss_pct", "rtt_avg_us", "rtt_p50_us",
     "rtt_p99_us", "rtt_max_us", "cpu_pct", "cpu_max_pct", "agent_cpu_pct",
     "workers", "layer"]
p = argparse.ArgumentParser()
p.add_argument("dir")
p.add_argument("--steps", default="20000,40000,60000,80000,max")
p.add_argument("--hosts", default="a1,a2,b1,b2,c1,c2")
p.add_argument("--ceiling", type=float, default=70000.0)
p.add_argument("--host", action="append", default=[])
p.add_argument("--rack", action="append", default=[])
p.add_argument("--r0", type=float, default=40.0)
p.add_argument("--b", type=float, default=10.0)
p.add_argument("--cpu", action="append", default=[])
p.add_argument("--interval", type=int, default=5)
p.add_argument("--per-step", type=int, default=4)
p.add_argument("--gap", type=int, default=0)
p.add_argument("--start", type=int, default=1000)
p.add_argument("--dirs", action="store_true")
p.add_argument("--size", type=int, default=1434)
p.add_argument("--rep", type=int, default=34)
p.add_argument("--noise", type=float, default=0.0)
p.add_argument("--pstep", action="append", default=[])
p.add_argument("--swap", default="")
p.add_argument("--dip", action="append", default=[])
p.add_argument("--stagger", type=int, default=0)
p.add_argument("--warmup", type=float, default=1.0)
p.add_argument("--join", action="append", default=[])
a = p.parse_args()
hosts = a.hosts.split(",")
kv = lambda items: dict(i.split("=", 1) for i in items)
hostc = dict((k, float(v)) for k, v in kv(a.host).items())
rackc = dict((k, float(v)) for k, v in kv(a.rack).items())
cpu = dict((k, float(v)) for k, v in kv(a.cpu).items())
rng = random.Random(7)
steps = a.steps.split(",")
if a.swap:
    i, j = (int(x) for x in a.swap.split(":"))
    steps[i], steps[j] = steps[j], steps[i]

dips = dict((int(k), float(v)) for k, v in kv(a.dip).items())
pstep = dict((int(k), float(v)) for k, v in kv(a.pstep).items())
join = dict((k, int(v)) for k, v in kv(a.join).items())

def ceil(h, si=None):
    if si in dips:
        return dips[si]
    return hostc.get(h, rackc.get(h[0], a.ceiling))

def noisy(v):
    return v * (1 + a.noise / 100.0 * (2 * rng.random() - 1)) if a.noise else v

ts = a.start
writers = {}
for si, step in enumerate(steps):
    T = None if step == "max" else float(step)
    d = os.path.join(a.dir, "step%d" % si) if a.dirs else a.dir
    os.makedirs(d, exist_ok=True)
    for k in range(a.per_step):
        for h in hosts:
            if si < join.get(h, 0):
                continue
            path = os.path.join(d, h + ".csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(F)
                C = ceil(h, si)
                sat = False
                for peer in hosts:
                    if peer == h:
                        continue
                    got = C if T is None else min(T, C)
                    sat = sat or T is None or T > C
                    u = got / C
                    p99 = a.r0 * 20 if u >= 0.98 else a.r0 + a.b * u / (1 - u)
                    w.writerow([ts + hosts.index(h) * a.stagger, h, "tx", peer, a.size, a.rep,
                                "" if T is None else "%g" % T,
                                "%.1f" % (T if T is not None else got),
                                "", "%.1f" % noisy(got * (a.warmup if k == 0 else 1)),
                                "", "0.000", "",
                                "%.0f" % a.r0, "%.4f" % (noisy(p99) * pstep.get(si, 1.0)), "",
                                "", "", "", "", ""])
                agent = cpu.get(h, 20.0) if sat else 20.0
                w.writerow([ts + hosts.index(h) * a.stagger, h, "host", "*", a.size, a.rep, "", "", "", "",
                            "", "", "", "", "", "", "25", "30",
                            "%.0f" % agent, "4", ""])
        ts += a.interval
    ts += a.gap
EOF
}

rampgen() { "$PY" "$TEST_TMPDIR/rampgen.py" "$@"; }

setup_ramp() {
    setup_run
    write_rampgen
    write_flat
}

# The same three racks with two uplinks each: 20 Gb/s out of a rack is
# 195312.5 cross-rack requests/s, above the NICs' 156250, so the NICs are
# every flow's limit and every flow's fair share is one figure, 156250.
# rampgen gives every peer one ceiling, which only a floor like this one
# can carry: on floor.dc the cross-rack flows stop at 97656.25.
write_flat() {
    sed 's/uplinks=1/uplinks=2/' "$TEST_TMPDIR/floor.dc" > "$TEST_TMPDIR/flat.dc"
}

# One fit as `name=value ...` from the --json model, for a case to assert on.
model() {
    "$PY" - "$@" <<'EOF'
import json, subprocess, sys
doc = json.loads(subprocess.run(sys.argv[1:], stdout=subprocess.PIPE,
                                check=True).stdout)
f = doc["model"]["fleet"]
r = lambda v: "-" if v is None else "%.2f" % v
print("steps=%d ceiling=%s r0=%s b=%s knee=%s sustained=%s first_short=%s "
      "check=%s" % (len(doc["steps"]), r(f["ceiling"]), r(f["r0_us"]),
                    r(f["b_us"]), r(f["knee"]), r(f["sustained"]),
                    r(f["first_short"]), f["check"]))
EOF
}

t_a_ramp_is_fitted_and_held_to_the_numbers() {
    setup_ramp
    rampgen ramp
    out="$(model "$PY" "$RK" floor.dc --ramp ramp --json)"
    assert_eq "$out" "steps=5 ceiling=70000.00 r0=40.00 b=10.00 knee=64615.38 sustained=60000.00 first_short=80000.00 check=validated"
    out="$(rk floor.dc --ramp ramp --predict 50000 | tr -s ' \n' '  ')"
    # 50000 is u = 5/7, so u/(1-u) = 2.5 and p99 = 40 + 25.
    assert_contains "$out" "the fleet delivers 50.00 kpps per flow, p99 65 us"
    assert_contains "$out" "p99 reaches 4x at 64.62 kpps, 92% of the ceiling"
    assert_contains "$out" "delivered within 0%, p99 within 0% -- validated"
}

t_the_three_shapes_of_a_ramp_fit_one_model() {
    # Back to back (mx reload), with gaps between runs, one directory per
    # step, and hosts restarting seconds apart: one fabric, one model.
    # And each step's first interval -- the agents starting, here at a
    # fifth of the rate -- is not part of it.  A reload that restarts the
    # fleet in waves, 30 s from the first host to the last, is still one
    # change per step, not a step of its own between each -- and so is a
    # staggered ramp one host joined a step late, which has to be cut by
    # time because that host changed one time fewer.
    setup_ramp
    rampgen back
    rampgen gaps --gap 600
    rampgen dirs --dirs
    rampgen stag --stagger 2
    rampgen warm --warmup 0.2
    rampgen slow --stagger 6 --per-step 8
    rampgen late --stagger 2 --join c2=1
    want="$(model "$PY" "$RK" floor.dc --ramp back --json)"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp gaps --json)" "$want"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp dirs/step0 dirs/step1 \
        dirs/step2 dirs/step3 dirs/step4 --json)" "$want"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp stag --json)" "$want"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp warm --json)" "$want"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp slow --json)" "$want"
    assert_eq "$(model "$PY" "$RK" floor.dc --ramp late --json)" "$want"
}

t_a_host_that_saturates_early_is_named() {
    setup_ramp
    rampgen ramp --ceiling 150000 --steps 40000,80000,120000,160000,max \
        --host a1=60000
    out="$(findings flat.dc --ramp ramp)"
    assert_contains "$out" "a1,1095,HOST_CEILING,CRITICAL"
    assert_contains "$out" "a1 saturates at 60.00 kpps per flow while the rest of r01 reaches 150.00 kpps (40%)"
    # The median host is the fleet's reading, so one slow host does not
    # drag the fleet's ceiling down with it.
    assert_eq "$(grep -c '^[a-z*0-9]*,1095,' <<<"$out")" "1"
}

t_a_rack_that_saturates_early_is_one_finding() {
    setup_ramp
    rampgen ramp --ceiling 150000 --steps 40000,80000,120000,160000,max \
        --rack a=60000
    out="$(findings floor.dc --ramp ramp)"
    assert_contains "$out" "GROUP_CEILING,CRITICAL"
    assert_contains "$out" "rack r01 saturates at 60.00 kpps per flow while the other racks reach 150.00 kpps (40%)"
    assert_contains "$out" "The hardware puts r01's limit on its uplinks"
    assert_not_contains "$out" "HOST_CEILING"
    assert_not_contains "$out" "FIT_CHECK"
}

t_a_ceiling_that_is_the_test_hosts_cpu() {
    setup_ramp
    rampgen one --ceiling 150000 --steps 40000,80000,120000,160000,max \
        --host a1=60000 --cpu a1=97
    out="$(findings floor.dc --ramp one)"
    assert_contains "$out" "a1,1095,RAMP_CPU,WARN"
    assert_contains "$out" "a1 saturated at 60.00 kpps per flow (300.00 kpps in all) with its agent at 97% of a core"
    # Explained by its CPU, so not also a host fault.
    assert_not_contains "$out" "HOST_CEILING"
    rampgen all --cpu a1=97 --cpu a2=97 --cpu b1=97 --cpu b2=97 \
        --cpu c1=97 --cpu c2=97
    out="$(findings floor.dc --ramp all)"
    assert_contains "$out" "6 of the 6 hosts that saturated did so with their mx agent or a core out of CPU"
    assert_contains "$out" "the ramp measured the test hosts, not the network"
    # A fleet held back by its own test agents is not the hardware's
    # shortfall, whatever the ceiling against the hardware says.
    assert_not_contains "$out" "FLEET_CEILING"
}

t_a_ramp_that_never_saturates_says_so() {
    setup_ramp
    rampgen ramp --ceiling 150000 --steps 20000,40000,60000
    out="$(findings floor.dc --ramp ramp)"
    assert_contains "$out" "RAMP_NOT_SATURATED,INFO"
    assert_contains "$out" "it kept up to 60.00 kpps per flow, and its ceiling is somewhere above that"
    assert_not_contains "$out" "FIT_CHECK"
    out="$(rk floor.dc --ramp ramp --predict 100000 | tr -s ' \n' '  ')"
    assert_contains "$out" "The fleet kept up at every step, to 60.00 kpps per flow: its ceiling is above the ramp."
    assert_contains "$out" "beyond the ramp: no step asked for this much and no ceiling was found"
    out="$(model "$PY" "$RK" floor.dc --ramp ramp --json)"
    assert_contains "$out" "ceiling=60000.00 r0=- b=- knee=- sustained=60000.00 first_short=- check=unchecked"
}

t_steps_that_disagree_are_named_and_noise_is_not() {
    setup_ramp
    rampgen bad --ceiling 150000 --steps 40000,80000,120000,160000 \
        --dip 1=60000
    out="$(findings floor.dc --ramp bad)"
    assert_contains "$out" "RAMP_ERRATIC,WARN"
    assert_contains "$out" "the fleet kept up at 120000 pps but fell short at 80000 pps, a lower rate"
    # Either side of the --keep-up line by a fraction of a point is noise
    # on the line: 97.9% then 98.3% is not a fabric that improved.
    rampgen near --ceiling 150000 --steps 40000,80000,120000,160000 \
        --dip 1=78350 --dip 2=118000
    assert_not_contains "$(findings floor.dc --ramp near)" "RAMP_ERRATIC"
}

t_overload_collapse_and_a_one_step_ceiling() {
    # Unpaced, the fleet delivers less than it did paced at its ceiling --
    # and with only one step at the ceiling, leaving it out leaves nothing
    # that shows it, which the check says.
    setup_ramp
    rampgen ramp --ceiling 55000 --steps 20000,40000,60000,max --dip 3=30000
    out="$(findings floor.dc --ramp ramp)"
    assert_contains "$out" "COLLAPSE,WARN"
    assert_contains "$out" "at step max it delivered 30.00 kpps per flow, 55% of the 55.00 kpps it reached at step 60000 pps"
    assert_contains "$out" "only step 60000 pps sits at the ceiling, so leaving it out leaves nothing that shows it"
}

t_queues_that_build_early_are_named() {
    # b = 200, r0 = 40: u/(1-u) = 3 * 40 / 200 = 0.6, u = 0.375 -- the
    # knee at 37.5% of a 150000 ceiling, 56250.
    setup_ramp
    rampgen ramp --ceiling 150000 --steps 30000,60000,90000,120000,max --b 200
    out="$(findings floor.dc --ramp ramp)"
    assert_contains "$out" "QUEUES_EARLY,WARN"
    assert_contains "$out" "p99 reaches 4x its low-load 40 us at 56.25 kpps per flow"
}

t_a_model_that_does_not_fit_its_steps_says_so() {
    setup_ramp
    rampgen two --ceiling 150000 --steps 60000,max
    out="$(findings floor.dc --ramp two)"
    assert_contains "$out" "FIT_CHECK,INFO"
    assert_contains "$out" "with 2 steps the model is fitted but not checked"
    # Throughput on one curve, one step's p99 three times off it: the
    # model is good for one and not the other, and says which.
    rampgen p99 --ceiling 150000 --steps 30000,60000,90000,120000,180000,max \
        --pstep 2=3
    out="$(findings flat.dc --ramp p99)"
    assert_contains "$out" "FIT_CHECK,WARN"
    assert_contains "$out" "the model predicts delivered within 0% but not p99"
    out="$(rk flat.dc --ramp p99 --predict 50000 | tr -s ' \n' '  ')"
    assert_contains "$out" "the delivered rate passed its check; the p99 did not"
}

t_a_knee_when_the_fit_pins_r0_at_zero() {
    setup_ramp
    rampgen ramp --ceiling 150000 --steps 30000,60000,90000,120000,max \
        --b 40 --pstep 0=0.01 --pstep 1=0.2
    rk floor.dc --ramp ramp --json m.json --quiet >/dev/null
    "$PY" - m.json <<'EOF'
import json, sys
f = json.load(open(sys.argv[1]))["model"]["fleet"]
assert f["r0_us"] == 0.0, f["r0_us"]
assert abs(f["p99_base_us"] - 0.5) < 1e-9, f["p99_base_us"]
assert abs(f["knee"] - 5646.69) < 0.01, f["knee"]
EOF
    assert_status $? 0
}

t_a_ceiling_one_step_sets_is_not_validated() {
    # Paced steps that all kept up, then one unpaced: the ceiling is that
    # step's reading alone.  Leaving it out leaves no ceiling to predict
    # it from, so the model is unchecked -- never validated by default --
    # even when that one reading is twice what the rest would allow.
    setup_ramp
    rampgen alone --steps 20000,40000,60000,max --ceiling 70000
    out="$(model "$PY" "$RK" flat.dc --ramp alone --json)"
    assert_contains "$out" "ceiling=70000.00"
    assert_contains "$out" "check=unchecked"
    out="$(findings flat.dc --ramp alone)"
    assert_contains "$out" "FIT_CHECK,INFO"
    assert_contains "$out" "only step max fell short of what it asked, so leaving it out leaves no ceiling to predict it from"
    out="$(rk flat.dc --ramp alone | tr -s ' \n' '  ')"
    assert_contains "$out" "first falls short at step max, unpaced"
    assert_contains "$out" "unchecked: only step max fell short, and the ceiling rests on it alone"
    rampgen liar --steps 20000,40000,60000,max --ceiling 70000 --dip 3=140000
    out="$(rk flat.dc --ramp liar --predict 100000 | tr -s ' \n' '  ')"
    assert_contains "$out" "from a model that has not passed its own check"
    assert_not_contains "$out" "the delivered rate passed its check"
    # A second step above the ceiling, and the unpaced one is predicted
    # from it -- the ceiling checked against itself.
    rampgen pair --steps 20000,40000,60000,90000,max --ceiling 70000
    assert_contains "$(model "$PY" "$RK" flat.dc --ramp pair --json)" "check=validated"
    rampgen pairliar --steps 20000,40000,60000,90000,max --ceiling 70000 \
        --dip 4=140000
    assert_contains "$(model "$PY" "$RK" flat.dc --ramp pairliar --json)" "check=failed"
}

t_a_ramp_checks_the_declaration_like_a_run() {
    # The checks a single run makes of the layout, --speeds and the
    # hosts' names, made once for the whole ramp.
    setup_ramp
    rampgen ramp
    printf 'a1 1000\n' > speeds.txt
    out="$(findings floor.dc --ramp ramp --speeds speeds.txt)"
    assert_contains "$out" "a1,1095,LINK_SPEED,CRITICAL"
    assert_contains "$out" "a1 negotiated 1 Gb/s on a link the layout says is 10 Gb/s"
    assert_contains "$out" "a1,1095,ABOVE_HARDWARE,WARN"
    assert_eq "$(grep -c LINK_SPEED <<<"$out")" "1"
    # A ceiling above what the hardware allows is the declaration's
    # fault, not good news.
    rampgen above --steps 100000,200000,300000,400000,max --ceiling 320000
    out="$(findings floor.dc --ramp above)"
    assert_contains "$out" "ABOVE_HARDWARE,WARN"
    assert_contains "$out" "carried more than the declared hardware allows -- worst a1 -> b1 at step 400000 pps, 328% of expected (320.00 kpps against 97.66 kpps, limit r01 uplinks out)"
    assert_not_contains "$out" "nothing wrong"
    rampgen stray --hosts a1,a2,b1,b2,c1,c2,zz9
    out="$(findings floor.dc --ramp stray)"
    assert_contains "$out" "NOT_IN_LAYOUT,WARN"
    assert_contains "$out" "1 measured host(s) are not in floor.dc, so their traffic is charged to no uplink: zz9"
    assert_contains "$out" "UNMODELLED,WARN"
    # A host the others send to, silent for a step.
    rampgen silent --dirs
    rm silent/step2/c2.csv
    out="$(findings flat.dc --ramp silent/step0 silent/step1 silent/step2 \
        silent/step3 silent/step4)"
    assert_contains "$out" "NO_REPORT,CRITICAL"
    assert_contains "$out" "1 host(s) took part in the run and reported nothing: c2"
}

t_a_ramp_on_two_matrices_is_refused() {
    # mx gen --peers without --seed draws new pairs every step: the
    # points are not on one curve.  A host silent in one step is not a
    # new matrix -- the others still send to it.
    setup_ramp
    rampgen mix/a --steps 20000
    rampgen mix/b --steps 40000 --hosts a1,b1,c1
    rampgen mix/c --steps 60000,max
    set +e
    out="$(rk floor.dc --ramp mix/a mix/b mix/c 2>&1)"; rc=$?
    set -e
    assert_status "$rc" 2
    assert_contains "$out" "the steps send to different peers -- a1 sends to a2 in step a 20000 pps and not in step b 40000 pps"
    assert_contains "$out" "generate each step with the same --seed"
    # Every step missing a different host: the hardware is still the
    # whole matrix's -- every host on flat.dc 5 x 156250 -- taken from all
    # the steps, not from whichever one looked widest.
    rampgen held --dirs
    rm held/step0/a1.csv held/step1/a2.csv held/step2/b1.csv \
        held/step3/b2.csv held/step4/c1.csv
    set +e
    rk flat.dc --ramp held/step0 held/step1 held/step2 held/step3 \
        held/step4 --json m.json --quiet >/dev/null; rc=$?
    set -e
    assert_status "$rc" 0
    "$PY" - m.json <<'EOF'
import json, sys
hosts = json.load(open(sys.argv[1]))["model"]["hosts"]
got = dict((n, h["hardware_total"]) for n, h in hosts.items())
assert got == dict((n, 781250.0) for n in ("a1", "a2", "b1", "b2", "c1",
                                           "c2")), got
EOF
    assert_status $? 0
}

t_a_ramp_reaches_every_output() {
    setup_ramp
    rampgen ramp --host a1=30000
    rk floor.dc --ramp ramp --predict 50000 --json m.json --overlay m.tsv \
        --csv m.csv --run wk40 --quiet >/dev/null
    "$PY" - m.json m.tsv <<'EOF'
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["meta"]["mode"] == "ramp" and doc["meta"]["label"] == "wk40"
assert set(doc["model"]["form"]) == {"delivered", "p99_us"}, doc["model"]["form"]
a1 = doc["model"]["hosts"]["a1"]
assert a1["reached"] and round(a1["ceiling_per_flow"]) == 30000, a1
assert doc["prediction"]["pps_per_flow"] == 50000.0
assert doc["prediction"]["delivered_check"] == "validated", doc["prediction"]
assert any(f["rule_id"] == "HOST_CEILING" for f in doc["findings"])
declared, used = set(), set()
for line in open(sys.argv[2]):
    cells = line.rstrip("\n").split("\t")
    if line.startswith("#") or not line.strip():
        continue
    if cells[0] == "!test":
        declared.add(cells[1])
        continue
    used.add(cells[0])
    assert "run=wk40" in cells, cells
assert used <= declared, used - declared
assert {"reckon_ceiling", "reckon_knee"} <= used, used
EOF
    assert_status $? 0
    assert_eq "$(head -1 m.csv)" "host,ts,rule_id,severity,title,detail,fix"
    rk floor.dc --ramp ramp --json m2.json --overlay m2.tsv --csv m2.csv \
        --predict 50000 --run wk40 --quiet >/dev/null
    for x in json tsv csv; do
        cmp -s "m.$x" "m2.$x" || fail "two fits of one ramp differ ($x)"
    done
}

t_a_ramp_needs_no_layout() {
    # The model is the data's; the hardware is only what it is held to.
    setup_ramp
    rampgen ramp
    out="$(model "$PY" "$RK" --ramp ramp --json)"
    assert_contains "$out" "ceiling=70000.00"
    out="$(rk --ramp ramp --all | tr -s ' \n' '  ')"
    assert_contains "$out" "fleet ceiling no hardware to compare the ceiling with"
}

t_a_ramp_that_cannot_be_fitted_is_refused() {
    setup_ramp
    mxgen single
    rampgen small --dirs --size 64 --rep 64
    rampgen big --dirs
    for case in "--ramp single|a ramp needs two steps or more" \
                "--ramp small/step0 big/step1|the steps use different packet sizes" \
                "--ramp small --mx single|give one run to compare" \
                "--ramp small --idle single|--idle is for one run" \
                "--ramp small --flows f.csv|--flows is for one run" \
                "--ramp small --baseline x.json|--baseline is for one run" \
                "--mx single --predict 5000|--predict needs --ramp" \
                "--ramp small --keep-up 0|--keep-up must be a percentage" \
                "--ramp small --keep-up 150|--keep-up must be a percentage" \
                "--ramp small --predict 0|--predict must be a rate above zero" \
                "--ramp small --predict nan|--predict must be a rate above zero"; do
        set +e
        # shellcheck disable=SC2086
        out="$(rk floor.dc ${case%%|*} 2>&1)"; rc=$?
        set -e
        assert_status $rc 2
        assert_contains "$out" "${case#*|}"
    done
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
run_test "a ramp is fitted to the numbers"      t_a_ramp_is_fitted_and_held_to_the_numbers
run_test "three ramp shapes, one model"         t_the_three_shapes_of_a_ramp_fit_one_model
run_test "a host saturating early"              t_a_host_that_saturates_early_is_named
run_test "a rack saturating early"              t_a_rack_that_saturates_early_is_one_finding
run_test "a ceiling that is the CPU"            t_a_ceiling_that_is_the_test_hosts_cpu
run_test "a ramp that never saturates"          t_a_ramp_that_never_saturates_says_so
run_test "steps that disagree"                  t_steps_that_disagree_are_named_and_noise_is_not
run_test "overload collapse"                    t_overload_collapse_and_a_one_step_ceiling
run_test "queues that build early"              t_queues_that_build_early_are_named
run_test "a model that does not fit"            t_a_model_that_does_not_fit_its_steps_says_so
run_test "a knee when r0 is pinned at zero"     t_a_knee_when_the_fit_pins_r0_at_zero
run_test "one step's ceiling is not validated"  t_a_ceiling_one_step_sets_is_not_validated
run_test "a ramp checks its declaration"       t_a_ramp_checks_the_declaration_like_a_run
run_test "two matrices are not one ramp"       t_a_ramp_on_two_matrices_is_refused
run_test "a ramp reaches every output"          t_a_ramp_reaches_every_output
run_test "a ramp needs no layout"               t_a_ramp_needs_no_layout
run_test "an unfittable ramp is refused"        t_a_ramp_that_cannot_be_fitted_is_refused
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
