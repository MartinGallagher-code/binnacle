#!/usr/bin/env bash
# muster: the pool, leases, expiry, conflicts and concurrency.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
MU="$BINNACLE_DIR/muster.py"

mu() { "$PY" "$MU" "$@"; }

# The `done` verb is quoted throughout. It is a shell keyword, so the
# linter reads a bare one as a malformed loop (SC1010) and the CI lint
# does not tolerate warnings. Nothing is wrong with the command itself --
# bash only treats `done` specially where a loop is open -- so do not
# unquote these while tidying. (A comment here must not begin with the
# linter's own name either: that is read as a directive.)
items() { grep -v '^#' "$1"; }

t_a_malformed_duration_is_refused_not_a_traceback() {
    # "." and "1.2.3" are runs of digits and dots that are not numbers;
    # both reached float() and raised a bare ValueError at the user.
    cd "$TEST_TMPDIR"
    mu add 'a[1-2]' >/dev/null 2>&1
    for bad in . 1.2.3s; do
        set +e
        out="$(mu take 1 --lease "$bad" 2>&1)"; rc=$?
        set -e
        assert_status $rc 2
        assert_contains "$out" "bad duration"
        assert_not_contains "$out" "Traceback"
        assert_not_contains "$out" "ValueError"
    done
}

t_the_shared_flags_work_before_the_verb_too() {
    # `muster --pool p.csv status` is what people type. It used to fail
    # with argparse blaming the verb for an invalid choice.
    cd "$TEST_TMPDIR"
    mu add 'a[1-3]' --pool mine.csv >/dev/null 2>&1
    out="$(mu --pool mine.csv status)"
    assert_contains "$out" "3 item(s)"
    # And one given after the verb still wins over one given before.
    mu add 'b[1-5]' --pool other.csv >/dev/null 2>&1
    out="$(mu --pool mine.csv status --pool other.csv)"
    assert_contains "$out" "5 item(s)"
}

t_a_ticket_that_cannot_be_written_puts_the_items_back() {
    # The pool is written before the ticket, so a failed ticket left the
    # items leased to somebody with no record of which ones -- stuck for
    # the whole lease. A typo'd -o is enough to hit it.
    cd "$TEST_TMPDIR"
    mu add 'a[1-3]' >/dev/null 2>&1
    set +e
    out="$(mu take 2 -o /nonexistent-dir/t.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "put back"
    assert_eq "$(mu list --state held | tail -n +2 | wc -l | tr -d ' ')" "0"
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "3"
}

t_a_hash_inside_a_name_is_part_of_the_name() {
    # Splitting on a bare "#" turned file#1 and file#2 into two copies of
    # "file": one item where there were two, and the other never worked
    # on. A comment opens the line or follows whitespace.
    cd "$TEST_TMPDIR"
    printf 'file#1\nfile#2\nreal   # a comment\n# whole line\n' > in.txt
    mu add in.txt >/dev/null 2>&1
    body="$(mu list --state any)"
    assert_contains "$body" "file#1"
    assert_contains "$body" "file#2"
    assert_contains "$body" "real"
    assert_not_contains "$body" "a comment"
    assert_not_contains "$body" "whole line"
    assert_eq "$(mu list --state any | tail -n +2 | wc -l | tr -d ' ')" "3"
}

t_a_directory_is_not_a_list_of_items() {
    cd "$TEST_TMPDIR"
    mkdir -p somedir
    set +e
    out="$(mu add somedir 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "is a directory"
    assert_no_file "muster.csv"
}

t_a_held_row_with_no_deadline_is_not_a_lease() {
    # Nothing could ever free it, so the item was held forever -- and the
    # report counted down from the epoch: "next expires in -20692d23h".
    cd "$TEST_TMPDIR"
    printf 'item,state,holder,lease_id,taken_ts,expires_ts,done_ts,attempts,note\n' > p.csv
    printf 'x1,held,someone,abc,,,,1,\n' >> p.csv
    out="$(mu status --pool p.csv)"
    assert_not_contains "$out" "-20692d"
    assert_contains "$out" "RECLAIMED"
    assert_eq "$(mu list --pool p.csv --state available | tail -n +2 | wc -l | tr -d ' ')" "1"
}

