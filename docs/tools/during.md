# during

**What limited this run, and can I trust the number?**

Samples the box across a whole window — a benchmark, a load test, a
migration — classifies every sample into exactly one state, and reports
which resource was at its ceiling for the largest share of the run, along
with everything that happened during it that makes the number untrustworthy.

```bash
during -- ./benchmark.sh          # wrap a command; its output and status pass through
during --seconds 120              # watch a window while something else drives load
during --samples run.csv -- make  # keep the raw series for plotting
during --from-samples run.csv     # re-analyse a saved series anywhere
during --baseline yesterday.csv -- ./benchmark.sh
```

## Why the other tools cannot answer this

`why-slow` samples for two seconds and diagnoses an instant. A benchmark is
a window with phases, and three questions about it are invisible to any
point-in-time tool:

- **Attribution over the window.** The bottleneck is the resource at its
  ceiling for the *longest*, not the one with the biggest number when you
  happened to look. A single sample lands in one phase or the other and
  reports whichever it found.
- **Whether the box was the bottleneck at all.** If nothing was near a
  ceiling, the limit was the load generator, the peer, or something
  serialised inside the application — and no amount of hardware here will
  move the number.
- **Whether the number can be believed.** Warmup averaged in with steady
  state, a cron job inside the window, a clock that fell partway through, a
  burst balance that ran out, a neighbour taking steal. Each turns the
  benchmark into a measurement of something else, and each is visible only
  as a change *across* the run.

## What it looks like

```text
during -- node07, 60s window, 60 samples, wrapping benchmark.sh

  VERDICT   Something else was running inside the benchmark window, so the
            result measures both.  Fix that before reading the bottleneck
            below.

  WARN      something else ran     backup took 71% CPU in 18% of the samples
  INFO      what limited the run   io for 68% of the run (waiting on storage)
  INFO      warmup in the average  steady only from 9s, so 15% of the samples are warmup
  ok        cpu clock fell, hypervisor took time, storage throttled (+3)

  TIMELINE  (60 samples, 1.0s apart)
    cpu  ..::=++**###########**###########**#########**####
    core :::==++**##################################**#####
    io   ..::==++*#########################################
    bound .....IIIIIIIIIIIIIIIIIIIIIIIIIIIICCCIIIIIIIIIIIII
    C cpu  1 one core  I io  M mem  N net  T throttled  S stolen  . not bound
              ^ steady from here

  SHARES
    io             68%   waiting on storage
    cpu             7%   at the CPU's ceiling
    not bound      25%   not at any ceiling
```

## One state per sample

Each sample gets exactly **one** state, chosen by precedence. Reporting two
at once would be a non-answer, and the shares would not add up:

| State | The sample looked like |
|---|---|
| `stolen` | ≥ 10% steal — the hypervisor took the time |
| `throttled` | ≥ 20% of scheduling periods throttled by a quota |
| `memory` | paging, or `psi.mem.full` ≥ 10% |
| `io` | `psi.io.full` ≥ 10%, or a device ≥ 85% busy, or iowait ≥ 25% |
| `network` | ≥ 80% of the interface's link rate |
| `cpu` | ≥ 85% busy across the whole box |
| `one core` | a single core ≥ 95% while the box looked idle |
| `not bound` | nothing near a ceiling |

The order is `why-slow`'s argument again: a box that is paging also looks
CPU-busy, and a box whose quota is exhausted looks idle, so causes are
tested before the symptoms they produce.

### `one core` earns its own state

A single-threaded benchmark pins one core and leaves an eight-core box
reading 12% busy. Every whole-box average calls that idle, and the
conclusion — "the machine was not the bottleneck" — is wrong in the most
expensive way, because the next step is usually to buy a bigger instance
that changes nothing. The work is serialised, and that is the finding.

## Trust outranks attribution

The verdict leads with whatever makes the run untrustworthy, and only then
reports the bottleneck:

```text
VERDICT   The CPU slowed down partway through, so this run averages two
          different machines.  Fix that before reading the bottleneck below.
```

A bottleneck attributed from an invalid run is a *confident wrong answer*,
which is worse than no answer — so `SHORT_RUN`, `STEAL`, `INTRUDER`,
`CLOCK_DRIFT`, `BURST_EXHAUSTED` and `UNSTABLE` all rank above `BOUND_BY` in
the verdict, while still appearing as findings in their own right.

## The rules

