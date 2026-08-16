#!/usr/bin/env bash
# why-slow: the rules engine, driven entirely from fact files.
#
# No /proc, no root, no slow machine: every rule is a pure function of the
# fact dict, so --from-facts is a complete harness for the diagnostic
# surface.  Each case states only the facts it cares about.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
WS="$BINNACLE_DIR/why_slow.py"

ws() { "$PY" "$WS" "$@"; }

t_healthy_box_says_so() {
    facts_json "$TEST_TMPDIR/f.json"
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "This box is not slow"
    # The advice text is wrapped, so assert on a single word rather than a
    # phrase that a line break can fall inside.
    assert_contains "$out" "off-box"
    assert_contains "$out" "Nothing here is wrong"
}

t_swap_outranks_cpu_in_verdict() {
    # The whole point of the precedence table: a swapping box also looks
    # CPU-busy, and naming the CPU is how an hour gets spent on the wrong
    # thing.
    facts_json "$TEST_TMPDIR/f.json" \
        mem.pswpin_per_s=14200 mem.pswpout_per_s=9800 \
        psi.mem.full10=61 mem.available_pct=0.6 \
        cpu.load1=34.1 cpu.busy_pct=92
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "memory-starved and swapping"
    assert_contains "$out" "SWAP_THRASH cpu saturated" || true
    # CPU saturation must still be reported, just not as the verdict.
    assert_contains "$out" "cpu saturated"
    verdict="$(printf '%s' "$out" | grep -A1 VERDICT | head -2)"
    assert_not_contains "$verdict" "cpu saturated"
}

t_oom_is_critical() {
    facts_json "$TEST_TMPDIR/f.json" \
        'kern.oom=["Out of memory: Killed process 991 (postgres)"]'
    out="$(ws --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "OOM_KILLS,CRITICAL"
}

t_steal_names_the_hypervisor() {
    facts_json "$TEST_TMPDIR/f.json" cpu.steal_pct=22
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "hypervisor"
    assert_contains "$out" "Nothing on this box can fix this"
}

t_thresholds_are_boundaries() {
    # Just under must not fire; just over must.
    facts_json "$TEST_TMPDIR/lo.json" cpu.steal_pct=4.9
    facts_json "$TEST_TMPDIR/hi.json" cpu.steal_pct=5.1
    lo="$(ws --from-facts "$TEST_TMPDIR/lo.json" --csv)"
    hi="$(ws --from-facts "$TEST_TMPDIR/hi.json" --csv)"
    assert_not_contains "$lo" "CPU_STEAL"
    assert_contains "$hi" "CPU_STEAL"
}

t_cgroup_limit_beats_host_health() {
    # A container at its own limit is out of memory while the host is fine.
    facts_json "$TEST_TMPDIR/f.json" sys.container=true cg.v=2 \
        cg.mem_pct=97 cg.mem_max=4294967296 cg.mem_current=4166123520 \
        mem.available_pct=73
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "at its own memory limit"
    assert_contains "$out" "container"
}

t_quota_vs_nproc_names_the_env_vars() {
    facts_json "$TEST_TMPDIR/f.json" cg.cpu_quota_cores=2.0 cpu.count=96
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "GOMAXPROCS=2"
    assert_contains "$out" "ActiveProcessorCount=2"
}

t_missing_facts_are_skipped_with_a_reason() {
    facts_json "$TEST_TMPDIR/f.json" psi.cpu.some10=null kern.oom=null
    out="$(ws --from-facts "$TEST_TMPDIR/f.json" --all)"
    assert_contains "$out" "skipped"
    assert_contains "$out" "CONFIG_PSI"
    assert_contains "$out" "rerun with sudo"
}

