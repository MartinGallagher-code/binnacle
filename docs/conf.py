# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Sphinx configuration for binnacle.

Two principles, both anti-drift:

- The prose pages *include* the repository's own markdown (README,
  CHANGELOG, PUBLISHING) rather than restating it, so there is one copy of
  every sentence.
- The CLI reference is generated at build time from the real argparse
  parsers, so a flag cannot exist without appearing here, nor linger here
  after it is removed.  Each tool's ``--help`` prints its own module
  docstring, so the manual, the help text and this page are the same words.
"""

import os
import sys

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DOCS_DIR)
sys.path.insert(0, REPO_ROOT)

import binnacle  # noqa: E402
from binnacle import agree, logtriage, netmesh, reachable, why_slow  # noqa: E402

project = "binnacle"
author = "Martin J. Gallagher"
copyright = "2026, Martin J. Gallagher"  # noqa: A001 - sphinx's name for it
version = binnacle.VERSION
release = binnacle.VERSION

extensions = ["myst_parser"]
myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
master_doc = "index"
exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"
html_title = "binnacle %s" % binnacle.VERSION
html_theme_options = {"collapse_navigation": False, "navigation_depth": 3}

# The included README carries repo-relative links (LICENSE, tests/...) that
# have no docs-site counterpart; MyST flags them as xref warnings. They are
# correct where the file lives, so quieten just that class.
suppress_warnings = ["myst.xref_missing"]


# Each tool, and how to reach its parser. Two shapes exist: a single parser
# (why-slow, logtriage, reachable) and a verb parser with subcommands
# (agree, netmesh).
TOOLS = [
    ("why-slow", why_slow, False),
    ("agree", agree, True),
    ("logtriage", logtriage, False),
    ("netmesh", netmesh, True),
    ("reachable", reachable, False),
]


def _subparsers(parser):
    import argparse
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _help_of(parser, prog):
    """Format a parser's help with a stable prog name.

    Without pinning prog, the text would carry whatever argv[0] the build
    machine happened to use, and the page would differ between a local
    build and Read the Docs.
    """
    parser.prog = prog
    return parser.format_help().rstrip()


def _generate_cli_reference():
    """Write cli.md from the live parsers. Runs on every build."""
    out = [
        "# CLI reference",
        "",
        "Generated from the argparse parsers at build time, so this page",
        "cannot drift from what the tools accept. Every one of these is",
        "also available offline as `<tool> --help`.",
        "",
    ]
    for name, mod, has_verbs in TOOLS:
        out.append("## %s" % name)
        out.append("")
        parser = mod.build_parser()
        if has_verbs:
            parser, sub = (parser if isinstance(parser, tuple)
                           else (parser, _subparsers(parser)))
        else:
            sub = None
        out.append("```text")
        out.append(_help_of(parser, name))
        out.append("```")
        out.append("")
        if sub is not None:
            for verb, vparser in sorted(sub.choices.items()):
                if verb == "help":
                    continue
                out.append("### %s %s" % (name, verb))
                out.append("")
                out.append("```text")
                out.append(_help_of(vparser, "%s %s" % (name, verb)))
                out.append("```")
                out.append("")

    out += [
        "## Environment variables",
        "",
        "Every flag has a matching environment variable, so a setting can",
        "live in a profile or a unit file instead of every command line.",
        "",
        "| Tool | Variables |",
        "|---|---|",
        "| `why-slow` | `WHY_SLOW_INTERVAL`, `WHY_SLOW_TOP`, "
        "`WHY_SLOW_MIN_SEVERITY`, `WHY_SLOW_BOOT_WINDOW`, `WHY_SLOW_PROC`, "
        "`WHY_SLOW_SYS`, `NO_COLOR` |",
        "| `agree` | `AGREE_HOSTS`, `AGREE_JOBS`, `AGREE_TIMEOUT`, "
        "`AGREE_USER` (falls back to `SSH_USER`), `AGREE_SSH`, `AGREE_SCP`, "
        "`AGREE_REMOTE_DIR`, `AGREE_LIMIT` |",
        "| `logtriage` | `LOGTRIAGE_TOP`, `LOGTRIAGE_MAX_TEMPLATES`, "
        "`LOGTRIAGE_BUCKET`, `LOGTRIAGE_BASELINE`, `LOGTRIAGE_WEIGHTS` |",
        "| `netmesh` | `NETMESH_MESH`, `NETMESH_USER`, `NETMESH_JOBS`, "
        "`NETMESH_REMOTE_DIR`, `NETMESH_PYTHON`, `NETMESH_SSH`, "
        "`NETMESH_SCP`, `NETMESH_REPORTS`, `NETMESH_PPS`, `NETMESH_SIZE`, "
        "`NETMESH_PORT` |",
        "| `reachable` | `REACHABLE_JOBS`, `REACHABLE_TIMEOUT`, "
        "`REACHABLE_USER`, `REACHABLE_SSH`, `REACHABLE_PING` |",
        "",
    ]
    with open(os.path.join(DOCS_DIR, "cli.md"), "w") as f:
        f.write("\n".join(out) + "\n")


_generate_cli_reference()
