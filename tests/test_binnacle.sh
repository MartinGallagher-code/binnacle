#!/usr/bin/env bash
# binnacle: the index -- what is installed, what version each says it is,
# and every tool's help on one page.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
BN="$BINNACLE_DIR/binnacle.py"

bn() { "$PY" "$BN" "$@"; }

# A writable copy of the package, so a case can break one instrument
# without touching the real one.
sandbox_pkg() {
    mkdir -p "$TEST_TMPDIR/pkg"
    cp "$BINNACLE_DIR"/*.py "$TEST_TMPDIR/pkg/"
    printf '%s\n' "$TEST_TMPDIR/pkg/binnacle.py"
}

# The version the package declares, read the same way the suite's
# version-agreement case reads it.
pkg_version() {
    sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$BINNACLE_DIR/__init__.py" | head -1
}

# How many instruments sit beside binnacle.py, counted the way discover()
# counts them: every .py in the package directory that is neither private
# nor the housing itself.  Derived rather than written down, so adding a
# twelfth tool does not turn four of these cases red.
pkg_tool_count() {
    ls "$BINNACLE_DIR"/*.py \
        | sed 's#.*/##' \
        | grep -v -e '^_' -e '^binnacle\.py$' \
        | wc -l | tr -d ' '
}

t_lists_every_installed_tool() {
    out="$(bn)"
    for tool in why-slow agree logtriage netmesh reachable resolve during skew muster manifest dredge; do
        assert_contains "$out" "$tool"
    done
}

t_names_a_version_for_each_tool() {
    # The point of the column: every instrument is asked separately, so
    # the answer is per-file rather than one number printed once.
    v="$(pkg_version)"
    count="$(bn list --quiet | awk -v v="$v" '$2 == v {n++} END {print n+0}')"
    assert_eq "$count" "$(pkg_tool_count)"
}

t_says_when_they_all_agree() {
    out="$(bn)"
    assert_contains "$out" "$(pkg_tool_count) instruments, all at $(pkg_version)."
    assert_not_contains "$out" "SKEW"
    assert_not_contains "$out" "BROKEN"
}

t_a_clean_install_exits_zero() {
    bn >/dev/null
    assert_status $? 0
}

t_version_matches_the_house_format() {
    out="$(bn --version)"
    assert_contains "$out" "$(pkg_version)"
    assert_contains "$out" "Copyright (C) 2026 Martin J. Gallagher"
    assert_contains "$out" "License: GPL-3.0-or-later"
}

t_help_covers_every_tool() {
    out="$(bn help)"
    # Each tool's help is its own module docstring, so its usage line is
    # the thing to look for -- and under its own name, not binnacle's.
    for tool in why-slow agree logtriage netmesh reachable resolve during skew muster manifest; do
        assert_contains "$out" "usage: $tool"
    done
}

t_help_reaches_the_subcommands_too() {
    # agree and netmesh hide most of their flags behind verbs; a page
    # that stopped at the top parser would be missing most of the manual.
    out="$(bn help)"
    assert_contains "$out" "usage: agree script"
    assert_contains "$out" "usage: netmesh summarize"
    assert_contains "$out" "usage: netmesh selftest"
}