t_two_rows_for_one_item_are_refused() {
    # add() cannot make these and writes are locked, so a duplicate means
    # a hand edit or a merge. The rows disagree about state, every count
    # is wrong, and only one would ever be updated again.
    cd "$TEST_TMPDIR"
    printf 'item,state,holder,lease_id,taken_ts,expires_ts,done_ts,attempts,note\n' > p.csv
    printf 'y1,available,,,,,,0,\ny1,done,me,,,,111,1,\n' >> p.csv
    set +e
    out="$(mu status --pool p.csv 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "twice"
    assert_contains "$out" "y1"
}

t_a_pool_error_does_not_strand_the_lock() {
    # The caller's finally is not in force until _open has returned, so a
    # pool that would not parse left its lock behind -- and every later
    # run then waited out --stale-lock. On a shared pool that is the
    # whole fleet stopped by one bad row.
    #
    # Triggered with a file that is not a pool at all, rather than with
    # the duplicate rows above: that refusal is itself a fix from this
    # same round, so using it here would have made this case pass
    # against the very code it is meant to catch.
    cd "$TEST_TMPDIR"
    printf 'web01\nweb02\n' > notapool.txt
    mu status --pool notapool.txt >/dev/null 2>&1 || true
    assert_no_file "notapool.txt.lock"
}

t_add_expands_ranges_and_ignores_duplicates() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-05]' >/dev/null 2>&1
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "5"
    # Adding the same range again must not double the pool.
    mu add 'web[01-05]' >/dev/null 2>&1
    assert_eq "$(mu list --state any | tail -n +2 | wc -l | tr -d ' ')" "5"
}

t_add_reads_a_file_and_stdin() {
    cd "$TEST_TMPDIR"
    printf 'alpha\nbravo   # a comment\n\ncharlie\n' > in.txt
    mu add in.txt >/dev/null 2>&1
    printf 'delta\n' | mu add - >/dev/null 2>&1
    body="$(mu list --state any)"
    for n in alpha bravo charlie delta; do assert_contains "$body" "$n"; done
    # The comment is stripped, not kept as part of the name.
    assert_not_contains "$body" "a comment"
}

t_a_mistyped_path_is_not_silently_an_item() {
    # `muster add ./hosts.tzt` must not add an item called "./hosts.tzt"
    # and report success -- that is found weeks later, if ever.
    cd "$TEST_TMPDIR"
    set +e
    out="$(mu add ./nope.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such file"
}

t_the_pool_is_a_csv_with_the_documented_header() {
    cd "$TEST_TMPDIR"
    mu add 'a[1-3]' >/dev/null 2>&1
    assert_eq "$(head -1 muster.csv)" \
        "item,state,holder,lease_id,taken_ts,expires_ts,done_ts,attempts,note"
}

t_take_writes_a_ticket_and_holds_the_items() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-10]' >/dev/null 2>&1
    mu take 4 -o mine.txt >/dev/null 2>&1
    assert_eq "$(items mine.txt | wc -l | tr -d ' ')" "4"
    assert_eq "$(mu list --state held | tail -n +2 | wc -l | tr -d ' ')" "4"
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "6"
    # The ticket is item-per-line for whatever does the work, with the
    # lease recorded in comments above it.
    assert_contains "$(cat mine.txt)" "# lease "
    assert_eq "$(items mine.txt | head -1)" "web01"
}

t_take_a_named_item() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-10]' >/dev/null 2>&1
    mu take --item web07 -o mine.txt >/dev/null 2>&1
    assert_eq "$(items mine.txt)" "web07"
}

t_taking_a_held_item_is_refused_not_stolen() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-05]' >/dev/null 2>&1
    mu take --item web02 --as first -o a.txt >/dev/null 2>&1
    set +e
    out="$(mu take --item web02 --as second -o b.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "held by first"
    assert_eq "$(items b.txt | wc -l | tr -d ' ')" "0"
}

t_done_completes_and_reports_the_percentage() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-10]' >/dev/null 2>&1
    mu take 4 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    assert_status $? 0
    out="$(mu status)"
    assert_contains "$out" "4 of 10 done (40%)"
}

t_a_percentage_never_rounds_into_a_lie() {
    # 399 of 400 is not 100%: that reads as finished when one is left.
    cd "$TEST_TMPDIR"
    mu add 'n[001-400]' >/dev/null 2>&1
    mu take 399 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    out="$(mu status)"
    assert_contains "$out" "399 of 400 done (>99%)"
    assert_not_contains "$out" "(100%)"
}

