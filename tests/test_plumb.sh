#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher

# plumb: the judgements from fact files, the comparison against real files.
#
# Same split as test_skew.sh.  Every rule is a pure function of the fact
# dict, so --from-facts drives the whole diagnostic surface with no disk
# being read.  The comparison cannot be tested that way, and a page cache
# that disagrees with its disk cannot be made on demand without root and
# a loop device -- so --disk-view stands in for the disk: the cache side
# is the real file, read the way every program reads it, and the disk side
# is a twin of it under a directory the test controls and edits.
#
# Every fact-layer case runs against a /proc and /sys of its own, so the
# test machine's kernel log, EDAC counters and taint cannot leak into a
# report these cases assert about -- the lesson test_skew.sh learned from
# a CI runner's hardware clock.

set -u
source "$(dirname "${BASH_SOURCE[0]}")/test_helper.bash"
PL="$BINNACLE_DIR/plumb.py"

# A quiet box: booted an hour ago (so files made by a test were written
# since boot), untainted, no modules, no EDAC, no md, no dpkg database.
# BTIME moves boot into the future, which makes every file predate it.
plumb_root() {
    local p="$TEST_TMPDIR/proc"
    mkdir -p "$p/self" "$p/sys/kernel" "$TEST_TMPDIR/sys" "$TEST_TMPDIR/dpkg"
    printf 'cpu  1 2 3 4\nbtime %s\n' "${BTIME:-$(( $(date +%s) - 3600 ))}" \
        > "$p/stat"
    echo 6.12.0-test > "$p/sys/kernel/osrelease"
    echo 0 > "$p/sys/kernel/tainted"
    : > "$p/modules"
}

pl_exec() {
    [ -d "$TEST_TMPDIR/proc" ] || plumb_root
    "$PY" "$PL" --proc-root "$TEST_TMPDIR/proc" --sys-root "$TEST_TMPDIR/sys" \
        --dpkg-info "$TEST_TMPDIR/dpkg" "$@"
}
pl() { pl_exec --no-exec "$@"; }

# mkfile PATH KIB [PATTERN] -- a file of KIB kibibytes.  The default
# pattern has no zero bytes, so zeros in a difference mean something.
mkfile() {
    "$PY" - "$1" "$2" "${3:-nz}" <<'EOF'
import sys
path, kib, pat = sys.argv[1], int(sys.argv[2]), sys.argv[3]
unit = bytes(range(1, 256)) if pat == "nz" else bytes(range(256))
data = (unit * (kib * 1024 // len(unit) + 1))[:kib * 1024]
with open(path, "wb") as f:
    f.write(data)
EOF
}

# twin FILE -- a copy where --disk-view looks for FILE's disk side.
twin() {
    mkdir -p "$(dirname "$(dv "$1")")"
    cp "$1" "$(dv "$1")"
}

# poke FILE OFFSET HEX -- overwrite bytes in place.
poke() {
    "$PY" - "$1" "$2" "$3" <<'EOF'
import sys
path, off, hexs = sys.argv[1], int(sys.argv[2], 0), sys.argv[3]
with open(path, "r+b") as f:
    f.seek(off)
    f.write(bytes(bytearray.fromhex(hexs)))
EOF
}

# zero FILE OFFSET LENGTH
zero() {
    "$PY" - "$1" "$2" "$3" <<'EOF'
import sys
path, off, n = sys.argv[1], int(sys.argv[2], 0), int(sys.argv[3], 0)
with open(path, "r+b") as f:
    f.seek(off)
    f.write(bytes(n))
EOF
}

# mkelf PATH -- a small ELF64 program whose entry point is at file offset
# 0x1000, filled with 0xcc so no byte of it is zero by accident.
mkelf() {
    "$PY" - "$1" <<'EOF'
import struct, sys
size = 8192
hdr = b"\x7fELF" + bytes(bytearray([2, 1, 1, 0])) + bytes(8)
hdr += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0x401000, 64, 0, 0, 64, 56,
                   1, 0, 0, 0)
ph = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, size, size,
                 0x1000)
data = hdr + ph
data += b"\xcc" * (size - len(data))
with open(sys.argv[1], "wb") as f:
    f.write(data)
EOF
}

facts_of() { pl "$@" --facts; }

# real PATH -- where a path really is.  plumb works on real paths, so a
# twin, a package record or a fake mount has to name the same one even
# when the temp directory sits behind a symlink.
real() { "$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

# dv FILE -- where --disk-view looks for FILE's disk side.
dv() { printf '%s/dv%s\n' "$TEST_TMPDIR" "$(real "$1")"; }

# flat TEXT -- a report with its wrapping undone, so a phrase can be found
# whichever line the wrap put it on.
flat() { printf '%s' "$1" | tr -s ' \n' ' '; }

# plf ARGS -- a --from-facts run, flattened.
plf() { flat "$("$PY" "$PL" "$@")"; }

# jget JSON-TEXT PYTHON-EXPR -- evaluate EXPR against the parsed facts as f.
jget() {
    printf '%s' "$1" | "$PY" -c 'import json, sys
f = json.load(sys.stdin)
print(eval(sys.argv[1]))' "$2"
}

# --- the rules, from facts -------------------------------------------------

t_agreement_says_so_plainly() {
    plumb_facts_json "$TEST_TMPDIR/f.json"
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "agree on all 3 files compared"
    assert_not_contains "$out" "CRITICAL"
    assert_not_contains "$out" "DIFFERENCES"
}

t_a_privileged_file_altered_in_memory_is_tampering() {
    # The disk matches what the package installed and the cache does not:
    # that settles which copy is wrong, and on a setuid program a few
    # bytes rewritten at the entry point is the 2026 bug class exactly.
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"pkg":{"manager":"dpkg","name":"passwd","algo":"md5","match":"disk"}}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    csv="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$csv" "CACHE_TAMPERED,CRITICAL"
    verdict="${out#*VERDICT}"; verdict="${verdict%%CRITICAL*}"
    assert_contains "$verdict" "altered in memory"
    assert_contains "$verdict" "compromised"
    assert_contains "$out" "the program's entry point"
    assert_contains "$out" "the disk matches the md5 passwd records"
    assert_contains "$out" "Copy Fail"
}

