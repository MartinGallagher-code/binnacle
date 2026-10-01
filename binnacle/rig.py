#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""rig.py -- run a command with its settings kept in a file, asking for any left blank.

Usage: rig app.conf -- mytool                mytool --jobs 20 --verbose
       rig app.conf -- ssh {host} uptime     a setting where {host} says
       rig app.conf -n -- mytool             print the command line, run nothing
       rig app.conf --set host=web01 -- mytool   answer one here, not asked
       rig grep.conf --init -- grep          write grep.conf from grep --help

The file, one setting a line:

  jobs=20              --jobs 20
  verbose              --verbose
  host=                ask for it
  mode=fast|safe       ask which one
  # a comment          skipped, like a blank line

A one-letter name is a short flag: j=20 is -j 20.  Write the dashes
yourself to spell one any other way: -name=x is -name x, and
--color=auto stays joined, as an optional value has to be.  Quote a
value to take it just as it is: pattern="a|b", note="".

In the command, {name} puts that setting's value there instead of
adding a flag, and {...} marks where the flags go (default: the end).

Options:
  -n, --dry-run       print the command line and stop
      --set NAME=VAL  answer NAME without being asked, or replace what
                      the file says; repeatable
      --init          write CONFIG from the command's --help, -h or man
                      page, every flag in it with its # still on; never
                      over a file that is already there

The questions are one screen, with the command line they make written
underneath.  Tab and the arrows move, Left and Right pick, Enter runs
it, Esc runs nothing.  A name marked * is used in the command, so it
has to have an answer.

Exit status
  the command's own, once it runs
  0    --init wrote the file, or --dry-run printed the line
  1    --init found no flags to write
  2    usage error, or a mistake in the file
  126  the command could not be run
  127  the command was not found
  130  cancelled at the questions: nothing ran

