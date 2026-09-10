#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Every numeric option in the package is classified, and the ones that
cannot mean zero say so.

Five bugs in one audit came from the same shape: a number a user can type
that the tool then acts on as if it made sense.  `agree --first 0` ran the
whole fleet instead of the canary, `during -- true` returned 1 because a
successful `0` read as "no status", `resolve --timeout 0` announced a DNS
outage, `netmesh --interval 0` wrote 247,637 report rows in three seconds
onto every host, and `muster --stale-lock 0` broke a lock that was one
second old.

Fixing five instances does not stop the sixth.  This is the check that
does: it finds every `type=int`/`type=float` option on every parser in the
package and requires each one to appear below, in one list or the other.
A new numeric flag fails this until somebody decides which it is, and the
reason column is the argument for putting it where it is.

Run directly, or through tests/run_tests.sh.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "binnacle")

# Flags where zero or a negative cannot mean anything, and the tool must
# say so rather than act on it.  Checked by running the tool.
MUST_REFUSE = {
    ("agree", "--first"), ("agree", "--limit"), ("agree", "--jobs"),
    ("agree", "--max-output"), ("agree", "--timeout"), ("agree", "--field"),
    ("agree", "--head"), ("agree", "--tail"), ("agree", "--max-diff-lines"),
    ("dredge", "--head"), ("dredge", "--tail"), ("dredge", "--jobs"),
    ("dredge", "--max-files"), ("dredge", "--timeout"),
    ("logtriage", "--split"),
    ("muster", "--stale-lock"),
    ("netmesh", "--pps"), ("netmesh", "--interval"), ("netmesh", "--for"),
    ("netmesh", "--jobs"), ("netmesh", "--port"),
    ("resolve", "--timeout"), ("resolve", "--attempts"),
    ("skew", "--timeout"), ("skew", "--attempts"), ("skew", "--samples"),
}

# Flags where zero (or a negative) is a real answer.  The reason is the
# point of the entry: it is what a reviewer reads instead of guessing.
ZERO_IS_MEANINGFUL = {
    ("dredge", "--max-bytes"): "0 is documented as no ceiling",
    ("during", "--interval"): "0 means a single snapshot, not a rate",
    ("during", "--seconds"): "0 means no window; a command was given instead",
    ("during", "--settle"): "0 means do not wait before sampling",
    ("during", "--rtt-ms"): "a measurement passed in, not a setting",
    ("during", "--expect-mbps"): "a measurement passed in, not a setting",
    ("logtriage", "--top"): "0 shows the header and no findings",
    ("logtriage", "--bucket"): "falls back to the automatic bucket width",
    ("logtriage", "--min-count"): "0 is no floor",
    ("logtriage", "--burst-factor"): "a multiplier; the report is unharmed",
    ("logtriage", "--example-len"): "0 drops the example line",
    ("logtriage", "--max-template-len"): "clamped where the mask is built",
    ("logtriage", "--max-templates"): "clamped where the table is evicted",
    ("muster", "--lock-timeout"): "0 means try once rather than block",
    ("netmesh", "--size"): "clamped by _packet where the payload is built",
    ("netmesh", "--flows"): "clamped to max(1, ...) in the agent",
    ("netmesh", "--hops"): "0 is off; the hop sweep is opt-in",
    ("netmesh", "--mtu-ceiling"): "clamped where the PMTU search is bounded",
    ("netmesh", "--pmtu-every"): "measured harmless: a run with 0 is a normal run",
    ("netmesh", "--duration"): "0 means derive it from --for",
    ("netmesh", "--baseline"): "0 means no idle baseline",
    ("netmesh", "--load-split"): "an epoch stamp, not a setting",
    ("netmesh", "--lines"): "0 asks for no log lines",
    ("netmesh", "--top"): "0 shows no rows",
    ("netmesh", "--worst"): "0 shows no rows",
    ("netmesh", "--slow-us"): "a threshold; 0 means every pair is slow",
    ("netmesh", "--asym-us"): "a threshold",
    ("netmesh", "--asym-pct"): "a threshold",
    ("netmesh", "--bloat-factor"): "a threshold",
    ("netmesh", "--flow-factor"): "a threshold",
    ("reachable", "--jobs"): "clamped to max(1, ...) where the pool is sized",
    ("reachable", "--timeout"): "clamped to max(1, ...) for ConnectTimeout",
    ("reachable", "--ping-timeout"): "handed to ping, which rejects its own",
    ("reachable", "--ping-count"): "0 means do not ping",
    ("skew", "--max-offset"): "a threshold; 0 means any offset is critical",
    ("skew", "--warn-offset"): "a threshold",
    ("why-slow", "--interval"): "0 means a single snapshot, not a rate",
    ("why-slow", "--top"): "0 shows no rows",
    ("why-slow", "--boot-window"): "0 means do not look at boot time",
}