t_a_file_not_written_since_boot_puts_the_blame_on_the_cache() {
    # No package to ask -- but its cache was filled from this disk after
    # boot, so a cache that disagrees changed in memory afterwards.
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"written_since_boot":false}'
    csv="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$csv" "CACHE_TAMPERED,CRITICAL"
    assert_not_contains "$csv" "CACHE_DISK_DIFFER"
}

t_the_entry_points_present_are_named() {
    plumb_facts_json "$TEST_TMPDIR/loaded.json" \
        '+differ={"written_since_boot":false}' \
        'ctx.modules={"algif_aead":"built in","esp4":"loaded","esp6":"available","rxrpc":"absent","xt_TEE":"absent","act_pedit":"absent"}'
    out="$(plf --from-facts "$TEST_TMPDIR/loaded.json")"
    assert_contains "$out" "algif_aead built in (Copy Fail)"
    assert_contains "$out" "esp4 loaded (Dirty Frag, Fragnesia, DirtyClone)"
    # With none loaded, the ones an exploit could load are named instead.
    plumb_facts_json "$TEST_TMPDIR/none.json" \
        '+differ={"written_since_boot":false}'
    out="$(plf --from-facts "$TEST_TMPDIR/none.json")"
    assert_contains "$out" "loaded on demand"
}

t_the_same_bytes_with_no_evidence_are_not_called_tampering() {
    # Written since boot and unknown to any package: nothing can say which
    # copy is wrong, and the report must not guess -- it names the shape
    # and leaves the side open.
    plumb_facts_json "$TEST_TMPDIR/f.json" '+differ={}'
    csv="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$csv" "CACHE_DISK_DIFFER,CRITICAL"
    assert_not_contains "$csv" "CACHE_TAMPERED"
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "nothing here can say which copy is right"
    assert_contains "$out" "shape of a page-cache write"
}

t_the_disk_copy_wrong_is_the_urgent_one() {
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"path":"/usr/lib/libfoo.so.1","role":"file","mode":"644","elf":null,"pkg":{"manager":"rpm","name":"foo-libs","algo":"sha256","match":"cache"}}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    verdict="${out#*VERDICT}"; verdict="${verdict%%CRITICAL*}"
    assert_contains "$verdict" "wrong on disk and right in memory"
    assert_contains "$verdict" "only good copy"
    assert_contains "$out" "do not reboot, drop caches or unmount"
    assert_contains "$out" "reinstalling foo-libs"
    assert_contains "$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)" \
        "DISK_WRONG,CRITICAL"
}

t_zeros_on_disk_are_a_lost_write_and_the_kernel_is_named() {
    # The Fedora CoreOS shape: written since boot, the disk holds zeros
    # where the cache holds what was written, ext4 on a kernel in the
    # reported range.
    for k in 7.1.8-200.fc44.ppc64le 7.1.11-200.fc44.x86_64; do
        plumb_facts_json "$TEST_TMPDIR/$k.json" "sys.kernel=$k" \
            '+differ={"path":"/boot/vmlinuz-7.1.10","role":"boot","mode":"644","elf":null,"diff":{"ranges":[[2621440,4194304]],"bytes":1572864,"disk_zero":true,"page_aligned":true}}'
    done
    out="$(plf --from-facts "$TEST_TMPDIR/7.1.8-200.fc44.ppc64le.json")"
    assert_contains "$out" "never reached the disk"
    assert_contains "$out" "the disk reads zeros where the cache holds data"
    assert_contains "$out" "fixed upstream in 7.1.11"
    out="$(plf --from-facts "$TEST_TMPDIR/7.1.11-200.fc44.x86_64.json")"
    assert_contains "$out" "never reached the disk"
    assert_not_contains "$out" "fixed upstream"
}

t_one_flipped_bit_is_blamed_on_memory() {
    plumb_facts_json "$TEST_TMPDIR/ecc.json" 'ctx.edac={"controllers":2,"ce":5,"ue":0}' \
        '+differ={"path":"/srv/data.db","role":"file","elf":null,"written_since_boot":false,"diff":{"bytes":1,"single_bit":true,"ranges":[[4096,4097]]}}'
    out="$(plf --from-facts "$TEST_TMPDIR/ecc.json")"
    assert_contains "$out" "CRITICAL corrupted in memory"
    assert_contains "$out" "logged 5 corrected"
    assert_contains "$out" "WARN memory reporting errors"

    plumb_facts_json "$TEST_TMPDIR/noedac.json" 'ctx.edac=null' \
        '+differ={"path":"/srv/data.db","role":"file","elf":null,"written_since_boot":false,"diff":{"bytes":1,"single_bit":true}}'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/noedac.json")" \
        "no EDAC driver is loaded"

    plumb_facts_json "$TEST_TMPDIR/clean.json" \
        '+differ={"path":"/srv/data.db","role":"file","elf":null,"written_since_boot":false,"diff":{"bytes":1,"single_bit":true}}'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/clean.json")" \
        "ECC does not cover"
}

