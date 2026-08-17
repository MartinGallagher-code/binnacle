#!/usr/bin/env bash
# skew: the rules engine from fact files, the wire code against a real
# responder on loopback.
#
# Same split as test_resolve.sh, for the same reason.  Every rule is a pure
# function of the fact dict, so --from-facts drives the whole diagnostic
# surface with no clock being read.  The SNTP code cannot be tested that
# way, so a responder is started on 127.0.0.1 and queried for real -- one
# that packs its own replies, so a packing bug here cannot cancel out a
# parsing bug there.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
SK="$BINNACLE_DIR/skew.py"

sk() { "$PY" "$SK" "$@"; }

# --- the rules, from facts -------------------------------------------------

t_a_correct_clock_says_so() {
    skew_facts_json "$TEST_TMPDIR/f.json"
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "knows what time it is"
    # A clean run must not read like breakage anywhere in it.
    assert_not_contains "$out" "CRITICAL"
}

t_no_source_outranks_the_drift_it_causes() {
    # The whole reason the tool exists: the daemon is running, the box
    # looks healthy, and nothing is correcting the clock.  Naming the
    # offset as the verdict would send someone to makestep instead of to
    # the firewall.
    skew_facts_json "$TEST_TMPDIR/f.json" \
        'srv.dead=["10.0.0.1","10.0.0.2","10.0.0.3"]' 'srv.alive=[]' \
        clock.offset_s=252.0 'clock.spread_s=null' 'srv.max_stratum=null'
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    verdict="$(printf '%s' "$out" | grep -A3 VERDICT | head -4)"
    assert_contains "$verdict" "No time source is reachable"
    # The offset must still be reported, just not as the verdict.
    assert_contains "$out" "4m 12s"
    assert_not_contains "$verdict" "4m 12s"
}

t_an_unreadable_config_is_not_a_box_without_sources() {
    # None and zero are different findings with different fixes, and the
    # first convention in the docs is that they must not be conflated.
    skew_facts_json "$TEST_TMPDIR/none.json" 'cfg.servers=null' \
        'srv.count=null' 'srv.list=[]' 'srv.all={}' 'srv.dead=null' \
        'srv.alive=null' 'srv.unusable=null' 'clock.offset_s=null' \
        'clock.spread_s=null' 'srv.max_stratum=null'
    out="$(sk --from-facts "$TEST_TMPDIR/none.json" --all)"
    assert_contains "$out" "skipped"
    assert_not_contains "$out" "NO_SYNC_CONFIGURED"

    skew_facts_json "$TEST_TMPDIR/zero.json" 'cfg.servers=[]' \
        srv.count=0 'srv.list=[]' 'srv.all={}' 'srv.dead=[]' \
        'srv.alive=[]' 'srv.unusable=[]' 'clock.offset_s=null' \
        'clock.spread_s=null' 'srv.max_stratum=null'
    out="$(sk --from-facts "$TEST_TMPDIR/zero.json" --csv)"
    assert_contains "$out" "NO_SYNC_CONFIGURED,CRITICAL"
}

t_offset_thresholds_fire_only_past_the_line() {
    # Either side of --warn-offset and --max-offset, since an off-by-one
    # in a threshold is invisible by eye.
    skew_facts_json "$TEST_TMPDIR/under.json" clock.offset_s=0.99
    skew_facts_json "$TEST_TMPDIR/warn.json" clock.offset_s=1.0
    skew_facts_json "$TEST_TMPDIR/crit.json" clock.offset_s=300.0
    assert_not_contains "$(sk --from-facts "$TEST_TMPDIR/under.json" --csv)" \
        "CLOCK_OFFSET"
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/warn.json" --csv)" \
        "CLOCK_OFFSET,WARN"
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/crit.json" --csv)" \
        "CLOCK_OFFSET,CRITICAL"
}

t_thresholds_are_movable() {
    skew_facts_json "$TEST_TMPDIR/f.json" clock.offset_s=5.0
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/f.json" --csv)" \
        "CLOCK_OFFSET,WARN"
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/f.json" --csv \
        --max-offset 2)" "CLOCK_OFFSET,CRITICAL"
    assert_not_contains "$(sk --from-facts "$TEST_TMPDIR/f.json" --csv \
        --warn-offset 10)" "CLOCK_OFFSET"
}

