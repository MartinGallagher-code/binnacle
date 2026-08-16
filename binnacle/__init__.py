# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""binnacle -- instruments for Linux boxes, fleets and the networks between.

A binnacle is the housing on a ship's deck that holds the instruments. This
one holds seven:

    why-slow    why is this box slow?
    agree       which hosts in this fleet disagree with the rest?
    logtriage   which ten lines of this log matter?
    netmesh     is it the network, and which link is sick?
    reachable   which entries in this server list are still real?
    resolve     is it DNS, and which resolver is wrong?
    during      what limited this run, and can you trust the number?

Each is a complete standalone program with no imports from its siblings and
no dependencies beyond the standard library, because they are routinely
copied onto machines that have never heard of this package.  Importing this
module is therefore optional: `from binnacle import netmesh` works, and so
does `scp netmesh.py somehost:` followed by running it there.
"""

VERSION = "0.2.0"
__version__ = VERSION

__all__ = ["VERSION", "TOOLS"]

#: The console scripts this distribution installs, and what each answers.
TOOLS = {
    "why-slow": "why is this box slow?",
    "agree": "which hosts in this fleet disagree with the rest?",
    "logtriage": "which ten lines of this log matter?",
    "netmesh": "is it the network, and which link is sick?",
    "reachable": "which entries in this server list are still real?",
    "resolve": "is it DNS, and which resolver is wrong?",
    "during": "what limited this run, and can you trust the number?",
}