t_a_flipped_bit_in_a_privileged_file_is_still_memory() {
    # Hardware does not pick its targets.  A single bit in a setuid
    # program is the same flipped bit, and calling it an attack would send
    # someone hunting an intruder instead of replacing a DIMM.
    plumb_facts_json "$TEST_TMPDIR/f.json" 'ctx.edac={"controllers":1,"ce":0,"ue":2}' \
        '+differ={"written_since_boot":false,"elf":{"at_entry":false},"diff":{"bytes":1,"single_bit":true}}'
    csv="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$csv" "CACHE_CORRUPT,CRITICAL"
    assert_not_contains "$csv" "CACHE_TAMPERED"
    assert_contains "$csv" "MEMORY_ERRORS,CRITICAL"
}

t_whole_pages_wrong_in_memory_is_a_kernel_bug() {
    plumb_facts_json "$TEST_TMPDIR/f.json" 'ctx.kernel_log={"pagecache":1}' \
        '+differ={"path":"/srv/big.dat","role":"file","elf":null,"written_since_boot":false,"diff":{"bytes":8192,"cache_zero":true,"page_aligned":true,"ranges":[[8192,16384]]}}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "a kernel bug in the page cache"
    assert_contains "$out" "has changed in memory since"
    assert_contains "$out" "pagecache line"
}

t_an_unstable_disk_outranks_everything() {
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"written_since_boot":false}' \
        '+differ={"path":"/srv/a.dat","role":"file","elf":null,"disk_unstable":true}' \
        'ctx.md=[{"name":"md0","level":"raid1","mismatch_cnt":128}]'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    verdict="${out#*VERDICT}"; verdict="${verdict%%CRITICAL*}"
    assert_contains "$verdict" "different bytes each time"
    assert_contains "$out" "md0 (raid1) reports mismatch_cnt 128"
    # The tampered file is still reported, below it.
    assert_contains "$out" "privileged file altered"
}

t_a_disk_that_will_not_read_is_its_own_finding() {
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"path":"/srv/a.dat","status":"unreadable","failed":"disk","reason":"Input/output error","diff":null,"elf":null}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "CRITICAL disk cannot be read"
    assert_contains "$out" "only one left"
    assert_contains "$out" "Input/output error"
}

t_no_cache_drop_while_a_good_copy_lives_only_in_memory() {
    # Dropping caches clears the tampered page -- and the only good copy
    # of the other file with it.  The advice has to know about both.
    plumb_facts_json "$TEST_TMPDIR/f.json" \
        '+differ={"written_since_boot":false}' \
        '+differ={"path":"/srv/a.dat","role":"file","elf":null,"diff":{"disk_zero":true}}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "Do not reboot or drop caches yet"
}

t_a_mapped_page_cannot_be_evicted_and_says_so() {
    plumb_facts_json "$TEST_TMPDIR/mapped.json" \
        '+differ={"written_since_boot":false,"mapped":[{"pid":812,"comm":"sshd"}]}'
    out="$(plf --from-facts "$TEST_TMPDIR/mapped.json")"
    assert_contains "$out" "mapped by sshd (812)"
    assert_contains "$out" "cannot be evicted"

    plumb_facts_json "$TEST_TMPDIR/free.json" 'ctx.mapped_complete=false' \
        '+differ={"written_since_boot":false}'
    out="$(plf --from-facts "$TEST_TMPDIR/free.json")"
    assert_contains "$out" "iflag=nocache count=0"
    assert_contains "$out" "Without root only your own processes"
}

t_unchecked_files_are_counted_not_hidden() {
    plumb_facts_json "$TEST_TMPDIR/user.json" sys.euid=1000 '+skip={}' \
        '+skip={"path":"/run/x","skip":"nodisk","reason":"tmpfs keeps files in memory"}'
    out="$(plf --from-facts "$TEST_TMPDIR/user.json")"
    assert_contains "$out" "2 of 5: 1 need root, 1 not on a disk"
    assert_contains "$out" "sudo"
    # Unchecked files are INFO: they do not turn a clean verdict dirty.
    assert_contains "$out" "agree on all 3 files compared"
    out="$(plf --from-facts "$TEST_TMPDIR/user.json" --all)"
    assert_contains "$out" "NOT CHECKED"
    assert_contains "$out" "/run/x"

    plumb_facts_json "$TEST_TMPDIR/root.json" sys.euid=0 '+skip={}'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/root.json")" \
        "1 permission denied"
}

t_a_run_that_compared_nothing_is_not_clean() {
    plumb_facts_json "$TEST_TMPDIR/f.json" 'files.all=[]' '+skip={}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "not a clean bill of health"
    assert_contains "$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)" \
        "NOTHING_COMPARED,WARN"
    plumb_facts_json "$TEST_TMPDIR/e.json" 'files.all=[]'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/e.json")" \
        "no files to compare"
}

