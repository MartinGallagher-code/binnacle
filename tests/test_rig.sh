#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# rig: a file of settings turned into a command line, the questions asked
# for the ones left blank, and a file to start from written out of a
# tool's own help.
#
# Every run goes through `rg`, which takes the controlling terminal away
# first: a case that asks a question must answer it with RIG_TEST_KEYS
# or --set, and one that forgets fails here rather than opening the
# questions on the terminal of whoever is running the suite.  The one
# case about the real screen makes its own terminal, a pty.

# run_test runs every case under `set -e`, so a cd that fails has already
# ended the case.  The other suites get this from shellcheck for free by
# toggling set -e somewhere; this one never needs to.
# shellcheck disable=SC2164
set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
RG="$BINNACLE_DIR/rig.py"

# Python rather than setsid(1), which is util-linux and not everywhere
# this suite runs; given with -c so the command keeps its own stdin.
DETACH='
import os, sys
try:
    os.setsid()
except OSError:
    # Already a group leader, which setsid refuses: do it in a child.
    pid = os.fork()
    if pid:
        _, st = os.waitpid(pid, 0)
        sys.exit(os.WEXITSTATUS(st) if os.WIFEXITED(st)
                 else 128 + os.WTERMSIG(st))
    os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
'

rg() { "$PY" -c "$DETACH" "$PY" "$RG" "$@"; }

conf() { cat > "$TEST_TMPDIR/$1"; }

# --- the four rules ---------------------------------------------------------

t_the_four_rules_make_the_command_line() {
    cd "$TEST_TMPDIR"
    conf app.conf <<'EOF'
# settings for mytool
jobs=20

verbose
j=4
EOF
    assert_eq "$(rg app.conf -n -- mytool)" "mytool --jobs 20 --verbose -j 4"
}

t_a_name_written_twice_is_two_flags() {
    # The second is not a correction of the first: -S web01 -S web02.
    cd "$TEST_TMPDIR"
    printf 'S=web01\nS=web02\n' > app.conf
    assert_eq "$(rg app.conf -n -- dredge)" "dredge -S web01 -S web02"
}

t_quotes_take_a_value_as_it_stands() {
    cd "$TEST_TMPDIR"
    printf 'pattern="a|b"\nnote=""\nsay='"'"'x y'"'"'\n' > app.conf
    assert_eq "$(rg app.conf -n -- t)" "t --pattern 'a|b' --note '' --say 'x y'"
}

t_the_dashes_say_how_a_flag_is_spelled() {
    # --color=auto stays joined, because an optional value only works
    # that way; -name is find's spelling; a value starting with a dash is
    # joined so a parser cannot read it as the next flag.
    cd "$TEST_TMPDIR"
    printf -- '--color=auto\n-name=x\nsuffix=-raw\nn=-1\n-maxdepth=2\n' \
        > app.conf
    assert_eq "$(rg app.conf -n -- t)" \
        "t --color=auto -name x --suffix=-raw -n-1 -maxdepth 2"
}

t_a_value_is_one_argument_whatever_is_in_it() {
    # No shell anywhere: a space or a $ in a value is part of the value.
    cd "$TEST_TMPDIR"
    # shellcheck disable=SC2016  # the $ is the point: no shell expands it
    printf 'msg=two words $HOME ;rm\n' > app.conf
    out="$(rg app.conf -- "$PY" -c 'import sys; print(sys.argv[1:])')"
    assert_eq "$out" "['--msg', 'two words \$HOME ;rm']"
}

t_a_bad_name_is_named_by_its_line() {
    cd "$TEST_TMPDIR"
    printf 'ok=1\nnot a name=2\n' > app.conf
    rc=0; out="$(rg app.conf -n -- t 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "app.conf:2"
}

t_the_file_may_come_on_stdin() {
    cd "$TEST_TMPDIR"
    assert_eq "$(printf 'a=1\n' | rg - -n -- t)" "t -a 1"
}

t_bytes_in_the_file_reach_the_command_unchanged() {
    # Decoded the way argv is, so a Latin-1 byte in the file is that byte
    # in the command's argv, even under LANG=C.
    cd "$TEST_TMPDIR"
    printf 'name=caf\351\n' > app.conf
    out="$(LC_ALL=C rg app.conf -- sh -c 'printf %s "$2" | od -An -tx1' sh)"
    assert_eq "$(printf '%s' "$out" | tr -s ' ')" " 63 61 66 e9"
}

