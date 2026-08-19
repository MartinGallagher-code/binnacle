# why-slow

**Why is this box slow?**

Samples `/proc` twice a couple of seconds apart, adds the things only
visible once — the kernel log, cgroup limits, filesystem fullness, the
kernel's own tables and their ceilings — runs thirty rules over the result,
and prints the highest-severity finding as a verdict with the exact command
to run next.

```bash
why-slow                      # diagnose this box (2s sample)
why-slow --interval 10        # longer sample, steadier numbers
why-slow --ssh node07         # diagnose a remote box over ssh
why-slow --csv                # one row per finding, for fleet use
why-slow --rules              # every rule and its thresholds
why-slow --explain SWAP_THRASH
```

## What it looks like

```text
why-slow -- node07, 2.0s sample, Linux 6.1.0-18, 16 cores, container: no

  VERDICT   This box is memory-starved and swapping. Everything else you
            see is a consequence of that.

  CRITICAL  memory exhausted     MemAvailable 412 MB of 62.7 GB (0.6%)
  CRITICAL  swap thrashing       14.2k pg/s in, 9.8k out (~55 MB/s)
  WARN      cpu saturated        load 34.1 over 16 cores, 61% in system time
  ok        no steal, no I/O errors, disks < 60% busy, no OOM kills this boot
  skipped   per-process I/O (needs root)

  EVIDENCE
    top rss    java(1842) 48.1G   postgres(991) 6.2G   node(2210) 1.1G
    pressure   cpu some 41%  io some 12%  mem some 88%  mem full 61%  (avg10)
    disks      sda  22% busy  1.1k IOPS  await 3.1ms

  WHAT TO DO NEXT
    * java (pid 1842) holds 48.1 GB of 62.7 GB. Cap it and the rest of this
      report goes away: `systemd-run --scope -p MemoryMax=32G ...` on the
      next restart, or set -Xmx directly.
    * While it swaps, nothing you tune elsewhere will help -- the CPU number
      above is queueing on page faults, not on work.
    * Confirm after: why-slow --interval 10
```

## Causes outrank symptoms

This is the single most important thing about the tool, and the reason it
exists rather than being a shell alias for four `cat`s of `/proc`.

A swapping box **also looks CPU-busy**. Its load average is enormous, its
system time is high, and every process looks slow. If you follow the largest
number you spend an afternoon tuning thread pools, and the machine is still
slow, because the CPU was queueing on page faults the whole time.

So the verdict is not "the worst number", it is the **cause**. A hand-written
precedence table pulls causes to the front:

```text
IO_ERRORS > OOM_KILLS > LIMIT_HITS > SWAP_THRASH > MEM_EXHAUSTED >
CGROUP_MEM > FS_FULL > FS_INODES > CONNTRACK_FULL > FD_EXHAUSTION >
PID_EXHAUSTION > CGROUP_PIDS > EPHEMERAL_PORTS > ARP_TABLE_FULL >
CPU_STEAL > DISK_SATURATED > IO_STALL > ... > CPU_SATURATED > PSI_CPU > ...
```

`CPU_SATURATED` still *fires*, and still appears in the findings list — it
just does not get to be the headline while something upstream of it is
burning. Failing disks and OOM kills outrank everything, because no amount
of tuning fixes a dying drive.

## The rules

Thirty, each a pure function of a fact dictionary. `--rules` prints them all
with their thresholds and the reasoning; `--explain ID` prints one.