Full manual: https://binnacle.readthedocs.io/en/latest/tools/rig.html
"""

import argparse
import io
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys

VERSION = "0.8.0"
PROG = os.path.basename(sys.argv[0]) or "rig.py"

# What a name may be: the dashes are optional and say how the flag is
# spelled, the rest is the name {placeholders} use.  Nothing that would
# need quoting, because a flag that needs quoting is not one anybody
# meant to write.
NAME_RE = re.compile(r"^(-{1,2})?([A-Za-z0-9][A-Za-z0-9_.\-]*)$")

# `{name}` in the command, and the doubled braces that mean a brace.  A
# name has to start with a letter, so `{}` for find, `{1..5}` and `{a,b}`
# are not mistaken for one; awk's `{print}` is, and the refusal says how
# to write it instead.
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_.\-]*)\}")

# The one argument that says where the flags go, when the end is wrong:
# `ssh {...} host cmd`, where anything after the host is the remote
# command's.
FLAGS_HERE = "{...}"

CANCELLED = 130

# How a key is named in RIG_TEST_KEYS, and what the form calls it.
TEST_KEYS = {
    "<tab>": "TAB", "<btab>": "BTAB", "<enter>": "ENTER", "<esc>": "ESC",
    "<left>": "LEFT", "<right>": "RIGHT", "<up>": "UP", "<down>": "DOWN",
    "<bs>": "BS", "<del>": "DEL", "<home>": "HOME", "<end>": "END",
    "<kill>": "KILL",
}


# canonical copy: binnacle/why_slow.py.  Duplicated rather than imported
# for the reason given at the foot of this file.
def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    A setting's value is whatever somebody typed into a file, which is
    as likely to carry a non-ASCII name as any other document.
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


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


class Setting(object):
    """One line of the file: a name, how it is spelled, and what it holds.

    `kind` is one of four, one for each rule the file has:
    `value` (jobs=20), `switch` (verbose), `ask` (host=) and `choose`
    (mode=fast|safe).
    """

    __slots__ = ("name", "flag", "kind", "value", "choices", "where",
                 "joined")

    def __init__(self, name, flag, where):
        self.name = name
        self.flag = flag
        self.kind = "value"
        self.value = None
        self.choices = None
        self.where = where
        # Written with its two dashes, so it keeps its `=`: an option
        # whose value is optional -- `--color[=WHEN]` -- takes only the
        # joined spelling, and reads `--color auto` as a flag and a file.
        self.joined = False

    @property
    def asks(self):
        return self.kind in ("ask", "choose")


def parse_setting(line, where, literal=False):
    """One `name=value` line, read by the four rules.

    `literal` is for --set, which is an answer rather than a question:
    `--set mode=fast|safe` means that string, not a choice between two.
    """
    name, eq, value = line.partition("=")
    name = name.strip()
    m = NAME_RE.match(name)
    if not m:
        raise ConfigError("%s: %r is not a name a flag can have (letters, "
                          "digits, _ . -)" % (where, name[:40]))
    dashes, bare = m.group(1) or "", m.group(2)
    if dashes:
        flag = name
    else:
        flag = ("-" if len(bare) == 1 else "--") + bare
    s = Setting(bare, flag, where)
    s.joined = dashes == "--"
    if not eq:
        s.kind = "switch"
        return s
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        # Quoted: taken as it stands, which is how a value holds a `|`
        # or is deliberately empty.
        s.value = value[1:-1]
        return s
    if literal:
        s.value = value
        return s
    if not value:
        s.kind = "ask"
        return s
    if "|" in value:
        s.kind = "choose"
        s.choices = [c.strip() for c in value.split("|")]
        return s
    s.value = value
    return s


def parse_settings(text, source):
    """Every setting in the file, in the order written.

    Order is kept because it is the order the flags go on the command
    line, and a name written twice is two flags -- `-S web01`, `-S web02`
    -- rather than the second quietly winning.
    """
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(parse_setting(line, "%s:%d" % (source, n)))
    return out


def apply_sets(settings, sets):
    """--set NAME=VALUE: the answer to NAME, given here instead of asked.

    It takes the place of every line with that name, at the first one's
    position, so the flag lands where the file put it; a name the file
    does not have is added at the end.
    """
    for text in sets:
        new = parse_setting(text, "--set", literal=True)
        at = [i for i, s in enumerate(settings) if s.name == new.name]
        if not at:
            settings.append(new)
            continue
        if not text.lstrip().startswith("-"):
            # The answer, spelled the way the file spells the question:
            # `--set color=always` for a file that says `--color=`.
            new.flag, new.joined = settings[at[0]].flag, settings[at[0]].joined
        settings[at[0]] = new
        for i in reversed(at[1:]):
            del settings[i]
    return settings


def read_config(path):
    """The file's text, decoded the way the command line was.

    A value lands in an argv, so it is decoded exactly as Python decoded
    this process's own arguments: whatever bytes were in the file are
    the bytes the command is handed, locale or not.
    """
    try:
        if path == "-":
            data = sys.stdin.buffer.read()
        else:
            with io.open(path, "rb") as fh:
                data = fh.read()
    except (OSError, IOError) as exc:
        die("cannot read %s: %s" % (path, exc.strerror or exc))
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return os.fsdecode(data)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def placeholders(command):
    """The names the command asks for by {name}, in order, once each."""
    names = []
    for arg in command:
        if arg == FLAGS_HERE:
            continue
        for m in PLACEHOLDER_RE.finditer(arg):
            if m.group(1) and m.group(1) not in names:
                names.append(m.group(1))
    return names


def check_placeholders(command, settings, source):
    """Refuse a {name} the file cannot fill, before anybody is asked."""
    if sum(1 for a in command if a == FLAGS_HERE) > 1:
        raise ConfigError("%s appears twice in the command; the flags go "
                          "in one place" % FLAGS_HERE)
    for name in placeholders(command):
        mine = [s for s in settings if s.name == name]
        if not mine:
            raise ConfigError(
                "{%s} in the command names no setting in %s -- write "
                "{{%s}} to mean the braces themselves" % (name, source, name))
        if len(mine) > 1:
            raise ConfigError(
                "{%s} has %d settings in %s (%s), and a place in the "
                "command holds one" % (name, len(mine), source,
                                       ", ".join(s.where for s in mine)))
        if mine[0].kind == "switch":
            raise ConfigError(
                "{%s}: %s is a switch, so it has no value to put there"
                % (name, mine[0].where))


def flag_args(s, value):
    """What one setting adds: `--jobs 20`, or `--verbose` on its own.

    Two words by default, because that is the spelling every parser
    reads, hand-rolled `case "$1"` loops included.  Joined when the file
    wrote the dashes (`--color=auto`), and when the value starts with a
    dash, because `--suffix -raw` reads to most parsers as two flags
    and `--suffix=-raw` does not.
    """
    if s.kind == "switch":
        return [s.flag]
    if s.joined or value.startswith("-"):
        if s.flag.startswith("--"):
            return [s.flag + "=" + value]
        if len(s.flag) == 2:
            # -j-1: getopt and argparse both read a short flag's value
            # straight off the end of it.
            return [s.flag + value]
    return [s.flag, value]


def resolve(settings, answers):
    """Each setting's value: from the file, or from its answer.

    None is a setting left out -- asked, and answered with nothing, or
    with a choice that was empty.
    """
    out = []
    for i, s in enumerate(settings):
        if s.kind == "switch":
            out.append("")
        elif s.asks:
            v = answers.get(i)
            out.append(v if v else None)
        else:
            out.append(s.value)
    return out


def build_argv(command, settings, answers):
    """The command line: placeholders filled, the rest as flags."""
    values = resolve(settings, answers)
    used = set(placeholders(command))
    by_name = dict((s.name, v) for s, v in zip(settings, values)
                   if s.name in used)

    def fill(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        return by_name.get(m.group(1)) or ""

    flags = []
    for s, v in zip(settings, values):
        if s.name in used or v is None:
            continue
        flags += flag_args(s, v)

    argv, placed = [], False
    for arg in command:
        if arg == FLAGS_HERE:
            argv += flags
            placed = True
        else:
            argv.append(PLACEHOLDER_RE.sub(fill, arg))
    if not placed:
        argv += flags
    return argv


def quoted(argv):
    """The command line as a shell would need it typed."""
    return " ".join(shlex.quote(a) for a in argv)


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------

class Field(object):
    """One question: a line to type on, or a list to pick from."""

    def __init__(self, index, setting, required):
        self.index = index
        self.setting = setting
        self.required = required
        self.choices = setting.choices
        self.text = ""
        self.cur = 0
        self.pick = 0

    @property
    def label(self):
        return self.setting.name + (" *" if self.required else "")

    def answer(self):
        if self.choices is not None:
            return self.choices[self.pick]
        return self.text

    def key(self, k):
        if self.choices is not None:
            n = len(self.choices)
            if k in ("RIGHT", " "):
                self.pick = (self.pick + 1) % n
            elif k == "LEFT":
                self.pick = (self.pick - 1) % n
            elif k == "HOME":
                self.pick = 0
            elif k == "END":
                self.pick = n - 1
            elif len(k) == 1:
                # Type a letter to jump to the next choice starting with
                # it: quicker than arrowing along a list of twelve.
                for step in range(1, n + 1):
                    j = (self.pick + step) % n
                    if self.choices[j][:1].lower() == k.lower():
                        self.pick = j
                        break
            return
        if k == "LEFT":
            self.cur = max(0, self.cur - 1)
        elif k == "RIGHT":
            self.cur = min(len(self.text), self.cur + 1)
        elif k == "HOME":
            self.cur = 0
        elif k == "END":
            self.cur = len(self.text)
        elif k == "BS":
            if self.cur:
                self.text = self.text[:self.cur - 1] + self.text[self.cur:]
                self.cur -= 1
        elif k == "DEL":
            self.text = self.text[:self.cur] + self.text[self.cur + 1:]
        elif k == "KILL":
            self.text, self.cur = "", 0
        elif len(k) == 1:
            self.text = self.text[:self.cur] + k + self.text[self.cur:]
            self.cur += 1


class Form(object):
    """Every question on one screen, and what Enter and Esc mean."""

    def __init__(self, fields):
        self.fields = fields
        self.focus = 0
        self.message = ""

    def answers(self):
        return dict((f.index, f.answer()) for f in self.fields)

    def handle(self, k):
        """'run', 'cancel', or None to keep asking."""
        self.message = ""
        n = len(self.fields)
        if k in ("TAB", "DOWN"):
            self.focus = (self.focus + 1) % n
        elif k in ("BTAB", "UP"):
            self.focus = (self.focus - 1) % n
        elif k == "ESC":
            return "cancel"
        elif k == "ENTER":
            for i, f in enumerate(self.fields):
                if f.required and not f.answer():
                    # The command has a hole in it where this goes, so
                    # there is nothing yet to run.
                    self.focus = i
                    self.message = ("%s is used in the command, so it "
                                    "needs an answer" % f.setting.name)
                    return None
            return "run"
        else:
            self.fields[self.focus].key(k)
        return None


def build_form(settings, command):
    used = set(placeholders(command))
    return Form([Field(i, s, s.name in used)
                 for i, s in enumerate(settings) if s.asks])


def test_tokens(script):
    """RIG_TEST_KEYS, as the keys it stands for: `web01<tab><enter>`."""
    out, i = [], 0
    while i < len(script):
        if script[i] == "<":
            j = script.find(">", i)
            name = script[i:j + 1].lower() if j > i else ""
            if name in TEST_KEYS:
                out.append(TEST_KEYS[name])
                i = j + 1
                continue
        out.append(script[i])
        i += 1
    return out


def ask_scripted(form, script):
    """The form driven by RIG_TEST_KEYS, with no terminal and no drawing."""
    for k in test_tokens(script):
        result = form.handle(k)
        if result:
            return result
    if form.message:
        sys.stderr.write("%s: %s\n" % (PROG, form.message))
    sys.stderr.write("%s: RIG_TEST_KEYS ran out before Enter\n" % PROG)
    return "cancel"


def _shown(text):
    """A string curses can draw: bytes the locale cannot spell as '?'."""
    return os.fsencode(text).decode("utf-8", "replace")


def _key_name(curses, k):
    if isinstance(k, int):
        return {
            curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
            curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN",
            curses.KEY_BTAB: "BTAB", curses.KEY_BACKSPACE: "BS",
            curses.KEY_DC: "DEL", curses.KEY_HOME: "HOME",
            curses.KEY_END: "END", curses.KEY_ENTER: "ENTER",
            curses.KEY_RESIZE: None,
        }.get(k)
    named = {"\t": "TAB", "\n": "ENTER", "\r": "ENTER", "\x1b": "ESC",
             "\x7f": "BS", "\x08": "BS", "\x01": "HOME", "\x05": "END",
             "\x15": "KILL"}
    if k in named:
        return named[k]
    return k if k >= " " else None


# Arrow keys as a terminal sends them, for when its terminfo entry says
# otherwise: curses then hands them over a byte at a time, and the first
# byte is Esc -- which would cancel the form on a keypress meant to move.
ESCAPES = {
    "[A": "UP", "OA": "UP", "[B": "DOWN", "OB": "DOWN",
    "[C": "RIGHT", "OC": "RIGHT", "[D": "LEFT", "OD": "LEFT",
    "[H": "HOME", "OH": "HOME", "[1~": "HOME", "[7~": "HOME",
    "[F": "END", "OF": "END", "[4~": "END", "[8~": "END",
    "[3~": "DEL", "[Z": "BTAB",
}


def _escape_sequence(scr):
    """Esc on its own is cancel; Esc with more behind it is a key."""
    seq = ""
    scr.nodelay(True)
    try:
        while len(seq) < 8:
            try:
                c = scr.get_wch()
            except Exception:
                break
            if not isinstance(c, str):
                break
            seq += c
            if seq in ESCAPES:
                break
    finally:
        scr.nodelay(False)
    if not seq:
        return "ESC"
    return ESCAPES.get(seq)


def _put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if y >= h or x >= w:
        return
    try:
        scr.addstr(y, x, text[:max(0, w - x - 1)], attr)
    except Exception:
        # The bottom-right cell, or a character the terminal cannot
        # draw: the screen is worth more than the glyph.
        pass


def _wrap(text, width):
    out = []
    while len(text) > width:
        out.append(text[:width])
        text = text[width:]
    out.append(text)
    return out


def draw(scr, curses, form, title, preview):
    scr.erase()
    h, w = scr.getmaxyx()
    if h < 8 or w < 40:
        _put(scr, 0, 0, "make the window bigger to answer")
        scr.refresh()
        return
    _put(scr, 0, 1, title, curses.A_BOLD)
    labw = max(len(f.label) for f in form.fields)
    x = 3 + labw + 2
    room = max(1, h - 8)
    top = max(0, form.focus - room + 1)
    cursor = None
    for row, f in enumerate(form.fields[top:top + room]):
        i = top + row
        y = 2 + row
        mine = i == form.focus
        _put(scr, y, 3, f.label, curses.A_BOLD if mine else 0)
        if f.choices is not None:
            shown = [_shown(c) or "(leave out)" for c in f.choices]
            if sum(len(s) + 4 for s in shown) <= w - x - 1:
                # One column left, so the word itself lines up with the
                # text typed in the fields above and below it.
                cx = x - 1
                for j, s in enumerate(shown):
                    attr = curses.A_REVERSE if j == f.pick else 0
                    if mine and j == f.pick:
                        attr |= curses.A_BOLD
                    _put(scr, y, cx, " %s " % s, attr)
                    cx += len(s) + 4
            else:
                _put(scr, y, x, "< %s >  %d of %d"
                     % (shown[f.pick], f.pick + 1, len(shown)),
                     curses.A_REVERSE if mine else 0)
        else:
            width = max(4, w - x - 2)
            start = max(0, f.cur - width + 1)
            text = _shown(f.text)[start:start + width]
            _put(scr, y, x, text.ljust(width), curses.A_UNDERLINE)
            if mine:
                cursor = (y, x + f.cur - start)
    y = 3 + min(room, len(form.fields))
    for line in _wrap("$ " + _shown(preview), w - 4)[:max(1, h - y - 3)]:
        _put(scr, y, 3, line)
        y += 1
    if form.message:
        _put(scr, h - 2, 1, form.message, curses.A_BOLD)
    _put(scr, h - 1, 1, "Enter run   Tab/Up/Down move   Left/Right pick   "
         "Esc cancel", curses.A_DIM)
    try:
        curses.curs_set(1 if cursor else 0)
    except Exception:
        pass
    if cursor:
        try:
            scr.move(*cursor)
        except Exception:
            pass
    scr.refresh()


def ask_on_terminal(form, title, preview_of):
    """Put the questions on the terminal, whatever stdin and stdout are.

    The screen goes to /dev/tty rather than to stdout, so `rig ... |
    grep` still asks on the terminal and the pipe gets only what the
    command prints.  No terminal at all -- cron, CI, a pipe with nobody
    at the other end -- is refused rather than waited on.
    """
    try:
        import curses
    except ImportError:
        return None, "this Python has no curses to ask with"
    try:
        tty = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return None, "there is no terminal to ask on"
    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    saved = (os.dup(0), os.dup(1))
    os.dup2(tty, 0)
    os.dup2(tty, 1)

    def loop(scr):
        while True:
            draw(scr, curses, form, title, preview_of(form))
            k = _key_name(curses, scr.get_wch())
            if k == "ESC":
                k = _escape_sequence(scr)
            if k is None:
                continue
            result = form.handle(k)
            if result:
                return result

    try:
        return curses.wrapper(loop), None
    except KeyboardInterrupt:
        return "cancel", None
    except curses.error as exc:
        return None, "cannot draw the questions here (%s)" % exc
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        for fd in saved + (tty,):
            os.close(fd)


# ---------------------------------------------------------------------------
# A file to start from: --init
# ---------------------------------------------------------------------------
#
# Every tool already says what its flags are, in `--help`, in `-h`, or in
# its man page.  --init reads the first of those that has any, and writes
# each flag it finds as a line with the # still on: a file you take the
# #s off rather than one you have to know the flags to write.  Written
# commented out because every flag switched on at once is a command line
# nobody wants.

HELP_TIMEOUT = 10.0
HELP_LIMIT = 2 * 1024 * 1024

# No pager, no colour, no terminal to draw on: help text, plain.
HELP_ENV = {"PAGER": "cat", "MANPAGER": "cat", "GIT_PAGER": "cat",
            "MANWIDTH": "100", "COLUMNS": "100", "NO_COLOR": "1",
            "TERM": "dumb"}

# man in a UTF-8 locale draws a flag's dash as one of these.
FANCY_DASHES = u"‐‑‒–—−"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
OVERSTRIKE_RE = re.compile(r".\x08")
OPTION_LINE_RE = re.compile(r"^( {0,30})(-{1,2}[A-Za-z0-9].*)$")
OPTION_TOKEN_RE = re.compile(
    r"^(-{1,2}[A-Za-z0-9][A-Za-z0-9_.\-]*)(\[?=([^\]]*)\]?)?$")
# What an option's argument looks like, as opposed to the first word of
# its description: FILE, <file>, {a,b}, [=WHEN], a|b.
ARG_RE = re.compile(r"^\[?(<[^>]+>|\{[^}]+\}|[A-Z][A-Z0-9_.\-]*|"
                    r"[a-z0-9]+(\|[a-z0-9]+)+)\]?(\.\.\.)?$")
DEFAULT_RE = re.compile(
    r"[(\[]\s*default[:=]?\s*['\"]?([^)\]'\"\s,;]+)['\"]?\s*[)\]]"
    r"|\bdefaults? (?:is|to)\s+['\"]?([^\s'\",;)]+)", re.I)
SKIP_NAMES = frozenset(("--help", "--version", "--usage"))


class HelpOption(object):
    """One flag as a tool's help describes it."""

    __slots__ = ("names", "arg", "optional", "desc", "spec")

    def __init__(self, names, arg, optional, spec):
        self.names = names
        self.arg = arg
        self.optional = optional
        self.desc = ""
        self.spec = spec

    def choices(self):
        if not self.arg:
            return None
        a = self.arg.strip("[]")
        if a.startswith("{") and a.endswith("}"):
            return [c.strip() for c in a[1:-1].split(",") if c.strip()]
        if "|" in a:
            return [c for c in a.split("|") if c]
        return None

    def default(self):
        m = DEFAULT_RE.search(self.desc)
        if not m:
            return None
        value = (m.group(1) or m.group(2)).rstrip(".")
        # `(default: dredge-<stamp>)` describes a default rather than
        # being one, and a file that set it would pass the description.
        return None if re.search(r"[<>{}]", value) else value

    def setting(self):
        """The line this flag becomes, by the file's own rules."""
        longs = [n for n in self.names if n.startswith("--")]
        shorts = [n for n in self.names if len(n) == 2]
        if longs:
            # An optional value only works joined, and writing the dashes
            # is how the file says to join it.
            name = longs[0] if self.optional else longs[0][2:]
        elif shorts:
            name = shorts[0][1:]
        else:
            name = self.names[0]
        if not self.arg:
            return name
        choices = self.choices()
        if choices:
            return "%s=%s" % (name, "|".join(choices))
        return "%s=%s" % (name, self.default() or "")