# What each tool needs before a flag can be reached, per verb where the
# flag lives on one.  Kept minimal: these must reach validation and then
# do as little as possible.
TMP = tempfile.mkdtemp(prefix="numeric-args-")
_EMPTY = os.path.join(TMP, "empty")
_MESH = os.path.join(TMP, "m.csv")
open(_EMPTY, "w").close()

BASE = {
    "agree": ["hosts", "-H", "h1"],
    "dredge": ["/etc/hostname", "-H", "h1", "--dry-run"],
    "logtriage": [_EMPTY],
    "muster": ["status", "--pool", _EMPTY],
    "netmesh": ["gen", "a=1.1.1.1", "b=2.2.2.2", "--mesh", _MESH],
    "resolve": ["--no-exec"],
    "skew": ["--no-exec"],
}
VERB_BASE = {
    ("netmesh", "--interval"): ["agent", "--mesh", _MESH, "--host", "a",
                                "--dir", TMP],
    ("netmesh", "--for"): ["run", "--mesh", _MESH],
    ("netmesh", "--jobs"): ["status", "--mesh", _MESH],
}

MODULE = {"why-slow": "why_slow"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def numeric_flags(parser):
    found = set()

    def walk(p):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for _verb, sp in a.choices.items():
                    walk(sp)
            elif a.type in (int, float) and a.option_strings:
                # The long spelling, not whichever alias came last:
                # `--jobs, -j` must not be filed under `-j`.
                longs = [o for o in a.option_strings if o.startswith("--")]
                found.add(longs[0] if longs else a.option_strings[0])
    walk(parser)
    return found


def main():
    problems = []
    checked = 0
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py") or fn in ("__init__.py", "binnacle.py"):
            continue
        tool = fn[:-3].replace("_", "-")
        module = load("_na_" + fn[:-3], os.path.join(PKG, fn))
        build = getattr(module, "build_parser", None)
        if build is None:
            continue
        built = build()
        parser = built[0] if isinstance(built, tuple) else built

        for flag in sorted(numeric_flags(parser)):
            key = (tool, flag)
            classified = key in MUST_REFUSE or key in ZERO_IS_MEANINGFUL
            if not classified:
                problems.append(
                    "%s %s is a new numeric option and is in neither list.\n"
                    "    Decide: does zero mean something here? Put it in\n"
                    "    ZERO_IS_MEANINGFUL with the reason, or in MUST_REFUSE\n"
                    "    and make the tool refuse it." % (tool, flag))
                continue
            if key not in MUST_REFUSE:
                continue
            base = VERB_BASE.get(key) or BASE.get(MODULE.get(tool, tool)) \
                or BASE.get(tool)
            if base is None:
                problems.append("%s %s is in MUST_REFUSE with no way to run "
                                "the tool; add one to BASE." % (tool, flag))
                continue
            for value in ("0", "-1"):
                argv = [sys.executable, os.path.join(PKG, fn)] + base \
                    + [flag, value]
                checked += 1
                try:
                    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT)
                    out = p.communicate(timeout=30)[0]
                except subprocess.TimeoutExpired:
                    p.kill()
                    problems.append("%s %s %s did not finish"
                                    % (tool, flag, value))
                    continue
                if p.returncode != 2:
                    problems.append(
                        "%s %s %s was accepted (exit %s): %s"
                        % (tool, flag, value, p.returncode,
                           out.decode("utf-8", "replace").strip()
                           .splitlines()[:1]))

    if problems:
        sys.stderr.write("numeric options: %d problem(s)\n\n" % len(problems))
        for text in problems:
            sys.stderr.write("  %s\n" % text)
        return 1
    sys.stdout.write("every numeric option is classified; %d refusal(s) "
                     "checked\n" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