t_a_contradictory_threshold_pair_is_refused() {
    set +e
    out="$(sk --warn-offset 500 --max-offset 10 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--warn-offset"
}

t_which_way_round_the_error_is() {
    # Getting this backwards sends someone to the wrong end of the
    # problem, so both directions are asserted.
    skew_facts_json "$TEST_TMPDIR/behind.json" clock.offset_s=120.0
    skew_facts_json "$TEST_TMPDIR/fast.json" clock.offset_s=-120.0
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/behind.json")" "behind"
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/fast.json")" "fast"
}

t_a_source_that_answers_can_still_be_useless() {
    # Stratum 16 answers every reachability check and provides no time.
    skew_facts_json "$TEST_TMPDIR/f.json" \
        'srv.unusable=["10.0.0.3"]' srv.max_stratum=16
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "unsynchronised"
    # STRATUM_HIGH must stand down: 16 means unsynchronised, not distant,
    # and reporting both would be reporting the same thing twice.
    assert_not_contains "$(sk --from-facts "$TEST_TMPDIR/f.json" --csv)" \
        "STRATUM_HIGH"
}

t_disagreeing_sources_are_a_finding() {
    skew_facts_json "$TEST_TMPDIR/f.json" clock.spread_s=4.5
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "sources disagree"
    assert_contains "$out" "cannot all be right"
}

t_one_dead_source_among_several_is_only_a_warning() {
    skew_facts_json "$TEST_TMPDIR/f.json" 'srv.dead=["10.0.0.2"]' \
        'srv.alive=["10.0.0.1","10.0.0.3"]'
    out="$(sk --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "SOURCE_DEAD,WARN"
    assert_not_contains "$out" "NO_SYNC_SOURCE"
}

t_a_whole_hour_of_rtc_delta_is_not_drift() {
    # An RTC kept in local time is normal and is not a dying battery.
    skew_facts_json "$TEST_TMPDIR/tz.json" rtc.delta_s=3600.0
    skew_facts_json "$TEST_TMPDIR/drift.json" rtc.delta_s=412.0
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/tz.json")" "local time"
    assert_contains "$(sk --from-facts "$TEST_TMPDIR/drift.json")" "hwclock"
}

t_a_single_source_cannot_be_checked() {
    skew_facts_json "$TEST_TMPDIR/f.json" srv.count=1 \
        'cfg.servers=["10.0.0.1"]' 'srv.list=["10.0.0.1"]' \
        'srv.alive=["10.0.0.1"]' 'clock.spread_s=null'
    out="$(sk --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "SINGLE_SOURCE,INFO"
}

t_configured_but_nothing_running_to_use_it() {
    skew_facts_json "$TEST_TMPDIR/f.json" sync.daemon_running=false \
        'sync.daemon=null'
    out="$(sk --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "NO_SYNC_DAEMON,CRITICAL"
}

t_sync_switched_off_is_its_own_finding() {
    skew_facts_json "$TEST_TMPDIR/f.json" sync.ntp_enabled=false
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "switched off"
    assert_contains "$out" "set-ntp true"
}

t_a_daemon_disagreeing_with_a_good_clock_is_worth_seeing() {
    skew_facts_json "$TEST_TMPDIR/f.json" sync.synchronized=false
    out="$(sk --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "not synchronised"
    assert_contains "$out" "has not finished selecting"
}

t_missing_facts_skip_with_a_reason() {
    skew_facts_json "$TEST_TMPDIR/f.json" 'rtc.delta_s=null' \
        'sync.synchronized=null'
    out="$(sk --from-facts "$TEST_TMPDIR/f.json" --all)"
    assert_contains "$out" "hardware clock"
    assert_contains "$out" "timedatectl not available"
}

t_an_empty_fact_file_is_not_a_clean_bill_of_health() {
    printf '{}' > "$TEST_TMPDIR/empty.json"
    out="$(sk --from-facts "$TEST_TMPDIR/empty.json")"
    assert_contains "$out" "empty report"
}

t_a_foreign_fact_file_is_refused() {
    printf '[1, 2, 3]' > "$TEST_TMPDIR/list.json"
    set +e
    out="$(sk --from-facts "$TEST_TMPDIR/list.json" 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "fact dictionary"
}

t_csv_header_is_stable() {
    # Other tools merge against this, so the header is an interface.
    skew_facts_json "$TEST_TMPDIR/f.json"
    out="$(sk --from-facts "$TEST_TMPDIR/f.json" --csv | head -1)"
    assert_eq "$out" "host,ts,rule_id,severity,title,detail,fix"
}

