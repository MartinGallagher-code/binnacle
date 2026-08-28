#!/usr/bin/env bash
# manifest: reading a .dc layout and answering "which servers are those".

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
MF="$BINNACLE_DIR/manifest.py"

mf() { "$PY" "$MF" "$@"; }

# A layout in the documented .dc format, written here rather than copied
# from the layout_visualizer repository: that repository carries no
# licence file, and this one is REUSE-compliant, so vendoring one of its
# examples would put an unlicensed file into a GPL tree.
#
# Two rooms x two rows x three racks x (1 switch + 4 servers), with flat
# hostnames, so 48 servers and 12 switches.  The row goes into the name
# because the rack numbers restart in every row here; leave it out and
# two machines in different rows answer to the same hostname, which is
# a layout bug rather than a tool one.
write_layout() {
    cat > "$TEST_TMPDIR/dc.dc" <<'EOF'
# A small floor, in the documented format.
dc SITE name="Site" +prod region=us-east

  room wr[01..02] +hall
    row A..B +compute
      rack r[01..03] u=42
        node tor at=42 role=tor +switch name={room}{row}{rack}tor
        node u[01..04] role=server name={room}{row}{rack}{id} +x86 model=r7625

net data label="Data" color=#4fa3ff
link data role=server role=tor scope=rack
EOF
}

t_a_container_selector_means_everything_under_it() {
    # `rack[1-3]` is the servers in those racks, not the three rack
    # elements -- filtering those by role=server would leave nothing.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'rack[1-3]' --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc 'rack[1]' --count 2>/dev/null)" "16"
    assert_eq "$(mf dc.dc 'room[1]' --count 2>/dev/null)" "24"
    assert_eq "$(mf dc.dc 'row[A]' --count 2>/dev/null)" "24"
}

t_selectors_and_together() {
    write_layout
    cd "$TEST_TMPDIR"
    # One room, one row, one rack: four servers.
    assert_eq "$(mf dc.dc 'room[1]' 'row[A]' 'rack[1]' --count 2>/dev/null)" "4"
}

t_commas_inside_one_selector_are_or() {
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'rack[1,3]' --count 2>/dev/null)" "32"
    assert_eq "$(mf dc.dc 'row[A,B]' --count 2>/dev/null)" "48"
}

t_a_plural_kind_is_the_same_question() {
    write_layout
    cd "$TEST_TMPDIR"
    a="$(mf dc.dc 'rack[1-2]' --count 2>/dev/null)"
    b="$(mf dc.dc 'racks[1-2]' --count 2>/dev/null)"
    assert_eq "$a" "$b"
    assert_eq "$a" "32"
}

t_both_range_spellings_work() {
    # rack[1-3] is how the rest of this package writes a range; rack[1..3]
    # is how the layout file does. A reader should not have to know which
    # side of the fence they are standing on.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'rack[1-3]' --count 2>/dev/null)" \
              "$(mf dc.dc 'rack[1..3]' --count 2>/dev/null)"
}

t_an_id_is_matched_forgivingly() {
    # The racks are called r01..r03. `rack[1]` is the rack you mean.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'rack[1]' --count 2>/dev/null)" "16"
    assert_eq "$(mf dc.dc 'rack[01]' --count 2>/dev/null)" "16"
    assert_eq "$(mf dc.dc 'rack[r01]' --count 2>/dev/null)" "16"
}

t_names_come_from_the_name_attribute() {
    # name={room}{row}{rack}{id} is what makes a flat hostname, and that
    # is what a fleet tool needs to be handed.  name= is not inherited,
    # so `name="Site"` on the dc line stays the site's name and does not
    # become forty-eight machines all called Site.
    write_layout
    cd "$TEST_TMPDIR"
    # Rack r01 exists in both rows of the room, so this is eight
    # machines, not four -- the rack numbers restart per row here.
    out="$(mf dc.dc 'room[1]' 'rack[1]' --sort 2>/dev/null)"
    assert_eq "$(printf '%s\n' "$out" | head -1)" "wr01Ar01u01"
    assert_eq "$(mf dc.dc 'rack[1-3]' 2>/dev/null | grep -c Site)" "0"
    assert_eq "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "8"
}

t_servers_by_name_directly() {
    write_layout
    cd "$TEST_TMPDIR"
    out="$(mf dc.dc 'wr01Ar01u01,wr01Br02u03,wr02Ar03u04' --sort 2>/dev/null)"
    assert_eq "$(printf '%s\n' "$out" | tr '\n' ' ')" \
              "wr01Ar01u01 wr01Br02u03 wr02Ar03u04 "
}

t_switches_are_not_servers() {
    # Every rack has a tor. The default question is about servers.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc --role tor --count 2>/dev/null)" "12"
    assert_contains "$(mf dc.dc --role tor 2>/dev/null)" "wr01Ar01tor"
}

t_tags_are_inherited_from_ancestors() {
    # +prod is on the dc line only; every machine carries it.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc +prod --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc +compute --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc +x86 --count 2>/dev/null)" "48"
}

