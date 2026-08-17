# binnacle

A binnacle is the housing on a ship's deck that holds the instruments. This
one holds eight, for Linux boxes, the fleets they belong to, and the networks
between them.

```{toctree}
:maxdepth: 2
:caption: Getting started

installation
composing
```

```{toctree}
:maxdepth: 2
:caption: The tools

tools/why-slow
tools/agree
tools/logtriage
tools/netmesh
tools/reachable
tools/resolve
tools/during
tools/skew
```

```{toctree}
:maxdepth: 2
:caption: Reference

cli
conventions
changelog
publishing
```

## The eight

| Tool | The question it answers |
|---|---|
| [`why-slow`](tools/why-slow.md) | Why is this box slow? |
| [`agree`](tools/agree.md) | Which hosts in this fleet disagree with the rest? |
| [`logtriage`](tools/logtriage.md) | Which ten lines of this log matter? |
| [`netmesh`](tools/netmesh.md) | Is it the network, and which link is sick? |
| [`reachable`](tools/reachable.md) | Which entries in this server list are still real? |
| [`resolve`](tools/resolve.md) | Is it DNS, and which resolver is wrong? |
| [`during`](tools/during.md) | What limited this run, and can I trust the number? |
| [`skew`](tools/skew.md) | Does this box know what time it is? |

## Diagnose, don't dump

`top` and `sar` show you numbers. `pssh` shows you outputs. `grep` shows you
lines. In every case the hard part — deciding what the numbers *mean* — is
left to you, and that is the part that takes years to learn.

These tools do that part. Every one of them ends its report with a
**WHAT TO DO NEXT** section that reads the actual numbers it just collected,
names the mechanism, and gives you the exact command to run:

```text
  VERDICT   This box is memory-starved and swapping. Everything else you
            see is a consequence of that.

  CRITICAL  memory exhausted     MemAvailable 412 MB of 62.7 GB (0.6%)
  CRITICAL  swap thrashing       14.2k pg/s in, 9.8k out (~55 MB/s)
  WARN      cpu saturated        load 34.1 over 16 cores, 61% in system time

  WHAT TO DO NEXT
    * java (pid 1842) holds 48.1 GB of 62.7 GB. Cap it and the rest of this
      report goes away.
    * While it swaps, nothing you tune elsewhere will help -- the CPU number
      above is queueing on page faults, not on work.
```

Note what that example is *not* doing: it is not leading with the CPU, even
though the CPU number is the largest one on the page. A swapping box always
looks CPU-busy, and following that number is how an afternoon disappears
into the wrong problem. Causes outrank symptoms, deliberately.

## They compose

The reason to have seven rather than any one of them:

```bash
# Prune the list first, so the fan-out is not half wasted on dead entries.
reachable prod.txt -i

# Fleet-wide triage: every box diagnosed, hosts grouped by what is wrong.
agree script ./why_slow.py --hosts prod.txt --fleet-csv --merge-csv triage.csv -- --csv

# The same trick for logs -- hosts grouped by which templates they emit.
agree script ./logtriage.py --hosts prod.txt --fleet-csv -- /var/log/syslog --csv

# why-slow says the box is fine, so ask whether it is DNS.
resolve db01.example.com

# Benchmarking rather than firefighting: what limited the run?
during -- ./benchmark.sh

# ...and then whether it is the network.
netmesh check web01 db01
```

`why-slow` and `logtriage` emit deterministic CSV; `agree` fans them out and
groups the results. That gives you *"44 hosts healthy, 4 agree they are
swapping, 2 unreachable"* in one command, which is fleet-wide triage — and
nothing else does it. See [Composing them](composing.md).

## What they will not do

Worth knowing before you install anything:

- **No agents, no daemons, no dotdirs.** Nothing is left running and nothing
  is left behind. State is re-derived by looking, not remembered.
- **No dependencies.** Standard library only, Python 3.6 and up. This is a
  hard constraint rather than a preference: `netmesh` copies itself to every
  host in the mesh and `agree script` pushes a tool to a fleet, so anything
  they needed would have to be installed everywhere first.
- **No root.** Running as root reveals more — the kernel log especially —
  and every tool says exactly what it could not see without it, rather than
  quietly checking less.
- **Nothing is changed.** These read and report. `reachable` is the single
  exception, and it only ever edits the host-list file you name.

## Install

```bash
pip install binnacle
```

Or copy one file to a machine and run it — they are self-contained by
design. See [Installation](installation.md).