t_one_of_many_is_not_zero_percent() {
    cd "$TEST_TMPDIR"
    mu add 'n[001-400]' >/dev/null 2>&1
    mu take --item n001 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    assert_contains "$(mu status)" "1 of 400 done (<1%)"
}

t_all_done_really_is_100_percent() {
    cd "$TEST_TMPDIR"
    mu add 'web[1-4]' >/dev/null 2>&1
    mu take 4 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    out="$(mu status)"
    assert_contains "$out" "4 of 4 done (100%)"
    assert_contains "$out" "Every item is done"
}

t_status_csv_is_one_row_of_numbers() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-12]' >/dev/null 2>&1
    mu take 5 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    out="$(mu status --csv)"
    assert_eq "$(printf '%s\n' "$out" | head -1)" \
        "pool,total,done,held,available,done_pct,stuck"
    row="$(printf '%s\n' "$out" | tail -1)"
    assert_contains "$row" "muster.csv,12,5,0,7,41.67,0"
}

t_a_lease_that_runs_out_puts_the_item_back() {
    # Nothing runs to make this happen: an expired lease is simply not a
    # lease, worked out from the deadline as the pool is opened.
    cd "$TEST_TMPDIR"
    mu add 'web[01-04]' >/dev/null 2>&1
    mu take 2 --lease 1s --as gone -o gone.txt >/dev/null 2>&1
    assert_eq "$(mu list --state held | tail -n +2 | wc -l | tr -d ' ')" "2"
    sleep 2
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "4"
}

t_a_reclaimed_item_can_be_taken_by_someone_else() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 2 --lease 1s --as alice -o alice.txt >/dev/null 2>&1
    sleep 2
    out="$(mu take 2 --as bob -o bob.txt 2>&1 >/dev/null)"
    assert_contains "$out" "expired lease"
    assert_eq "$(items bob.txt | wc -l | tr -d ' ')" "2"
}

t_finishing_after_the_lease_lapsed_is_a_conflict() {
    # The chosen policy: the work did happen, so it counts -- but two
    # people on one host is what the lease existed to prevent, so it is
    # reported loudly rather than absorbed.
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 2 --lease 1s --as alice -o alice.txt >/dev/null 2>&1
    sleep 2
    mu take 2 --as bob -o bob.txt >/dev/null 2>&1

    set +e
    out="$(mu "done" alice.txt --as alice 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "CONFLICT"
    assert_contains "$out" "held by bob"
    assert_contains "$out" "their work is duplicate"
    assert_contains "$out" "longer --lease"
    # Accepted, not refused: the pool must not send it out a third time.
    assert_eq "$(mu list --state "done" | tail -n +2 | wc -l | tr -d ' ')" "2"
}

t_finishing_late_with_nobody_else_holding_is_quieter() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 2 --lease 1s --as alice -o alice.txt >/dev/null 2>&1
    sleep 2
    set +e
    out="$(mu "done" alice.txt --as alice 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "LATE"
    assert_contains "$out" "nothing was done twice"
    assert_not_contains "$out" "CONFLICT"
}

t_done_twice_is_a_finding_not_a_silent_no_op() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-03]' >/dev/null 2>&1
    mu take 3 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    set +e
    out="$(mu "done" mine.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "ALREADY"
}

t_release_gives_items_back_without_completing_them() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-05]' >/dev/null 2>&1
    mu take 3 -o mine.txt >/dev/null 2>&1
    mu release mine.txt >/dev/null 2>&1
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "5"
    assert_eq "$(mu list --state "done" | tail -n +2 | wc -l | tr -d ' ')" "0"
}

t_release_does_not_take_an_item_from_its_holder() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 2 --lease 1s --as alice -o alice.txt >/dev/null 2>&1
    sleep 2
    mu take 2 --as bob -o bob.txt >/dev/null 2>&1
    set +e
    out="$(mu release alice.txt --as alice 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "CONFLICT"
    assert_contains "$out" "left alone"
    assert_eq "$(mu list --state held | tail -n +2 | wc -l | tr -d ' ')" "2"
}

t_reset_undoes_a_completion() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-03]' >/dev/null 2>&1
    mu take 3 -o mine.txt >/dev/null 2>&1
    mu "done" mine.txt >/dev/null 2>&1
    mu reset --item web02 >/dev/null 2>&1
    assert_eq "$(mu list --state "done" | tail -n +2 | wc -l | tr -d ' ')" "2"
    assert_eq "$(mu list --state available | tail -n +2 | wc -l | tr -d ' ')" "1"
}

