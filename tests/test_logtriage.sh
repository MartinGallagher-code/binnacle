#!/usr/bin/env bash
# logtriage: masking order, prefix parsing, ranking, multi-line, eviction.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
LT="$BINNACLE_DIR/logtriage.py"

lt() { "$PY" "$LT" "$@"; }

t_mask_order_uuid_before_hex() {
    # A UUID must be caught before the hex rule would eat its pieces.
    out="$(lt --mask 'req 550e8400-e29b-41d4-a716-446655440000 done' | head -1)"
    assert_contains "$out" "<UUID>"
    assert_not_contains "$out" "<HEX>"
}

t_mask_timestamps_before_numbers() {
    out="$(lt --mask '2026-01-07T12:34:56Z started' | head -1)"
    assert_contains "$out" "<TS>"
    assert_not_contains "$out" "<NUM>-<NUM>"
}

t_mask_ip_and_path_and_quoted() {
    out="$(lt --mask 'conn from 10.0.0.5 file /var/log/app.log said "hello there"' | head -1)"
    assert_contains "$out" "<IP>"
    assert_contains "$out" "<PATH>"
    assert_contains "$out" "<STR>"
}

t_keep_numbers_disables_that_mask() {
    out="$(lt --mask 'retry 42 times' --keep-numbers | head -1)"
    assert_contains "$out" "42"
}

t_user_rule_wins_over_builtins() {
    out="$(lt --mask 'tenant acct-99887 failed' --rule 'acct-\d+=>ACCT' | head -1)"
    assert_contains "$out" "<ACCT>"
}

t_same_shape_same_id_across_runs() {
    # Determinism is what lets a fleet be compared at all.
    a="$(lt --mask 'user bob from 10.0.0.1' | tail -1)"
    b="$(lt --mask 'user bob from 10.9.9.9' | tail -1)"
    assert_eq "$a" "$b"
}

t_parse_each_prefix_format() {
    out="$(lt --parse 'Jan  7 12:34:56 web01 sshd[1234]: Failed password')"
    assert_contains "$out" "format:  syslog"
    assert_contains "$out" "program: sshd"
    assert_contains "$out" "pid:     1234"

    out="$(lt --parse '[  123.456789] EXT4-fs error')"
    assert_contains "$out" "format:  dmesg"
    assert_contains "$out" "program: kernel"

    out="$(lt --parse '{"time":"2026-01-07T12:00:00Z","level":"error","msg":"boom"}')"
    assert_contains "$out" "format:  json"
    assert_contains "$out" "ERROR boom"

    out="$(lt --parse 'just some text with no prefix')"
    assert_contains "$out" "format:  bare"
}

t_novel_beats_frequent() {
    # The planted structure: 3000 steady auth failures, 1200 novel kernel
    # errors, 3 novel OOM kills.  Frequency must NOT win.
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --csv)"
    first_sev="$(printf '%s' "$out" | sed -n '2p' | cut -d, -f7)"
    first_count="$(printf '%s' "$out" | sed -n '2p' | cut -d, -f3)"
    assert_eq "$first_sev" "crit"
    assert_eq "$first_count" "3"
    # And the 3000-count steady template must be ranked below the novel ones.
    rank_steady="$(printf '%s' "$out" | grep 'Failed password' | cut -d, -f2)"
    "$PY" -c "import sys; sys.exit(0 if int('$rank_steady') > 2 else 1)" \
        || _fail "steady 3000-count template ranked at $rank_steady, expected > 2"
}

t_counts_are_exact() {
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --csv)"
    ext4="$(printf '%s' "$out" | grep 'EXT4-fs error' | cut -d, -f3)"
    assert_eq "$ext4" "1200"
    auth="$(printf '%s' "$out" | grep 'Failed password' | cut -d, -f3)"
    assert_eq "$auth" "3000"
}

t_tracebacks_collapse_to_one_template() {
    # 40 tracebacks of 4 lines each must be 40 counts of ONE template, not
    # 160 orphan lines each looking novel.
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --csv)"
    n="$(printf '%s' "$out" | grep -c 'NullPointerException')"
    assert_eq "$n" "1"
    count="$(printf '%s' "$out" | grep 'NullPointerException' | cut -d, -f3)"
    assert_eq "$count" "40"
    assert_not_contains "$out" "com.example.Foo.bar,"
}

t_multiline_off_shatters_it() {
    # The negative control: with --multiline off the frames become their own
    # templates, which is exactly the ranking damage the default prevents.
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --multiline off --csv)"
    assert_contains "$out" "at com.example.Foo.bar"
}

t_split_at_sharpens_novelty() {
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --split-at 11:00 --top 3)"
    assert_contains "$out" "baseline: everything before 11:00"
    assert_contains "$out" "NEW"
}

t_baseline_file_marks_known_templates() {
    gen_log "$TEST_TMPDIR/test.log"
    lt "$TEST_TMPDIR/test.log" --save-templates "$TEST_TMPDIR/digest.csv" \
        >/dev/null
    assert_file_exists "$TEST_TMPDIR/digest.csv"
    # Compared against itself, nothing is novel any more.
    out="$(lt "$TEST_TMPDIR/test.log" \
             --baseline-templates "$TEST_TMPDIR/digest.csv")"
    assert_contains "$out" "Nothing here is new relative to the baseline"
}