def _clean_help(text):
    text = OVERSTRIKE_RE.sub("", ANSI_RE.sub("", text))
    for d in FANCY_DASHES:
        text = text.replace(d, "-")
    return text.expandtabs(8)


def _parse_spec(spec):
    """`-j N, --jobs N` -> the names, the argument, whether it is optional."""
    names, arg, optional = [], None, False
    parts = [p for p in re.split(r",?\s+|,(?=-)|\|(?=-)", spec.strip()) if p]
    for n, tok in enumerate(parts):
        if tok.startswith("-"):
            # git's `--[no-]quiet`: the flag is --quiet, and the file can
            # say --no-quiet itself if it wants the other one.
            m = OPTION_TOKEN_RE.match(re.sub(r"^--\[no-?\]", "--", tok))
            if not m:
                break
            names.append(m.group(1))
            if m.group(2):
                arg = arg or m.group(3) or "VALUE"
                optional = optional or m.group(2).startswith("[")
        elif names and arg is None and (ARG_RE.match(tok)
                                        or n == len(parts) - 1):
            arg = tok
            optional = tok.startswith("[")
        else:
            break
    return (names, arg, optional) if names else None


def parse_help(text):
    """Every flag a help page or man page describes, once each, in order."""
    lines = _clean_help(text).splitlines()
    out, seen = [], set()
    i = 0
    while i < len(lines):
        m = OPTION_LINE_RE.match(lines[i].rstrip())
        i += 1
        if not m:
            continue
        indent = len(m.group(1))
        body = m.group(2)
        gap = re.search(r"\s{2,}", body)
        spec, desc = (body[:gap.start()], body[gap.end():]) if gap \
            else (body, "")
        parsed = _parse_spec(spec)
        if parsed is None:
            continue
        opt = HelpOption(parsed[0], parsed[1], parsed[2], spec.strip())
        more = [desc.strip()] if desc.strip() else []
        # The description carries on underneath, indented further: a
        # man page puts all of it there.
        while i < len(lines) and lines[i].strip():
            nxt = lines[i]
            if len(nxt) - len(nxt.lstrip()) <= indent \
                    or nxt.lstrip().startswith("-"):
                break
            more.append(nxt.strip())
            i += 1
        opt.desc = " ".join(more)
        low = opt.desc.lower()
        if SKIP_NAMES.intersection(opt.names) \
                or (opt.names in (["-h"], ["-?"]) and "help" in low) \
                or (opt.names == ["-V"] and "version" in low):
            continue
        key = opt.names[-1]
        if key in seen:
            continue
        seen.add(key)
        out.append(opt)
    return out