t_an_unknown_item_is_named_not_ignored() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    printf 'web01\nnot-in-the-pool\n' > hand.txt
    set +e
    out="$(mu "done" hand.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "UNKNOWN"
    assert_contains "$out" "not-in-the-pool"
}

t_a_hand_written_list_works_as_a_ticket() {
    # The ticket is item-per-line on purpose: a list somebody typed is
    # still a valid input, it only loses the conflict detection.
    cd "$TEST_TMPDIR"
    mu add 'web[01-04]' >/dev/null 2>&1
    mu take 4 -o real.txt >/dev/null 2>&1
    printf 'web01\nweb03\n' > hand.txt
    mu "done" hand.txt >/dev/null 2>&1
    assert_status $? 0
    assert_eq "$(mu list --state "done" | tail -n +2 | wc -l | tr -d ' ')" "2"
}

t_repeatedly_taken_and_never_finished_is_stuck() {
    # Not a scheduling problem: an item three workers picked up and none
    # closed is a host that cannot be worked on, and it would absorb the
    # pool forever unless somebody is told.
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    for i in 1 2 3; do
        mu take --item web01 --lease 1s --as "w$i" -o /dev/null >/dev/null 2>&1
        sleep 1.1
    done
    set +e
    out="$(mu status)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "STUCK"
    assert_contains "$out" "web01"
    assert_contains "$out" "3 attempts"
}

t_concurrent_takes_never_hand_out_the_same_item() {
    # The whole point of the lock. A pool shared over NFS is the stated
    # use, so this must hold with no coordination but the file itself.
    cd "$TEST_TMPDIR"
    mu add 'n[001-060]' >/dev/null 2>&1
    for i in $(seq 1 6); do
        ( mu take 10 --as "w$i" -o "t$i.txt" >/dev/null 2>&1 ) &
    done
    wait
    cat t*.txt | grep -v '^#' | sort > all.txt
    assert_eq "$(wc -l < all.txt | tr -d ' ')" "60"
    assert_eq "$(sort -u all.txt | wc -l | tr -d ' ')" "60"
    assert_eq "$(mu list --state held | tail -n +2 | wc -l | tr -d ' ')" "60"
}

t_the_lock_is_not_left_behind() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 1 -o mine.txt >/dev/null 2>&1
    assert_no_file "muster.csv.lock"
}

t_a_stale_lock_is_broken_not_waited_on_forever() {
    # A worker killed mid-write must not stop the fleet.
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    printf 'someone-who-died 00000000\n1\n' > muster.csv.lock
    touch -d '1970-01-01' muster.csv.lock 2>/dev/null || touch -t 197001010000 muster.csv.lock
    out="$(mu take 1 --stale-lock 5 -o mine.txt 2>&1 >/dev/null)"
    assert_contains "$out" "breaking a stale lock"
    assert_eq "$(items mine.txt | wc -l | tr -d ' ')" "1"
}

t_a_fresh_lock_is_waited_for_then_refused() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    printf 'someone-alive 00000000\n%s\n' "$(date +%s)" > muster.csv.lock
    set +e
    out="$(mu take 1 --lock-timeout 1 --stale-lock 3600 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "could not lock"
}

t_an_item_with_a_control_character_is_refused() {
    # An item is a line in a ticket file; a newline in one would come
    # back as two items with names nobody chose.
    cd "$TEST_TMPDIR"
    printf 'good\nbad\tone\n' > in.txt
    set +e
    out="$(mu add in.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "control character"
    assert_no_file "muster.csv"
}

t_taking_from_an_empty_pile_says_so() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    mu take 2 -o all.txt >/dev/null 2>&1
    out="$(mu take 5 -o more.txt 2>&1 >/dev/null)"
    assert_contains "$out" "no available items"
    assert_eq "$(items more.txt | wc -l | tr -d ' ')" "0"
}

t_no_verb_is_the_question_people_have() {
    cd "$TEST_TMPDIR"
    mu add 'web[01-02]' >/dev/null 2>&1
    assert_contains "$(mu)" "PROGRESS"
}

t_an_empty_pool_says_what_to_do() {
    cd "$TEST_TMPDIR"
    out="$(mu status)"
    assert_contains "$out" "pool is empty"
}

