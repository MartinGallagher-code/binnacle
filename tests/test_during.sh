#!/usr/bin/env bash
# during: the analysis from synthetic sample series, the sampler for real.
#
# The whole diagnostic surface is a pure function of the sample series, so
# --from-samples drives it with no benchmark, no load and no waiting.  Only
# the handful of cases that are about the sampler itself -- wrapping a
# command, passing its status through, writing the series back out -- run
# anything.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
DU="$BINNACLE_DIR/during.py"

du_() { "$PY" "$DU" "$@"; }

t_a_run_at_no_ceiling_says_the_box_was_not_the_bottleneck() {
    # The finding nothing else prints, and the one that saves the most time.
    during_samples "$TEST_TMPDIR/s.csv" 30
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_contains "$out" "was not the bottleneck"
    assert_contains "$out" "load generator"
}

t_a_pinned_core_is_not_an_idle_box() {
    # 12% busy across 8 cores looks idle and is not: one core is at its
    # ceiling and the work is serialised, which no whole-box average shows.
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=12.5 \
        cpu_max_core_pct=99 cpu_cores=8
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_contains "$out" "one core"
    assert_contains "$out" "serialised"
    assert_not_contains "$out" "was not the bottleneck"
    csv="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --csv)"
    assert_contains "$csv" "BOUND_BY"
}

t_a_saturated_box_is_cpu_bound_not_serialised() {
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=94 \
        cpu_max_core_pct=97 cpu_cores=8
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --quiet)"
    assert_contains "$out" "cpu for 100% of the run"
    assert_not_contains "$out" "one core"
}

t_trust_outranks_attribution_in_the_verdict() {
    # The box really was cpu bound, but the number cannot be believed, and
    # saying "cpu bound" first would send the reader off to tune it.
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=95 \
        cpu_max_core_pct=96 cpu_steal_pct=18
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    verdict="$(printf '%s' "$out" | grep -A2 VERDICT | head -3)"
    assert_contains "$verdict" "tenant"
    assert_not_contains "$verdict" "at the CPU"
    # ...and the bottleneck is still reported, just not as the headline.
    assert_contains "$out" "stolen"
}

t_warmup_is_separated_from_the_steady_window() {
    # A ramp for the first third, flat after: averaging the two together
    # reports a number the system never sustained.
    during_samples "$TEST_TMPDIR/s.csv" 30 \
        cpu_busy_pct=10,20,30,40,50,60,70,80,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90,90 \
        cpu_max_core_pct=12,24,36,48,60,72,84,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95,95
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --all)"
    assert_contains "$out" "warmup"
    assert_contains "$out" "--settle"
}

t_a_steady_run_reports_no_warmup() {
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=90 cpu_max_core_pct=95
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --csv)"
    assert_not_contains "$out" "WARMUP"
    assert_not_contains "$out" "UNSTABLE"
}

t_an_interloper_is_named() {
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=90 \
        cpu_max_core_pct=95 \
        top_comm=,,,,,,,,,,,,,,,backup,backup,backup,,,,,,,,,,,,, \
        top_cpu_pct=,,,,,,,,,,,,,,,70,70,70,,,,,,,,,,,,,
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_contains "$out" "backup"
    assert_contains "$out" "competing for this machine"
}

t_the_benchmark_itself_is_not_an_interloper() {
    # Present in every sample, so it is the workload rather than a surprise.
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=90 \
        cpu_max_core_pct=95 top_comm=bench top_cpu_pct=95
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --csv)"
    assert_not_contains "$out" "INTRUDER"
}

t_burst_exhaustion_is_not_just_a_busy_disk() {
    # Throughput collapses while utilisation stays pinned: a credit budget
    # running out, not a device getting busier.
    during_samples "$TEST_TMPDIR/s.csv" 30 disk_util_pct=95 \
        disk_mbps=250:40 disk_await_ms=8 cpu_busy_pct=20 psi_io_full=30
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_contains "$out" "burst"
    assert_contains "$out" "baseline rate"
}

t_a_clock_that_falls_is_two_different_machines() {
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=95 \
        cpu_max_core_pct=96 cpu_ghz=3.4:2.2
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --csv)"
    assert_contains "$out" "CLOCK_DRIFT"
}