t_exit_codes_avoid_usage_collision() {
    skew_facts_json "$TEST_TMPDIR/ok.json"
    skew_facts_json "$TEST_TMPDIR/warn.json" clock.offset_s=4.0
    skew_facts_json "$TEST_TMPDIR/crit.json" clock.offset_s=900.0
    set +e
    sk --from-facts "$TEST_TMPDIR/ok.json" --exit-code >/dev/null; ok=$?
    sk --from-facts "$TEST_TMPDIR/warn.json" --exit-code >/dev/null; warn=$?
    sk --from-facts "$TEST_TMPDIR/crit.json" --exit-code >/dev/null; crit=$?
    set -e
    assert_status $ok 0
    assert_status $warn 10
    assert_status $crit 20
}

t_rules_and_explain_are_generated() {
    out="$(sk --rules)"
    assert_contains "$out" "CLOCK_OFFSET"
    assert_contains "$out" "NO_SYNC_SOURCE"
    assert_contains "$(sk --explain clock_offset)" "Kerberos"
    set +e
    out="$(sk --explain NOPE 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such rule"
}

# --- the wire, against a real responder ------------------------------------

t_a_healthy_source_measures_near_zero() {
    port="$(free_port)"
    start_fake_ntp "$port" '{"offset": 0}'
    out="$(sk --server "127.0.0.1:$port" --samples 1 --json)"
    offset="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["facts"]["clock.offset_s"])' /dev/stdin <<<"$out")"
    # Loopback against our own clock: the offset is the measurement error,
    # which is microseconds.  A tenth of a second is a generous ceiling
    # that still fails if the arithmetic is wrong.
    "$PY" -c "import sys; sys.exit(0 if abs(float(sys.argv[1])) < 0.1 else 1)" \
        "$offset" || _fail "loopback offset was $offset, expected near zero"
}

t_a_wrong_source_is_measured_as_wrong() {
    port="$(free_port)"
    start_fake_ntp "$port" '{"offset": 600}'
    out="$(sk --server "127.0.0.1:$port" --samples 1)"
    assert_contains "$out" "CRITICAL"
    assert_contains "$out" "behind"
    # 600s claimed by the source means this box is 10 minutes behind it.
    assert_contains "$out" "10m"
}

t_a_reply_that_does_not_echo_the_query_is_not_an_answer() {
    # A stale datagram, or an off-path forgery.  Either way it is not this
    # query's reply and must not become the offset.
    port="$(free_port)"
    start_fake_ntp "$port" '{"offset": 600, "bad_origin": true}'
    out="$(sk --server "127.0.0.1:$port" --samples 1 --timeout 1 --attempts 1)"
    assert_contains "$out" "all 1 configured source unreachable"
    assert_not_contains "$out" "10m"
}

t_a_silent_source_is_unreachable_not_zero() {
    port="$(free_port)"
    start_fake_ntp "$port" '{"drop": true}'
    out="$(sk --server "127.0.0.1:$port" --samples 1 --timeout 1 --attempts 1)"
    assert_contains "$out" "no answer"
    # Blank means not measured: an unreachable source must never
    # contribute a zero offset that flatters the average.
    assert_not_contains "$out" "0.0s"
}

t_an_unsynchronised_source_answers_and_is_not_a_source() {
    port="$(free_port)"
    start_fake_ntp "$port" '{"offset": 0, "stratum": 16, "leap": 3}'
    out="$(sk --server "127.0.0.1:$port" --samples 1)"
    assert_contains "$out" "unsynchronised"
}

t_a_bad_server_spec_is_refused_by_name() {
    set +e
    out="$(sk --server '10.0.0.1:notaport' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "not a port"
}

# --- configuration ---------------------------------------------------------

t_sources_are_read_from_whichever_config_exists() {
    printf 'pool 2.debian.pool.ntp.org iburst\nserver 10.0.0.9\n# server x\n' \
        > "$TEST_TMPDIR/chrony.conf"
    out="$(sk --chrony-conf "$TEST_TMPDIR/chrony.conf" --facts)"
    assert_contains "$out" "2.debian.pool.ntp.org"
    assert_contains "$out" "10.0.0.9"
    # The commented-out one must not be picked up.
    assert_not_contains "$out" '"x"'
}