t_attributes_are_matched_and_globbed() {
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'model=r7625' --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc 'model=r76*' --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc 'model=nope*' --count 2>/dev/null)" "0"
}

t_a_selector_can_be_negated() {
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc '!room[1]' --count 2>/dev/null)" "24"
    assert_eq "$(mf dc.dc 'rack[1-3]' '!rack[3]' --count 2>/dev/null)" "32"
}

t_paths_are_printed_on_request() {
    write_layout
    cd "$TEST_TMPDIR"
    out="$(mf dc.dc 'room[1]' 'rack[1]' --path --sort 2>/dev/null | head -1)"
    assert_eq "$out" "SITE/wr01/A/r01/u01"
}

t_a_path_or_its_suffix_selects() {
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'SITE/wr01/A/r01/u01' --count 2>/dev/null)" "1"
    # A suffix may name more than one: both rooms have A/r01/u01.
    assert_eq "$(mf dc.dc 'A/r01/u01' --count 2>/dev/null)" "2"
}

t_csv_carries_where_each_one_is() {
    write_layout
    cd "$TEST_TMPDIR"
    out="$(mf dc.dc 'room[1]' 'rack[1]' --csv --sort 2>/dev/null)"
    assert_eq "$(printf '%s\n' "$out" | head -1)" \
        "name,path,kind,role,room,row,rack,slot"
    assert_contains "$out" "wr01Ar01u01,SITE/wr01/A/r01/u01,node,server,wr01,A,r01,u01"
}

t_net_and_link_lines_are_not_elements() {
    # They describe cabling. A tool answering "which machines" reads past
    # them rather than inventing two elements called data.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc data --role any --count 2>/dev/null)" "0"
}

t_a_layout_with_no_roles_still_answers() {
    # Filtering by role= would answer nothing however the question was
    # asked, so the leaf nodes are taken as the machines and it says so.
    cd "$TEST_TMPDIR"
    printf 'dc D1\n  room R1\n    rack rk[1..2]\n      node n[1..3]\n' > plain.dc
    out="$(mf plain.dc 'rack[1]' 2>&1)"
    assert_contains "$out" "n1"
    assert_contains "$out" "sets no role="
    assert_eq "$(mf plain.dc 'rack[1]' --count 2>/dev/null)" "3"
}

t_nothing_matched_is_exit_one() {
    write_layout
    cd "$TEST_TMPDIR"
    set +e
    out="$(mf dc.dc 'rack[99]' 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "nothing matched"
}

t_matching_only_containers_says_so() {
    # `--role server` over a selector that only picks switches is a real
    # question with an empty answer; the way out is named.
    write_layout
    cd "$TEST_TMPDIR"
    set +e
    out="$(mf dc.dc 'role=tor' 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "--role any"
}

t_a_missing_layout_is_refused() {
    cd "$TEST_TMPDIR"
    set +e
    out="$(mf nosuch.dc 'rack[1]' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such layout file"
}

t_the_layout_may_come_from_the_environment() {
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(MANIFEST_LAYOUT=dc.dc mf 'rack[1]' --count 2>/dev/null)" "16"
}

t_attributes_inherit_but_layout_keys_do_not() {
    # region= is on the dc line, so every machine is in a thing that has
    # it.  u=42 is the rack's own height: it is not copied onto the
    # machines, but a selector names elements and the answer is the
    # machines at or under them, so `u=42` is still the machines in the
    # 42U racks.  That is one rule, not an exception for attributes.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'region=us-east' --count 2>/dev/null)" "48"
    assert_eq "$(mf dc.dc 'u=42' --count 2>/dev/null)" "48"
    # ...and the racks are the only things carrying it: drop the ancestor
    # that has u= and nothing is left.
    assert_eq "$(mf dc.dc 'u=42' '!rack[1-3]' --count 2>/dev/null)" "0"
}

t_it_contacts_nothing() {
    # This reads a file and prints. If it shelled out anywhere, the shim
    # would have logged it.
    write_layout
    cd "$TEST_TMPDIR"
    install_fake_ssh
    PATH="$FAKE_BIN:$PATH" mf dc.dc 'rack[1-3]' >/dev/null 2>&1
    assert_eq "$(wc -c < "$FAKE_SSH_LOG" | tr -d ' ')" "0"
}

t_the_layout_file_is_never_modified() {
    write_layout
    cd "$TEST_TMPDIR"
    before="$(cksum < dc.dc)"
    mf dc.dc 'rack[1-3]' >/dev/null 2>&1
    mf dc.dc --role any --csv >/dev/null 2>&1
    assert_eq "$(cksum < dc.dc)" "$before"
}

t_output_feeds_the_other_tools() {
    # The whole point of one per line: muster takes it, agree takes it.
    write_layout
    cd "$TEST_TMPDIR"
    mf dc.dc 'rack[1]' > hosts.txt 2>/dev/null
    assert_eq "$(wc -l < hosts.txt | tr -d ' ')" "16"
    "$PY" "$BINNACLE_DIR/muster.py" add hosts.txt --pool p.csv >/dev/null 2>&1
    assert_eq "$("$PY" "$BINNACLE_DIR/muster.py" list --pool p.csv --state any \
                 | tail -n +2 | wc -l | tr -d ' ')" "16"
}

