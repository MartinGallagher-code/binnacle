#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# The one rm -rf agree and netmesh each send to a host, as an invariant:
# each deletes only a directory it made and marked (BN-001, after
# quarterdeck's QD-014).
#
#   agree    pushes into --remote-dir/agree.<run id>, holding .agree-owned
#            with that id, and cleanup removes that directory and nothing
#            else: never --remote-dir itself, nor another run's.
#   netmesh  deploys only into a directory it makes (and marks with
#            .netmesh-owned) or one it marked before, and `clean` removes
#            only a directory holding that mark.
#
# Each case makes a directory the real way, through the fake ssh, then
# breaks the path or the mark the way a bug could, cleans up, and checks
# that it refused, said so, and left everything -- the canaries -- where it
# was.  A stand-in rm on PATH turns any delete of something that exists
# outside the case's temp dir into a logged refusal, so even a broken guard
# (the mutation cases) cannot reach this machine's / or ~.

# Single quotes throughout keep "$AGREE_DIR", "$0" and the like for the
# host's shell, or the mutants' python, to expand -- not this one.
# shellcheck disable=SC2016

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"

PKG="$BINNACLE_DIR"

setup_host() {
    install_fake_ssh
    fake_host h
    # canaries in the host's root and beside it: nothing may touch them
    echo keep > "$FAKE_ROOT/h/canary"
    echo keep > "$FAKE_ROOT/canary"
    mkdir -p "$TEST_TMPDIR/guard"
    # the stand-in rm: deletes inside this case's temp dir; anything else
    # that exists is refused and logged, and what does not exist is
    # nothing to delete (netmesh's stop clears a pid file that may not be
    # there)
    cat > "$TEST_TMPDIR/guard/rm" <<'EOF'
#!/bin/bash
t="$(cd "$TEST_TMPDIR" && pwd -P)"
keep=()
for a in "$@"; do
    case "$a" in -*) keep+=("$a"); continue ;; esac
    d="$(cd "$(dirname -- "$a")" 2> /dev/null && pwd -P)" || d="?"
    case "$d/$(basename -- "$a")/" in
        "$t"/*) keep+=("$a") ;;
        *) if [ -e "$a" ] || [ -L "$a" ]; then
               echo "$a" >> "$TEST_TMPDIR/outside.log"; exit 1
           fi ;;
    esac
done
exec /bin/rm "${keep[@]}"
EOF
    # a sudo that runs the command as it is, and says it did
    cat > "$TEST_TMPDIR/guard/sudo" <<'EOF'
#!/bin/bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$TEST_TMPDIR/sudo.log"
exec "$@"
EOF
    chmod +x "$TEST_TMPDIR/guard/rm" "$TEST_TMPDIR/guard/sudo"
    export PATH="$TEST_TMPDIR/guard:$PATH"
    cd "$TEST_TMPDIR"
}

# ---------------------------------------------------------------------------
# agree: the Runner, driven directly
# ---------------------------------------------------------------------------

# A driver for agree.Runner on one fake host.  argv: CASE PKGDIR; prints
# what cleanup returned, so the case can assert on it.
write_agree_driver() {
    cat > "$TEST_TMPDIR/drive_agree.py" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[2])
import agree


class Args(object):
    pass


case = sys.argv[1]
a = Args()
a.ssh = os.environ["FAKE_BIN"] + "/ssh"
a.scp = os.environ["FAKE_BIN"] + "/scp"
a.user, a.timeout, a.max_output = None, 30, 1 << 20
a.remote_dir, a.pull_dir = "rd", "results"
a.sudo = os.environ.get("CASE_SUDO") == "1"
a.sudo_user = None
r = agree.Runner(a)
h = agree.Host("h", "h")
root = os.path.join(os.environ["FAKE_ROOT"], "h")
open("payload.txt", "w").write("x\n")

if case == "push-sentinel":
    open(agree.SENTINEL, "w").write("x\n")
    rc, err = r.push_files(h, [agree.SENTINEL])
    print("push rc=%d %s" % (rc, err.strip()))
    sys.exit(0)

rc, err = r.push_files(h, ["payload.txt"])
assert rc == 0, err
mine = os.path.join(root, r.work_dir)
mark = os.path.join(mine, agree.SENTINEL)
assert os.path.basename(mine) == "agree." + r.run_id, mine
assert open(mark).read() == r.run_id + "\n"
assert os.stat(mine).st_mode & 0o777 == 0o700, oct(os.stat(mine).st_mode)
open(os.path.join(root, "rd", "canary"), "w").write("keep\n")
open(os.path.join(mine, "inside"), "w").write("x\n")

if case == "empty":
    r.work_dir = ""
elif case in ("root", "tilde", "dot", "dotdot-name"):
    r.work_dir = {"root": "/", "tilde": "~", "dot": ".",
                  "dotdot-name": ".."}[case]
elif case == "remote-dir":
    # a refactor that lost the agree.<id> part: the original bug
    r.work_dir = "rd"
elif case == "dotdot":
    r.work_dir = "rd/x/../" + os.path.basename(r.work_dir)
    os.makedirs(os.path.join(root, "rd", "x"))
elif case == "other-run":
    # an agree.* that is another run's, with its own mark
    other = os.path.join(root, "rd", "agree.0123456789ab")
    os.makedirs(other)
    open(os.path.join(other, agree.SENTINEL), "w").write("0123456789ab\n")
    r.work_dir = "rd/agree.0123456789ab"
elif case == "symlink":
    # the run's directory swapped for a link to one that even holds a
    # matching mark: the link is refused, not followed
    target = os.path.join(root, "elsewhere")
    os.makedirs(target)
    open(os.path.join(target, agree.SENTINEL), "w").write(r.run_id + "\n")
    open(os.path.join(target, "canary"), "w").write("keep\n")
    os.rename(mine, os.path.join(root, "moved"))
    os.symlink(target, mine)
elif case == "no-sentinel":
    os.remove(mark)
elif case == "wrong-id":
    open(mark, "w").write("ffffffffffff\n")
elif case == "sentinel-link":
    os.remove(mark)
    open(os.path.join(root, "fake-mark"), "w").write(r.run_id + "\n")
    os.symlink(os.path.join(root, "fake-mark"), mark)
elif case == "not-a-dir":
    os.rename(mine, os.path.join(root, "moved"))
    open(mine, "w").write("a file\n")
elif case != "normal":
    sys.exit("no such case: " + case)

print("RESULT " + r.cleanup(h))
EOF
}

drive_agree() { "$PY" "$TEST_TMPDIR/drive_agree.py" "$1" "${2:-$PKG}"; }

# what every agree refusal leaves: the run's directory, the canaries, and
# no delete outside the case
agree_untouched() {
    local root="$FAKE_ROOT/h" mine d
    assert_eq "$(cat "$root/canary")" "keep"
    assert_eq "$(cat "$FAKE_ROOT/canary")" "keep"
    assert_eq "$(cat "$root/rd/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
    mine=""
    for d in "$root"/moved "$root"/rd/agree.*; do
        [ -e "$d" ] && { mine="$d"; break; }
    done
    [ -n "$mine" ] || fail "the run's directory is gone"
    assert_file_exists "$mine/inside"
}

agree_refused() {
    setup_host
    write_agree_driver
    out="$(drive_agree "$1")"
    assert_contains "$out" "RESULT could not remove"
    assert_contains "$out" "agree: refusing to remove"
    [ -z "${2:-}" ] || assert_contains "$out" "$2"
    agree_untouched
}

t_agree_cleanup_removes_only_the_run() {
    setup_host
    write_agree_driver
    out="$(drive_agree normal)"
    assert_contains "$out" "RESULT "
    assert_not_contains "$out" "agree:"
    assert_eq "$(ls -A "$FAKE_ROOT/h/rd")" "canary"
    assert_eq "$(cat "$FAKE_ROOT/h/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
}

t_agree_empty_path() { agree_refused empty "not this run's"; }
t_agree_root() { agree_refused root "not this run's"; }
t_agree_tilde() { agree_refused tilde "not this run's"; }
t_agree_dot() { agree_refused dot "not this run's"; }
t_agree_dotdot_name() { agree_refused dotdot-name "not this run's"; }
t_agree_remote_dir_itself() { agree_refused remote-dir "not this run's"; }
t_agree_dotdot_in_path() { agree_refused dotdot "has a .. in it"; }
t_agree_symlink() {
    agree_refused symlink "it is a symlink"
    assert_eq "$(cat "$FAKE_ROOT/h/elsewhere/canary")" "keep"
    [ -L "$(ls -d "$FAKE_ROOT"/h/rd/agree.*)" ] || fail "the link went"
}
t_agree_no_sentinel() { agree_refused no-sentinel "holds no .agree-owned"; }
t_agree_wrong_run_id() { agree_refused wrong-id "another run's"; }
t_agree_sentinel_is_a_link() {
    agree_refused sentinel-link "holds no .agree-owned"
    assert_file_exists "$FAKE_ROOT/h/fake-mark"
}
t_agree_not_a_directory() { agree_refused not-a-dir "not a directory"; }

t_agree_another_runs_directory() {
    setup_host
    write_agree_driver
    out="$(drive_agree other-run)"
    assert_contains "$out" "not this run's"
    assert_file_exists "$FAKE_ROOT/h/rd/agree.0123456789ab/.agree-owned"
    assert_no_file "$TEST_TMPDIR/outside.log"
}

t_agree_pushed_file_cannot_be_the_mark() {
    setup_host
    write_agree_driver
    out="$(drive_agree push-sentinel)"
    assert_contains "$out" "push rc=1"
    assert_contains "$out" "cannot push a file named .agree-owned"
}

# --sudo: the checks run under the same sudo as the delete
t_agree_under_sudo() {
    setup_host
    write_agree_driver
    out="$(CASE_SUDO=1 drive_agree wrong-id)"
    assert_contains "$out" "another run's"
    assert_contains "$(cat "$TEST_TMPDIR/sudo.log")" "refusing to remove"
    agree_untouched
    rm -f "$TEST_TMPDIR/sudo.log"
    rm -rf "$FAKE_ROOT/h/rd"
    out="$(CASE_SUDO=1 drive_agree normal)"
    assert_not_contains "$out" "agree:"
    assert_contains "$(cat "$TEST_TMPDIR/sudo.log")" ".agree-owned"
    assert_eq "$(ls -A "$FAKE_ROOT/h/rd")" "canary"
}

# ---------------------------------------------------------------------------
# agree: whole runs, through the command line
# ---------------------------------------------------------------------------

agv() {
    local verb="$1"; shift
    "$PY" "$PKG/agree.py" "$verb" --ssh "$FAKE_BIN/ssh" \
        --scp "$FAKE_BIN/scp" "$@"
}

# The bug itself: --remote-dir already holding files -- ~/bin, say -- and
# one of them even named like the file pushed.  The run works, and every
# one of them is still there afterwards.
scenario_agree_remote_dir_survives() {
    setup_host
    mkdir -p "$FAKE_ROOT/h/rd"
    echo theirs > "$FAKE_ROOT/h/rd/theirs"
    echo theirs > "$FAKE_ROOT/h/rd/s.sh"
    printf '#!/bin/sh\necho "ran from $(dirname "$0")"\n' > s.sh
    out="$(agv script ./s.sh -S h --quiet --remote-dir rd --)"
    assert_contains "$out" "ran from rd/agree."
    out="$(agv run -S h --quiet --remote-dir rd --push s.sh \
             -- '"$AGREE_DIR"/s.sh')"
    assert_contains "$out" "ran from rd/agree."
    assert_eq "$(cat "$FAKE_ROOT/h/rd/theirs")" "theirs"
    assert_eq "$(cat "$FAKE_ROOT/h/rd/s.sh")" "theirs"
    # shellcheck disable=SC2012  # two plain names, and the order matters
    assert_eq "$(ls -A "$FAKE_ROOT/h/rd" | tr '\n' ' ')" "s.sh theirs "
    assert_no_file "$TEST_TMPDIR/outside.log"
}
t_agree_remote_dir_with_other_files_survives() {
    scenario_agree_remote_dir_survives
}

# Two runs at once on one host and one --remote-dir: the first finishes
# and cleans up while the second is still running, and the second still
# finds its own files.  With one shared directory, the first run's cleanup
# deleted the second's script and payload out from under it.
scenario_agree_two_runs() {
    setup_host
    for n in 1 2; do
        mkdir -p "r$n"
        printf '#!/bin/sh\nsleep %s\ncat "$(dirname "$0")/payload"\n' \
            "$n" > "r$n/s.sh"
        echo "payload-$n" > "r$n/payload"
    done
    (cd r1 && agv run -S h --quiet --remote-dir rd --push s.sh \
        --push payload -- '"$AGREE_DIR"/s.sh' > ../out1 2>&1) &
    p1=$!
    sleep 0.5
    (cd r2 && agv run -S h --quiet --remote-dir rd --push s.sh \
        --push payload -- '"$AGREE_DIR"/s.sh' > ../out2 2>&1) &
    p2=$!
    rc1=0; wait "$p1" || rc1=$?
    rc2=0; wait "$p2" || rc2=$?
    assert_contains "$(cat out1)" "payload-1"
    assert_contains "$(cat out2)" "payload-2"
    assert_status "$rc1" 0
    assert_status "$rc2" 0
    # both runs' directories are gone, and nothing else was touched
    assert_eq "$(ls -A "$FAKE_ROOT/h/rd" 2> /dev/null)" ""
    assert_eq "$(cat "$FAKE_ROOT/h/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
}
t_agree_two_runs_at_once_keep_each_others_files() {
    scenario_agree_two_runs
}

t_agree_made_remote_dir_goes_when_empty() {
    setup_host
    printf '#!/bin/sh\necho ok\n' > s.sh
    out="$(agv script ./s.sh -S h --quiet --remote-dir rd/deeper --)"
    assert_contains "$out" "ok"
    # the run made rd/deeper, so it goes again; rd, which mkdir -p made on
    # the way, is left: only the directory named is the run's to remove
    assert_no_file "$FAKE_ROOT/h/rd/deeper"
}

t_agree_a_refused_cleanup_is_reported_and_fails_the_run() {
    setup_host
    # the command itself spoils its run's mark: cleanup must refuse
    printf '#!/bin/sh\necho ffffffffffff > "$AGREE_DIR/.agree-owned"\necho done\n' \
        > s.sh
    set +e
    out="$(agv script ./s.sh -S h --remote-dir rd -- 2>&1)"
    rc=$?
    set -e
    assert_status "$rc" 3
    assert_contains "$out" "NOT CLEANED UP"
    assert_contains "$out" "could not remove rd/agree."
    assert_contains "$out" "another run's"
    assert_contains "$out" "still on h"
    # the answer itself is not moved out of its group
    assert_contains "$out" "1 group"
    ls -d "$FAKE_ROOT"/h/rd/agree.* > /dev/null || fail "the directory went"
}

t_agree_keep_remote_says_where() {
    setup_host
    printf '#!/bin/sh\necho ok\n' > s.sh
    out="$(agv script ./s.sh -S h --quiet --remote-dir rd --keep-remote \
             -- 2>&1)"
    assert_contains "$out" "--keep-remote: what is pushed stays in rd/agree."
    ls "$FAKE_ROOT"/h/rd/agree.*/s.sh > /dev/null || fail "nothing was kept"
    ls "$FAKE_ROOT"/h/rd/agree.*/.agree-owned > /dev/null \
        || fail "the mark was not kept"
}