t_eviction_is_reported_not_silent() {
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt "$TEST_TMPDIR/test.log" --max-templates 2)"
    assert_contains "$out" "evicted into <OTHER>"
    assert_contains "$out" "raise --max-templates"
}

t_reads_stdin_and_gzip() {
    gen_log "$TEST_TMPDIR/test.log"
    out="$(lt - --csv < "$TEST_TMPDIR/test.log" | wc -l)"
    "$PY" -c "import sys; sys.exit(0 if int('$out') > 3 else 1)" \
        || _fail "stdin produced only $out csv lines"
    gzip -c "$TEST_TMPDIR/test.log" > "$TEST_TMPDIR/test.log.gz"
    out="$(lt "$TEST_TMPDIR/test.log.gz" --csv | wc -l)"
    "$PY" -c "import sys; sys.exit(0 if int('$out') > 3 else 1)" \
        || _fail "gzip produced only $out csv lines"
}

t_no_timestamps_falls_back_to_line_numbers() {
    printf 'something broke\nsomething else broke\nfine\nfine\n' \
        > "$TEST_TMPDIR/plain.log"
    out="$(lt "$TEST_TMPDIR/plain.log")"
    assert_contains "$out" "no timestamps"
    assert_contains "$out" "ordering by line number"
}

t_csv_swallowing_the_log_is_diagnosed() {
    # `logtriage --csv /var/log/syslog` reads stdin and finds nothing, which
    # under ssh or a pipeline is not a terminal, so the help-on-no-input
    # guard never fires.  The bare "no records" message sends people to look
    # at the log; this one names the actual mistake.
    gen_log "$TEST_TMPDIR/syslog"
    set +e
    out="$(lt --csv "$TEST_TMPDIR/syslog" < /dev/null 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "taken as the output path"
    assert_contains "$out" "The file comes first"
    # ...and the log it was pointed at is still a log.
    assert_contains "$(head -1 "$TEST_TMPDIR/syslog")" "sshd"
}

echo "logtriage"
run_test "--csv swallowing the log is caught" t_csv_swallowing_the_log_is_diagnosed
run_test "uuid masked before hex"             t_mask_order_uuid_before_hex
run_test "timestamps masked before numbers"   t_mask_timestamps_before_numbers
run_test "ip, path and quoted string masked"  t_mask_ip_and_path_and_quoted
run_test "--keep-numbers disables that mask"  t_keep_numbers_disables_that_mask
run_test "user --rule wins over built-ins"    t_user_rule_wins_over_builtins
run_test "same shape gives the same id"       t_same_shape_same_id_across_runs
run_test "every prefix format parses"         t_parse_each_prefix_format
run_test "novel+severe beats frequent"        t_novel_beats_frequent
run_test "counts are exact"                   t_counts_are_exact
run_test "tracebacks collapse to one"         t_tracebacks_collapse_to_one_template
run_test "multiline off shatters them"        t_multiline_off_shatters_it
run_test "--split-at sharpens novelty"        t_split_at_sharpens_novelty
run_test "saved digest works as a baseline"   t_baseline_file_marks_known_templates

t_new_years_eve_does_not_reverse_the_log() {
    # Syslog lines carry no year, and a log crosses New Year's Eve every
    # year it survives long enough.  Stamping every line with the file's
    # year put "Dec 31" eleven months into the future: the span printed
    # backwards, the midnight incident fell into the baseline, and a
    # routine heartbeat outranked it.
    {
        for m in 50 51 52 53 54 55 56 57 58 59; do
            echo "Dec 31 23:${m}:00 web01 app[1]: routine heartbeat ok"
        done
        echo "Jan  1 00:00:01 web01 app[1]: error: payment gateway unreachable"
        echo "Jan  1 00:00:02 web01 app[1]: error: payment gateway unreachable"
        echo "Jan  1 00:00:03 web01 app[1]: error: payment gateway unreachable"
    } > "$TEST_TMPDIR/newyear.log"
    # The mtime is the anchor: the file was last written just after
    # midnight, so its December lines belong to the year before.
    touch -d "2026-01-01 00:10" "$TEST_TMPDIR/newyear.log"

    out="$(lt "$TEST_TMPDIR/newyear.log")"
    # the span runs forward through midnight, not eleven months backwards
    assert_contains "$out" "23:50:00 -> 00:00:03"
    # and the incident outranks the heartbeat
    first_ranked="$(printf '%s\n' "$out" | grep -m1 '#1' -A 1 | tail -1)"
    assert_contains "$first_ranked" "payment gateway"

    # The absolute year matters too: relative order can be repaired while
    # every December line still sits in the wrong year.  The mtime is the
    # anchor, so December belongs to the year before it.
    dec_epoch="$(lt "$TEST_TMPDIR/newyear.log" --csv \
                   | awk -F, '/heartbeat/ {print $5}')"
    assert_eq "$(date -d "@$dec_epoch" +%Y)" "2025"
}

run_test "eviction is reported, not silent"   t_eviction_is_reported_not_silent
run_test "reads stdin and gzip"               t_reads_stdin_and_gzip
run_test "no timestamps degrades cleanly"     t_no_timestamps_falls_back_to_line_numbers
run_test "new year does not reverse the log"  t_new_years_eve_does_not_reverse_the_log
finish