| Rule | Fires when | Why it matters |
|---|---|---|
| `NOT_BOUND` | ≥ 50% of samples at no ceiling | the limit is off this box entirely |
| `BOUND_BY` | one state holds ≥ 50% of the run | the attribution itself |
| `SHORT_RUN` | < 10 samples | every share below is a coin toss dressed as a percentage |
| `UNSTABLE` | key metric varies ≥ 20% in steady state | the run cannot be compared with itself an hour later |
| `WARMUP` | ≥ 10% of samples before steady state | averaging it in reports a number never sustained |
| `INTRUDER` | a process ≥ 20% CPU in part of the window | the result measures the benchmark *and* it |
| `CLOCK_DRIFT` | clock ≥ 7% lower by the end | the run averages two different machines |
| `BURST_EXHAUSTED` | throughput collapses, utilisation stays pinned | a credit budget ran out, not a busier device |
| `STEAL` | ≥ 3% steal across the run | not repeatable by anyone, including you |
| `SHIFTED` | the binding state changed during the run | there is no single bottleneck to report |
| `BASELINE_DRIFT` | `--baseline` differs by ≥ 10%, or hit a different ceiling | the two runs are different experiments |

## The timeline is a fixed width

One column per sample up to 60, and buckets after that — a ten-minute run at
the default interval is 600 samples, and one character each would be a
600-character line that survives neither a terminal nor a paste into a
ticket. Each column shows the **peak** of its bucket, because a spike a mean
would smooth away is the thing worth seeing, and the header says how many
samples went into a column so a 60-wide picture is not misread as a
60-sample run. The `bound` row shows the state holding the most samples in
each bucket, ties broken by the same precedence the headline attribution
uses — so the picture and the verdict cannot disagree.

## The benchmark is not its own interloper

When a command is wrapped, its entire process tree is excluded from
interloper detection — re-derived every sample, so a benchmark that forks
workers mid-run is still recognised as itself. Without a wrapped command
nothing can be excluded, and the finding says so rather than pretending.

## Steady state

Warmup is found by comparing each sample against the median of the run's
second half and taking the first index after which nothing deviates by more
than 20%. The stability figure is then computed over the steady window only.
Both are judged on whichever series the run was actually leaning on — the
busiest core, the disk, the interface — rather than always on the CPU, which
would call every disk-bound run unstable each time the CPU idled.

## Two grains of CSV

- `--csv` is the **findings**, with the same header as
  [`why-slow`](why-slow.md), so `agree --merge-csv` stacks them and hosts
  group by what limited them.
- `--samples` is the **raw series**, one row per sample, tidy and
  deterministic — the artifact for plotting. Blank means not measured.

```bash
# Every host diagnosed while the fleet is under load, grouped by what bound it
agree script ./during.py --hosts prod.txt --fleet-csv -- --seconds 60 --csv
```

## With the rest of the toolchain

`during` does not generate load; it watches while something else does.