t_a_file_that_kept_moving_is_named_not_judged() {
    plumb_facts_json "$TEST_TMPDIR/f.json" '+moving={}'
    csv="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv)"
    assert_contains "$csv" "CHANGED_WHILE_READ,INFO"
    assert_not_contains "$csv" "CRITICAL"
}

t_memory_errors_grade_by_whether_they_were_corrected() {
    plumb_facts_json "$TEST_TMPDIR/ce.json" 'ctx.edac={"controllers":2,"ce":3,"ue":0}'
    plumb_facts_json "$TEST_TMPDIR/ue.json" 'ctx.edac={"controllers":2,"ce":3,"ue":1}'
    plumb_facts_json "$TEST_TMPDIR/none.json" 'ctx.edac=null'
    assert_contains "$("$PY" "$PL" --from-facts "$TEST_TMPDIR/ce.json" --csv)" \
        "MEMORY_ERRORS,WARN"
    assert_contains "$("$PY" "$PL" --from-facts "$TEST_TMPDIR/ue.json" --csv)" \
        "MEMORY_ERRORS,CRITICAL"
    out="$(plf --from-facts "$TEST_TMPDIR/none.json" --all)"
    assert_contains "$out" "no EDAC driver is loaded here"
}

t_a_taint_that_matters_is_read_and_one_that_does_not_is_not() {
    plumb_facts_json "$TEST_TMPDIR/m.json" ctx.taint=16
    plumb_facts_json "$TEST_TMPDIR/w.json" ctx.taint=512
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/m.json")" \
        "a machine check"
    assert_not_contains "$("$PY" "$PL" --from-facts "$TEST_TMPDIR/w.json" --csv)" \
        "KERNEL_TAINT"
}

t_the_kernel_log_is_evidence_even_when_files_agree() {
    plumb_facts_json "$TEST_TMPDIR/f.json" 'ctx.kernel_log={"filesystem":2}'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "2 filesystem lines in the kernel log"
    verdict="${out#*VERDICT}"; verdict="${verdict%%WARN*}"
    assert_contains "$verdict" "agree on all 3 files compared, but"
    plumb_facts_json "$TEST_TMPDIR/n.json" 'ctx.kernel_log=null'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/n.json" --all)" \
        "rerun with sudo"
}

t_old_and_new_writers_are_named_by_what_this_kernel_has() {
    plumb_facts_json "$TEST_TMPDIR/dp.json" sys.kernel=5.15.10-generic \
        '+differ={"written_since_boot":false}'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/dp.json")" \
        "Dirty Pipe's range"
    plumb_facts_json "$TEST_TMPDIR/fixed.json" sys.kernel=5.15.25-generic \
        '+differ={"written_since_boot":false}'
    assert_not_contains "$(plf --from-facts "$TEST_TMPDIR/fixed.json")" \
        "Dirty Pipe"
    plumb_facts_json "$TEST_TMPDIR/pedit.json" ctx.userns=true \
        'ctx.modules={"act_pedit":"loaded"}' '+differ={"written_since_boot":false}'
    assert_contains "$(plf --from-facts "$TEST_TMPDIR/pedit.json")" \
        "how pedit COW gets CAP_NET_ADMIN"
}

t_missing_facts_skip_with_a_reason() {
    plumb_facts_json "$TEST_TMPDIR/f.json" 'ctx.taint=null'
    out="$(plf --from-facts "$TEST_TMPDIR/f.json" --all)"
    assert_contains "$out" "skipped"
    assert_contains "$out" "tainted could not be read"
}

t_an_empty_fact_file_is_not_a_clean_bill_of_health() {
    echo '{}' > "$TEST_TMPDIR/f.json"
    out="$(plf --from-facts "$TEST_TMPDIR/f.json")"
    assert_contains "$out" "nothing could be checked"
    assert_not_contains "$out" "agree on all"
}

t_a_foreign_fact_file_is_refused() {
    echo '[1, 2, 3]' > "$TEST_TMPDIR/f.json"
    set +e
    out="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
    assert_contains "$out" "does not hold a fact dictionary"
    assert_not_contains "$out" "Traceback"
}

t_csv_header_is_stable() {
    plumb_facts_json "$TEST_TMPDIR/f.json"
    head="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" --csv | head -1)"
    assert_eq "$head" "host,ts,rule_id,severity,title,detail,fix"
}

t_exit_codes_avoid_usage_collision() {
    plumb_facts_json "$TEST_TMPDIR/ok.json"
    plumb_facts_json "$TEST_TMPDIR/warn.json" ctx.taint=16
    plumb_facts_json "$TEST_TMPDIR/crit.json" '+differ={}'
    set +e
    "$PY" "$PL" --from-facts "$TEST_TMPDIR/ok.json" --exit-code >/dev/null; a=$?
    "$PY" "$PL" --from-facts "$TEST_TMPDIR/warn.json" --exit-code >/dev/null; b=$?
    "$PY" "$PL" --from-facts "$TEST_TMPDIR/crit.json" --exit-code >/dev/null; c=$?
    "$PY" "$PL" --from-facts "$TEST_TMPDIR/crit.json" >/dev/null; d=$?
    set -e
    assert_eq "$a/$b/$c/$d" "0/10/20/0"
}

