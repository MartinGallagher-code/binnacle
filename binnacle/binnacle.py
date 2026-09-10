#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""binnacle.py -- what is in this binnacle, and how to drive it.

Usage: binnacle                    the instruments installed here, and their versions
       binnacle list               the same table
       binnacle help               every instrument's --help, in one page
       binnacle help TOOL          just that one
       binnacle copy TOOL...       put those instruments' files here, to copy on
       binnacle copy --all         all of them
       binnacle --version          this command's own version

Options:
      --paths         name the file each instrument was loaded from, in
                      place of the question it answers
      --quiet         the table alone: no heading, no verdict, no hints
  -d, --dir DIR       with copy, where the files land (default: here)
      --force         with copy, overwrite a file that is already there

What it does
  A binnacle is the housing that holds the instruments; this is the
  housing talking.  It answers the three questions you have before you
  can use any of the others: **what is installed here**, **how do I
  drive it**, and **where is the file** -- without needing to already
  know the eleven names.

  `binnacle` on its own prints one row per instrument: the command name,
  the version that instrument reports for *itself*, and the question it
  answers.  `binnacle help` concatenates every tool's `--help`, which is
  each tool's own module docstring, so one page is the whole manual for
  the package as installed rather than as documented somewhere else.

Why the version column is per-tool and not one number
  It would be shorter to print the package version once.  It would also
  hide the failure this is most useful for.  Each of these files carries
  its own `VERSION`, because each is routinely copied to a machine that
  has never heard of this package -- `netmesh` scp's itself across a mesh,
  `agree script why-slow` pushes a tool to a fleet -- and out there its
  own `VERSION` is the only version there is.  `agree` then groups a fleet
  by what `--version` reports, so one module left behind at an older
  number does not read as a bad install: it reads as version skew across
  the whole fleet, and the hunt starts in the wrong place.

  So every instrument is asked separately, and a disagreement is a
  finding, printed and reflected in the exit status.

Getting a tool onto another machine
  Every instrument is a standalone file -- standard library only, no
  imports from its siblings -- because the way it usually gets used is
  copied onto a box that has never heard of this package.  Doing that
  needs the file, and after `pip install` the file is somewhere under
  site-packages that nobody has memorised.

  `binnacle copy netmesh` puts `netmesh.py` in the current directory,
  executable, ready for `scp netmesh.py somehost:`.  It keeps the file
  name the package uses, because that is the name the rest of the
  package expects: `agree script why-slow` looks for `why_slow.py`, and
  a copy renamed on the way out stops matching.

  It refuses to write over a file that is already there and differs,
  because the thing most likely to be sitting under that name is your
  edited copy.  `--force` says otherwise.  A file already there and
  byte-identical is reported as such and left alone.

What it does not do
  It runs nothing.  Building a parser to format its help does not execute
  any tool, touch the network, or read /proc.  This command reads the
  directory it lives in and nothing else.

Exit status
  0   every instrument loaded and agreed on a version; or, for copy,
      every named file is now here
  1   an instrument could not be loaded, or a version disagrees; or, for
      copy, a file could not be written or would have overwritten one
  2   usage error -- an unknown verb, or a tool that is not installed
"""

import argparse
import importlib.util
import io
import os
import sys

VERSION = "0.6.0"
PROG = os.path.basename(sys.argv[0]) or "binnacle.py"

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(os.path.abspath(__file__))

VERBS = ("list", "help", "copy")

# The rule separating one tool's help from the next, matching what
# `agree help` and `netmesh help` already print between their verbs.
RULE = "=" * 72


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    A tool's docstring is this command's output, and those docstrings
    carry en-dashes and arrows.  Under LANG=C -- a RHEL 8 default, and
    the floor this package targets -- stdout is ASCII, so printing one
    would turn the whole page into a UnicodeEncodeError.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "buffer"):
            continue
        enc = (getattr(stream, "encoding", None) or "").lower()
        if enc.replace("-", "_") in ("ascii", "ansi_x3.4_1968", "us_ascii"):
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding=stream.encoding,
                errors="backslashreplace", line_buffering=True))


def die(msg, code=2):
    sys.stderr.write("%s: %s\n" % (PROG, msg))
    raise SystemExit(code)


def emit(text):
    """Write a page, tolerating a reader that stopped reading.

    `binnacle help | head` is the obvious way to read this.  The console
    script calls main() directly, so the __main__ guard at the foot of
    this file never sees the error and it has to be caught here.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        raise SystemExit(0)