t_help_does_not_wear_binnacles_name() {
    # Every tool computes its prog from argv[0], which is this command
    # when this command is the one asking. Unpinned, the whole page would
    # tell the reader to run `binnacle --pps`.
    out="$(bn help netmesh)"
    assert_contains "$out" "usage: netmesh"
    assert_not_contains "$out" "usage: binnacle.py [-h] [--version]
               {check"
}

t_help_for_one_tool_is_only_that_tool() {
    out="$(bn help skew)"
    assert_contains "$out" "usage: skew"
    assert_not_contains "$out" "usage: why-slow"
}

t_help_accepts_the_module_spelling() {
    # The command is why-slow, the file is why_slow.py; a reader who has
    # only seen the file should not have to guess.
    a="$(bn help why-slow)"
    b="$(bn help why_slow)"
    c="$(bn help why_slow.py)"
    assert_eq "$a" "$b"
    assert_eq "$a" "$c"
}

t_an_unknown_tool_names_the_real_ones() {
    set +e
    out="$(bn help nosuchtool 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such instrument: nosuchtool"
    assert_contains "$out" "logtriage"
}

t_an_unknown_verb_is_refused() {
    set +e
    out="$(bn wibble 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such verb: wibble"
}

t_list_takes_no_tool_argument() {
    # `binnacle list netmesh` looks like it should work and does not mean
    # anything; answering something else would be worse than refusing.
    set +e
    out="$(bn list netmesh 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "takes no TOOL argument"
}

t_paths_names_files_that_exist() {
    out="$(bn --paths)"
    path="$(printf '%s\n' "$out" | awk '$1 == "netmesh" {print $3}')"
    assert_file_exists "$path"
    assert_eq "$(basename "$path")" "netmesh.py"
}

t_version_skew_is_a_finding_not_a_shrug() {
    # The failure this exists to catch: one module left behind at an old
    # number, which `agree` would report as fleet-wide skew.
    bn_copy="$(sandbox_pkg)"
    sed -i 's/^VERSION = ".*"$/VERSION = "0.0.1"/' "$TEST_TMPDIR/pkg/netmesh.py"

    set +e
    out="$("$PY" "$bn_copy" 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "SKEW"
    assert_contains "$out" "netmesh says 0.0.1"
    # The other seven are still reported: a skewed install is still an
    # install, and the table is what says which file is the odd one.
    assert_contains "$out" "logtriage"
}

t_a_broken_instrument_is_reported_not_fatal() {
    bn_copy="$(sandbox_pkg)"
    printf 'import a_module_that_is_not_installed\n' > "$TEST_TMPDIR/pkg/resolve.py"

    set +e
    out="$("$PY" "$bn_copy" 2>&1)"; rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "BROKEN"
    assert_contains "$out" "resolve"
    assert_contains "$out" "ModuleNotFoundError"
    # Everything else still lists. One unimportable file must not cost
    # the answer to "what is installed here".
    assert_contains "$out" "why-slow"
    assert_contains "$out" "skew"
}

t_a_broken_instrument_does_not_stop_the_help_page() {
    bn_copy="$(sandbox_pkg)"
    printf 'import a_module_that_is_not_installed\n' > "$TEST_TMPDIR/pkg/resolve.py"

    set +e
    out="$("$PY" "$bn_copy" help 2>&1)"
    set -e
    assert_contains "$out" "resolve could not be loaded"
    assert_contains "$out" "usage: netmesh"
}

t_an_unregistered_tool_still_shows_up() {
    # A file that ships without being added to TOOLS in __init__.py is
    # exactly the drift worth seeing, so it is appended rather than
    # dropped -- with no description, which is the visible tell.
    bn_copy="$(sandbox_pkg)"
    printf 'VERSION = "%s"\n' "$(pkg_version)" > "$TEST_TMPDIR/pkg/newtool.py"

    out="$("$PY" "$bn_copy" 2>&1)"
    assert_contains "$out" "newtool"
    assert_contains "$out" "$(( $(pkg_tool_count) + 1 )) instruments"
}

t_private_files_are_not_instruments() {
    # __init__.py is the registry, not a tool, and neither are the
    # dunder files a build leaves behind.
    out="$(bn list --quiet)"
    assert_not_contains "$out" "__init__"
    assert_not_contains "$out" "binnacle"
}

t_it_runs_nothing_to_answer() {
    # Building a parser to format its help must not execute the tool.
    # If any of them shelled out to ssh, the shim would have logged it.
    install_fake_ssh
    PATH="$FAKE_BIN:$PATH" bn help >/dev/null
    assert_eq "$(wc -c < "$FAKE_SSH_LOG" | tr -d ' ')" "0"
}

t_a_closed_pipe_is_not_an_error() {
    # `binnacle help | head` is the obvious way to read 1400 lines.
    out="$(bn help 2>&1 | head -3)"
    assert_contains "$out" "usage: why-slow"
    assert_not_contains "$out" "BrokenPipeError"
}

t_quiet_is_the_table_alone() {
    out="$(bn list --quiet)"
    assert_not_contains "$out" "the housing"
    assert_not_contains "$out" "binnacle help"
    assert_eq "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "$(pkg_tool_count)"
}


# --- copy: getting a file out of site-packages and into your hand ----------

# A directory to copy into, fresh per case.
dest() {
    d="$TEST_TMPDIR/dest.$1"
    rm -rf "$d"; mkdir -p "$d"
    printf '%s\n' "$d"
}

t_copy_puts_the_file_here() {
    d="$(dest one)"
    bn copy netmesh -d "$d" >/dev/null
    assert_status $? 0
    assert_file_exists "$d/netmesh.py"
    # Byte for byte: this is the file that gets scp'd somewhere and run.
    cmp -s "$BINNACLE_DIR/netmesh.py" "$d/netmesh.py"
    assert_status $? 0
}

t_copy_keeps_the_package_file_name() {
    # why-slow the command is why_slow.py the file, and `agree script
    # why-slow` looks for the file.  A copy renamed on the way out stops
    # matching, so the underscore spelling is what lands.
    d="$(dest name)"
    bn copy why-slow -d "$d" >/dev/null
    assert_file_exists "$d/why_slow.py"
    assert_no_file "$d/why-slow.py"
}

t_copy_takes_either_spelling() {
    d="$(dest spell)"
    bn copy why_slow.py -d "$d" >/dev/null
    assert_file_exists "$d/why_slow.py"
}

t_copy_is_executable() {
    d="$(dest exec)"
    bn copy skew -d "$d" >/dev/null
    if [ -x "$d/skew.py" ]; then :; else
        _fail "copied file is not executable"
    fi
}

t_copy_runs_where_it_lands() {
    # The whole claim of the standalone-file rule, checked rather than
    # asserted: the copy answers --version with nothing else beside it.
    d="$(dest run)"
    bn copy skew -d "$d" >/dev/null
    out="$("$PY" "$d/skew.py" --version 2>&1)"
    assert_contains "$out" "$(pkg_version)"
}

t_copy_all_brings_every_instrument() {
    d="$(dest all)"
    bn copy --all -d "$d" >/dev/null
    assert_eq "$(ls "$d" | wc -l | tr -d ' ')" "$(pkg_tool_count)"
    # The housing is not an instrument and does not come along.
    assert_no_file "$d/binnacle.py"
}

t_copy_takes_several_at_once() {
    d="$(dest many)"
    bn copy skew muster -d "$d" >/dev/null
    assert_file_exists "$d/skew.py"
    assert_file_exists "$d/muster.py"
}

t_copy_refuses_to_overwrite_your_edit() {
    d="$(dest over)"
    printf 'my own edited copy\n' > "$d/skew.py"
    set +e
    out="$(bn copy skew -d "$d" 2>&1)"
    rc=$?
    set -e
    assert_status $rc 1
    assert_contains "$out" "REFUSED"
    assert_eq "$(cat "$d/skew.py")" "my own edited copy"
}

t_copy_force_overwrites() {
    d="$(dest force)"
    printf 'my own edited copy\n' > "$d/skew.py"
    bn copy skew -d "$d" --force >/dev/null
    assert_status $? 0
    cmp -s "$BINNACLE_DIR/skew.py" "$d/skew.py"
    assert_status $? 0
}

t_copy_of_the_same_file_is_not_a_failure() {
    # Running it twice is the common case and must not read as trouble.
    d="$(dest twice)"
    bn copy skew -d "$d" >/dev/null
    out="$(bn copy skew -d "$d" 2>&1)"
    assert_status $? 0
    assert_contains "$out" "identical"
}

t_copy_names_the_real_ones_when_wrong() {
    set +e
    out="$(bn copy nosuchtool 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "no such instrument"
    assert_contains "$out" "netmesh"
}

t_copy_needs_something_to_copy() {
    set +e
    out="$(bn copy 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "--all"
}

t_copy_refuses_names_and_all_together() {
    set +e
    out="$(bn copy skew --all 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "not both"
}

t_copy_refuses_a_directory_that_is_not_one() {
    set +e
    out="$(bn copy skew -d "$TEST_TMPDIR/nowhere" 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "not a directory"
}

t_copy_flags_belong_to_copy() {
    # --force on a list is a typo worth naming rather than ignoring.
    set +e
    out="$(bn list --force 2>&1)"
    rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "belongs to copy"
}

echo "binnacle"
run_test "lists every installed tool"         t_lists_every_installed_tool
run_test "names a version for each tool"      t_names_a_version_for_each_tool
run_test "says when they all agree"           t_says_when_they_all_agree
run_test "a clean install exits zero"         t_a_clean_install_exits_zero
run_test "--version matches the house format" t_version_matches_the_house_format
run_test "help covers every tool"             t_help_covers_every_tool
run_test "help reaches the subcommands"       t_help_reaches_the_subcommands_too
run_test "help does not wear binnacle's name" t_help_does_not_wear_binnacles_name
run_test "help TOOL is only that tool"        t_help_for_one_tool_is_only_that_tool
run_test "help accepts the module spelling"   t_help_accepts_the_module_spelling
run_test "an unknown tool names the real"     t_an_unknown_tool_names_the_real_ones
run_test "an unknown verb is refused"         t_an_unknown_verb_is_refused
run_test "list takes no TOOL argument"        t_list_takes_no_tool_argument
run_test "--paths names files that exist"     t_paths_names_files_that_exist
run_test "version skew is a finding"          t_version_skew_is_a_finding_not_a_shrug
run_test "a broken instrument is reported"    t_a_broken_instrument_is_reported_not_fatal
run_test "a broken one keeps the help page"   t_a_broken_instrument_does_not_stop_the_help_page
run_test "an unregistered tool still shows"   t_an_unregistered_tool_still_shows_up
run_test "private files are not instruments"  t_private_files_are_not_instruments
run_test "it runs nothing to answer"          t_it_runs_nothing_to_answer
run_test "a closed pipe is not an error"      t_a_closed_pipe_is_not_an_error
run_test "--quiet is the table alone"          t_quiet_is_the_table_alone
run_test "copy puts the file here"            t_copy_puts_the_file_here
run_test "copy keeps the package file name"   t_copy_keeps_the_package_file_name
run_test "copy takes either spelling"         t_copy_takes_either_spelling
run_test "copy is executable"                 t_copy_is_executable
run_test "a copy runs where it lands"         t_copy_runs_where_it_lands
run_test "copy --all brings every one"        t_copy_all_brings_every_instrument
run_test "copy takes several at once"         t_copy_takes_several_at_once
run_test "copy refuses to overwrite"          t_copy_refuses_to_overwrite_your_edit
run_test "--force overwrites"                 t_copy_force_overwrites
run_test "copying the same file is fine"      t_copy_of_the_same_file_is_not_a_failure
run_test "copy names the real ones"           t_copy_names_the_real_ones_when_wrong
run_test "copy needs something to copy"       t_copy_needs_something_to_copy
run_test "copy refuses names and --all"       t_copy_refuses_names_and_all_together
run_test "copy refuses a missing dir"         t_copy_refuses_a_directory_that_is_not_one
run_test "copy's flags belong to copy"        t_copy_flags_belong_to_copy
finish