t_shares_are_exclusive_and_add_up() {
    during_samples "$TEST_TMPDIR/s.csv" 20 cpu_busy_pct=95 \
        cpu_max_core_pct=99 cpu_steal_pct=0
    total="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --json | "$PY" -c \
        'import json,sys; d=json.load(sys.stdin)["summary"]["bound.shares"]; print(round(sum(v for v in d.values() if v)))')"
    assert_eq "$total" "100"
}

t_a_short_window_refuses_to_conclude() {
    during_samples "$TEST_TMPDIR/s.csv" 3 cpu_busy_pct=95 cpu_max_core_pct=96
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_contains "$out" "too short"
    verdict="$(printf '%s' "$out" | grep -A2 VERDICT | head -3)"
    assert_contains "$verdict" "too short"
}

t_a_baseline_difference_is_the_finding() {
    during_samples "$TEST_TMPDIR/base.csv" 30 cpu_busy_pct=90 cpu_max_core_pct=95
    during_samples "$TEST_TMPDIR/now.csv" 30 cpu_busy_pct=45 cpu_max_core_pct=95
    out="$(du_ --from-samples "$TEST_TMPDIR/now.csv" \
             --baseline "$TEST_TMPDIR/base.csv" --csv)"
    assert_contains "$out" "BASELINE_DRIFT"
}

t_missing_series_are_skipped_with_a_reason() {
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_ghz= cg_throttled_pct=
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --all)"
    assert_contains "$out" "skipped"
    assert_contains "$out" "no cpufreq information"
}

t_csv_header_matches_why_slow() {
    during_samples "$TEST_TMPDIR/s.csv" 30
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv" --csv | head -1)"
    assert_eq "$out" "host,ts,rule_id,severity,title,detail,fix"
}