# --- placeholders -----------------------------------------------------------

t_a_placeholder_puts_the_value_there_instead_of_a_flag() {
    cd "$TEST_TMPDIR"
    printf 'user=deploy\nhost=web01\np=2222\n' > ssh.conf
    assert_eq "$(rg ssh.conf -n -- ssh '{user}@{host}' uptime)" \
        "ssh deploy@web01 uptime -p 2222"
}

t_the_flags_go_where_the_command_says() {
    # After the host, a flag is the remote command's rather than ssh's.
    cd "$TEST_TMPDIR"
    printf 'host=web01\np=2222\n' > ssh.conf
    assert_eq "$(rg ssh.conf -n -- ssh '{...}' '{host}' uptime)" \
        "ssh -p 2222 web01 uptime"
}

t_an_unknown_placeholder_is_refused_with_the_way_round() {
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > app.conf
    rc=0; out="$(rg app.conf -n -- awk '{print}' f 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "{{print}}"
    assert_eq "$(rg app.conf -n -- awk '{{print}}' f)" "awk '{print}' f -a 1"
}

t_braces_that_are_not_a_name_are_left_alone() {
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > app.conf
    assert_eq "$(rg app.conf -n -- find . -exec echo {} + '{a,b}')" \
        "find . -exec echo '{}' + '{a,b}' -a 1"
}

t_a_placeholder_needs_exactly_one_value() {
    cd "$TEST_TMPDIR"
    printf 'h=a\nh=b\nv\n' > app.conf
    rc=0; out="$(rg app.conf -n -- t '{h}' 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "app.conf:1, app.conf:2"
    rc=0; out="$(rg app.conf -n -- t '{v}' 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "switch"
}

# --- --set ------------------------------------------------------------------

t_set_answers_replaces_and_adds() {
    cd "$TEST_TMPDIR"
    printf 'host=\njobs=20\nS=a\nS=b\n' > app.conf
    out="$(rg app.conf --set host=web01 --set jobs=5 --set S=c \
              --set extra=1 -n -- t)"
    # In the file's order, the replaced lines where the file had them.
    assert_eq "$out" "t --host web01 --jobs 5 -S c --extra 1"
}

t_set_keeps_the_files_spelling() {
    cd "$TEST_TMPDIR"
    printf -- '--color=auto\n' > app.conf
    assert_eq "$(rg app.conf --set color=never -n -- ls)" "ls --color=never"
}

t_set_is_an_answer_not_a_question() {
    cd "$TEST_TMPDIR"
    printf 'mode=fast|safe\n' > app.conf
    assert_eq "$(rg app.conf --set 'mode=a|b' -n -- t)" "t --mode 'a|b'"
}

# --- the questions ----------------------------------------------------------

t_answers_fill_the_command() {
    cd "$TEST_TMPDIR"
    printf 'host=\nmode=fast|safe|paranoid\n' > app.conf
    out="$(RIG_TEST_KEYS='web01<tab><right>' rg app.conf -n -- ssh '{host}' \
           2>/dev/null)" || true
    # Out of keys before Enter is a cancel, not a run.
    assert_eq "$out" ""
    out="$(RIG_TEST_KEYS='web01<tab><right><enter>' rg app.conf -n -- \
           ssh '{host}')"
    assert_eq "$out" "ssh web01 --mode safe"
}

t_a_letter_jumps_to_the_choice() {
    cd "$TEST_TMPDIR"
    printf 'mode=fast|safe|paranoid\n' > app.conf
    assert_eq "$(RIG_TEST_KEYS='p<enter>' rg app.conf -n -- t)" \
        "t --mode paranoid"
}

t_text_can_be_edited() {
    cd "$TEST_TMPDIR"
    printf 'host=\n' > app.conf
    out="$(RIG_TEST_KEYS='wbe01<home><right><del><right>b<end>x<bs><enter>' \
           rg app.conf -n -- t)"
    assert_eq "$out" "t --host web01"
}

t_an_empty_answer_leaves_the_flag_out() {
    cd "$TEST_TMPDIR"
    printf 'host=\nlevel=|debug|trace\nv\n' > app.conf
    assert_eq "$(RIG_TEST_KEYS='<enter>' rg app.conf -n -- t)" "t -v"
}

