# `binnacle`

**What is in this binnacle, and how to drive it.**

A binnacle is the housing that holds the instruments. `binnacle` is the
housing talking: it is not a ninth instrument, it is the index. It answers
the two questions you have before you can use any of the others — *what is
installed here*, and *how do I drive it* — without needing to already know
the eight names.

```bash
binnacle                # the instruments installed here, and their versions
binnacle help           # every instrument's --help, in one page
binnacle help netmesh   # just that one
```

## What you get

```text
binnacle 0.5.0 -- the housing, and what is in it

  why-slow   0.5.0  why is this box slow?
  agree      0.5.0  which hosts in this fleet disagree with the rest?
  logtriage  0.5.0  which ten lines of this log matter?
  netmesh    0.5.0  is it the network, and which link is sick?
  reachable  0.5.0  which entries in this server list are still real?
  resolve    0.5.0  is it DNS, and which resolver is wrong?
  during     0.5.0  what limited this run, and can you trust the number?
  skew       0.5.0  does this box know what time it is?
  muster     0.5.0  who has which of these, and what is still outstanding?
  manifest   0.5.0  which servers are those, in the layout?

  10 instruments, all at 0.5.0.

  binnacle help        every instrument's --help, in one page
  binnacle help TOOL   just that one
```

`binnacle help` concatenates every tool's `--help`, verbs included, so one
page is the whole manual for the package **as installed** rather than as
documented somewhere else. It is around 1,400 lines; `binnacle help | less`
is the intended reading, and a closed pipe is not an error.

## Why the version column is per-tool

It would be shorter to print the package version once. It would also hide
the failure this is most useful for.

Each of these files carries its own `VERSION`, because each is routinely
copied to a machine that has never heard of this package — `netmesh` scp's
itself across a mesh, `agree script why-slow` pushes a tool to a fleet — and
out there its own `VERSION` is the only version there is. `agree` then
groups a fleet by what `--version` reports. So one module left behind at an
older number does not read as a bad install: it reads as version skew across
the whole fleet, and the hunt starts in the wrong place.

Every instrument is therefore asked separately, and a disagreement is a
finding:

```text
  SKEW     netmesh says 0.4.0; this command says 0.5.0.
           A tool carries its own version because it is
           routinely copied to a machine on its own, and
           `agree` groups a fleet by what --version says.
           One module left behind reads as fleet-wide
           skew rather than a bad install here. Reinstall.
```

A file that cannot be imported at all is reported the same way, as `BROKEN`,
and the other nine still list — one unimportable file must not cost you the
answer to *what is installed here*.

## It runs nothing

Formatting a tool's help builds its argparse parser. It does not execute the
tool, touch the network, or read `/proc`. This command reads the directory it
lives in and nothing else, which is why it is safe to run first on a box you
know nothing about.

## Options

```text
--paths     name the file each instrument was loaded from, in place of the
            question it answers -- which install am I actually running?
--quiet     the table alone: no heading, no verdict, no hints
```

`--paths` is the one to reach for when two installs shadow each other and
`pip show` and `which` disagree.

## Exit status

| Code | Meaning |
|---|---|
| `0` | every instrument loaded and agreed on a version |
| `1` | an instrument could not be loaded, or a version disagrees |
| `2` | usage error — an unknown verb, or a tool that is not installed |

The non-zero cases are both *"this install is not self-consistent"*, which
makes `binnacle >/dev/null` a usable post-install check in a Dockerfile or a
configuration-management run.

## Why this one file may read its siblings

Every other module in binnacle is a complete standalone program with no
imports from the rest of the package — a hard requirement, since those files
get copied to machines that have never heard of binnacle, and a sibling
import would break the moment one landed. See
[Conventions](../conventions.md).

`binnacle` is the exception, and only because it is not an instrument.
Its entire subject is which instruments are present and what they say about
themselves, which cannot be answered without reading them. Copying it
somewhere on its own would be meaningless, so the constraint that produces
the rule does not apply to it.

It still does not `import binnacle`. It loads each file from the directory it
is in, so it reports the tools actually sitting beside it rather than
whichever ones a `sys.path` search happened to find first — which is the
question being asked.