def help_words(command):
    """The command, and its subcommand if it has one: `git commit`.

    As far as the first word that is neither a plain name nor a script
    that is there -- `python3 tool.py` asks tool.py -- so the help asked
    for is the tool's and not a run of it: `ssh {host} uptime` asks ssh.
    """
    words = [command[0]]
    for a in command[1:]:
        if not (re.match(r"^[A-Za-z][A-Za-z0-9_\-]*$", a)
                or ("{" not in a and os.path.isfile(a))):
            break
        words.append(a)
    return words


def _capture(argv):
    env = dict(os.environ)
    env.update(HELP_ENV)
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=env, timeout=HELP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout[:HELP_LIMIT].decode("utf-8", "replace")


def read_help(words):
    """(what was run, the flags it described), from the first that has any.

    stderr counts: plenty of tools print their usage there, and exit
    non-zero when asked for -h they do not have.
    """
    tries = [words + ["--help"], words + ["-h"]]
    if len(words) > 1:
        tries.append(["man", "-".join(words)])
    tries.append(["man", os.path.basename(words[0])])
    for argv in tries:
        text = _capture(argv)
        if text:
            opts = parse_help(text)
            if opts:
                return argv, opts
    return None, []


def render_config(config, words, source, opts):
    cmd = " ".join(words)
    if config == "-":
        # A script run by its interpreter is named for the script.
        named = words[1:2] if len(words) > 1 and os.path.isfile(words[1]) \
            else words
        config = "%s.conf" % "-".join(
            os.path.splitext(os.path.basename(w))[0] for w in named)
    out = [
        "# %s -- settings for rig, read from `%s`." % (cmd, " ".join(source)),
        "#",
        "# Take the # off each line you want.  name=value sets it, a name on",
        "# its own is a switch, name= asks every time, and a|b|c asks which.",
        "#",
        "#   rig %s -- %s" % (config, cmd),
    ]
    for o in opts:
        about = o.spec if not o.desc else "%-24s %s" % (o.spec, o.desc)
        if len(about) > 96:
            about = about[:93].rstrip() + "..."
        out += ["", "# " + about, "#" + o.setting()]
    return "\n".join(out) + "\n"