t_timesyncd_keeps_its_sources_somewhere_else_entirely() {
    printf '[Time]\nNTP=a.example b.example\nFallbackNTP=c.example\n' \
        > "$TEST_TMPDIR/timesyncd.conf"
    out="$(sk --chrony-conf /nonexistent --ntp-conf /nonexistent \
             --timesyncd-conf "$TEST_TMPDIR/timesyncd.conf" --facts)"
    assert_contains "$out" "a.example"
    assert_contains "$out" "c.example"
}

t_the_daemon_is_found_by_reading_not_by_exec() {
    # comm is truncated to 15 bytes, so the systemd one has to be matched
    # short or a box running it reports having no time daemon at all.
    mkdir -p "$TEST_TMPDIR/proc/941"
    printf 'systemd-timesyn\n' > "$TEST_TMPDIR/proc/941/comm"
    out="$(sk --proc-root "$TEST_TMPDIR/proc" --no-exec --facts)"
    assert_contains "$out" "systemd-timesyncd"
}

t_the_hardware_clock_is_read_from_where_it_lives() {
    printf '%d\n' "$(( $(date +%s) + 4000 ))" > "$TEST_TMPDIR/rtc"
    out="$(sk --rtc-path "$TEST_TMPDIR/rtc" --no-exec \
             --chrony-conf /nonexistent --ntp-conf /nonexistent \
             --timesyncd-conf /nonexistent)"
    assert_contains "$out" "hardware clock"
}

echo "skew"
run_test "a correct clock says so"              t_a_correct_clock_says_so
run_test "no source outranks the drift"         t_no_source_outranks_the_drift_it_causes
run_test "unreadable config is not no sources"  t_an_unreadable_config_is_not_a_box_without_sources
run_test "offset thresholds fire past the line" t_offset_thresholds_fire_only_past_the_line
run_test "thresholds are movable"               t_thresholds_are_movable
run_test "contradictory thresholds refused"     t_a_contradictory_threshold_pair_is_refused
run_test "which way round the error is"         t_which_way_round_the_error_is
run_test "an answering source can be useless"   t_a_source_that_answers_can_still_be_useless
run_test "disagreeing sources are a finding"    t_disagreeing_sources_are_a_finding
run_test "one dead source is only a warning"    t_one_dead_source_among_several_is_only_a_warning
run_test "a whole hour of rtc is not drift"     t_a_whole_hour_of_rtc_delta_is_not_drift
run_test "a single source cannot be checked"    t_a_single_source_cannot_be_checked
run_test "configured with nothing running"      t_configured_but_nothing_running_to_use_it
run_test "sync switched off is its own finding" t_sync_switched_off_is_its_own_finding
run_test "daemon disagreeing with a good clock" t_a_daemon_disagreeing_with_a_good_clock_is_worth_seeing
run_test "missing facts skip with a reason"     t_missing_facts_skip_with_a_reason
run_test "an empty fact file is not clean"      t_an_empty_fact_file_is_not_a_clean_bill_of_health
run_test "a foreign fact file is refused"       t_a_foreign_fact_file_is_refused
run_test "csv header is stable"                 t_csv_header_is_stable
run_test "exit codes avoid usage collision"     t_exit_codes_avoid_usage_collision
run_test "--rules and --explain generated"      t_rules_and_explain_are_generated
run_test "a healthy source measures near zero"  t_a_healthy_source_measures_near_zero
run_test "a wrong source is measured as wrong"  t_a_wrong_source_is_measured_as_wrong
run_test "a reply not echoing the query"        t_a_reply_that_does_not_echo_the_query_is_not_an_answer
run_test "a silent source is not a zero"        t_a_silent_source_is_unreachable_not_zero
run_test "an unsynchronised source answers"     t_an_unsynchronised_source_answers_and_is_not_a_source
run_test "a bad server spec is refused"         t_a_bad_server_spec_is_refused_by_name
run_test "sources read from whichever config"   t_sources_are_read_from_whichever_config_exists
run_test "timesyncd keeps sources elsewhere"    t_timesyncd_keeps_its_sources_somewhere_else_entirely
run_test "the daemon is found by reading"       t_the_daemon_is_found_by_reading_not_by_exec
run_test "the hardware clock is read"           t_the_hardware_clock_is_read_from_where_it_lives
finish