t_rules_and_explain_are_generated() {
    out="$("$PY" "$PL" --rules)"
    for id in DISK_UNSTABLE DISK_UNREADABLE DISK_WRONG CACHE_TAMPERED \
              CACHE_CORRUPT CACHE_DISK_DIFFER NOTHING_COMPARED MEMORY_ERRORS \
              KERNEL_TAINT KERNEL_LOG CHANGED_WHILE_READ NOT_CHECKED; do
        assert_contains "$out" "$id"
    done
    assert_contains "$("$PY" "$PL" --explain cache_tampered)" "Fragnesia"
    set +e
    out="$("$PY" "$PL" --explain NO_SUCH 2>&1)"; rc=$?
    set -e
    assert_status $rc 2
}

t_bad_arguments_are_refused_by_name() {
    set +e
    a="$("$PY" "$PL" --max-mb -1 2>&1)"; ra=$?
    b="$("$PY" "$PL" --disk-view "$TEST_TMPDIR/nope" 2>&1)"; rb=$?
    echo '{}' > "$TEST_TMPDIR/f.json"
    c="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/f.json" /etc/passwd 2>&1)"; rc=$?
    set -e
    assert_eq "$ra/$rb/$rc" "2/2/2"
    assert_contains "$a" "--max-mb"
    assert_contains "$b" "--disk-view"
    assert_contains "$c" "takes no paths"
}

# --- the comparison, against real files ------------------------------------

t_identical_copies_agree() {
    mkfile "$TEST_TMPDIR/a.bin" 300
    twin "$TEST_TMPDIR/a.bin"
    out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")")"
    assert_contains "$out" "agree on the one file compared"
    assert_contains "$out" "disk side read from"
}

t_a_few_bytes_are_found_exactly() {
    mkfile "$TEST_TMPDIR/a.bin" 3000
    twin "$TEST_TMPDIR/a.bin"
    # Past the first megabyte, so the chunking is under test as well.
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0x180123 deadbeef
    f="$(facts_of "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["status"]')" "differ"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["ranges"]')" \
        "[[1573155, 1573159]]"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["bytes"]')" "4"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["first"]["disk"][:11]')" \
        "de ad be ef"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["single_bit"]')" "False"
}

t_one_flipped_bit_is_measured_as_one() {
    mkfile "$TEST_TMPDIR/a.bin" 64
    twin "$TEST_TMPDIR/a.bin"
    # Offset 0 holds 0x01; 0x03 is that with one more bit set.
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0 03
    f="$(facts_of "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["single_bit"]')" "True"
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0 07
    f="$(facts_of "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["single_bit"]')" "False"
}

t_zeros_on_disk_are_seen_as_zeros_in_whole_pages() {
    mkfile "$TEST_TMPDIR/a.bin" 64 withzeros
    twin "$TEST_TMPDIR/a.bin"
    # Data that holds zeros of its own: a byte that is zero on both sides
    # must not split one zeroed region into hundreds of ranges.
    zero "$(dv "$TEST_TMPDIR/a.bin")" 0x4000 0x2000
    f="$(facts_of "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["n_ranges"]')" "1"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["disk_zero"]')" "True"
    assert_eq "$(jget "$f" 'f["files.all"][0]["diff"]["page_aligned"]')" "True"
    out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv" --csv)")"
    # Written a moment ago, and the disk holds zeros: a lost write.
    assert_contains "$out" "DISK_WRONG,CRITICAL"
}

t_a_file_older_than_boot_is_judged_wrong_in_memory() {
    BTIME=$(( $(date +%s) + 86400 )) plumb_root
    mkfile "$TEST_TMPDIR/a.bin" 64
    twin "$TEST_TMPDIR/a.bin"
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0x2000 41424344
    out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")")"
    assert_contains "$out" "not written since boot"
    assert_contains "$out" "the cache is wrong"
    assert_contains "$out" "CRITICAL corrupted in memory"
}

t_the_entry_point_and_the_header_are_found() {
    BTIME=$(( $(date +%s) + 86400 )) plumb_root
    mkelf "$TEST_TMPDIR/prog"
    chmod 4755 "$TEST_TMPDIR/prog" 2>/dev/null || chmod 755 "$TEST_TMPDIR/prog"
    twin "$TEST_TMPDIR/prog"
    # Whichever copy holds the patch, the report says where it landed.
    poke "$(dv "$TEST_TMPDIR/prog")" 0x1000 31c0c390
    f="$(facts_of "$TEST_TMPDIR/prog" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["elf"]["entry_off"]')" "4096"
    assert_eq "$(jget "$f" 'f["files.all"][0]["elf"]["at_entry"]')" "True"
    assert_eq "$(jget "$f" 'f["files.all"][0]["elf"]["header"]')" "False"
    out="$(flat "$(pl "$TEST_TMPDIR/prog" --disk-view "$TEST_TMPDIR/dv")")"
    assert_contains "$out" "the program's entry point (0x1000)"
    poke "$(dv "$TEST_TMPDIR/prog")" 0x19 20
    f="$(facts_of "$TEST_TMPDIR/prog" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["elf"]["header"]')" "True"
}