t_rules_and_explain_are_generated() {
    out="$(du_ --rules)"
    assert_contains "$out" "NOT_BOUND"
    assert_contains "$out" "needs:"
    one="$(du_ --explain BOUND_BY)"
    assert_contains "$one" "ceiling for the longest"
    set +e
    du_ --explain NOT_A_RULE >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_wrapping_passes_the_commands_status_through() {
    # A drop-in prefix has to be invisible to whatever runs it.
    set +e
    du_ --interval 0.2 -- sh -c 'sleep 0.7; exit 3' >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 3
    set +e
    du_ --interval 0.2 -- sh -c 'sleep 0.7' >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 0
}

t_exit_code_opts_into_severity_instead() {
    set +e
    du_ --interval 0.2 --exit-code -- sh -c 'sleep 1.2' >/dev/null 2>&1; rc=$?
    set -e
    # Short window plus an idle box: at least one rule fires, and a command
    # that succeeded leaves the status free to carry the severity.
    assert_status $rc 10
}

t_an_interrupt_reports_without_waiting_for_the_command() {
    # The docstring promises ^C still prints the report.  It did not: the
    # run blocked in wait() until the command finished, so a benchmark that
    # traps SIGINT -- make, a JVM, most test harnesses -- held the report
    # hostage for as long as it liked.
    # Not the du_ helper: backgrounding a shell function makes $! the
    # subshell, and the signal would go there instead of to the tool.
    "$PY" "$DU" --interval 0.2 -- sh -c 'trap "" INT; sleep 20' \
        > "$TEST_TMPDIR/int.out" 2>&1 &
    local pid=$!
    sleep 1.2
    kill -INT "$pid" 2>/dev/null
    # Bounded wait: the grace period is 2s, so 6 is generous and a hang
    # fails the case instead of stalling the suite.
    local i=0
    while [ $i -lt 60 ]; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
        i=$((i + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
        _fail "still running 6s after the interrupt"
        return 1
    fi
    set +e
    wait "$pid"; rc=$?
    set -e
    assert_status $rc 130
    out="$(cat "$TEST_TMPDIR/int.out")"
    assert_contains "$out" "VERDICT"
    assert_contains "$out" "interrupted"
    # The command was not killed, so the report says where it went.
    assert_contains "$out" "still running as pid"
    pkill -f 'trap "" INT; sleep 20' 2>/dev/null || true
}

t_a_signalled_command_reports_what_a_shell_would() {
    # Popen gives -N for a signalled child, and returning that hands the
    # shell 256-N: 254 for a plain ^C, where running the command directly
    # would have given 130.  A drop-in prefix has to agree with the shell.
    set +e
    du_ --interval 0.2 -- sh -c 'sleep 0.7; kill -TERM $$' >/dev/null 2>&1
    rc=$?
    set -e
    assert_status $rc 143
}

t_corrupt_numbers_are_not_measurements() {
    # A NaN or an infinity in a numeric column is corrupt input, and
    # letting one through poisons every mean and threshold downstream.
    during_samples "$TEST_TMPDIR/s.csv" 30 cpu_busy_pct=95 \
        cpu_max_core_pct=97
    "$PY" - "$TEST_TMPDIR/s.csv" <<'EOF'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
for i, r in enumerate(rows):
    if i % 5 == 0:
        r["cpu_busy_pct"] = "1e400"          # inf once parsed
        r["cpu_max_core_pct"] = "nan"
w = csv.DictWriter(open(sys.argv[1], "w"), fieldnames=list(rows[0]),
                   lineterminator="\n")
w.writeheader()
w.writerows(rows)
EOF
    out="$(du_ --from-samples "$TEST_TMPDIR/s.csv")"
    assert_not_contains "$out" "inf"
    assert_not_contains "$out" "nan"
    # The samples that are real still carry the run.
    assert_contains "$out" "cpu"
}

t_a_series_without_a_hostname_says_so() {
    printf 'elapsed_s,cpu_busy_pct\n1.0,95\n2.0,95\n' > "$TEST_TMPDIR/n.csv"
    out="$(du_ --from-samples "$TEST_TMPDIR/n.csv" --quiet)"
    assert_contains "$out" "during.py -- ?"
    assert_not_contains "$out" "None"
}

t_a_long_run_still_fits_on_a_line() {
    # A ten-minute run at the default interval is 600 samples, and one
    # character per sample is a 600-character line: unreadable in a
    # terminal and unpasteable into a ticket.
    during_samples "$TEST_TMPDIR/long.csv" 600 cpu_busy_pct=95 \
        cpu_max_core_pct=97
    out="$(du_ --from-samples "$TEST_TMPDIR/long.csv")"
    widest="$(printf '%s' "$out" | awk '{ print length($0) }' | sort -n | tail -1)"
    assert_between "$widest" 0 90
    # The reader has to be told the columns are buckets, not samples.
    assert_contains "$out" "10 per column"
    # A short run is still one column per sample, with no note.
    during_samples "$TEST_TMPDIR/short.csv" 30 cpu_busy_pct=95 \
        cpu_max_core_pct=97
    short="$(du_ --from-samples "$TEST_TMPDIR/short.csv")"
    assert_not_contains "$short" "per column"
}

t_a_failed_command_outranks_a_severity() {
    # Reporting "warn" for a benchmark that never finished would hide a
    # build failure behind a diagnosis of it.
    set +e
    du_ --interval 0.2 --exit-code -- sh -c 'sleep 0.9; exit 7' \
        >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 7
}

t_the_series_written_out_reads_back_in() {
    du_ --interval 0.2 --samples "$TEST_TMPDIR/run.csv" \
        -- sh -c 'sleep 1.2' >/dev/null 2>&1
    assert_file_exists "$TEST_TMPDIR/run.csv"
    head="$(head -1 "$TEST_TMPDIR/run.csv")"
    assert_contains "$head" "elapsed_s"
    assert_contains "$head" "cpu_max_core_pct"
    # The analysis is a pure function of the series, so the file alone must
    # reproduce a report with no machine in that state.
    again="$(du_ --from-samples "$TEST_TMPDIR/run.csv")"
    assert_contains "$again" "VERDICT"
}

t_a_window_shorter_than_one_interval_says_so() {
    set +e
    out="$(du_ --interval 5 -- sh -c 'exit 0' 2>&1)"; rc=$?
    set -e
    assert_contains "$out" "shorter than one"
    assert_status $rc 1
}

t_asking_for_neither_a_command_nor_a_window_is_a_usage_error() {
    set +e
    du_ >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

echo "during"
t_a_foreign_csv_is_not_a_quiet_healthy_run() {
    # A file with none of the columns a sample carries is some other CSV.
    # Analysing it classified every row as "not bound" and printed a
    # confident "this box was not the bottleneck" from data that measured
    # nothing.
    printf 'a,b\n1,2\n' > "$TEST_TMPDIR/foreign.csv"
    rc=0
    out="$(du_ --from-samples "$TEST_TMPDIR/foreign.csv" 2>&1)" || rc=$?
    assert_status $rc 2
    assert_contains "$out" "none of the columns"
    assert_not_contains "$out" "not the bottleneck"
}

run_test "no ceiling means not the bottleneck" t_a_run_at_no_ceiling_says_the_box_was_not_the_bottleneck
run_test "a pinned core is not an idle box"    t_a_pinned_core_is_not_an_idle_box
run_test "a saturated box is cpu bound"        t_a_saturated_box_is_cpu_bound_not_serialised
run_test "trust outranks attribution"          t_trust_outranks_attribution_in_the_verdict
run_test "warmup separated from steady"        t_warmup_is_separated_from_the_steady_window
run_test "a steady run reports no warmup"      t_a_steady_run_reports_no_warmup
run_test "an interloper is named"              t_an_interloper_is_named
# --- the other end ---------------------------------------------------------

t_a_generator_at_its_ceiling_is_what_the_test_measured() {
    # The composing guide has described this case since before anything
    # computed it: a generator at a ceiling while the target idles means
    # the benchmark measured the generator.
    during_samples "$TEST_TMPDIR/gen.csv" 30 host=loadgen cpu_busy_pct=97 \
        cpu_max_core_pct=99
    during_samples "$TEST_TMPDIR/tgt.csv" 30 host=target
    out="$(du_ --from-samples "$TEST_TMPDIR/tgt.csv" \
             --peer-samples "$TEST_TMPDIR/gen.csv")"
    # Short fragments only: the report wraps at 76 columns, so anything
    # longer straddles a line break and never matches.
    assert_contains "$out" "loadgen was, at its"
    assert_contains "$out" "The result describes that machine"
}

t_a_bound_box_does_not_blame_the_peer() {
    # This end at a ceiling is this end's finding, whatever the peer did.
    during_samples "$TEST_TMPDIR/peer.csv" 30 host=peerbox cpu_busy_pct=97 \
        cpu_max_core_pct=99
    during_samples "$TEST_TMPDIR/me.csv" 30 host=mine cpu_busy_pct=96 \
        cpu_max_core_pct=98
    out="$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
             --peer-samples "$TEST_TMPDIR/peer.csv" --csv)"
    assert_not_contains "$out" "PEER_WAS_THE_LIMIT"
}

t_neither_end_bound_is_a_finding_neither_could_reach() {
    during_samples "$TEST_TMPDIR/peer.csv" 30 host=peerbox
    during_samples "$TEST_TMPDIR/me.csv" 30 host=mine
    out="$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
             --peer-samples "$TEST_TMPDIR/peer.csv")"
    assert_contains "$out" "Neither this box nor peerbox"
    assert_contains "$out" "between them or inside the application"
}

t_two_runs_that_did_not_overlap_cannot_be_compared() {
    # Trust outranks attribution again: comparing two windows that never
    # coincided says nothing about which end was the limit.
    during_samples "$TEST_TMPDIR/peer.csv" 30 host=peerbox \
        cpu_busy_pct=97 cpu_max_core_pct=99 "ts=1000000..1000030"
    during_samples "$TEST_TMPDIR/me.csv" 30 host=mine "ts=2000000..2000030"
    out="$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
             --peer-samples "$TEST_TMPDIR/peer.csv")"
    assert_contains "$out" "not watched over the same window"
    assert_contains "$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
        --peer-samples "$TEST_TMPDIR/peer.csv" --csv)" \
        "PEER_NOT_CONCURRENT,CRITICAL"
}