| Rule | Fires when | Why it matters |
|---|---|---|
| `CPU_SATURATED` | load/core ≥ 1.5, busy ≥ 85%, iowait < 15% | real compute demand, not a queue behind something else |
| `CPU_STEAL` | steal ≥ 5% | the hypervisor took your CPU; nothing in the guest can fix it |
| `PSI_CPU` | `psi.cpu.some10` ≥ 20% | counts real contention, unlike load average |
| `IO_STALL` | `psi.io.full10` ≥ 10% | *everything* stopped on I/O, not just one process |
| `DISK_SATURATED` | util ≥ 90% and await ≥ 20 ms | a device at its physical limit |
| `MEM_EXHAUSTED` | MemAvailable ≤ 10% | the honest memory figure, unlike "free" |
| `SWAP_THRASH` | ≥ 1000 pg/s in+out | why *everything* is slow, including idle processes |
| `OOM_KILLS` | any in the kernel log | a process is gone; downstream failures look unrelated |
| `CGROUP_MEM` | ≥ 90% of the cgroup limit | out of memory while the host is fine |
| `CGROUP_THROTTLED` | ≥ 10% of periods throttled | latency spikes on an apparently idle box |
| `CPU_QUOTA_VS_NPROC` | nproc/quota ≥ 4 | pools sized from the wrong number |
| `FS_FULL` | ≥ 95% used | everything that logs starts failing at once |
| `FS_INODES` | ≥ 90% inodes used | ENOSPC while `df` shows free space |
| `IO_ERRORS` | in the kernel log | failing hardware; stop tuning |
| `DSTATE_PROCS` | ≥ 3 in uninterruptible sleep | stuck on a device or mount, unkillable |
| `THERMAL_THROTTLE` | clock < 70% of max while busy | paying for silicon you are not getting |
| `NET_DROPS` | ≥ 0.01% of rx dropped | ring buffer or softirq cannot keep up |
| `LISTEN_OVERFLOW` | any overflow | connections dropped *silently* |
| `TCP_RETRANS` | ≥ 2% retransmitted | loss on the path; throughput collapses |
| `FORK_STORM` | ≥ 200 forks/s | a retry loop or overlapping cron |
| `SYSTEMD_FAILED` | any failed unit | including a dead clock sync, which makes every timestamp a lie |
| `GOVERNOR` | powersave while busy ≥ 30% | a free 20–30% |
| `ENTROPY` | < 200 on kernels < 5.6 | presents *only* as slow TLS handshakes |

### Ceilings rather than rates

Everything above measures how hard the box is working. These measure how
close it is to a wall — a different failure, which reads nothing like
slowness: the load is normal, the disks are quiet, memory is fine, and
connections are being refused anyway.

| Rule | Fires when | Why it matters |
|---|---|---|
| `LIMIT_HITS` | the kernel log says a table filled | it has since drained, so **every ratio below reads healthy** |
| `CONNTRACK_FULL` | ≥ 75% of `nf_conntrack_max` | past the ceiling, new connections are dropped silently |
| `FD_EXHAUSTION` | ≥ 75% of `fs.file-max` | every `open()` and `accept()` fails at once |
| `PID_EXHAUSTION` | ≥ 75% of `pid_max` | `fork()` fails for everything, including your shell |
| `CGROUP_PIDS` | ≥ 85% of the cgroup's `pids.max` | out of task slots while the host has thousands free |
| `EPHEMERAL_PORTS` | ≥ 70% of the local port range in TIME_WAIT | no outbound connections; a rate problem wearing a capacity mask |
| `ARP_TABLE_FULL` | ≥ 75% of `gc_thresh3` | intermittent unreachability that looks like a flapping switch |

`LIMIT_HITS` is the one to read first. A table that filled an hour ago has
drained since, so the ratios are all healthy and only the line the kernel
wrote at the time survives — which is why it ranks with `OOM_KILLS` rather
than with the ratios it explains.

## Containers

Detected from `/proc/1/cgroup` and `/.dockerenv`. Inside one, your **own
limit is checked first** and the host's numbers are demoted, because a 4 GB
container on a 256 GB host is out of memory while the host is perfectly
healthy — and the host's numbers will tell you nothing is wrong.

