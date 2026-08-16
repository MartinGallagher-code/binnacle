#!/usr/bin/env bash
# resolve: the rules engine from fact files, the wire code against a real
# responder on loopback.
#
# The rules are pure functions of the fact dict, so --from-facts drives the
# whole diagnostic surface with nothing resolving.  The packet code cannot
# be tested that way, so a small UDP responder is started on 127.0.0.1 and
# queried for real -- it builds its replies independently of the tool, which
# is the point of writing it twice.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
RS="$BINNACLE_DIR/resolve.py"

rs() { "$PY" "$RS" "$@"; }

t_healthy_dns_says_so() {
    resolve_facts_json "$TEST_TMPDIR/f.json"
    out="$(rs --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "DNS is not the problem here"
    assert_contains "$out" "Look at the box and the network next"
}

t_dead_first_resolver_outranks_the_slowness_it_causes() {
    # The whole reason this tool exists: everything still resolves, so every
    # single-server query says DNS is fine, and every lookup pays a timeout.
    resolve_facts_json "$TEST_TMPDIR/f.json" \
        'srv.dead=["10.0.0.53"]' 'srv.alive=["10.0.0.54"]' \
        srv.first_dead=true srv.fastest_ms=1.9 srv.slowest_ms=1.9 \
        srv.slowest=10.0.0.54 stub.slowest_ms=5002.0
    out="$(rs --from-facts "$TEST_TMPDIR/f.json")"
    # The verdict is wrapped, so assert on a fragment that cannot straddle
    # a line break rather than on a whole phrase.
    assert_contains "$out" "covering for"
    # The stub slowness must still be reported, just not as the verdict.
    assert_contains "$out" "getaddrinfo"
    verdict="$(printf '%s' "$out" | grep -A2 VERDICT | head -3)"
    assert_not_contains "$verdict" "getaddrinfo"
}

t_a_dead_first_server_is_worse_than_a_dead_second() {
    resolve_facts_json "$TEST_TMPDIR/first.json" \
        'srv.dead=["10.0.0.53"]' srv.first_dead=true
    resolve_facts_json "$TEST_TMPDIR/second.json" \
        'srv.dead=["10.0.0.54"]' srv.first_dead=false
    first="$(rs --from-facts "$TEST_TMPDIR/first.json" --csv)"
    second="$(rs --from-facts "$TEST_TMPDIR/second.json" --csv)"
    assert_contains "$first" "NS_DEAD,CRITICAL"
    assert_contains "$second" "NS_DEAD,WARN"
}

t_disagreement_names_both_answers() {
    resolve_facts_json "$TEST_TMPDIR/f.json" \
        'names.disagree=["db01.example.com"]' \
        'names.all={"db01.example.com": {"answers": {"10.0.0.53": ["10.0.0.7"], "10.0.0.54": ["192.0.2.9"]}, "hosts": null, "by_server": {}, "distinct": 2}}'
    out="$(rs --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "10.0.0.7"
    assert_contains "$out" "192.0.2.9"
    assert_contains "$out" "horizon"
}

t_all_dead_is_not_reported_as_some_dead() {
    resolve_facts_json "$TEST_TMPDIR/f.json" \
        'srv.dead=["10.0.0.53","10.0.0.54"]' 'srv.alive=[]' \
        srv.first_dead=true srv.slowest_ms=null srv.fastest_ms=null
    out="$(rs --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$out" "NS_ALL_DEAD"
    assert_not_contains "$out" "NS_DEAD,"
}

t_thresholds_are_boundaries() {
    resolve_facts_json "$TEST_TMPDIR/lo.json" srv.slowest_ms=99
    resolve_facts_json "$TEST_TMPDIR/hi.json" srv.slowest_ms=101
    lo="$(rs --from-facts "$TEST_TMPDIR/lo.json" --csv)"
    hi="$(rs --from-facts "$TEST_TMPDIR/hi.json" --csv)"
    assert_not_contains "$lo" "NS_SLOW"
    assert_contains "$hi" "NS_SLOW"
}

t_a_stub_resolver_is_declared_as_one() {
    resolve_facts_json "$TEST_TMPDIR/f.json" \
        'res.stub={"127.0.0.53": "systemd-resolved"}' \
        'res.upstreams=["10.0.0.53"]'
    out="$(rs --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "upstreams behind it were not measured"
    assert_contains "$out" "--server 10.0.0.53"
}

t_hosts_file_shadowing_is_reported() {
    resolve_facts_json "$TEST_TMPDIR/f.json" \
        'names.hosts_shadow=["db01.example.com"]' \
        'names.all={"db01.example.com": {"answers": {"10.0.0.53": ["10.0.0.7"]}, "hosts": ["192.168.9.9"], "by_server": {}, "distinct": 1}}'
    out="$(rs --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "192.168.9.9"
    assert_contains "$out" "hosts file wins"
}