t_enter_waits_for_every_starred_answer() {
    # A starred name is a {placeholder}: without it the command has a
    # hole in it, so Enter says so and stays.
    cd "$TEST_TMPDIR"
    printf 'host=\n' > app.conf
    rc=0; out="$(RIG_TEST_KEYS='<enter>' rg app.conf -n -- ssh '{host}' 2>&1)" \
        || rc=$?
    assert_status "$rc" 130
    assert_contains "$out" "host is used in the command, so it needs an answer"
    assert_eq "$(RIG_TEST_KEYS='<enter>h1<enter>' rg app.conf -n -- \
                 ssh '{host}')" "ssh h1"
}

t_esc_runs_nothing() {
    cd "$TEST_TMPDIR"
    printf 'host=\n' > app.conf
    rc=0; out="$(RIG_TEST_KEYS='web01<esc>' rg app.conf -- touch ran 2>&1)" \
        || rc=$?
    assert_status "$rc" 130
    assert_contains "$out" "nothing ran"
    assert_no_file ran
}

t_with_no_terminal_it_refuses_rather_than_waits() {
    cd "$TEST_TMPDIR"
    printf 'host=\nmode=a|b\njobs=2\n' > app.conf
    rc=0; out="$(rg app.conf -- touch ran 2>&1 </dev/null)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "host and mode need answers"
    assert_contains "$out" "--set host=... --set mode=..."
    assert_no_file ran
}

t_the_questions_are_drawn_on_a_real_terminal() {
    # The screen, for real: a pty, curses, keys typed at it.  stdout is a
    # file, so this is also the case where the questions have to find
    # the terminal by themselves.
    cd "$TEST_TMPDIR"
    printf 'host=\nmode=fast|safe\n' > app.conf
    "$PY" - "$PY" "$RG" <<'EOF'
import os, pty, select, sys, time

py, rig = sys.argv[1], sys.argv[2]
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"
    out = os.open("answer", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(out, 1)
    os.execvp(py, [py, rig, "app.conf", "--", "echo", "{host}"])

seen = b""


def pump(seconds):
    global seen
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                seen += os.read(fd, 65536)
            except OSError:
                return


pump(2.0)
# Typed, then Down, then Right as xterm sends it -- and as a terminal
# whose terminfo disagrees sends it -- then Enter.
for keys in (b"web01", b"\x1bOB", b"\x1b[C", b"\r"):
    os.write(fd, keys)
    pump(0.5)
pump(1.0)
_, status = os.waitpid(pid, 0)
code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
answer = open("answer").read()
bad = []
if code != 0:
    bad.append("exit %d" % code)
if answer != "web01 --mode safe\n":
    bad.append("stdout was %r" % answer)
if b"host *" not in seen:
    bad.append("the screen never showed 'host *'")
if bad:
    sys.stderr.write("; ".join(bad) + "\n" + repr(seen[-400:]) + "\n")
    sys.exit(1)
EOF
    assert_status $? 0
}

# --- running it -------------------------------------------------------------

t_it_becomes_the_command() {
    # Its stdin, its stdout, its exit status.
    cd "$TEST_TMPDIR"
    printf 'n=2\n' > app.conf
    assert_eq "$(printf 'a\nb\nc\n' | rg app.conf -- head)" "$(printf 'a\nb')"
    rc=0; rg app.conf -- sh -c 'exit 7' || rc=$?
    assert_status "$rc" 7
}

t_a_missing_command_is_127_and_a_dry_run_is_not() {
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > app.conf
    rc=0; out="$(rg app.conf -- no-such-command-here 2>&1)" || rc=$?
    assert_status "$rc" 127
    assert_contains "$out" "command not found"
    printf '#!/bin/sh\n' > notexec
    chmod 644 notexec
    rc=0; rg app.conf -- ./notexec 2>/dev/null || rc=$?
    assert_status "$rc" 126
    assert_eq "$(rg app.conf -n -- no-such-command-here)" \
        "no-such-command-here -a 1"
}

t_without_a_command_it_says_how() {
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > app.conf
    rc=0; out="$(rg app.conf 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "app.conf -- mytool"
}

t_without_dashes_the_command_starts_after_the_file() {
    # `rig app.conf ls -l`: the -l is ls's, not a flag of rig's.
    cd "$TEST_TMPDIR"
    printf 'a=1\n' > app.conf
    assert_eq "$(rg app.conf -n ls -l)" "ls -l -a 1"
}

t_a_missing_file_is_refused() {
    cd "$TEST_TMPDIR"
    rc=0; out="$(rg nope.conf -- true 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "cannot read nope.conf"
}

# --- --init -----------------------------------------------------------------

# A tool with GNU-style --help, one that only knows -h and says so on
# stderr the way argparse does, and one with nothing but a man page.
fake_tools() {
    export PATH="$FAKE_BIN:$PATH"
    cat > "$FAKE_BIN/gtool" <<'EOF'
#!/bin/sh
echo "$*" >> "$TEST_TMPDIR/gtool.log"
case "$1" in --help) cat <<'HELP'
Usage: gtool [OPTION]... FILE
Do things to FILE.

  -v, --verbose            say more
  -j, --jobs=N             run N at once (default: 5)
      --mode {fast,safe}   how careful to be
      --color[=WHEN]       colour the output WHEN
  -o FILE                  write to FILE
  -h, --help               display this help and exit
      --version            output version information and exit
HELP
exit 0 ;; esac
EOF
    cat > "$FAKE_BIN/htool" <<'EOF'
#!/bin/sh
case "$1" in
  --help) echo "htool: unrecognized option '--help'" >&2; exit 2 ;;
  -h) cat >&2 <<'HELP'
