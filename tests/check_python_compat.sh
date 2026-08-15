#!/usr/bin/env bash
# check_python_compat.sh -- hold the Python 3.6 floor.
#
# The floor is load-bearing rather than nostalgic: netmesh copies itself to
# every host in the mesh and agree pushes a tool to a fleet, so these files
# run on whatever python3 those machines happen to have. A RHEL 8 box ships
# 3.6, and syntax it cannot parse fails there and nowhere else.
#
# This only compiles; CI additionally runs vermin for stdlib APIs newer than
# the floor, which a syntax check cannot see.

set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY="${PY:-python3}"
fail=0

for f in binnacle/*.py; do
    if ! "$PY" -m py_compile "$f" 2>/dev/null; then
        printf 'FAIL  %s does not compile under %s\n' "$f" "$("$PY" -V 2>&1)"
        "$PY" -m py_compile "$f" || true
        fail=1
    fi
done

find . -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

if [ "$fail" -ne 0 ]; then
    exit 1
fi
printf 'all modules compile under %s\n' "$("$PY" -V 2>&1)"
