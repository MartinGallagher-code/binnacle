# binnacle

[![PyPI](https://img.shields.io/pypi/v/binnacle.svg)](https://pypi.org/project/binnacle/)
[![Python](https://img.shields.io/pypi/pyversions/binnacle.svg)](https://pypi.org/project/binnacle/)
[![CI](https://github.com/MartinGallagher-code/binnacle/actions/workflows/ci.yml/badge.svg)](https://github.com/MartinGallagher-code/binnacle/actions/workflows/ci.yml)
[![Docs](https://readthedocs.org/projects/binnacle/badge/?version=latest)](https://binnacle.readthedocs.io)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![REUSE](https://img.shields.io/badge/REUSE-compliant-green.svg)](https://reuse.software)

A binnacle is the housing on a ship's deck that holds the instruments. This
one holds ten, for Linux boxes, the fleets they belong to, and the networks
between them.

| Tool | The question it answers |
|---|---|
| `why-slow` | Why is this box slow? |
| `agree` | Which hosts in this fleet disagree with the rest? |
| `logtriage` | Which ten lines of this log matter? |
| `netmesh` | Is it the network, and which link is sick? |
| `reachable` | Which entries in this server list are still real? |
| `resolve` | Is it DNS, and which resolver is wrong? |
| `during` | What limited this run, and can I trust the number? |
| `skew` | Does this box know what time it is? |
| `muster` | Who has which of these, and what is still outstanding? |
| `manifest` | Which servers are those, in the layout? |

```bash
pip install binnacle
```

No dependencies. Python 3.6+. No agents, no daemons, no dotdirs, no root.

One more command comes with them, and it is the one to run first:

```bash
binnacle          # what is installed here, and what version each tool says it is
binnacle help     # every tool's --help, on one page
```

`binnacle` is the housing rather than a tenth instrument -- one name to
remember instead of nine, and the only thing that will tell you a module
was left behind at an older version.

## Diagnose, don't dump

`top` and `sar` show you numbers, `pssh` shows you outputs, `grep` shows you
lines. In each case the hard part — deciding what any of it *means* — is
left to you, and that is the part that takes years to learn.

These do that part:

```text
why-slow -- node07, 2.0s sample, Linux 6.1.0-18, 16 cores

  VERDICT   This box is memory-starved and swapping. Everything else you
            see is a consequence of that.

  CRITICAL  memory exhausted     MemAvailable 412 MB of 62.7 GB (0.6%)
  CRITICAL  swap thrashing       14.2k pg/s in, 9.8k out (~55 MB/s)
  WARN      cpu saturated        load 34.1 over 16 cores, 61% in system time
  skipped   per-process I/O (needs root)

  WHAT TO DO NEXT
    * java (pid 1842) holds 48.1 GB of 62.7 GB. Cap it and the rest of this
      report goes away.
    * While it swaps, nothing you tune elsewhere will help -- the CPU number
      above is queueing on page faults, not on work.
```

Note what it is *not* doing: leading with the CPU, even though that is the
biggest number on the page. A swapping box always looks CPU-busy, and
following that number is how an afternoon disappears into the wrong problem.
**Causes outrank symptoms**, deliberately.

## They compose

```bash
# Prune the list first, so the fan-out is not half wasted on dead entries.
reachable prod.txt -i

# Fleet-wide triage: every box diagnosed, hosts grouped by what is wrong.
agree script why-slow --hosts prod.txt --fleet-csv --merge-csv triage.csv -- --csv
```

```text
  GROUP 1  44 hosts  ok    <- baseline     (nothing wrong)
  GROUP 2   4 hosts  ok                    (+SWAP_THRASH, +MEM_EXHAUSTED)
  GROUP 3   2 hosts  unreachable
```

*44 healthy, 4 agreeing that they are swapping, 2 unreachable* — in one
command. It works because `why-slow --csv` is deterministic, so two hosts
with the same problem emit byte-identical rows and land in the same group.

## The ten, briefly

### why-slow

Samples `/proc` twice, adds the kernel log, cgroup limits and filesystem
fullness, and runs 30 rules. Container-aware: inside a cgroup your own limit
is checked first, because a 4 GB container on a 256 GB host is out of memory
while the host looks fine. Seven of the rules measure **ceilings rather than
rates** — conntrack, file descriptors, task slots, ephemeral ports, the
neighbour table — because a box can be idle and still refusing work, and
those have no gradient to watch: they work until abruptly they do not. Every
rule is a pure function of a fact dictionary, so `--from-facts` reproduces
any diagnosis anywhere.

### agree

Runs a command across a fleet and reports the consensus — 47 agree, 3 don't,
here's the diff — instead of N screens of output. **A failed host is a
group, not an error**: "3 hosts never answered" is usually the finding. The
active normalizations are always printed, because "47 agree" means nothing
if you masked every number to get there.

### logtriage

Masks the variable parts of each line to find its shape, then ranks by
novelty, severity and burst rather than frequency — the most frequent line
in any log is `session opened for user`, and it has never been the answer.
On the test fixture, **3 OOM kills rank above 2,762 auth failures**. Stack
traces attach to the line above, so 40 tracebacks are one finding.

### netmesh

The idle-network baseline that `iperf_orchestrator` (bandwidth under load)
and `matrix_orchestrator` (pps under load) both need but neither provides.
UDP echoes between temporary agents, so no root is required and only the
sender's clock is ever read — RTT is exact with no clock sync. The responder
counts what arrived, so **loss splits into forward and return legs**.
Unprivileged path-MTU discovery catches the black hole where small packets
echo and large ones vanish. `--flows N` sweeps the source port so the probes
take several paths through a LAG or ECMP bundle rather than one, which is
what finds **the sick member that a single-path test hits or misses by
luck** — the fault that never reproduces.

### during

The others diagnose an instant; a benchmark is a window. Samples the box
across the run and classifies every sample into exactly one state, so the
answer is *"io for 78% of the run"* rather than whichever number was
largest when you looked. **The most useful finding is "this box was not the
bottleneck"** -- nothing near a ceiling means the limit was the load
generator, the peer, or a lock in the application, and no amount of
hardware here will move it. One core pinned while the rest idle gets its
own state, because a serialised run looks idle in every whole-box average.
Then it judges whether the number can be believed at all: warmup separated
from steady state, a cron job that ran inside the window, a clock that fell
partway through, a burst balance that ran out, a neighbour taking steal.
**Trust outranks attribution** -- a bottleneck attributed from an invalid
run is a confident wrong answer.

### resolve

Asks every configured nameserver separately rather than through the stub,
because the stub is what hides the fault: a box whose first resolver is dead
resolves everything correctly and slowly for ever, and every `dig` against
the second one says DNS is fine. **A dead resolver earlier in the list
outranks the latency it causes.** Answers are compared across resolvers --
sorted, so round-robin rotation is never mistaken for disagreement -- and
the search-domain cost is *measured* by walking the list, so the finding is
"2 wasted queries, costing 41ms" rather than a lecture about `ndots`. A
local stub is named as one, because timing `127.0.0.53` says nothing about
what is behind it.

### reachable

Pings and ssh's every entry in a host list and comments out the failures,
preserving your comments, blank lines and ordering. **ssh is the gate, ping
is the explanation** — a host answering ssh is kept even with no ping,
because ICMP is blocked on plenty of healthy networks. Entries it comments
out are re-tested next run and **uncommented when they come back**, so the
list converges instead of decaying.

### skew

Every box runs something that corrects its clock, and the failure that
matters is not that service crashing -- `why-slow` already catches a failed
time unit. It is the daemon running perfectly while the clock is still
wrong: sources unreachable, none ever selected, or a machine resumed from a
snapshot. `systemctl status` says `active (running)` through all of it. So
this ignores the daemon's opinion and asks the sources on the wire, using
the four-timestamp calculation so the path's latency lands in the *delay*
rather than in the offset. **A box with no reachable source is diagnosed as
having no reachable source**, not as being four minutes fast -- the drift
is what that produced. Sources are compared against each other, because two
that disagree means the daemon may have picked the liar; and stratum 16 is
separated from both alive and dead, since a source reporting itself
unsynchronised answers every health check and provides no time.

### muster

A pool of items handed out under a lease, once each, with an honest answer
to how much is left. The thing that actually goes wrong on a list of forty
hosts is never the work, it is the bookkeeping: two people take the same
host, somebody's laptop shuts and eleven items are held by nobody forever,
and at the end nobody can say which twelve are left. **An expired lease is
not a lease**, worked out from the timestamps as each command opens the
pool, so nothing has to run and nothing has to notice -- there is no daemon
and no reaper, only the CSV you named. Finishing after your lease lapsed is
still accepted, because the work did happen, but it is reported as a
`CONFLICT` naming whoever holds it now.

### manifest

Turns a datacenter layout into a list of hostnames. `manifest floor.dc
'rack[1-3]'` is the servers in those racks, one per line, which is already
the input to `agree`, `reachable` or `muster`. **A selector names elements
and the answer is the machines at or under them** -- one rule, so `room[1]`,
`row[A,C]`, `+gpu`, `model=hgx*` and a bare hostname all mean the obvious
thing without anyone having to know that a rack is not a server. It reads
the layout and nothing else: no ssh, no DNS, no inventory API, and the file
is never written to.

## Documentation

Full docs at **[binnacle.readthedocs.io](https://binnacle.readthedocs.io)**,
including a per-tool manual, the design conventions and a CLI reference
generated from the live parsers. Each tool's `--help` prints its own manual,
so the two cannot drift.

## Tests

```bash
bash tests/run_tests.sh          # twelve suites, 348 checks
```

No network and no second machine: `ssh` and `scp` are replaced by a shim
that runs the "remote" command locally in a sandbox, and the only real
packets are `netmesh`'s own agents and `resolve`'s DNS responder talking to
themselves over loopback.

Writing that suite found seven real bugs, three of which looked fine by
hand: CRLF line endings in a CSV, an IPv6 token mis-parsed into a nonsense
address rather than refused, and an agent that discarded everything measured
since the last interval when told to stop. Extending it has kept finding
them — the [changelog](CHANGELOG.md)'s *Fixed* sections carry the running
tally.

## Related

- [iperf_orchestrator](https://github.com/MartinGallagher-code/iperf_orchestrator) — TCP bandwidth across a fleet
- [matrix_orchestrator](https://github.com/MartinGallagher-code/matrix_orchestrator) — request/response pps across a fleet
- [provost](https://github.com/MartinGallagher-code/provost) — turn command output into a tidy dataset

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
