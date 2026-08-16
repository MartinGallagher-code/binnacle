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
agree script ./during.py --hosts prod.txt --mask-hosts --mask-times \
    -- --seconds 60 --csv
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

## See also

- [`why-slow`](why-slow.md) — the same reasoning applied to a single instant
- [`netmesh`](netmesh.md) — when `during` says the run was network bound
- [`agree`](agree.md) — to run this across a fleet
