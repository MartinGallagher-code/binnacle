# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""binnacle -- instruments for Linux boxes, fleets and the networks between.

A binnacle is the housing on a ship's deck that holds the instruments. This
one holds ten:

    why-slow    why is this box slow?
    agree       which hosts in this fleet disagree with the rest?
    logtriage   which ten lines of this log matter?
    netmesh     is it the network, and which link is sick?
    reachable   which entries in this server list are still real?
    resolve     is it DNS, and which resolver is wrong?
    during      what limited this run, and can you trust the number?
    skew        does this box know what time it is?
    muster      who has which of these, and what is still outstanding?
    manifest    which servers are those, in the layout?

An eleventh console script, `binnacle`, comes with them and is not one of
them: it is the housing rather than an instrument.  It lists what is
installed here with each tool's own version and prints every tool's
--help on one page.  TOOLS below stays the ten, because it is the map
of the instruments.

Each is a complete standalone program with no imports from its siblings and
no dependencies beyond the standard library, because they are routinely
copied onto machines that have never heard of this package.  Importing this
module is therefore optional: `from binnacle import netmesh` works, and so
does `scp netmesh.py somehost:` followed by running it there.
"""

VERSION = "0.4.0"
__version__ = VERSION

__all__ = ["VERSION", "TOOLS"]

#: The ten instruments, and what each answers.  The `binnacle` command
#: reads this for the order and the wording of its table, so a tool added
#: to the package and not to this map still lists -- without a
#: description, which is the visible tell that it was missed here.
TOOLS = {
    "why-slow": "why is this box slow?",
    "agree": "which hosts in this fleet disagree with the rest?",
    "logtriage": "which ten lines of this log matter?",
    "netmesh": "is it the network, and which link is sick?",
    "reachable": "which entries in this server list are still real?",
    "resolve": "is it DNS, and which resolver is wrong?",
    "during": "what limited this run, and can you trust the number?",
    "skew": "does this box know what time it is?",
    "muster": "who has which of these, and what is still outstanding?",
    "manifest": "which servers are those, in the layout?",
}