t_a_partial_overlap_is_a_warning_not_a_refusal() {
    during_samples "$TEST_TMPDIR/peer.csv" 30 host=peerbox \
        "ts=1000000..1000030"
    during_samples "$TEST_TMPDIR/me.csv" 30 host=mine "ts=1000020..1000050"
    assert_contains "$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
        --peer-samples "$TEST_TMPDIR/peer.csv" --csv)" \
        "PEER_NOT_CONCURRENT,WARN"
}

t_loss_at_the_far_end_is_not_the_paths_loss() {
    during_samples "$TEST_TMPDIR/peer.csv" 30 host=peerbox \
        net_rx_missed_per_s=2500
    during_samples "$TEST_TMPDIR/me.csv" 30 host=mine
    out="$(du_ --from-samples "$TEST_TMPDIR/me.csv" \
             --peer-samples "$TEST_TMPDIR/peer.csv")"
    assert_contains "$out" "peerbox missed"
    assert_contains "$out" "not in the network"
}

t_without_a_peer_the_two_ended_rules_say_why_they_skipped() {
    during_samples "$TEST_TMPDIR/me.csv" 30
    out="$(du_ --from-samples "$TEST_TMPDIR/me.csv" --all)"
    assert_contains "$out" "--peer-samples"
    assert_contains "$out" "the other end of the test"
}


