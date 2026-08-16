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
    du_ --interval 0.2 --exit-code -- sh -c 'sleep 1.2; exit 3' \
        >/dev/null 2>&1; rc=$?
    set -e
    # Short window plus an idle box: at least one rule fires, and the status
    # is now a severity rather than the command's 3.
    assert_status $rc 10
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
run_test "no ceiling means not the bottleneck" t_a_run_at_no_ceiling_says_the_box_was_not_the_bottleneck
run_test "a pinned core is not an idle box"    t_a_pinned_core_is_not_an_idle_box
run_test "a saturated box is cpu bound"        t_a_saturated_box_is_cpu_bound_not_serialised
run_test "trust outranks attribution"          t_trust_outranks_attribution_in_the_verdict
run_test "warmup separated from steady"        t_warmup_is_separated_from_the_steady_window
run_test "a steady run reports no warmup"      t_a_steady_run_reports_no_warmup
run_test "an interloper is named"              t_an_interloper_is_named
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
run_test "the series reads back in"            t_the_series_written_out_reads_back_in
run_test "a too-short window says so"          t_a_window_shorter_than_one_interval_says_so
run_test "neither command nor window errors"   t_asking_for_neither_a_command_nor_a_window_is_a_usage_error
finish