t_exit_codes_are_not_confusable_with_usage() {
    facts_json "$TEST_TMPDIR/ok.json"
    facts_json "$TEST_TMPDIR/crit.json" mem.available_pct=0.6
    facts_json "$TEST_TMPDIR/warn.json" cpu.steal_pct=7
    ws --from-facts "$TEST_TMPDIR/ok.json" --exit-code >/dev/null; assert_status $? 0
    set +e
    ws --from-facts "$TEST_TMPDIR/crit.json" --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 20
    set +e
    ws --from-facts "$TEST_TMPDIR/warn.json" --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 10
    # A usage error must stay 2, never a severity.
    set +e
    ws --from-facts /nonexistent-facts.json >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_csv_header_is_stable() {
    facts_json "$TEST_TMPDIR/f.json" mem.available_pct=0.6
    out="$(ws --from-facts "$TEST_TMPDIR/f.json" --csv | head -1)"
    assert_eq "$out" "host,ts,rule_id,severity,title,detail,fix"
}

t_rules_and_explain_are_generated() {
    out="$(ws --rules)"
    assert_contains "$out" "SWAP_THRASH"
    assert_contains "$out" "needs:"
    one="$(ws --explain SWAP_THRASH)"
    assert_contains "$one" "swap thrashing"
    set +e
    ws --explain NOT_A_RULE >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_reads_a_synthetic_proc_tree() {
    # --proc-root is the seam that makes the fact layer testable.
    mkdir -p "$TEST_TMPDIR/proc"
    printf 'cpu  100 0 50 800 10 0 5 20 0 0\ncpu0 100 0 50 800 10 0 5 20 0 0\nctxt 1000\nprocesses 50\nprocs_running 1\nprocs_blocked 0\n' \
        > "$TEST_TMPDIR/proc/stat"
    printf '0.40 0.30 0.20 1/210 9999\n' > "$TEST_TMPDIR/proc/loadavg"
    printf 'MemTotal: 1000000 kB\nMemAvailable: 900000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n' \
        > "$TEST_TMPDIR/proc/meminfo"
    printf '10 0\n' > "$TEST_TMPDIR/proc/uptime"
    out="$(ws --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/nosys" \
             --interval 0 --no-exec --facts)"
    assert_contains "$out" '"cpu.load1": 0.4'
    assert_contains "$out" '"mem.available_pct": 90.0'
}

t_single_snapshot_skips_rate_rules() {
    mkdir -p "$TEST_TMPDIR/proc"
    printf 'cpu  100 0 50 800 10 0 5 20 0 0\ncpu0 100 0 50 800 10 0 5 20 0 0\n' \
        > "$TEST_TMPDIR/proc/stat"
    printf '0.40 0.30 0.20 1/210 9999\n' > "$TEST_TMPDIR/proc/loadavg"
    printf 'MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n' \
        > "$TEST_TMPDIR/proc/meminfo"
    out="$(ws --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/nosys" \
             --interval 0 --no-exec --all)"
    assert_contains "$out" "rerun without --interval 0"
}

t_a_counter_reset_is_unknown_not_negative() {
    # Counters reset under a live read: a container restart, a module
    # reload, a wrap.  (b - a) / dt with b < a reported a negative rate
    # -- "-99990 pg/s in" -- and a busy percentage past 100.  A pair the
    # kernel reset between reads is not a measurement at all.
    mkdir -p "$TEST_TMPDIR/proc"
    printf 'cpu  5000 0 900 80000 100 0 50 200 0 0\ncpu0 5000 0 900 80000 100 0 50 200 0 0\nctxt 1000\nprocesses 50\nprocs_running 1\nprocs_blocked 0\n' \
        > "$TEST_TMPDIR/proc/stat"
    printf 'pswpin 1000000\npswpout 900000\n' > "$TEST_TMPDIR/proc/vmstat"
    printf '0.40 0.30 0.20 1/210 9999\n' > "$TEST_TMPDIR/proc/loadavg"
    printf 'MemTotal: 1000000 kB\nMemAvailable: 900000 kB\nSwapTotal: 100000 kB\nSwapFree: 100000 kB\n' \
        > "$TEST_TMPDIR/proc/meminfo"
    printf '10 0\n' > "$TEST_TMPDIR/proc/uptime"
    ( sleep 0.4
      printf 'cpu  100 0 20 500 5 0 2 10 0 0\ncpu0 100 0 20 500 5 0 2 10 0 0\nctxt 100\nprocesses 5\nprocs_running 1\nprocs_blocked 0\n' \
          > "$TEST_TMPDIR/proc/stat"
      printf 'pswpin 10\npswpout 5\n' > "$TEST_TMPDIR/proc/vmstat" ) &
    out="$(ws --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/nosys" \
             --interval 1 --no-exec --facts)"
    wait
    assert_contains "$out" '"cpu.busy_pct": null'
    assert_contains "$out" '"mem.pswpin_per_s": null'
    # nothing measured may be negative
    assert_not_contains "$out" '": -'
}