t_a_pool_that_is_not_one_is_refused() {
    cd "$TEST_TMPDIR"
    printf 'web01\nweb02\n' > hosts.txt
    set +e
    out="$(mu status --pool hosts.txt 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "not a muster pool"
}

t_version_matches_the_house_format() {
    out="$(mu --version)"
    assert_contains "$out" "Copyright (C) 2026 Martin J. Gallagher"
    assert_contains "$out" "License: GPL-3.0-or-later"
}

t_help_prints_every_verbs_flags() {
    out="$(mu help)"
    for verb in add take "done" release reset status list; do
        assert_contains "$out" "muster.py $verb"
    done
}

echo "muster"
run_test "a malformed duration is refused"    t_a_malformed_duration_is_refused_not_a_traceback
run_test "shared flags work before the verb" t_the_shared_flags_work_before_the_verb_too
run_test "an unwritable ticket puts back"    t_a_ticket_that_cannot_be_written_puts_the_items_back
run_test "a hash in a name is part of it"    t_a_hash_inside_a_name_is_part_of_the_name
run_test "a directory is not a list"         t_a_directory_is_not_a_list_of_items
run_test "a held row with no deadline"       t_a_held_row_with_no_deadline_is_not_a_lease
run_test "two rows for one item are refused" t_two_rows_for_one_item_are_refused
run_test "a pool error strands no lock"      t_a_pool_error_does_not_strand_the_lock
run_test "add expands ranges, skips dupes"    t_add_expands_ranges_and_ignores_duplicates
run_test "add reads a file and stdin"         t_add_reads_a_file_and_stdin
run_test "a mistyped path is not an item"     t_a_mistyped_path_is_not_silently_an_item
run_test "the pool header is documented"      t_the_pool_is_a_csv_with_the_documented_header
run_test "take writes a ticket, holds items"  t_take_writes_a_ticket_and_holds_the_items
run_test "take a named item"                  t_take_a_named_item
run_test "a held item is refused, not stolen" t_taking_a_held_item_is_refused_not_stolen
run_test "done completes and reports percent" t_done_completes_and_reports_the_percentage
run_test "a percentage never rounds to a lie" t_a_percentage_never_rounds_into_a_lie
run_test "one of many is not zero percent"    t_one_of_many_is_not_zero_percent
run_test "all done really is 100 percent"     t_all_done_really_is_100_percent
run_test "status --csv is one row of numbers" t_status_csv_is_one_row_of_numbers
run_test "an expired lease puts it back"      t_a_lease_that_runs_out_puts_the_item_back
run_test "someone else can take it after"     t_a_reclaimed_item_can_be_taken_by_someone_else
run_test "finishing after the lease lapsed"   t_finishing_after_the_lease_lapsed_is_a_conflict
run_test "late with nobody else is quieter"   t_finishing_late_with_nobody_else_holding_is_quieter
run_test "done twice is a finding"            t_done_twice_is_a_finding_not_a_silent_no_op
run_test "release gives items back"           t_release_gives_items_back_without_completing_them
run_test "release does not steal"             t_release_does_not_take_an_item_from_its_holder
run_test "reset undoes a completion"          t_reset_undoes_a_completion
run_test "an unknown item is named"           t_an_unknown_item_is_named_not_ignored
run_test "a hand-written list is a ticket"    t_a_hand_written_list_works_as_a_ticket
run_test "taken and never finished is stuck"  t_repeatedly_taken_and_never_finished_is_stuck
run_test "concurrent takes never overlap"     t_concurrent_takes_never_hand_out_the_same_item
run_test "the lock is not left behind"        t_the_lock_is_not_left_behind
run_test "a stale lock is broken"             t_a_stale_lock_is_broken_not_waited_on_forever
run_test "a fresh lock is waited for"         t_a_fresh_lock_is_waited_for_then_refused
run_test "a control character is refused"     t_an_item_with_a_control_character_is_refused
run_test "taking from an empty pile says so"  t_taking_from_an_empty_pile_says_so
run_test "no verb is the question people ask" t_no_verb_is_the_question_people_have
run_test "an empty pool says what to do"      t_an_empty_pool_says_what_to_do
run_test "a pool that is not one is refused"  t_a_pool_that_is_not_one_is_refused
run_test "--version matches the house format" t_version_matches_the_house_format
run_test "help prints every verb's flags"     t_help_prints_every_verbs_flags
finish