t_agree_pull_reads_the_runs_directory() {
    setup_host
    printf '#!/bin/sh\necho result > "$AGREE_DIR/out.csv"\necho ok\n' > s.sh
    out="$(agv script ./s.sh -S h --quiet --remote-dir rd \
             --pull out.csv --pull-dir got --)"
    assert_contains "$out" "ok"
    assert_eq "$(cat got/h/out.csv)" "result"
    assert_no_file "$FAKE_ROOT/h/rd"
}

t_agree_dry_run_shows_the_runs_directory() {
    setup_host
    printf '#!/bin/sh\necho ok\n' > s.sh
    out="$(agv script ./s.sh -S h --remote-dir rd --dry-run -- --flag)"
    assert_contains "$out" "AGREE_DIR=rd/agree."
    assert_contains "$out" '"$AGREE_DIR"/s.sh --flag'
    assert_eq "$(ssh_calls)" "0"
}

t_agree_sudo_cleans_up_under_sudo() {
    setup_host
    printf '#!/bin/sh\necho ok\n' > s.sh
    out="$(agv script ./s.sh -S h --quiet --sudo --remote-dir rd --)"
    assert_contains "$out" "ok"
    log="$(cat "$TEST_TMPDIR/sudo.log")"
    assert_contains "$log" "sudo rd/agree."
    assert_contains "$log" "sh -c ( w=rd/agree."
    assert_no_file "$FAKE_ROOT/h/rd"
}

