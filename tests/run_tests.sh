#!/usr/bin/env bash
# run_tests.sh -- run every test_*.sh in this directory.
#
# Usage: run_tests.sh [NAME...]     e.g. run_tests.sh agree logtriage
#
# No network and no second machine are needed: ssh and scp are replaced by
# a shim that runs the "remote" command locally in a sandbox, and the only
# real packets are netmesh's own agents talking to themselves over
# loopback.

set -u
cd "$(dirname "${BASH_SOURCE[0]}")"

export BINNACLE_DIR="$(cd ../binnacle && pwd)"
export PY="${PY:-python3}"

if [ $# -gt 0 ]; then
    files=()
    for name in "$@"; do
        f="test_${name#test_}"
        f="${f%.sh}.sh"
        [ -f "$f" ] || { echo "no such suite: $f" >&2; exit 2; }
        files+=("$f")
    done
else
    files=(test_*.sh)
fi

total_failed=0
for f in "${files[@]}"; do
    bash "$f" || total_failed=$((total_failed + 1))
    echo
done

if [ "$total_failed" -gt 0 ]; then
    echo "$total_failed suite(s) failed"
    exit 1
fi
echo "all suites passed"