| Question | Tool |
|---|---|
| How much TCP bandwidth under load? | [`iperf_orchestrator`](https://github.com/MartinGallagher-code/iperf_orchestrator) |
| How many packets per second under load? | [`matrix_orchestrator`](https://github.com/MartinGallagher-code/matrix_orchestrator) |
| What was each box doing while that ran? | `during` |
| Which settings actually matter? | [`doehelper`](https://doehelper.com) |

Running it **on the load generator as well as the target** is the habit worth
forming: a generator reporting `cpu` or `one core` while the target reports
`not bound` means the benchmark measured the generator.

## Exit codes

The wrapped command's own status passes through, so `during -- make bench`
fails exactly when `make` does. `--exit-code` opts into severity instead
(`0` ok / `10` warn / `20` critical) — **except when the command failed**,
which always wins: reporting `warn` for a benchmark that never finished
would hide a build failure behind a diagnosis of it, and there is nothing
worth reading in a run that did not complete.

A command killed by a signal reports `128 + N`, the same as a shell would.
`Popen` gives `-N` for those, and passing that through hands the shell
`256 - N` — `254` for a plain `^C`, where running the command directly
would have given `130`.

`^C` finishes the run and prints the report for what was sampled, and does
**not** wait on a command that ignored the signal — losing a twenty-minute
benchmark's diagnosis to an impatient keystroke would be the worst behaviour
this could have, and so would holding the report until a benchmark that
traps `SIGINT` decides to finish. The command is given two seconds to exit
so its real status can be collected, and is then reported as still running
rather than killed: stopping somebody's benchmark uninvited would be
destructive without saying so, and saying so is cheaper.

## The receive path

Everything above watches the box as a whole, and a whole-box average is
structurally incapable of showing the one failure a network test lives or
dies on: **a machine that cannot pick packets up fast enough is not busy.**
Its work is in softirq, on one core, and every aggregate number on the page
reports it as almost idle.

So five more facts are sampled, all from procfs and sysfs, no root and no
`ethtool` needed:

| Column | From | What it catches |
|---|---|---|
| `softirq_max_core_pct` | per-CPU `/proc/stat` | receive processing pinned to one core |
| `softnet_drop_per_s` | `/proc/net/softnet_stat` | the kernel backlog overflowing |
| `time_squeeze_per_s` | `/proc/net/softnet_stat` | NAPI polls cut off with work still queued |
| `net_rx_missed_per_s` | `/sys/class/net/*/statistics/` | the card dropping because the ring was full |
| `net_cc`, `net_rmem_max_kb`, `net_numa` | sysctl and sysfs | whether the run could have reached line rate at all |

These are aggregated **worst-of, not mean-of**. A ring that overflowed for
ten seconds of a five-minute run dropped packets, and averaging that toward
zero would report a clean run.

### The cause outranks the state it produced

A box pinned in softirq is *why* the run looks network bound. Reporting
"network" as the verdict sends someone to the switch — the same mistake
`why-slow` refuses to make when it puts swapping ahead of the CPU number
that swapping caused. So a receive-path cause leads the verdict:

```text
  VERDICT   This box could not pick packets up fast enough: receive
            processing saturated a single core while the others idled.
            That is a host limit rather than a network one, and any loss
            or throughput figure from the far end is measuring it.

  CRITICAL  one core pinned in softirq   one core reached 96% softirq while
                                         the box averaged 12% busy
```

Note the second half of that verdict. Packets dropped **on this box** are
losses the network never caused, and every network-side measurement will
blame the path for them.

### Was the number even reachable?

Throughput on a single TCP flow cannot exceed *window ÷ round trip*,
whatever the link can do. A test whose ceiling was the receive buffer
measured the buffer and reports a number the network had no part in — which
is `during`'s existing **trust outranks attribution** rule applied to the
network, so `WINDOW_LIMITED` leads the verdict rather than sitting among the
findings.

The round trip is not measured here — `during` watches one box and a round
trip needs two — so it arrives by flag from whatever did measure it:

```bash
netmesh check web03 db01            # p50 comes out of this
during --rtt-ms 40 -- ./bench.sh    # ...and goes in here
```

Without it the rule **skips and says so**, naming the flag and `netmesh`,
rather than assuming a number. A 10 Gbit link at 40 ms is a ~49 MB
bandwidth-delay product; the common 6 MB `tcp_rmem` ceiling caps one flow
near 1.2 Gbit/s, and a test that reports 1.2 Gbit/s as "what the path can
carry" is wrong by a factor of eight.

### Traffic that was not yours

`during` has always checked the CPU equivalent — `INTRUDER`, something else
running inside your window makes the result a measurement of both. The link
was never checked, and it is the easier one to miss: a backup or a
replication stream through the same interface leaves no trace in the
benchmark's own output and is indistinguishable from your own traffic in
every whole-interface number.

```bash
during --expect-mbps 1000 -- ./iperf_orchestrator.sh all
```

Tell it what the test should have been pushing, and the interface total is
checked against it. Like `--rtt-ms`, the fact arrives from outside because
`during` cannot know it, and without the flag the rule **skips and names
it** rather than guessing. It is a trust rule, so it leads the verdict:

```text
  VERDICT   The interface carried substantially more than this test says
            it sent, so the window includes traffic that was not yours and
            the throughput number is a measurement of both.
```

## The other end

A network test has **two** machines in it, and every tool in this package
watches one. The composing guide has told you to run `during` on the load
generator as well as the target since before anything computed it:

> A generator that reports `cpu` or `one core` while the target reports
> `not bound` means the benchmark measured the generator, which is the most
> common way a load test lies — and neither machine's own numbers say so on
> their own.

`--peer-samples` makes that a finding instead of something you notice by
reading two reports side by side:

```bash
# on each end, over the same window
during --seconds 60 --samples target.csv
during --seconds 60 --samples loadgen.csv

# then, from either one
during --from-samples target.csv --peer-samples loadgen.csv
```

```text
  VERDICT   This box was not the bottleneck -- loadgen was, at its cpu
            ceiling. The result describes that machine, and neither end's
            own report could have said so.
```

Both ends go through the same `analyse()`, so the peer's facts are derived
exactly as this run's were. Four rules need both machines:

| Rule | Fires when |
|---|---|
| `PEER_NOT_CONCURRENT` | the two windows did not overlap |
| `PEER_WAS_THE_LIMIT` | this box at no ceiling, the far end at one |
| `NEITHER_END_BOUND` | both idle — the limit is the path or the application |
| `PEER_DROPPED` | the far end's card dropped packets |

`PEER_NOT_CONCURRENT` is a **trust** rule and leads the verdict, because two
windows that never coincided are two experiments and every conclusion below
is drawn from comparing them. It is also the one place two of these tools
meet: two machines that disagree about the time will report windows that did
not overlap when they did, and [`skew`](skew.md) is what tells you that is
what happened.

`NEITHER_END_BOUND` is the finding that most needs two machines. Both ends
with capacity to spare and the number still short means the limit is between
them or inside the application — the path, a lock, or a single flow that
cannot fill the link. No single box's report can reach it.

## See also

- [`why-slow`](why-slow.md) — the same reasoning applied to a single instant
- [`netmesh`](netmesh.md) — when `during` says the run was network bound
- [`agree`](agree.md) — to run this across a fleet