# ---------------------------------------------------------------------------
# netmesh: deploy and clean, through the command line
# ---------------------------------------------------------------------------

nmv() {
    local verb="$1"; shift
    "$PY" "$PKG/netmesh.py" "$verb" --mesh m.csv --ssh "$FAKE_BIN/ssh" \
        --scp "$FAKE_BIN/scp" "$@"
}

nm_mesh() {
    "$PY" "$PKG/netmesh.py" gen h=h g=g --mesh m.csv --pps 5 --no-pmtu \
        > /dev/null
    fake_host g
}

# Deploy the real way.  `false` for the agent's interpreter: the
# directory is made and filled, and no agent starts to be cleaned up.
nm_deploy() {
    set +e
    nmv start --remote-dir "$1" --python false > "$TEST_TMPDIR/start.out" 2>&1
    set -e
}

# clean, and expect it to refuse on h with WHY
nm_clean_refused() {
    set +e
    out="$(nmv clean --remote-dir "$1" 2>&1)"
    rc=$?
    set -e
    assert_status "$rc" 1
    assert_contains "$out" "netmesh: refusing to remove"
    assert_contains "$out" "$2"
    assert_contains "$out" "not removed"
    assert_eq "$(cat "$FAKE_ROOT/h/canary")" "keep"
    assert_eq "$(cat "$FAKE_ROOT/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
}