# ---------------------------------------------------------------------------
# Finding the instruments
# ---------------------------------------------------------------------------
#
# By listing the directory this file is in, not by importing the package.
# The same discovery `agree script` uses to turn a bare tool name into a
# file (bundled_tools in binnacle/agree.py), and for the same reason: the
# caller has no idea which site-packages directory holds why_slow.py, but
# this file is in it.  Going through the directory rather than `import
# binnacle` also means this works run straight out of a checkout, with the
# repository root nowhere on sys.path.


class Tool(object):
    """One instrument, as found on disk."""

    __slots__ = ("name", "path", "version", "answers", "module", "error")

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.version = None
        self.answers = ""
        self.module = None
        self.error = None

    @property
    def loaded(self):
        return self.module is not None


def _load(name, path):
    """Execute the file at PATH as a module and hand it back.

    Deliberately not registered in sys.modules: these files are
    standalone programs with no imports from each other, so nothing
    needs to find them by name, and leaving them out keeps this from
    colliding with an already-imported `binnacle.agree`.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry():
    """The curated name -> question map from the package __init__.

    It is the canonical order and the canonical wording.  Read from the
    file rather than imported, for the reason above; an unreadable or
    unexpected __init__ costs the descriptions, not the run.
    """
    try:
        mod = _load("_binnacle_registry", os.path.join(HERE, "__init__.py"))
    except Exception:
        return {}
    tools = getattr(mod, "TOOLS", None)
    return tools if isinstance(tools, dict) else {}


def on_disk():
    """[(name, path)] for every instrument beside this file, in order.

    Ordered by the registry first, so the table reads the way the README
    does; anything on disk the registry does not mention is appended
    rather than dropped, because a tool that ships without being
    registered is exactly the kind of drift worth seeing.

    Nothing here is loaded.  `copy` needs the paths and not the
    contents, and a sibling that fails to import should still be a file
    you can take away and look at.
    """
    try:
        found = sorted(os.listdir(HERE))
    except OSError as exc:
        die("cannot read %s: %s" % (HERE, exc), 1)

    paths = {}
    for filename in found:
        if not filename.endswith(".py"):
            continue
        if filename.startswith("_") or filename == SELF:
            continue
        name = filename[:-3].replace("_", "-")
        paths[name] = os.path.join(HERE, filename)

    known = registry()
    names = [n for n in known if n in paths]
    names += [n for n in sorted(paths) if n not in known]
    return [(n, paths[n]) for n in names]


def discover():
    """Every instrument beside this file, loaded, in the package's order."""
    known = registry()
    tools = []
    for name, path in on_disk():
        tool = Tool(name, path)
        tool.answers = known.get(name, "")
        try:
            tool.module = _load("_binnacle_tool_" + name.replace("-", "_"),
                                tool.path)
            tool.version = getattr(tool.module, "VERSION", None)
        except Exception as exc:
            tool.error = "%s: %s" % (exc.__class__.__name__, exc)
        tools.append(tool)
    return tools


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _subparsers(parser):
    """The subcommand action of a verb parser, or None.

    agree and netmesh hand back (parser, subparsers) already; the rest
    return a bare parser.  Both shapes are handled so a tool that grows
    verbs later needs no change here.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _parsers_of(tool):
    """(top parser, subcommand action) for TOOL, or (None, None)."""
    build = getattr(tool.module, "build_parser", None)
    if build is None:
        return None, None
    built = build()
    if isinstance(built, tuple):
        parser = built[0]
        sub = built[1] if len(built) > 1 else _subparsers(parser)
    else:
        parser, sub = built, _subparsers(built)
    return parser, sub


def _help_of(parser, prog):
    """A parser's help under a pinned prog name.

    Every tool computes its own prog from argv[0], which is this command
    when it is this command doing the asking.  Pinning it keeps the page
    saying `netmesh` rather than `binnacle`.
    """
    parser.prog = prog
    return parser.format_help().rstrip()


def render_help(tools):
    """Every tool's --help, verbs included, one page."""
    out = []
    for tool in tools:
        if out:
            out.append("")
            out.append(RULE)
            out.append("")
        if not tool.loaded:
            out.append("%s could not be loaded from %s"
                       % (tool.name, tool.path))
            out.append("  %s" % tool.error)
            continue
        parser, sub = _parsers_of(tool)
        if parser is None:
            out.append("%s exposes no parser to describe." % tool.name)
            continue
        out.append(_help_of(parser, tool.name))
        if sub is None:
            continue
        for verb, vparser in sorted(sub.choices.items()):
            # `help` prints what this page already is.
            if verb == "help":
                continue
            out.append("")
            out.append("--- %s %s ---" % (tool.name, verb))
            out.append("")
            out.append(_help_of(vparser, "%s %s" % (tool.name, verb)))
    return "\n".join(out) + "\n"