# --- the receive path ------------------------------------------------------

t_a_box_pinned_in_softirq_is_not_an_idle_box() {
    # The failure a whole-box average is structurally unable to show: one
    # core saturated in receive processing while the machine reports 12%.
    during_samples "$TEST_TMPDIR/run.csv" 40 cpu_busy_pct=12 \
        softirq_max_core_pct=96 net_mbps=6000
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv")"
    assert_contains "$out" "one core pinned in softirq"
    assert_contains "$out" "RSS"
}

t_softirq_on_a_busy_box_is_not_the_same_finding() {
    # Half a core in softirq only means something while the rest idle; on
    # a box that is genuinely busy it is just work.
    during_samples "$TEST_TMPDIR/busy.csv" 40 cpu_busy_pct=85 \
        softirq_max_core_pct=60
    assert_not_contains "$(du_ --from-samples "$TEST_TMPDIR/busy.csv" --csv)" \
        "SOFTIRQ_BOUND"
    during_samples "$TEST_TMPDIR/idle.csv" 40 cpu_busy_pct=10 \
        softirq_max_core_pct=60
    assert_contains "$(du_ --from-samples "$TEST_TMPDIR/idle.csv" --csv)" \
        "SOFTIRQ_BOUND,WARN"
}

t_the_cause_outranks_the_state_it_produces() {
    # A box pinned in softirq is why the run looks network bound.  Naming
    # the state as the verdict sends someone to the switch.
    during_samples "$TEST_TMPDIR/run.csv" 40 cpu_busy_pct=12 \
        softirq_max_core_pct=97 net_mbps=9900 net_link_mbps=10000
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv")"
    # Assert on the verdict's own words rather than on nearby lines: the
    # finding list sits directly below it and would match by accident.
    assert_contains "$out" "could not pick packets up fast enough"
    assert_contains "$out" "host limit rather than a network one"
}

t_packets_the_card_dropped_are_not_the_networks_loss() {
    during_samples "$TEST_TMPDIR/run.csv" 40 net_rx_missed_per_s=4200
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv")"
    assert_contains "$out" "the card dropped packets"
    assert_contains "$out" "not in the network"
    assert_contains "$(du_ --from-samples "$TEST_TMPDIR/run.csv" --csv)" \
        "RING_OVERFLOW,CRITICAL"
}

t_a_ring_overflow_is_worst_of_not_mean_of() {
    # Ten seconds of overflow in a long run dropped packets; averaging it
    # towards zero would report a clean run.
    during_samples "$TEST_TMPDIR/run.csv" 40 \
        "net_rx_missed_per_s=0,0,0,0,0,0,0,0,0,5000"
    assert_contains "$(du_ --from-samples "$TEST_TMPDIR/run.csv" --csv)" \
        "RING_OVERFLOW"
}

t_backlog_and_squeeze_are_separate_findings() {
    during_samples "$TEST_TMPDIR/run.csv" 40 softnet_drop_per_s=90 \
        time_squeeze_per_s=450
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv" --csv)"
    assert_contains "$out" "BACKLOG_DROPS,WARN"
    assert_contains "$out" "TIME_SQUEEZE,WARN"
}

t_a_window_limited_run_measured_the_buffer() {
    # 10 Gbit at 40ms is a 50 MB BDP; a 6 MB rmem cannot fill it, so the
    # run's number is about the buffer and not about the path.
    during_samples "$TEST_TMPDIR/run.csv" 40 net_link_mbps=10000 \
        net_rmem_max_kb=6144
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv" --rtt-ms 40)"
    assert_contains "$out" "the receive window capped it"
    assert_contains "$out" "Mbit/s"
    # Trust outranks attribution: this has to be the verdict, not the
    # bottleneck the run appeared to have.
    verdict="$(printf '%s' "$out" | grep -A3 VERDICT | head -4)"
    assert_contains "$verdict" "window"
}

t_a_window_big_enough_is_not_a_finding() {
    during_samples "$TEST_TMPDIR/run.csv" 40 net_link_mbps=1000 \
        net_rmem_max_kb=6144
    assert_not_contains "$(du_ --from-samples "$TEST_TMPDIR/run.csv" \
        --rtt-ms 1 --csv)" "WINDOW_LIMITED"
}