t_netmesh_deploy_marks_and_clean_removes() {
    setup_host
    nm_mesh
    nm_deploy nm
    assert_contains "$(cat "$FAKE_SSH_LOG")" "scp h nm/"
    id="$(cat "$FAKE_ROOT/h/nm/.netmesh-owned")"
    [ "${#id}" -eq 12 ] || fail "the mark holds '$id', not a deploy id"
    assert_file_exists "$FAKE_ROOT/h/nm/netmesh.py"
    # a second deploy uses the marked directory again, mark and all
    nm_deploy nm
    assert_eq "$(cat "$FAKE_ROOT/h/nm/.netmesh-owned")" "$id"
    out="$(nmv clean --remote-dir nm 2>&1)"
    assert_contains "$out" "cleaned"
    assert_no_file "$FAKE_ROOT/h/nm"
    assert_no_file "$FAKE_ROOT/g/nm"
    assert_eq "$(cat "$FAKE_ROOT/h/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
}

# The bug itself: --remote-dir an existing directory with someone's files
# in it.  netmesh will not deploy into it, and `clean` will not remove it.
scenario_netmesh_foreign_dir() {
    setup_host
    nm_mesh
    mkdir -p "$FAKE_ROOT/h/bin" "$FAKE_ROOT/g/bin"
    echo theirs > "$FAKE_ROOT/h/bin/tool"
    echo theirs > "$FAKE_ROOT/h/bin/mesh.csv"
    set +e
    out="$(nmv start --remote-dir bin --python false --keep-going 2>&1)"
    rc=$?
    set -e
    assert_status "$rc" 1
    assert_contains "$out" "refusing to deploy into bin"
    # nothing copied in, nothing overwritten, and no agent started there
    assert_no_file "$FAKE_ROOT/h/bin/netmesh.py"
    assert_eq "$(cat "$FAKE_ROOT/h/bin/mesh.csv")" "theirs"
    assert_not_contains "$(cat "$FAKE_SSH_LOG")" "nohup"
    nm_clean_refused bin "holds no .netmesh-owned"
    assert_eq "$(cat "$FAKE_ROOT/h/bin/tool")" "theirs"
    assert_eq "$(cat "$FAKE_ROOT/h/bin/mesh.csv")" "theirs"
}
t_netmesh_a_directory_it_did_not_make_is_left_alone() {
    scenario_netmesh_foreign_dir
}