t_dpkg_says_which_copy_is_right() {
    mkfile "$TEST_TMPDIR/a.bin" 64
    twin "$TEST_TMPDIR/a.bin"
    # The package installed what the disk now holds.
    poke "$TEST_TMPDIR/a.bin" 0x100 41424344
    sum="$(md5sum "$(dv "$TEST_TMPDIR/a.bin")" | cut -d' ' -f1)"
    plumb_root
    rp="$(real "$TEST_TMPDIR/a.bin")"
    printf '%s  %s\n' "$sum" "${rp#/}" \
        > "$TEST_TMPDIR/dpkg/testpkg:amd64.md5sums"
    f="$(facts_of "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'f["files.all"][0]["pkg"]["match"]')" "disk"
    assert_eq "$(jget "$f" 'f["files.all"][0]["pkg"]["name"]')" "testpkg:amd64"
    out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")")"
    assert_contains "$out" "the disk matches the md5 testpkg:amd64 records"
}

t_rpm_says_which_copy_is_right_unless_told_not_to_exec() {
    mkfile "$TEST_TMPDIR/a.bin" 64
    twin "$TEST_TMPDIR/a.bin"
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0x100 41424344
    # This time the cache is the copy the package installed.
    sum="$(sha256sum "$TEST_TMPDIR/a.bin" | cut -d' ' -f1)"
    cat > "$FAKE_BIN/rpm" <<EOF
#!/bin/sh
printf '%s\t8\t%s\tfoo-1.0-1.x86_64\n' "$(real "$TEST_TMPDIR/a.bin")" "$sum"
EOF
    chmod +x "$FAKE_BIN/rpm"
    f="$(PATH="$FAKE_BIN:$PATH" pl_exec "$TEST_TMPDIR/a.bin" \
           --disk-view "$TEST_TMPDIR/dv" --facts)"
    assert_eq "$(jget "$f" 'f["files.all"][0]["pkg"]["match"]')" "cache"
    assert_eq "$(jget "$f" 'f["files.all"][0]["pkg"]["algo"]')" "sha256"
    f="$(PATH="$FAKE_BIN:$PATH" pl "$TEST_TMPDIR/a.bin" \
           --disk-view "$TEST_TMPDIR/dv" --facts)"
    assert_eq "$(jget "$f" 'f["files.all"][0].get("pkg")')" "None"
}

t_a_mapping_process_is_found() {
    mkfile "$TEST_TMPDIR/lib.so" 16
    twin "$TEST_TMPDIR/lib.so"
    poke "$(dv "$TEST_TMPDIR/lib.so")" 0x10 ff
    plumb_root
    mkdir -p "$TEST_TMPDIR/proc/4242"
    printf '7f0000000000-7f0000004000 r-xp 00000000 08:01 1234  %s\n' \
        "$(real "$TEST_TMPDIR/lib.so")" > "$TEST_TMPDIR/proc/4242/maps"
    echo victimd > "$TEST_TMPDIR/proc/4242/comm"
    out="$(flat "$(pl "$TEST_TMPDIR/lib.so" --disk-view "$TEST_TMPDIR/dv")")"
    assert_contains "$out" "victimd (4242)"
}

t_the_kernel_log_edac_md_taint_and_modules_are_read() {
    plumb_root
    s="$TEST_TMPDIR/sys"
    mkdir -p "$s/devices/system/edac/mc/mc0" "$s/block/md0/md"
    echo 3 > "$s/devices/system/edac/mc/mc0/ce_count"
    echo 0 > "$s/devices/system/edac/mc/mc0/ue_count"
    echo raid1 > "$s/block/md0/md/level"
    echo 64 > "$s/block/md0/md/mismatch_cnt"
    echo 32 > "$TEST_TMPDIR/proc/sys/kernel/tainted"
    printf 'esp4 28672 0 - Live 0x0\next4 1003520 1 - Live 0x0\n' \
        > "$TEST_TMPDIR/proc/modules"
    cat > "$FAKE_BIN/dmesg" <<'EOF'
#!/bin/sh
echo '[  101.5] BTRFS warning (device sda2): csum failed root 5 ino 257 off 4096 csum 0x1 expected csum 0x2 mirror 1'
echo '[  102.0] EDAC MC0: 1 CE memory read error on CPU_SrcID#0_Ha#0_Chan#1_DIMM#0'
echo '[  103.0] usb 1-1: new high-speed USB device number 2'
EOF
    chmod +x "$FAKE_BIN/dmesg"
    mkfile "$TEST_TMPDIR/a.bin" 4
    twin "$TEST_TMPDIR/a.bin"
    f="$(PATH="$FAKE_BIN:$PATH" pl_exec "$TEST_TMPDIR/a.bin" \
           --disk-view "$TEST_TMPDIR/dv" --facts)"
    assert_eq "$(jget "$f" 'f["ctx.edac"]["ce"]')" "3"
    assert_eq "$(jget "$f" 'f["ctx.md"][0]["mismatch_cnt"]')" "64"
    assert_eq "$(jget "$f" 'f["ctx.taint"]')" "32"
    assert_eq "$(jget "$f" 'f["ctx.modules"]["esp4"]')" "loaded"
    assert_eq "$(jget "$f" 'f["ctx.kernel_log"]["filesystem"]["count"]')" "1"
    assert_eq "$(jget "$f" 'f["ctx.kernel_log"]["memory"]["count"]')" "1"
    out="$(PATH="$FAKE_BIN:$PATH" pl_exec "$TEST_TMPDIR/a.bin" \
             --disk-view "$TEST_TMPDIR/dv" --csv)"
    assert_contains "$out" "MEMORY_ERRORS,WARN"
    assert_contains "$out" "KERNEL_TAINT,WARN"
    assert_contains "$out" "KERNEL_LOG,WARN"
    # --no-exec means dmesg is not run, whatever else is read.
    f="$(PATH="$FAKE_BIN:$PATH" pl "$TEST_TMPDIR/a.bin" \
           --disk-view "$TEST_TMPDIR/dv" --facts)"
    assert_not_contains "$(jget "$f" 'f["ctx.kernel_log"]')" "'dmesg'"
}