def _rows(tools, paths):
    width = max([len(t.name) for t in tools] + [4])
    vwidth = max([len(t.version or "?") for t in tools] + [1])
    rows = []
    for tool in tools:
        third = tool.path if paths else tool.answers
        if not tool.loaded:
            third = "could not be loaded -- %s" % tool.error
        rows.append("  %-*s  %-*s  %s"
                    % (width, tool.name, vwidth, tool.version or "?", third))
    return rows


def _verdict(tools):
    """What the version column adds up to, as lines."""
    broken = [t for t in tools if not t.loaded]
    loaded = [t for t in tools if t.loaded]
    out = []
    for tool in broken:
        out.append("  BROKEN   %s could not be loaded from %s"
                   % (tool.name, tool.path))
        out.append("           %s" % tool.error)
    odd = [t for t in loaded if t.version != VERSION]
    if odd:
        out.append("  SKEW     %s; this command says %s."
                   % ("; ".join("%s says %s" % (t.name, t.version or "?")
                                for t in odd), VERSION))
        out.append("           A tool carries its own version because it is")
        out.append("           routinely copied to a machine on its own, and")
        out.append("           `agree` groups a fleet by what --version says.")
        out.append("           One module left behind reads as fleet-wide")
        out.append("           skew rather than a bad install here. Reinstall.")
    elif loaded and not broken:
        out.append("  %d instrument%s, all at %s."
                   % (len(loaded), "" if len(loaded) == 1 else "s", VERSION))
    return out


def render_list(tools, paths=False, quiet=False):
    out = []
    if not quiet:
        out.append("%s %s -- the housing, and what is in it" % (PROG, VERSION))
        out.append("")
    out += _rows(tools, paths)
    if quiet:
        return "\n".join(out) + "\n"
    verdict = _verdict(tools)
    if verdict:
        out.append("")
        out += verdict
    out.append("")
    out.append("  binnacle help        every instrument's --help, in one page")
    out.append("  binnacle help TOOL   just that one")
    return "\n".join(out) + "\n"


def status_of(tools):
    """1 when this install is not self-consistent, else 0."""
    if any(not t.loaded for t in tools):
        return 1
    if any(t.version != VERSION for t in tools):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Taking a copy away
# ---------------------------------------------------------------------------
#
# The whole point of the standalone-file rule is that an instrument can be
# carried to a machine that has no Python packaging on it at all.  Carrying
# it means having the file, and after a pip install the file is under some
# site-packages directory nobody has memorised.  This is the shortest path
# from "it is installed" to "it is in my hand".


def normalise(token):
    """What the user typed, as an instrument name.

    `why-slow`, `why_slow` and `why_slow.py` are all the same tool: the
    command is hyphenated, the file is underscored, and a person who has
    just been reading `--paths` output will type either.
    """
    name = token.strip()
    if name.endswith(".py"):
        name = name[:-3]
    return name.replace("_", "-").lower()


def _same_bytes(src, dest):
    try:
        with open(src, "rb") as a, open(dest, "rb") as b:
            return a.read() == b.read()
    except OSError:
        return False


def copy_one(src, dest_dir, force=False):
    """Put SRC in DEST_DIR under its own name.  -> (outcome, dest, note).

    Outcomes: "wrote", "same" (already there, byte for byte), "exists"
    (already there and different -- refused), "failed".
    """
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest) and not force:
        if _same_bytes(src, dest):
            return "same", dest, ""
        return "exists", dest, "already here and different; --force overwrites"

    # Through a temporary in the same directory, so an interrupted write
    # never leaves a half-file sitting under the name of a tool.
    tmp = dest + ".binnacle-tmp"
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        with open(tmp, "wb") as fh:
            fh.write(data)
        # Executable: the file carries a #! line and the next thing that
        # happens to it is usually `./netmesh.py` or an scp to somewhere
        # it will be run.
        try:
            os.chmod(tmp, 0o755)
        except OSError:
            pass
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "failed", dest, str(exc)
    return "wrote", dest, ""