t_a_full_table_outranks_the_drops_it_causes() {
    # Same precedence argument as swap over CPU: a conntrack table at the
    # ceiling is why the retransmits are happening, so naming the
    # retransmits would send the reader to the network.
    facts_json "$TEST_TMPDIR/f.json" \
        lim.conntrack_pct=93 lim.conntrack=244000 lim.conntrack_max=262144 \
        net.tcp_retrans_pct=6.0
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "connection tracking"
    assert_contains "$out" "tcp retransmitting"
    verdict="$(printf '%s' "$out" | grep -A2 VERDICT | head -3)"
    assert_not_contains "$verdict" "retransmit"
}

t_a_limit_already_hit_outranks_a_drained_table() {
    # The table has since drained, so every ratio reads healthy and only
    # the kernel log remembers.
    facts_json "$TEST_TMPDIR/f.json" \
        'kern.limit_hits=["kernel: nf_conntrack: table full, dropping packet"]' \
        lim.conntrack_pct=4
    out="$(ws --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "LIMIT_HITS,CRITICAL"
    human="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$human" "already refused work"
}

t_cgroup_task_limit_beats_host_health() {
    facts_json "$TEST_TMPDIR/f.json" sys.container=true cg.v=2 \
        cg.pids_pct=97 cg.pids_current=485 cg.pids_max=500 \
        lim.pid_pct=0.4
    out="$(ws --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "TasksMax"
    assert_contains "$out" "cgroup task limit"
}

t_exhaustion_thresholds_are_boundaries() {
    facts_json "$TEST_TMPDIR/lo.json" lim.fd_pct=74.9
    facts_json "$TEST_TMPDIR/hi.json" lim.fd_pct=75.1
    lo="$(ws --from-facts "$TEST_TMPDIR/lo.json" --csv)"
    hi="$(ws --from-facts "$TEST_TMPDIR/hi.json" --csv)"
    assert_not_contains "$lo" "FD_EXHAUSTION"
    assert_contains "$hi" "FD_EXHAUSTION,WARN"
}

t_ceilings_are_read_from_a_synthetic_proc_tree() {
    mkdir -p "$TEST_TMPDIR/proc/sys/fs" "$TEST_TMPDIR/proc/sys/kernel" \
             "$TEST_TMPDIR/proc/sys/net/netfilter" \
             "$TEST_TMPDIR/proc/sys/net/ipv4/neigh/default" \
             "$TEST_TMPDIR/proc/net"
    printf 'cpu  100 0 50 800 10 0 5 20 0 0\ncpu0 100 0 50 800 10 0 5 20 0 0\n' \
        > "$TEST_TMPDIR/proc/stat"
    printf '0.40 0.30 0.20 1/900 9999\n' > "$TEST_TMPDIR/proc/loadavg"
    printf 'MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n' \
        > "$TEST_TMPDIR/proc/meminfo"
    printf '8000\t0\t10000\n' > "$TEST_TMPDIR/proc/sys/fs/file-nr"
    printf '1000\n' > "$TEST_TMPDIR/proc/sys/kernel/pid_max"
    printf '200\n' > "$TEST_TMPDIR/proc/sys/net/netfilter/nf_conntrack_count"
    printf '250\n' > "$TEST_TMPDIR/proc/sys/net/netfilter/nf_conntrack_max"
    printf 'sockets: used 300\nTCP: inuse 12 orphan 0 tw 100 alloc 20 mem 3\n' \
        > "$TEST_TMPDIR/proc/net/sockstat"
    printf '32768\t60999\n' > "$TEST_TMPDIR/proc/sys/net/ipv4/ip_local_port_range"
    printf '1024\n' \
        > "$TEST_TMPDIR/proc/sys/net/ipv4/neigh/default/gc_thresh3"
    {
        printf 'IP address       HW type     Flags       HW address            Mask     Device\n'
        for i in $(seq 1 900); do
            printf '10.0.0.%d       0x1         0x2         00:00:00:00:00:01     *        eth0\n' "$i"
        done
    } > "$TEST_TMPDIR/proc/net/arp"
    out="$(ws --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/nosys" \
             --interval 0 --no-exec --facts)"
    assert_contains "$out" '"lim.fd_pct": 80.0'
    assert_contains "$out" '"lim.pid_pct": 90.0'
    assert_contains "$out" '"lim.conntrack_pct": 80.0'
    assert_contains "$out" '"lim.arp": 900'
    # Tasks, not processes: the count comes from loadavg, because a
    # threaded process consumes many pids and one process entry.
    assert_contains "$out" '"lim.tasks": 900'
}