t_netmesh_keep_going_starts_only_where_it_deployed() {
    setup_host
    nm_mesh
    mkdir -p "$FAKE_ROOT/g/nm"
    echo theirs > "$FAKE_ROOT/g/nm/theirs"
    set +e
    out="$(nmv start --remote-dir nm --python false --keep-going 2>&1)"
    set -e
    assert_contains "$out" "refusing to deploy into nm"
    assert_contains "$out" "0/2 agents running"
    # an agent was started on h, and only there: g's directory holds only
    # what was in it
    assert_file_exists "$FAKE_ROOT/h/nm/agent.pid"
    assert_eq "$(grep -c nohup "$FAKE_SSH_LOG")" "1"
    assert_eq "$(ls -A "$FAKE_ROOT/g/nm")" "theirs"
}

# A mark-shaped name with bad paths: each refused, canaries kept.
t_netmesh_root() { setup_host; nm_mesh; nm_clean_refused / "not a directory netmesh would make"; }
t_netmesh_dot() { setup_host; nm_mesh; nm_clean_refused . "not a directory netmesh would make"; }
t_netmesh_dotdot() { setup_host; nm_mesh; nm_clean_refused .. "not a directory netmesh would make"; }

t_netmesh_empty() {
    setup_host
    nm_mesh
    set +e
    out="$(nmv clean --remote-dir '' 2>&1)"
    set -e
    assert_contains "$out" "not a directory netmesh would make"
    assert_eq "$(cat "$FAKE_ROOT/h/canary")" "keep"
    assert_no_file "$TEST_TMPDIR/outside.log"
}