def copy_tools(wanted, dest_dir, force=False, quiet=False):
    """Copy the named instruments into DEST_DIR.  -> (report, status)."""
    available = on_disk()
    by_name = dict(available)

    chosen = []
    for token in wanted:
        name = normalise(token)
        if name not in by_name:
            die("no such instrument: %s  (installed: %s)"
                % (token, ", ".join(n for n, _ in available)))
        if name not in [n for n, _ in chosen]:
            chosen.append((name, by_name[name]))

    out = []
    results = []
    width = max([len(os.path.basename(p)) for _, p in chosen] + [4])
    for name, src in chosen:
        outcome, dest, note = copy_one(src, dest_dir, force=force)
        results.append(outcome)
        label = {"wrote": "", "same": "  (already here, identical)",
                 "exists": "  REFUSED", "failed": "  FAILED"}[outcome]
        out.append("  %-*s  %s%s" % (width, os.path.basename(dest), dest, label))
        if note:
            out.append("  %-*s  %s" % (width, "", note))

    if not quiet:
        wrote = results.count("wrote")
        if wrote:
            out.append("")
            out.append("  %d file%s here, executable.  scp them where they are"
                       " needed;" % (wrote, "" if wrote == 1 else "s"))
            out.append("  each runs on its own with nothing but python3.")

    status = 1 if [r for r in results if r in ("exists", "failed")] else 0
    return "\n".join(out) + "\n", status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("verb", nargs="?", default="list", metavar="VERB",
                   help="list (the default), help, or copy")
    p.add_argument("tool", nargs="*", metavar="TOOL",
                   help="with help, the one instrument to describe; "
                        "with copy, the ones to put here")
    p.add_argument("--paths", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("-d", "--dir", default=".", metavar="DIR",
                   help="with copy, where the files land (default: here)")
    p.add_argument("--all", action="store_true",
                   help="with copy, every installed instrument")
    p.add_argument("--force", action="store_true",
                   help="with copy, overwrite a file already there")
    return p


def main(argv=None):
    _stdio_safe()
    args = build_parser().parse_args(argv)

    if args.verb not in VERBS:
        die("no such verb: %s  (try: %s)" % (args.verb, ", ".join(VERBS)))
    if args.tool and args.verb == "list":
        die("list takes no TOOL argument")
    if len(args.tool) > 1 and args.verb == "help":
        die("help describes one instrument at a time")
    for flag, verb in (("all", "copy"), ("force", "copy"), ("paths", "list")):
        if getattr(args, flag) and args.verb != verb:
            die("--%s belongs to %s, not %s" % (flag, verb, args.verb))

    if args.verb == "copy":
        if args.all and args.tool:
            die("copy takes names or --all, not both")
        if not args.all and not args.tool:
            die("copy needs a TOOL, or --all  (installed: %s)"
                % ", ".join(n for n, _ in on_disk()))
        if not os.path.isdir(args.dir):
            die("not a directory: %s" % args.dir)
        wanted = [n for n, _ in on_disk()] if args.all else args.tool
        report, status = copy_tools(wanted, args.dir, force=args.force,
                                    quiet=args.quiet)
        emit(report)
        return status

    tools = discover()
    if not tools:
        die("no instruments found beside %s" % os.path.join(HERE, SELF), 1)

    if args.verb == "list":
        emit(render_list(tools, paths=args.paths, quiet=args.quiet))
        return status_of(tools)

    if args.tool:
        wanted = normalise(args.tool[0])
        chosen = [t for t in tools if t.name == wanted]
        if not chosen:
            die("no such instrument: %s  (installed: %s)"
                % (args.tool[0], ", ".join(t.name for t in tools)))
        emit(render_help(chosen))
        return 0 if chosen[0].loaded else 1

    emit(render_help(tools))
    return status_of(tools)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
    except BrokenPipeError:
        # `binnacle help | head` is a normal way to use this.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)

# ---------------------------------------------------------------------------
# Why this file is the one that may import its siblings
# ---------------------------------------------------------------------------
#
# Every other module in binnacle is a complete, standalone program:
# standard library only, and nothing loaded from the rest of the package.
# That is a requirement, not tidiness -- those files get copied to machines
# that have never heard of binnacle, and a sibling import would break the
# moment one landed.
#
# This file is the exception, and it is the exception because it is not an
# instrument. It is the housing: its entire subject is which instruments
# are present and what they say about themselves, which cannot be answered
# without reading them. Copying it somewhere on its own would be
# meaningless, so the constraint that produces the rule does not apply.
#
# It still does not `import binnacle`. It loads each file from the
# directory it is in, so it reports the tools actually sitting beside it
# rather than whichever ones a sys.path search happened to find first --
# which is the question being asked. _stdio_safe is duplicated from
# why_slow.py like everywhere else, with the same comment naming the
# canonical copy, and the suite holds the copies to each other.