def init_config(config, command):
    """Write CONFIG from the command's own help.  Never over a file."""
    words = help_words(command)
    if os.sep not in words[0] and shutil.which(words[0]) is None:
        die("%s: command not found" % words[0], 127)
    if config != "-" and os.path.lexists(config):
        die("%s already exists; --init writes a new file, never over one"
            % config)
    source, opts = read_help(words)
    if not opts:
        die("found no flags in `%s --help`, `%s -h` or its man page"
            % (" ".join(words), " ".join(words)), 1)
    text = render_config(config, words, source, opts)
    if config == "-":
        sys.stdout.write(text)
        return 0
    try:
        with io.open(config, "x", encoding="utf-8") as fh:
            fh.write(text)
    except (OSError, IOError) as exc:
        die("cannot write %s: %s" % (config, exc.strerror or exc))
    sys.stderr.write("%s: wrote %s: %d flag%s from `%s`, every one with its "
                     "# still on\n" % (PROG, config, len(opts),
                                       "" if len(opts) == 1 else "s",
                                       " ".join(source)))
    return 0


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def split_argv(argv):
    """(rig's own arguments, the command).

    `--` ends rig's arguments.  Without one, the command starts at the
    first word after the file, so `rig app.conf mytool -v` hands `-v` to
    mytool rather than refusing it here.
    """
    ours, config = [], False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return ours, argv[i + 1:]
        if a == "--set":
            ours += argv[i:i + 2]
            i += 2
            continue
        if a.startswith("-") and a != "-":
            ours.append(a)
        elif not config:
            ours.append(a)
            config = True
        else:
            return ours, argv[i:]
        i += 1
    return ours, []