t_netmesh_home() {
    # the host's home itself, by its absolute path: never marked by netmesh
    setup_host
    nm_mesh
    nm_clean_refused "$FAKE_ROOT/h" "holds no .netmesh-owned"
}

t_netmesh_tilde() {
    # "~" reaches the host as a name, not as the home directory; with a
    # directory of that name there, it is still not netmesh's
    setup_host
    nm_mesh
    mkdir "$FAKE_ROOT/h/~"
    echo keep > "$FAKE_ROOT/h/~/canary"
    nm_clean_refused '~' "holds no .netmesh-owned"
    assert_eq "$(cat "$FAKE_ROOT/h/~/canary")" "keep"
}

t_netmesh_dotdot_in_path() {
    setup_host
    nm_mesh
    nm_deploy nm
    mkdir -p "$FAKE_ROOT/h/x"
    nm_clean_refused x/../nm "has a .. in it"
    assert_file_exists "$FAKE_ROOT/h/nm/.netmesh-owned"
}

t_netmesh_symlink() {
    # the directory swapped for a link to one that even holds a mark
    setup_host
    nm_mesh
    nm_deploy nm
    mv "$FAKE_ROOT/h/nm" "$FAKE_ROOT/h/elsewhere"
    echo keep > "$FAKE_ROOT/h/elsewhere/canary"
    ln -s elsewhere "$FAKE_ROOT/h/nm"
    nm_clean_refused nm "it is a symlink"
    [ -L "$FAKE_ROOT/h/nm" ] || fail "the link went"
    assert_eq "$(cat "$FAKE_ROOT/h/elsewhere/canary")" "keep"
}

t_netmesh_no_sentinel() {
    setup_host
    nm_mesh
    nm_deploy nm
    rm "$FAKE_ROOT/h/nm/.netmesh-owned"
    echo keep > "$FAKE_ROOT/h/nm/canary"
    nm_clean_refused nm "holds no .netmesh-owned"
    assert_eq "$(cat "$FAKE_ROOT/h/nm/canary")" "keep"
}

t_netmesh_not_its_mark() {
    # the "wrong id" of a directory whose deploy id clean cannot know: a
    # mark that is not one netmesh writes
    setup_host
    nm_mesh
    nm_deploy nm
    echo "not an id" > "$FAKE_ROOT/h/nm/.netmesh-owned"
    echo keep > "$FAKE_ROOT/h/nm/canary"
    nm_clean_refused nm "is not netmesh's"
    assert_eq "$(cat "$FAKE_ROOT/h/nm/canary")" "keep"
}

t_netmesh_sentinel_is_a_link() {
    setup_host
    nm_mesh
    nm_deploy nm
    mv "$FAKE_ROOT/h/nm/.netmesh-owned" "$FAKE_ROOT/h/fake-mark"
    ln -s ../fake-mark "$FAKE_ROOT/h/nm/.netmesh-owned"
    nm_clean_refused nm "holds no .netmesh-owned"
    assert_file_exists "$FAKE_ROOT/h/fake-mark"
    assert_file_exists "$FAKE_ROOT/h/nm/netmesh.py"
}