`CPU_QUOTA_VS_NPROC` is worth calling out: `nproc` reports the host's cores,
not your quota, so a runtime that sizes its thread pool from it builds a
96-way pool for a 2-core budget and then throttles at every period boundary.
The tool names the specific variables — `GOMAXPROCS`, `-XX:ActiveProcessorCount`,
`OMP_NUM_THREADS` — with the right values already filled in.

## A missing measurement is not a zero

Every fact is `None` when it could not be measured, never `0`, and any rule
that needed it is reported as **skipped with the reason**:

```text
  skipped   oom kills          kernel log unreadable -- rerun with sudo to
                               check for OOM kills and I/O errors
  skipped   tasks waiting      no /proc/pressure -- kernel < 4.20 or CONFIG_PSI=n
```

A tool that quietly checks less when run without root is worse than one that
says so, because a clean report you cannot trust is worse than no report.
`--all` lists every skipped rule; the kernel-log skip is shown even without
it, because it is the biggest thing you lose.

## One remote box

```bash
why-slow --ssh node07
why-slow --ssh root@node07 --interval 10
```

`--ssh HOST` runs the diagnosis on HOST instead of here, by feeding this
very file to `python3 -` over ssh. Nothing is installed on the box,
nothing is copied to it and nothing is left behind: the source travels on
stdin and the report comes back on stdout, so it works on a machine that
has never heard of binnacle. The remote exit code is passed through, which
keeps `--exit-code` meaningful across the hop, and ssh's own `255` is
called out as *"the box was never diagnosed"* rather than being left to
read like a clean bill of health.

Use `root@HOST` to get the kernel-log rules (OOM kills, I/O errors);
without root they are reported as skipped, exactly as they would be
locally. `--ssh-cmd` swaps the transport if plain `ssh` is not it.

Two refusals worth knowing about: `--no-exec` contradicts `--ssh` (one
forbids subprocesses, the other *is* one), and `--csv PATH`/`--json PATH`
are refused with `--ssh` because the file would be written on the remote
box — use the bare form and redirect locally:
`why-slow --ssh node07 --csv > node07.csv`.

One host only, deliberately. Fanning out and grouping the answers is
[`agree`](agree.md)'s job:

```bash
agree script why-slow --hosts prod.txt --fleet-csv -- --csv
```

## For fleets

`--csv` emits one row per finding, deterministically ordered:

```text
host,ts,rule_id,severity,title,detail,fix
node07,1786741329,SWAP_THRASH,CRITICAL,swap thrashing,"24000 pages/s in+out...","Cap the biggest..."
```

Determinism is what makes it composable: two hosts with the same problem
produce byte-identical rows, so [`agree`](agree.md) groups them together:

```bash
agree script why-slow --hosts prod.txt --fleet-csv --merge-csv triage.csv -- --csv
```

That reports *"44 hosts healthy, 4 agree they are swapping, 2 unreachable"*.

## Exit codes

`0` always, unless the tool itself failed (`2` = usage). Severity is opt-in
via `--exit-code`, which returns `0` ok / `10` warn / `20` critical —
deliberately not `1`/`2`, so a monitoring wrapper can never confuse a
severity with a usage error.

## Testing and reproducing a diagnosis

Every rule is a pure function of the fact dictionary, so a diagnosis can be
reproduced with no machine in that state:

```bash
why-slow --facts > facts.json          # capture this box
why-slow --from-facts facts.json       # re-run the rules anywhere
```

That is also how the tool is tested: the suite drives all thirty rules from
JSON fixtures, including boundary cases either side of every threshold.
`--proc-root` and `--sys-root` point the fact layer at a synthetic tree for
testing the parsers, and `--no-exec` forbids subprocesses entirely.

## See also

- [`resolve`](resolve.md) — when `why-slow` says the box is fine and the
  next question is whether it is DNS
- [`netmesh`](netmesh.md) — when `why-slow` says the box is fine and you
  need to know whether the network is
- [`logtriage`](logtriage.md) — when you need to know *when* it started
- [`agree`](agree.md) — to run this across a fleet
