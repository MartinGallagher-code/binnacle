# `rig`

**Run that command with the settings in this file.**

Rigging is everything set up on a ship before it sails. This is that for a
command line: the flags a tool needs, written down once in a file, and
asked for on the spot when the file leaves one blank.

```bash
rig app.conf -- mytool                   # mytool --jobs 20 --verbose
rig ssh.conf -- ssh {user}@{host} uptime # a setting where {host} says
rig app.conf -n -- mytool                # print the command line, run nothing
rig grep.conf --init -- grep             # a file to start from, from grep --help
```

## The file

One setting a line, and four rules:

| In the file | On the command line |
|---|---|
| `jobs=20` | `--jobs 20` |
| `verbose` | `--verbose` |
| `host=` | asks for it |
| `mode=fast\|safe` | asks which one |

Blank lines, and lines starting with `#`, are skipped. That is the whole
format; the rest of this page is what happens at the edges.

```text
# mytool, the way the nightly run wants it
jobs=20
verbose
host=
mode=fast|safe|paranoid
```

The settings go on the command line in the order the file has them, after
anything the command already says. A name written twice is two flags —
`S=web01` and `S=web02` are `-S web01 -S web02` — rather than the second one
quietly winning.

### How a flag is spelled

A one-letter name is a short flag: `j=4` is `-j 4`. Anything longer gets two
dashes. When a tool spells its flags some other way, write the dashes
yourself:

| In the file | On the command line | Why |
|---|---|---|
| `-name=x` | `-name x` | find's single-dash long flags |
| `--color=auto` | `--color=auto` | written with its dashes, it keeps its `=` |
| `suffix=-raw` | `--suffix=-raw` | a value starting with a dash is always joined |
| `n=-1` | `-n-1` | the same, for a short flag |

The joined spelling matters for an **optional value** — `ls --color[=WHEN]`
reads `--color auto` as the flag and then a file called `auto`, and only
`--color=auto` as the flag with its value. Otherwise it is two words, because
that is the one spelling every parser reads, the `case "$1" in` loops in
shell scripts included.

### Quoting

Quote a value to take it exactly as it is. That is how a value holds a `|`
without becoming a choice, and how it is deliberately empty:

```text
pattern="error|warn"     # --pattern 'error|warn', not a question
note=""                  # --note ''
```

Nothing else is special, and no shell ever sees the file: a space, a `$` or
a `;` in a value is part of that one argument.

## The questions

A setting left blank (`host=`), or given choices (`mode=fast|safe`), is asked
for when the command runs. Every question is on **one screen**, with the
command line the answers make written underneath and changing as you type.
With this `ssh.conf`:

```text
user=deploy
host=
p=22|2222|
```

`rig ssh.conf -- ssh {...} {user}@{host} uptime` asks:

```text
 rig -- ssh.conf

   host *  web01
   p        22    2222    (leave out)

   $ ssh -p 2222 deploy@web01 uptime

 Enter run   Tab/Up/Down move   Left/Right pick   Esc cancel
```

- **Tab**, **Up** and **Down** move between questions.
- **Left** and **Right** pick a choice; typing a letter jumps to the next
  choice starting with it.
- **Enter** runs the command. **Esc** runs nothing, and exits 130, as if you
  had pressed Ctrl-C.

An answer left empty leaves that flag out altogether, and so does an empty
choice: `level=|debug|trace` offers "(leave out)" first. A name marked `*` is
used in the command as a `{placeholder}`, so it has to have an answer —
Enter says so and stays on it rather than running a command with a hole in
it.

The screen is drawn on the terminal itself, not on stdout, so
`rig app.conf -- mytool | grep x` still asks you on the terminal and the pipe
gets only what mytool prints. With **no terminal at all** — cron, CI, a
pipeline nobody is watching — `rig` does not wait for an answer that cannot
come. It refuses, exits 2, and says which settings need answers and how to
give them:

```text
rig: host and mode need answers, and there is no terminal to ask on.
  Answer them here instead: --set host=... --set mode=...
```

After questions, the command line they made is written to stderr before it
runs, so the scrollback says what actually ran.