t_netmesh_not_a_directory() {
    setup_host
    nm_mesh
    echo "a file" > "$FAKE_ROOT/h/nm"
    nm_clean_refused nm "not a directory"
    assert_eq "$(cat "$FAKE_ROOT/h/nm")" "a file"
}

t_netmesh_refuses_to_deploy_into_a_symlink() {
    setup_host
    nm_mesh
    mkdir -p "$FAKE_ROOT/h/real"
    echo keep > "$FAKE_ROOT/h/real/canary"
    echo 0123456789ab > "$FAKE_ROOT/h/real/.netmesh-owned"
    ln -s real "$FAKE_ROOT/h/nm"
    set +e
    out="$(nmv start --remote-dir nm --python false 2>&1)"
    set -e
    assert_contains "$out" "refusing to deploy into nm"
    assert_no_file "$FAKE_ROOT/h/real/netmesh.py"
}

# ---------------------------------------------------------------------------
# The suite can fail
# ---------------------------------------------------------------------------

# mutant PATCH: a copy of the package with one change made to it, in
# $TEST_TMPDIR/mut.  PATCH is python run on the copy's directory; it must
# exit non-zero if the change no longer applies, so a refactor cannot leave
# a mutation that tests nothing.
mutant() {
    rm -rf "$TEST_TMPDIR/mut"
    mkdir -p "$TEST_TMPDIR/mut"
    cp "$BINNACLE_DIR"/*.py "$TEST_TMPDIR/mut/"
    "$PY" - "$TEST_TMPDIR/mut" <<<"$1" || fail "the mutation no longer applies"
    # a mutant that does not even load fails every case for the wrong reason
    for f in "$TEST_TMPDIR"/mut/*.py; do
        "$PY" -m py_compile "$f" || fail "the mutant $f does not compile"
    done
}

# expect_caught SCENARIO: the scenario, run against the mutant, fails
expect_caught() {
    set +e
    (PKG="$TEST_TMPDIR/mut"; set -e; "$1") > "$TEST_TMPDIR/mutant.out" 2>&1
    rc=$?
    set -e
    [ "$rc" -ne 0 ] || fail "the mutant passed $1: the suite cannot see the check go"
}

t_mutation_agree_without_the_id_check_fails() {
    setup_host
    mutant '
import sys
p = sys.argv[1] + "/agree.py"
s = open(p).read()
cut = "\"[ \\\"$(cat %s)\\\" = %s ] || %s; \""
args = "s, rid, refuse(\"its %s is another run\x27s\" % SENTINEL))"
if cut not in s or args not in s:
    sys.exit("gone")
s = s.replace(cut, "").replace(args, ")")
s = s.replace("s, s, refuse(\"it holds no %s\" % SENTINEL),",
              "s, s, refuse(\"it holds no %s\" % SENTINEL)")
open(p, "w").write(s)
'
    write_agree_driver
    out="$(drive_agree wrong-id "$TEST_TMPDIR/mut")"
    assert_not_contains "$out" "refusing"
    if (agree_untouched) 2> /dev/null; then
        fail "the mutant kept the directory: the suite cannot see the check go"
    fi
}

t_mutation_agree_removing_remote_dir_fails() {
    # the bug as it was: cleanup removes --remote-dir itself
    mutant '
import sys
p = sys.argv[1] + "/agree.py"
s = open(p).read()
old = "cmd = guarded_rm(self.work_dir, self.run_id)"
if old not in s:
    sys.exit("gone")
s = s.replace(old, "cmd = \"rm -rf %s\" % shlex.quote(self.remote_dir)")
open(p, "w").write(s)
'
    expect_caught scenario_agree_remote_dir_survives
    assert_contains "$(cat "$TEST_TMPDIR/mutant.out")" "assertion failed"
    expect_caught scenario_agree_two_runs
    assert_contains "$(cat "$TEST_TMPDIR/mutant.out")" "assertion failed"
}

t_mutation_netmesh_without_the_mark_check_fails() {
    # the mark checks made always true, each keeping its arguments
    mutant '
import sys
p = sys.argv[1] + "/netmesh.py"
s = open(p).read()
for old, new in (
        ("\"[ -f %s ] && [ ! -L %s ] || %s; \"", "\"[ -n %s%s ] || %s; \""),
        ("in \x27\x27|*[!0-9a-f]*) %s ;; esac; \"", "in x) %s ;; esac; \""),
        ("\"[ ${#id} -eq 12 ] || %s; \"", "\"[ -n x ] || %s; \"")):
    if s.count(old) != 1:
        sys.exit("gone: " + old)
    s = s.replace(old, new)
open(p, "w").write(s)
'
    expect_caught scenario_netmesh_foreign_dir
    assert_contains "$(cat "$TEST_TMPDIR/mutant.out")" "assertion failed"
}

echo "cleanup"
run_test "agree: cleanup removes the run's directory, nothing else" t_agree_cleanup_removes_only_the_run
run_test "agree refuses: an empty work dir"           t_agree_empty_path
run_test "agree refuses: /"                           t_agree_root
run_test "agree refuses: ~"                           t_agree_tilde
run_test "agree refuses: ."                           t_agree_dot
run_test "agree refuses: .."                          t_agree_dotdot_name
run_test "agree refuses: --remote-dir itself"         t_agree_remote_dir_itself
run_test "agree refuses: a .. in the path"            t_agree_dotdot_in_path
run_test "agree refuses: a symlinked work dir"        t_agree_symlink
run_test "agree refuses: no sentinel"                 t_agree_no_sentinel
run_test "agree refuses: another run's id"            t_agree_wrong_run_id
run_test "agree refuses: a sentinel that is a link"   t_agree_sentinel_is_a_link
run_test "agree refuses: not a directory"             t_agree_not_a_directory
run_test "agree refuses: another run's agree.* dir"   t_agree_another_runs_directory
run_test "agree: a pushed file cannot be the mark"    t_agree_pushed_file_cannot_be_the_mark
run_test "agree: under --sudo the checks run under sudo" t_agree_under_sudo
run_test "agree: --remote-dir's own files survive a run" t_agree_remote_dir_with_other_files_survives
run_test "agree: two runs at once keep each other's files" t_agree_two_runs_at_once_keep_each_others_files
run_test "agree: a --remote-dir it made goes when empty" t_agree_made_remote_dir_goes_when_empty
run_test "agree: a refused cleanup is reported, exit 3" t_agree_a_refused_cleanup_is_reported_and_fails_the_run
run_test "agree: --keep-remote says where"            t_agree_keep_remote_says_where
run_test "agree: --pull reads the run's directory"    t_agree_pull_reads_the_runs_directory
run_test "agree: --dry-run shows the run's directory" t_agree_dry_run_shows_the_runs_directory
run_test "agree: --sudo cleans up under sudo"         t_agree_sudo_cleans_up_under_sudo
run_test "netmesh: deploy marks, clean removes"       t_netmesh_deploy_marks_and_clean_removes
run_test "netmesh: a directory it did not make is left alone" t_netmesh_a_directory_it_did_not_make_is_left_alone
run_test "netmesh: --keep-going starts only where it deployed" t_netmesh_keep_going_starts_only_where_it_deployed
run_test "netmesh refuses: /"                         t_netmesh_root
run_test "netmesh refuses: ."                         t_netmesh_dot
run_test "netmesh refuses: .."                        t_netmesh_dotdot
run_test "netmesh refuses: an empty path"             t_netmesh_empty
run_test "netmesh refuses: the home directory"        t_netmesh_home
run_test "netmesh refuses: ~"                         t_netmesh_tilde
run_test "netmesh refuses: a .. in the path"          t_netmesh_dotdot_in_path
run_test "netmesh refuses: a symlinked directory"     t_netmesh_symlink
run_test "netmesh refuses: no sentinel"               t_netmesh_no_sentinel
run_test "netmesh refuses: a mark that is not its own" t_netmesh_not_its_mark
run_test "netmesh refuses: a sentinel that is a link" t_netmesh_sentinel_is_a_link
run_test "netmesh refuses: not a directory"           t_netmesh_not_a_directory
run_test "netmesh: will not deploy through a symlink" t_netmesh_refuses_to_deploy_into_a_symlink
run_test "mutation: agree without the id check fails" t_mutation_agree_without_the_id_check_fails
run_test "mutation: agree removing --remote-dir fails" t_mutation_agree_removing_remote_dir_fails
run_test "mutation: netmesh without the mark check fails" t_mutation_netmesh_without_the_mark_check_fails
finish