t_without_an_rtt_the_window_check_says_why_it_skipped() {
    # A round trip needs two machines and during watches one, so the fact
    # is absent rather than assumed -- and the skip has to name the fix.
    during_samples "$TEST_TMPDIR/run.csv" 40
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv" --all)"
    assert_contains "$out" "--rtt-ms"
    assert_contains "$out" "netmesh"
}

t_the_receive_path_columns_survive_a_round_trip() {
    during_samples "$TEST_TMPDIR/run.csv" 10 softirq_max_core_pct=44
    out="$(du_ --from-samples "$TEST_TMPDIR/run.csv" --samples \
             "$TEST_TMPDIR/again.csv" >/dev/null && head -1 \
             "$TEST_TMPDIR/again.csv")"
    assert_contains "$out" "softirq_max_core_pct"
    assert_contains "$out" "net_rx_missed_per_s"
    assert_contains "$out" "net_cc"
}


run_test "the benchmark is not an interloper"  t_the_benchmark_itself_is_not_an_interloper
run_test "burst exhaustion is not a busy disk" t_burst_exhaustion_is_not_just_a_busy_disk
run_test "a falling clock is two machines"     t_a_clock_that_falls_is_two_different_machines
run_test "shares are exclusive and add up"     t_shares_are_exclusive_and_add_up
run_test "a short window refuses to conclude"  t_a_short_window_refuses_to_conclude
run_test "a baseline difference is a finding"  t_a_baseline_difference_is_the_finding
run_test "missing series skip with a reason"   t_missing_series_are_skipped_with_a_reason
run_test "csv header matches why-slow"         t_csv_header_matches_why_slow
run_test "--rules and --explain generated"     t_rules_and_explain_are_generated
run_test "wrapping passes status through"      t_wrapping_passes_the_commands_status_through
run_test "--exit-code opts into severity"      t_exit_code_opts_into_severity_instead
run_test "a failed command outranks severity" t_a_failed_command_outranks_a_severity
run_test "an interrupt reports immediately"   t_an_interrupt_reports_without_waiting_for_the_command
run_test "a signalled command reports 128+N"  t_a_signalled_command_reports_what_a_shell_would
run_test "a long run still fits on a line"    t_a_long_run_still_fits_on_a_line
run_test "corrupt numbers are not measured"   t_corrupt_numbers_are_not_measurements
run_test "a series with no hostname says ?"   t_a_series_without_a_hostname_says_so
run_test "the series reads back in"            t_the_series_written_out_reads_back_in
run_test "a too-short window says so"          t_a_window_shorter_than_one_interval_says_so
run_test "neither command nor window errors"   t_asking_for_neither_a_command_nor_a_window_is_a_usage_error
run_test "a foreign csv is refused"           t_a_foreign_csv_is_not_a_quiet_healthy_run
run_test "softirq pinning is not an idle box"  t_a_box_pinned_in_softirq_is_not_an_idle_box
run_test "softirq on a busy box differs"       t_softirq_on_a_busy_box_is_not_the_same_finding
run_test "the cause outranks its state"        t_the_cause_outranks_the_state_it_produces
run_test "card drops are not network loss"     t_packets_the_card_dropped_are_not_the_networks_loss
run_test "ring overflow is worst-of"           t_a_ring_overflow_is_worst_of_not_mean_of
run_test "backlog and squeeze are separate"    t_backlog_and_squeeze_are_separate_findings
run_test "a window-limited run measured rmem"  t_a_window_limited_run_measured_the_buffer
run_test "a big enough window is not a finding" t_a_window_big_enough_is_not_a_finding
run_test "no rtt says why it skipped"          t_without_an_rtt_the_window_check_says_why_it_skipped
run_test "receive columns survive a round trip" t_the_receive_path_columns_survive_a_round_trip
run_test "a generator at its ceiling"          t_a_generator_at_its_ceiling_is_what_the_test_measured
run_test "a bound box does not blame the peer" t_a_bound_box_does_not_blame_the_peer
run_test "neither end bound is a finding"      t_neither_end_bound_is_a_finding_neither_could_reach
run_test "runs that did not overlap"           t_two_runs_that_did_not_overlap_cannot_be_compared
run_test "a partial overlap warns"             t_a_partial_overlap_is_a_warning_not_a_refusal
run_test "far-end loss is not the path's"      t_loss_at_the_far_end_is_not_the_paths_loss
run_test "no peer says why it skipped"         t_without_a_peer_the_two_ended_rules_say_why_they_skipped
finish