usage: htool [-h] [-q] [--level {1,2,3}]

options:
  -h, --help       show this help message and exit
  -q, --quiet      say less
  --level {1,2,3}  how much
HELP
  exit 0 ;;
esac
EOF
    cat > "$FAKE_BIN/mtool" <<'EOF'
#!/bin/sh
exit 1
EOF
    # man, as groff draws it in a UTF-8 locale: bold by overstrike, and a
    # flag's dashes as U+2010.
    cat > "$FAKE_BIN/man" <<'EOF'
#!/bin/sh
[ "$1" = mtool ] || exit 16
printf 'MTOOL(1)\n\nOPTIONS\n'
printf '       -\b--\b-f\bfo\bor\brc\bce\be\n              do it anyway\n\n'
printf '       \342\200\220x, \342\200\220\342\200\220extra=N\n'
printf '              extra things\n'
EOF
    chmod +x "$FAKE_BIN/gtool" "$FAKE_BIN/htool" "$FAKE_BIN/mtool" \
        "$FAKE_BIN/man"
}

t_init_writes_every_flag_commented_out() {
    fake_tools
    cd "$TEST_TMPDIR"
    rg gtool.conf --init -- gtool 2>err
    assert_status $? 0
    assert_contains "$(cat err)" "wrote gtool.conf: 5 flags from \`gtool --help\`"
    f="$(cat gtool.conf)"
    assert_contains "$f" "$(printf '# -v, --verbose            say more\n#verbose')"
    assert_contains "$f" "#jobs=5"
    assert_contains "$f" "#mode=fast|safe"
    assert_contains "$f" "#--color="
    assert_contains "$f" "#o="
    assert_not_contains "$f" "#help"
    assert_not_contains "$f" "#version"
    # As written, nothing is switched on.
    assert_eq "$(rg gtool.conf -n -- gtool)" "gtool"
}

t_an_init_file_runs_once_its_hashes_are_off() {
    fake_tools
    cd "$TEST_TMPDIR"
    rg gtool.conf --init -- gtool 2>/dev/null
    sed 's/^#\([a-z-]\)/\1/' gtool.conf > on.conf
    out="$(rg on.conf --set mode=safe --set color=always --set o=out.txt \
              -n -- gtool)"
    assert_eq "$out" \
        "gtool --verbose --jobs 5 --mode safe --color=always -o out.txt"
}

t_init_falls_back_to_dash_h() {
    fake_tools
    cd "$TEST_TMPDIR"
    rg - --init -- htool > h.conf 2>/dev/null
    f="$(cat h.conf)"
    assert_contains "$f" "read from \`htool -h\`"
    assert_contains "$f" "#quiet"
    assert_contains "$f" "#level=1|2|3"
    assert_not_contains "$f" "#help"
}

t_init_falls_back_to_the_man_page() {
    fake_tools
    cd "$TEST_TMPDIR"
    rg - --init -- mtool > m.conf 2>/dev/null
    f="$(cat m.conf)"
    assert_contains "$f" "read from \`man mtool\`"
    assert_contains "$f" "#force"
    assert_contains "$f" "#extra="
}