t_files_over_the_ceiling_are_skipped_by_name() {
    mkfile "$TEST_TMPDIR/big.bin" 2048
    twin "$TEST_TMPDIR/big.bin"
    out="$(flat "$(pl "$TEST_TMPDIR/big.bin" --disk-view "$TEST_TMPDIR/dv" --max-mb 1 --all)")"
    assert_contains "$out" "over --max-mb"
    assert_contains "$out" "nothing was compared"
    out="$(flat "$(pl "$TEST_TMPDIR/big.bin" --disk-view "$TEST_TMPDIR/dv" --max-mb 0)")"
    assert_contains "$out" "agree on the one file compared"
}

t_a_filesystem_without_a_disk_is_not_compared() {
    # A tmpfs takes O_DIRECT on a modern kernel and serves it from memory,
    # so a read "past the cache" there agrees with the cache every time.
    # That all-clear would mean nothing, so the file is never read at all.
    plumb_root
    mkfile "$TEST_TMPDIR/a.bin" 4
    printf '22 1 0:21 / / rw - ext4 /dev/sda1 rw\n' \
        > "$TEST_TMPDIR/proc/self/mountinfo"
    printf '30 22 0:30 / %s rw,nosuid - tmpfs tmpfs rw\n' "$(real "$TEST_TMPDIR")" \
        >> "$TEST_TMPDIR/proc/self/mountinfo"
    out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --all)")"
    assert_contains "$out" "tmpfs keeps files in memory"
    assert_contains "$out" "1 not on a disk"
    assert_not_contains "$out" "agree on all"
    sed -i 's/ - tmpfs tmpfs rw/ - nfs4 srv:\/x rw/' \
        "$TEST_TMPDIR/proc/self/mountinfo"
    assert_contains "$(pl "$TEST_TMPDIR/a.bin")" "1 on network filesystems"
}

t_a_real_read_past_the_cache_never_invents_a_difference() {
    # No --disk-view: the real O_DIRECT path, on whatever filesystem this
    # machine's temp directory is.  Agreement, or an honest skip -- never
    # a difference between a file and itself.
    plumb_root
    cp /proc/self/mountinfo "$TEST_TMPDIR/proc/self/mountinfo"
    mkfile "$TEST_TMPDIR/real.bin" 1500
    out="$(flat "$(pl "$TEST_TMPDIR/real.bin" --csv)")"
    assert_not_contains "$out" "CRITICAL"
    f="$(facts_of "$TEST_TMPDIR/real.bin")"
    status="$(jget "$f" 'f["files.all"][0]["status"]')"
    case "$status" in
        ok|skipped) ;;
        *) fail "a file compared against itself came back $status" ;;
    esac
}

t_links_are_one_file_and_directories_are_walked() {
    d="$TEST_TMPDIR/tree"
    mkdir -p "$d/sub"
    mkfile "$d/one" 4
    mkfile "$d/sub/two" 4
    ln -s one "$d/link"
    ln "$d/one" "$d/hard"
    for x in one hard sub/two; do twin "$d/$x"; done
    f="$(facts_of "$d" "$d/link" --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'len(f["files.all"])')" "2"
    assert_eq "$(jget "$f" 'sorted(x["status"] for x in f["files.all"])')" \
        "['ok', 'ok']"
    f="$(facts_of "$d" --one-fs --disk-view "$TEST_TMPDIR/dv")"
    assert_eq "$(jget "$f" 'len(f["files.all"])')" "2"
}

t_what_cannot_be_read_is_a_reason_not_a_crash() {
    mkfile "$TEST_TMPDIR/a.bin" 4
    out="$(flat "$(pl "$TEST_TMPDIR/gone" /dev/null --all 2>&1)")"
    assert_not_contains "$out" "Traceback"
    assert_contains "$out" "no such file"
    assert_contains "$out" "not a regular file"
    assert_contains "$out" "nothing was compared"
    if [ "$(id -u)" -ne 0 ]; then
        twin "$TEST_TMPDIR/a.bin"
        chmod 000 "$TEST_TMPDIR/a.bin"
        out="$(flat "$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv")")"
        assert_contains "$out" "1 need root"
    fi
}

t_the_default_set_covers_what_an_attack_would_aim_at() {
    plumb_root
    f="$(facts_of)"
    paths="$(jget "$f" '" ".join(x["path"] for x in f["files.all"])')"
    assert_contains "$paths" "/etc/passwd"
    assert_eq "$(jget "$f" 'f["run.targets"]')" "default"
    # Every file is either compared or skipped with a reason.
    assert_eq "$(jget "$f" 'all(x["status"] in ("ok","skipped","moving","differ","unreadable") and (x["status"] != "skipped" or x.get("reason")) for x in f["files.all"])')" \
        "True"
}