### `--set`

`--set NAME=VALUE` answers a question without asking it, or replaces what the
file says. A `--set` is an answer, not a question: `--set 'mode=a|b'` means
that string. It takes the place of every line with that name, spelled the way
the file spelled it, and a name the file does not have is added at the end.

```bash
rig app.conf --set host=web01 -- mytool
```

## Placeholders

`{name}` in the command puts that setting's value there instead of adding it
as a flag:

```bash
rig ssh.conf -- ssh {user}@{host} uptime
```

A `{name}` the file has no setting for is refused before anything is asked,
rather than run with the braces still in it. That catches a typo, and it is
also what happens to awk's `{print}` — so the refusal says to write
`{{print}}`, which is how braces that are not a placeholder are written.
Braces that could not be a name are left alone: `find -exec rm {} +`, `{a,b}`
and `{1..5}` pass through untouched.

A placeholder holds one value, so a name written twice in the file, or a
switch, is refused as a placeholder.

### Where the flags go

At the end of the command, unless the command says otherwise with `{...}`.
That matters when something after a certain point belongs to another
program:

```bash
rig ssh.conf -- ssh {...} {host} uptime   # ssh -p 2222 web01 uptime
```

Without the `{...}`, `-p 2222` would land after `uptime` and go to the remote
command instead of to ssh.

## Starting from nothing: `--init`

Every tool already says what its flags are. `--init` reads them, and writes a
file with each one on a line — commented out, under the line of help that
describes it:

```bash
rig grep.conf --init -- grep
# rig: wrote grep.conf: 46 flags from `grep --help`, every one with its # still on
```

```text
# grep -- settings for rig, read from `grep --help`.
#
# Take the # off each line you want.  name=value sets it, a name on
# its own is a switch, name= asks every time, and a|b|c asks which.
#
#   rig grep.conf -- grep

# -E, --extended-regexp    PATTERNS are extended regular expressions
#extended-regexp

# -e, --regexp=PATTERNS    use PATTERNS for matching
#regexp=

# -i, --ignore-case        ignore case distinctions in patterns and data
#ignore-case
```

Take the `#` off the lines you want, and the file runs. Everything is
commented out because every flag switched on at once is a command line
nobody wants.

- **Where the flags come from.** `COMMAND --help` first, then `COMMAND -h`,
  then the man page — the first of them that describes any flags. What a
  tool prints on stderr counts, since plenty of them put their usage there.
  `git commit` reads `git commit -h`; `python3 tool.py` reads the script's
  help, not Python's.
- **What each flag becomes.** A flag with no value is a switch. One with a
  value asks (`regexp=`), unless the help lists its choices — `{a,b}` or
  `a|b` — which become a choice, or names a default — `(default: 5)` — which
  becomes the value. An optional value (`--color[=WHEN]`) is written with
  its dashes, so it stays joined. `--help` and `--version` are left out.
- **It asks the tool, not a run of it.** Only the command and its
  subcommand are handed `--help` — as far as the first word that is not a
  plain name — so `rig ssh.conf --init -- ssh {host} uptime` asks ssh, and
  nothing is ever run against a host.
- **It never writes over a file.** A file already there is refused, exit 2.
  `-` as the file prints it to stdout instead.

## Exit status

Once the command runs, `rig` has become it: its output, its signals, its exit
status. Before that:

| Status | Meaning |
|---|---|
| 0 | `--dry-run` printed the line, or `--init` wrote the file |
| 1 | `--init` found no flags in the help or the man page |
| 2 | usage error, a mistake in the file, or questions with no terminal to ask on |
| 126 | the command could not be run |
| 127 | the command was not found |
| 130 | cancelled at the questions: nothing ran |

## Options

| Option | Meaning |
|---|---|
| `-n, --dry-run` | print the command line, shell-quoted, and stop |
| `--set NAME=VALUE` | answer NAME without being asked, or replace it; repeatable |
| `--init` | write CONFIG from the command's help; never over an existing file |

The command follows `--`. Without one, the command starts at the first word
after the file, so `rig app.conf ls -l` hands `-l` to ls. The file may be `-`
for stdin.