t_a_closed_pipe_is_not_an_error() {
    write_layout
    cd "$TEST_TMPDIR"
    out="$(mf dc.dc 'rack[1-3]' 2>/dev/null | head -2)"
    assert_eq "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "2"
    assert_not_contains "$out" "BrokenPipeError"
}

t_a_kind_the_layout_lacks_is_named() {
    # "nothing matched" would leave someone checking their rack numbers.
    # The layout is the authority on what kinds exist, so it says which.
    write_layout
    cd "$TEST_TMPDIR"
    set +e
    err="$(mf dc.dc 'cage[1]' 2>&1 >/dev/null)"
    set -e
    assert_contains "$err" "no cage"
    assert_contains "$err" "rack"
    # ...but only as a report, since one branch of an OR may legitimately
    # name a kind this particular floor does not have.
    assert_eq "$(mf dc.dc 'rack[1],cage[2]' --count 2>/dev/null)" "16"
}

t_a_range_larger_than_a_building_is_refused() {
    # A filter, not a list of things to create: expanding a mistyped zero
    # would eat the memory before it could say anything.
    write_layout
    cd "$TEST_TMPDIR"
    set +e
    out="$(mf dc.dc 'rack[1-90000000]' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "zero too many"
}

t_a_directory_is_not_a_layout() {
    write_layout
    cd "$TEST_TMPDIR"
    mkdir -p adir
    set +e
    out="$(mf adir 'rack[1]' 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "is a directory"
}

t_a_big_range_gives_the_same_answer() {
    # The wanted ids are indexed rather than scanned; the answer must not
    # depend on how wide the range around them was written.
    write_layout
    cd "$TEST_TMPDIR"
    assert_eq "$(mf dc.dc 'rack[1-3]' --count 2>/dev/null)" \
              "$(mf dc.dc 'rack[1-500]' --count 2>/dev/null)"
    assert_eq "$(mf dc.dc 'rack[1-3]' --sort 2>/dev/null | cksum)" \
              "$(mf dc.dc 'rack[1-500]' --sort 2>/dev/null | cksum)"
    # A glob among the wants still works, and still only matches its own.
    assert_eq "$(mf dc.dc 'rack[r0*]' --count 2>/dev/null)" "48"
    set +e
    assert_eq "$(mf dc.dc 'rack[u0*]' --count 2>/dev/null)" "0"
    set -e
}

t_version_matches_the_house_format() {
    out="$(mf --version)"
    assert_contains "$out" "Copyright (C) 2026 Martin J. Gallagher"
    assert_contains "$out" "License: GPL-3.0-or-later"
}

echo "manifest"
run_test "a container means everything under" t_a_container_selector_means_everything_under_it
run_test "selectors AND together"             t_selectors_and_together
run_test "commas in one selector are OR"      t_commas_inside_one_selector_are_or
run_test "a plural kind is the same"          t_a_plural_kind_is_the_same_question
run_test "both range spellings work"          t_both_range_spellings_work
run_test "an id is matched forgivingly"       t_an_id_is_matched_forgivingly
run_test "names come from name="              t_names_come_from_the_name_attribute
run_test "servers by name directly"           t_servers_by_name_directly
run_test "switches are not servers"           t_switches_are_not_servers
run_test "tags inherit from ancestors"        t_tags_are_inherited_from_ancestors
run_test "attributes match and glob"          t_attributes_are_matched_and_globbed
run_test "a selector can be negated"          t_a_selector_can_be_negated
run_test "paths on request"                   t_paths_are_printed_on_request
run_test "a path or its suffix selects"       t_a_path_or_its_suffix_selects
run_test "csv carries where each one is"      t_csv_carries_where_each_one_is
run_test "net and link are not elements"      t_net_and_link_lines_are_not_elements
run_test "a layout with no roles answers"     t_a_layout_with_no_roles_still_answers
run_test "nothing matched is exit 1"          t_nothing_matched_is_exit_one
run_test "only containers matched says so"    t_matching_only_containers_says_so
run_test "a missing layout is refused"        t_a_missing_layout_is_refused
run_test "the layout may come from env"       t_the_layout_may_come_from_the_environment
run_test "attrs inherit, layout keys do not"  t_attributes_inherit_but_layout_keys_do_not
run_test "it contacts nothing"                t_it_contacts_nothing
run_test "the layout is never modified"       t_the_layout_file_is_never_modified
run_test "output feeds the other tools"       t_output_feeds_the_other_tools
run_test "a closed pipe is not an error"      t_a_closed_pipe_is_not_an_error
run_test "an absent kind is named"            t_a_kind_the_layout_lacks_is_named
run_test "a range past any building"          t_a_range_larger_than_a_building_is_refused
run_test "a directory is not a layout"        t_a_directory_is_not_a_layout
run_test "a wide range answers the same"      t_a_big_range_gives_the_same_answer
run_test "--version matches the house format" t_version_matches_the_house_format
finish