t_json_carries_the_judgements() {
    mkfile "$TEST_TMPDIR/a.bin" 16
    twin "$TEST_TMPDIR/a.bin"
    poke "$(dv "$TEST_TMPDIR/a.bin")" 0x10 ff
    pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv" \
        --json "$TEST_TMPDIR/out.json" >/dev/null
    doc="$(cat "$TEST_TMPDIR/out.json")"
    assert_eq "$(jget "$doc" 'f["judgements"][0]["kind"]')" "differ"
    assert_eq "$(jget "$doc" 'f["meta"]["tool"]')" "plumb.py"
    # And the saved facts reproduce the same diagnosis with nothing read.
    a="$("$PY" "$PL" --from-facts "$TEST_TMPDIR/out.json" --csv | cut -d, -f3-)"
    b="$(pl "$TEST_TMPDIR/a.bin" --disk-view "$TEST_TMPDIR/dv" --csv | cut -d, -f3-)"
    assert_eq "$a" "$b"
}

echo "plumb"
run_test "agreement says so plainly"            t_agreement_says_so_plainly
run_test "privileged + altered is tampering"    t_a_privileged_file_altered_in_memory_is_tampering
run_test "older than boot blames the cache"     t_a_file_not_written_since_boot_puts_the_blame_on_the_cache
run_test "entry points present are named"       t_the_entry_points_present_are_named
run_test "no evidence, no accusation"           t_the_same_bytes_with_no_evidence_are_not_called_tampering
run_test "a wrong disk copy is the urgent one"  t_the_disk_copy_wrong_is_the_urgent_one
run_test "zeros on disk name the kernel range"  t_zeros_on_disk_are_a_lost_write_and_the_kernel_is_named
run_test "one flipped bit is memory"            t_one_flipped_bit_is_blamed_on_memory
run_test "a flipped bit is never an attack"     t_a_flipped_bit_in_a_privileged_file_is_still_memory
run_test "whole pages wrong is a kernel bug"    t_whole_pages_wrong_in_memory_is_a_kernel_bug
run_test "an unstable disk outranks all"        t_an_unstable_disk_outranks_everything
run_test "an unreadable disk is a finding"      t_a_disk_that_will_not_read_is_its_own_finding
run_test "no cache drop over a good copy"       t_no_cache_drop_while_a_good_copy_lives_only_in_memory
run_test "a mapped page cannot be evicted"      t_a_mapped_page_cannot_be_evicted_and_says_so
run_test "unchecked files are counted"          t_unchecked_files_are_counted_not_hidden
run_test "comparing nothing is not clean"       t_a_run_that_compared_nothing_is_not_clean
run_test "a moving file is named, not judged"   t_a_file_that_kept_moving_is_named_not_judged
run_test "memory errors graded by correction"   t_memory_errors_grade_by_whether_they_were_corrected
run_test "taint flags that matter are read"     t_a_taint_that_matters_is_read_and_one_that_does_not_is_not
run_test "the kernel log is evidence"           t_the_kernel_log_is_evidence_even_when_files_agree
run_test "writers named by what kernel has"     t_old_and_new_writers_are_named_by_what_this_kernel_has
run_test "missing facts skip with a reason"     t_missing_facts_skip_with_a_reason
run_test "an empty fact file is not clean"      t_an_empty_fact_file_is_not_a_clean_bill_of_health
run_test "a foreign fact file is refused"       t_a_foreign_fact_file_is_refused
run_test "csv header is stable"                 t_csv_header_is_stable
run_test "exit codes avoid usage collision"     t_exit_codes_avoid_usage_collision
run_test "--rules and --explain generated"      t_rules_and_explain_are_generated
run_test "bad arguments refused by name"        t_bad_arguments_are_refused_by_name
run_test "identical copies agree"               t_identical_copies_agree
run_test "a few bytes are found exactly"        t_a_few_bytes_are_found_exactly
run_test "one flipped bit measured as one"      t_one_flipped_bit_is_measured_as_one
run_test "zeros on disk, in whole pages"        t_zeros_on_disk_are_seen_as_zeros_in_whole_pages
run_test "older than boot: wrong in memory"     t_a_file_older_than_boot_is_judged_wrong_in_memory
run_test "entry point and header are found"     t_the_entry_point_and_the_header_are_found
run_test "dpkg says which copy is right"        t_dpkg_says_which_copy_is_right
run_test "rpm says which, unless --no-exec"     t_rpm_says_which_copy_is_right_unless_told_not_to_exec
run_test "a mapping process is found"           t_a_mapping_process_is_found
run_test "log, edac, md, taint, modules read"   t_the_kernel_log_edac_md_taint_and_modules_are_read
run_test "over the ceiling, skipped by name"    t_files_over_the_ceiling_are_skipped_by_name
run_test "no disk, no comparison"               t_a_filesystem_without_a_disk_is_not_compared
run_test "a real read never invents a diff"     t_a_real_read_past_the_cache_never_invents_a_difference
run_test "links are one file, dirs walked"      t_links_are_one_file_and_directories_are_walked
run_test "unreadable is a reason, not a crash"  t_what_cannot_be_read_is_a_reason_not_a_crash
run_test "the default set covers the targets"   t_the_default_set_covers_what_an_attack_would_aim_at
run_test "json carries the judgements"          t_json_carries_the_judgements
finish