def run(argv):
    """Become the command: its output, its signals, its exit status."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        die("%s: command not found" % argv[0], 127)
    except PermissionError:
        die("%s: permission denied" % argv[0], 126)
    except (OSError, UnicodeError) as exc:
        die("%s: %s" % (argv[0], getattr(exc, "strerror", None) or exc), 126)


# canonical copy: binnacle/agree.py.
def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        usage="%(prog)s CONFIG [-n] [--set NAME=VALUE] [--init] "
              "-- COMMAND [ARG ...]",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("config", metavar="CONFIG",
                   help="the file of settings, or - for stdin")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="print the command line and stop")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="answer NAME without being asked; repeatable")
    p.add_argument("--init", action="store_true",
                   help="write CONFIG from the command's own help")
    return p


def main(argv=None):
    _stdio_safe()
    argv = sys.argv[1:] if argv is None else list(argv)
    ours, command = split_argv(argv)
    args = build_parser().parse_args(ours)
    if not command:
        die("which command? give it after the file: "
            "%s %s -- mytool" % (PROG, args.config))
    if args.init:
        if args.set or args.dry_run:
            die("--init writes the file and stops; --set and --dry-run are "
                "for running it")
        return init_config(args.config, command)

    source = "stdin" if args.config == "-" else args.config
    try:
        settings = apply_sets(parse_settings(read_config(args.config), source),
                              [os.fsdecode(os.fsencode(s)) for s in args.set])
        check_placeholders(command, settings, source)
    except ConfigError as exc:
        die(str(exc))

    answers = {}
    form = build_form(settings, command)
    if form.fields:
        script = os.environ.get("RIG_TEST_KEYS")
        if script is not None:
            result, why = ask_scripted(form, script), None
        else:
            result, why = ask_on_terminal(
                form, "%s -- %s" % (PROG, source),
                lambda f: quoted(build_argv(command, settings, f.answers())))
        if result is None:
            names = [f.setting.name for f in form.fields]
            one = len(names) == 1
            die("%s %s, and %s.\n  Answer %s here instead: %s"
                % (names[0] if one else
                   "%s and %s" % (", ".join(names[:-1]), names[-1]),
                   "needs an answer" if one else "need answers", why,
                   "it" if one else "them",
                   " ".join("--set %s=..." % n for n in names)))
        if result == "cancel":
            sys.stderr.write("%s: cancelled, nothing ran\n" % PROG)
            return CANCELLED
        answers = form.answers()

    argv = build_argv(command, settings, answers)
    if args.dry_run:
        sys.stdout.flush()
        sys.stdout.buffer.write(os.fsencode(quoted(argv)) + b"\n")
        sys.stdout.flush()
        return 0
    if form.fields:
        # The screen that showed it is gone, so the scrollback says what
        # the answers made.
        sys.stderr.write("%s: %s\n" % (PROG, _shown(quoted(argv))))
    run(argv)
    return 126


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(CANCELLED)

# ---------------------------------------------------------------------------
# Why this file has no imports from its siblings
# ---------------------------------------------------------------------------
#
# Every module in binnacle is a complete, standalone program: standard
# library only, and nothing imported from the rest of the package. That is
# not tidiness, it is a requirement. These files get copied to machines that
# have never heard of binnacle -- `netmesh` scp's itself to every host in the
# mesh, and `agree script ./why_slow.py` pushes this file to a fleet -- and a
# relative import would break the moment it landed. A helper duplicated
# across two modules, with a comment naming the canonical copy, is the
# accepted cost of that.