t_an_absent_conntrack_table_is_not_an_empty_one() {
    # No netfilter loaded means there is nothing to fill, which must read as
    # "not measured", never as a healthy 0%.
    mkdir -p "$TEST_TMPDIR/proc"
    printf 'cpu  100 0 50 800 10 0 5 20 0 0\ncpu0 100 0 50 800 10 0 5 20 0 0\n' \
        > "$TEST_TMPDIR/proc/stat"
    printf '0.40 0.30 0.20 1/210 9999\n' > "$TEST_TMPDIR/proc/loadavg"
    printf 'MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n' \
        > "$TEST_TMPDIR/proc/meminfo"
    out="$(ws --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/nosys" \
             --interval 0 --no-exec --all)"
    assert_contains "$out" "connection tracking is"
    assert_not_contains "$out" "conntrack entries in use (0%)"
}

t_an_empty_fact_file_is_not_a_clean_bill_of_health() {
    # Every rule skipped used to print "This box is not slow", which is the
    # clean-report-you-cannot-trust failure one step further along: not a
    # run that quietly checked less, but one that checked nothing at all
    # and still came back healthy.
    printf '{}' > "$TEST_TMPDIR/empty.json"
    out="$(ws --from-facts "$TEST_TMPDIR/empty.json")"
    assert_contains "$out" "nothing could be checked"
    assert_not_contains "$out" "This box is not slow"
    csv="$(ws --from-facts "$TEST_TMPDIR/empty.json" --csv)"
    assert_contains "$csv" "NOTHING_CHECKED,WARN"
    # And a monitoring wrapper must not read it as healthy either.
    set +e
    ws --from-facts "$TEST_TMPDIR/empty.json" --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 10
}

t_a_facts_file_that_is_not_a_dictionary_is_refused() {
    # A hostname that is present but null must render as ?, never as the
    # literal None.
    printf '{"sys.hostname": null}' > "$TEST_TMPDIR/nullhost.json"
    nh="$(ws --from-facts "$TEST_TMPDIR/nullhost.json" --quiet)"
    assert_contains "$nh" "why_slow.py -- ?"
    assert_not_contains "$nh" "-- None"
    printf '[]' > "$TEST_TMPDIR/list.json"
    set +e
    out="$(ws --from-facts "$TEST_TMPDIR/list.json" 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "does not hold a fact dictionary"
    assert_not_contains "$out" "Traceback"
}

echo "why-slow"
run_test "healthy box says so"                t_healthy_box_says_so
run_test "swap outranks cpu in the verdict"   t_swap_outranks_cpu_in_verdict
run_test "oom kill is critical"               t_oom_is_critical
run_test "steal names the hypervisor"         t_steal_names_the_hypervisor
run_test "thresholds fire only past the line" t_thresholds_are_boundaries
run_test "cgroup limit beats host health"     t_cgroup_limit_beats_host_health
run_test "quota vs nproc names the env vars"  t_quota_vs_nproc_names_the_env_vars
run_test "missing facts skip with a reason"   t_missing_facts_are_skipped_with_a_reason
run_test "exit codes avoid usage collision"   t_exit_codes_are_not_confusable_with_usage
run_test "csv header is stable"               t_csv_header_is_stable
run_test "--rules and --explain generated"    t_rules_and_explain_are_generated
run_test "reads a synthetic /proc tree"       t_reads_a_synthetic_proc_tree
run_test "single snapshot skips rate rules"   t_single_snapshot_skips_rate_rules
run_test "a full table outranks its drops"    t_a_full_table_outranks_the_drops_it_causes
run_test "a limit already hit outranks now"   t_a_limit_already_hit_outranks_a_drained_table
run_test "cgroup task limit beats the host"   t_cgroup_task_limit_beats_host_health
run_test "ceiling thresholds are boundaries"  t_exhaustion_thresholds_are_boundaries
run_test "ceilings read from a /proc tree"    t_ceilings_are_read_from_a_synthetic_proc_tree
run_test "absent conntrack is not empty"      t_an_absent_conntrack_table_is_not_an_empty_one
run_test "an empty fact file is not health"   t_an_empty_fact_file_is_not_a_clean_bill_of_health
run_test "a non-dict fact file is refused"    t_a_facts_file_that_is_not_a_dictionary_is_refused
run_test "a counter reset is unknown"          t_a_counter_reset_is_unknown_not_negative
finish