t_init_never_writes_over_a_file() {
    fake_tools
    cd "$TEST_TMPDIR"
    printf 'mine=1\n' > gtool.conf
    rc=0; out="$(rg gtool.conf --init -- gtool 2>&1)" || rc=$?
    assert_status "$rc" 2
    assert_contains "$out" "already exists"
    assert_eq "$(cat gtool.conf)" "mine=1"
}

t_init_with_nothing_to_read_is_exit_1() {
    fake_tools
    cd "$TEST_TMPDIR"
    rc=0; out="$(rg x.conf --init -- true 2>&1)" || rc=$?
    assert_status "$rc" 1
    assert_contains "$out" "found no flags"
    assert_no_file x.conf
    rc=0; rg x.conf --init -- no-such-command-here 2>/dev/null || rc=$?
    assert_status "$rc" 127
}

t_init_asks_the_tool_and_not_a_run_of_it() {
    # `gtool {host} deploy`: the help is gtool's, and nothing after the
    # placeholder is handed to it.
    fake_tools
    cd "$TEST_TMPDIR"
    rg - --init -- gtool '{host}' deploy >/dev/null 2>&1
    assert_eq "$(cat gtool.log)" "--help"
}

t_version_matches_the_house_format() {
    out="$(rg --version)"
    assert_contains "$out" "Copyright (C) 2026 Martin J. Gallagher"
    assert_contains "$out" "License: GPL-3.0-or-later"
}

run_test "the four rules make the command"    t_the_four_rules_make_the_command_line
run_test "a name twice is two flags"          t_a_name_written_twice_is_two_flags
run_test "quotes take a value as it is"       t_quotes_take_a_value_as_it_stands
run_test "dashes spell the flag"              t_the_dashes_say_how_a_flag_is_spelled
run_test "a value is one argument"            t_a_value_is_one_argument_whatever_is_in_it
run_test "a bad name is named by line"        t_a_bad_name_is_named_by_its_line
run_test "the file may come on stdin"         t_the_file_may_come_on_stdin
run_test "bytes reach the command unchanged"  t_bytes_in_the_file_reach_the_command_unchanged
run_test "a placeholder puts it there"        t_a_placeholder_puts_the_value_there_instead_of_a_flag
run_test "{...} places the flags"             t_the_flags_go_where_the_command_says
run_test "an unknown {name} is refused"       t_an_unknown_placeholder_is_refused_with_the_way_round
run_test "other braces are left alone"        t_braces_that_are_not_a_name_are_left_alone
run_test "a placeholder takes one value"      t_a_placeholder_needs_exactly_one_value
run_test "--set answers, replaces and adds"   t_set_answers_replaces_and_adds
run_test "--set keeps the file's spelling"    t_set_keeps_the_files_spelling
run_test "--set is an answer"                 t_set_is_an_answer_not_a_question
run_test "answers fill the command"           t_answers_fill_the_command
run_test "a letter jumps to the choice"       t_a_letter_jumps_to_the_choice
run_test "text can be edited"                 t_text_can_be_edited
run_test "an empty answer leaves it out"      t_an_empty_answer_leaves_the_flag_out
run_test "Enter waits for starred answers"    t_enter_waits_for_every_starred_answer
run_test "Esc runs nothing"                   t_esc_runs_nothing
run_test "no terminal is refused, not hung"   t_with_no_terminal_it_refuses_rather_than_waits
run_test "the questions on a real terminal"   t_the_questions_are_drawn_on_a_real_terminal
run_test "it becomes the command"             t_it_becomes_the_command
run_test "127 and 126, and dry runs"          t_a_missing_command_is_127_and_a_dry_run_is_not
run_test "no command says how"               t_without_a_command_it_says_how
run_test "no -- starts after the file"        t_without_dashes_the_command_starts_after_the_file
run_test "a missing file is refused"          t_a_missing_file_is_refused
run_test "--init comments out every flag"     t_init_writes_every_flag_commented_out
run_test "an --init file runs"                t_an_init_file_runs_once_its_hashes_are_off
run_test "--init falls back to -h"            t_init_falls_back_to_dash_h
run_test "--init falls back to man"           t_init_falls_back_to_the_man_page
run_test "--init never overwrites"            t_init_never_writes_over_a_file
run_test "--init with nothing is exit 1"      t_init_with_nothing_to_read_is_exit_1
run_test "--init asks the tool, not a run"    t_init_asks_the_tool_and_not_a_run_of_it
run_test "--version matches the house format" t_version_matches_the_house_format
finish