t_missing_facts_are_skipped_with_a_reason() {
    resolve_facts_json "$TEST_TMPDIR/f.json" names.disagree=null \
        search.wasted=null
    out="$(rs --from-facts "$TEST_TMPDIR/f.json" --all)"
    assert_contains "$out" "skipped"
    assert_contains "$out" "pass a name your application"
}

t_csv_header_matches_why_slow() {
    # Same header as why-slow so `agree --merge-csv` can stack them.
    resolve_facts_json "$TEST_TMPDIR/f.json" srv.slowest_ms=800
    out="$(rs --from-facts "$TEST_TMPDIR/f.json" --csv | head -1)"
    assert_eq "$out" "host,ts,rule_id,severity,title,detail,fix"
}

t_exit_codes_are_not_confusable_with_usage() {
    resolve_facts_json "$TEST_TMPDIR/ok.json"
    resolve_facts_json "$TEST_TMPDIR/crit.json" 'srv.dead=["10.0.0.53","10.0.0.54"]'
    resolve_facts_json "$TEST_TMPDIR/warn.json" srv.slowest_ms=300
    rs --from-facts "$TEST_TMPDIR/ok.json" --exit-code >/dev/null
    assert_status $? 0
    set +e
    rs --from-facts "$TEST_TMPDIR/crit.json" --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 20
    set +e
    rs --from-facts "$TEST_TMPDIR/warn.json" --exit-code >/dev/null; rc=$?
    set -e
    assert_status $rc 10
    set +e
    rs --from-facts /nonexistent-facts.json >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_rules_and_explain_are_generated() {
    out="$(rs --rules)"
    assert_contains "$out" "NS_DISAGREE"
    assert_contains "$out" "needs:"
    one="$(rs --explain NS_DEAD)"
    assert_contains "$one" "in order"
    set +e
    rs --explain NOT_A_RULE >/dev/null 2>&1; rc=$?
    set -e
    assert_status $rc 2
}

t_resolv_conf_is_parsed_in_order() {
    printf '# managed by nothing\nnameserver 10.0.0.53\nnameserver 10.0.0.54\nsearch a.example.com b.example.com\noptions ndots:5 timeout:3 attempts:4 rotate\n' \
        > "$TEST_TMPDIR/resolv.conf"
    out="$(rs --resolv-conf "$TEST_TMPDIR/resolv.conf" --timeout 0.2 \
             --no-stub --facts)"
    assert_contains "$out" '"res.ndots": 5'
    assert_contains "$out" '"res.timeout": 3'
    assert_contains "$out" '"res.attempts": 4'
    assert_contains "$out" '"res.rotate": true'
    # Order is load-bearing: the first entry is the one that costs a
    # timeout on every lookup when it is dead.
    first="$(printf '%s' "$out" | "$PY" -c \
        'import json,sys; print(json.load(sys.stdin)["res.nameservers"][0])')"
    assert_eq "$first" "10.0.0.53"
    # 24 x timeout:3 x attempts:4 is what an application waits.
    assert_contains "$out" '"budget.worst_s": 24'
}

t_a_domain_line_is_a_one_entry_search_list() {
    printf 'nameserver 10.0.0.53\ndomain corp.example.com\n' \
        > "$TEST_TMPDIR/resolv.conf"
    out="$(rs --resolv-conf "$TEST_TMPDIR/resolv.conf" --timeout 0.2 \
             --no-stub --facts)"
    assert_contains "$out" '"corp.example.com"'
}

t_answers_a_real_query_on_loopback() {
    port="$(free_port)"
    start_fake_dns "$port" \
        '{"records": {"db01.example.com": {"A": ["10.0.0.7"], "AAAA": ["2001:db8::7"]}}}'
    out="$(rs --server "127.0.0.1:$port" --no-stub --timeout 2 \
             db01.example.com)"
    assert_contains "$out" "10.0.0.7"
    assert_contains "$out" "2001:db8::7"
    assert_contains "$out" "DNS is not the problem"
}

t_a_silent_server_is_dead_not_slow() {
    port="$(free_port)"
    start_fake_dns "$port" '{"records": {}}'
    # Point at a port nothing is on: no listener, so nothing ever answers.
    dead="$(free_port)"
    out="$(rs --server "127.0.0.1:$dead" --no-stub --timeout 0.3 --csv)"
    assert_contains "$out" "NS_ALL_DEAD"
}

t_two_servers_disagreeing_is_caught_on_the_wire() {
    p1="$(free_port)"
    p2="$(free_port)"
    start_fake_dns "$p1" '{"records": {"db01.example.com": {"A": ["10.0.0.7"]}}}'
    start_fake_dns "$p2" '{"records": {"db01.example.com": {"A": ["192.0.2.9"]}}}'
    out="$(rs --server "127.0.0.1:$p1" --server "127.0.0.1:$p2" \
             --no-stub --type A --timeout 2 db01.example.com --csv)"
    assert_contains "$out" "NS_DISAGREE,CRITICAL"
}

t_record_order_is_not_disagreement() {
    # A resolver rotating its records round-robin is healthy, and reporting
    # it as a disagreement would bury the real ones.
    p1="$(free_port)"
    p2="$(free_port)"
    start_fake_dns "$p1" \
        '{"records": {"web.example.com": {"A": ["10.0.0.1", "10.0.0.2"]}}}'
    start_fake_dns "$p2" \
        '{"records": {"web.example.com": {"A": ["10.0.0.2", "10.0.0.1"]}}}'
    out="$(rs --server "127.0.0.1:$p1" --server "127.0.0.1:$p2" \
             --no-stub --type A --timeout 2 web.example.com --csv)"
    assert_not_contains "$out" "NS_DISAGREE"
}

t_a_dropped_aaaa_is_a_black_hole_not_a_missing_record() {
    port="$(free_port)"
    start_fake_dns "$port" \
        '{"records": {"db01.example.com": {"A": ["10.0.0.7"]}}, "drop": ["AAAA"]}'
    out="$(rs --server "127.0.0.1:$port" --no-stub --timeout 0.3 \
             db01.example.com --csv)"
    assert_contains "$out" "AAAA_STALL"
}

t_truncation_with_no_tcp_is_reported() {
    port="$(free_port)"
    start_fake_dns "$port" \
        '{"records": {"big.example.com": {"A": ["10.0.0.7"]}}, "truncate": ["big.example.com"], "tcp": false}'
    out="$(rs --server "127.0.0.1:$port" --no-stub --type A --timeout 0.3 \
             big.example.com --csv)"
    assert_contains "$out" "TCP53_BLOCKED"
}

t_search_domains_cost_is_measured_not_assumed() {
    port="$(free_port)"
    start_fake_dns "$port" \
        '{"records": {"db01.c.example.com": {"A": ["10.0.0.7"]}}, "nxdomain": ["db01.a.example.com", "db01.b.example.com"]}'
    printf 'nameserver 127.0.0.1\nsearch a.example.com b.example.com c.example.com\n' \
        > "$TEST_TMPDIR/resolv.conf"
    out="$(rs --server "127.0.0.1:$port" --resolv-conf "$TEST_TMPDIR/resolv.conf" \
             --no-stub --type A --timeout 2 db01)"
    assert_contains "$out" "2 wasted queries"
}

echo "resolve"
run_test "healthy dns says so"                 t_healthy_dns_says_so
run_test "dead resolver outranks its slowness" t_dead_first_resolver_outranks_the_slowness_it_causes
run_test "first dead server is worse"          t_a_dead_first_server_is_worse_than_a_dead_second
run_test "disagreement names both answers"     t_disagreement_names_both_answers
run_test "all dead is its own finding"         t_all_dead_is_not_reported_as_some_dead
run_test "thresholds fire only past the line"  t_thresholds_are_boundaries
run_test "a stub resolver is declared"         t_a_stub_resolver_is_declared_as_one
run_test "hosts file shadowing is reported"    t_hosts_file_shadowing_is_reported
run_test "missing facts skip with a reason"    t_missing_facts_are_skipped_with_a_reason
run_test "csv header matches why-slow"         t_csv_header_matches_why_slow
run_test "exit codes avoid usage collision"    t_exit_codes_are_not_confusable_with_usage
run_test "--rules and --explain generated"     t_rules_and_explain_are_generated
run_test "resolv.conf parsed in order"         t_resolv_conf_is_parsed_in_order
run_test "domain line is a search list"        t_a_domain_line_is_a_one_entry_search_list
run_test "answers a real query on loopback"    t_answers_a_real_query_on_loopback
run_test "a silent server is dead, not slow"   t_a_silent_server_is_dead_not_slow
run_test "disagreement caught on the wire"     t_two_servers_disagreeing_is_caught_on_the_wire
run_test "record order is not disagreement"    t_record_order_is_not_disagreement
run_test "a dropped AAAA is a black hole"      t_a_dropped_aaaa_is_a_black_hole_not_a_missing_record
run_test "truncation with no tcp reported"     t_truncation_with_no_tcp_is_reported
run_test "search cost is measured"             t_search_domains_cost_is_measured_not_assumed
finish
