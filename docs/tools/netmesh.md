# netmesh

**Is it the network, and which link is sick?**

Measures round-trip time, jitter, loss and path MTU between machines when
nothing else is running — the idle baseline every other network number needs.

```bash
netmesh check web01 db01                    # one-shot, cleans up after itself
netmesh check web01 web02 db01 --for 60
netmesh selftest                            # prove it works here first
netmesh gen --servers prod.txt              # a mesh file for repeat runs
netmesh run --for 300 --grid grids/
netmesh paths --compare web03:db01 web01:db01
```

Two hosts and one line is the design centre. The mesh file, the grids and
the layered scale-up all exist, but somebody with two boxes should never
have to see them.

## What it looks like

```text
netmesh summarize -- 5 hosts, 20 measured pairs, reports span 30s

  BASELINE   p50 214us across all pairs   worst pair p50 1.9ms
  JITTER     median 11us                  worst pair 640us
  LOSS       0.31% overall  (0.31% forward, 0.00% on the way back)
  PATH MTU   9000 on 18 of 20 pairs; 2 pairs lower; 1 BLACKHOLE

  WORST PAIRS (of 20, ranked by p99)
    web03 -> db01   p50 1.9ms  p99 8.4ms  jitter 640us  loss 0.9% (fwd 0.9%)
    web03 -> db02   p50 1.8ms  p99 7.9ms  jitter 610us  loss 0.8% (fwd 0.8%)
    db01  -> web03  p50 220us  p99 400us  jitter 14us   loss 0.0%

  ASYMMETRY
    web03 -> db01 is 1.7ms slower than db01 -> web03 (8.0x)
      -- the return path is fine, so look at web03's egress queueing

  PATH MTU
    web03 -> db01  BLACKHOLE above 1500 bytes: small packets get through,
      large ones vanish and nothing reports back.
      Applications hang on big transfers and work fine on small ones.

  WHAT TO DO NEXT
    Every slow pair starts at web03, so this is web03's egress or its first
    hop, not the fabric.  Traffic *into* web03 is fine, which narrows it further.
      netmesh paths --compare web03:db01 web01:db01
    If the paths come back clean, the network is not the bottleneck:
      mx run --for 60           (packets per second)
      iperf_orchestrator.sh all (TCP bandwidth)
```

## The gap it fills

`iperf_orchestrator` answers *how much bandwidth under load*.
`matrix_orchestrator` (`mx`) answers *how many packets per second under
load*. Neither says what the network does when it is **idle** — and that is
the baseline both of those numbers have to be read against.

It is also deliberately the cheapest of the three: ten small packets per
second per pair, safe to run on production during an incident.

## How it probes, and why not ICMP

Probes are **UDP echoes between temporary agents**, which is the only
approach that gives all four of these at once:

**No root.** An ordinary unprivileged UDP socket at both ends. Raw ICMP
sockets need root or `CAP_NET_RAW`, which would break the whole
drops-onto-any-host promise. Linux's unprivileged `IPPROTO_ICMP` datagram
socket exists but needs `net.ipv4.ping_group_range` to cover your gid, which
is untrue on most distributions by default — a coin flip, not a design.

**Exact RTT with no clock synchronisation.** The sender writes its own
`time.monotonic()` into the packet and the responder echoes it back
untouched, so **only one clock is ever read**. No PTP, no NTP assumptions.

**Loss split into forward and return legs.** The responder counts what
actually arrived, independently. That is the difference between a sick
sender and a sick receiver, and it is simply not available from `ping`.

**It measures the data plane.** ICMP is handled by the router control plane
— exactly the path that gets rate-limited and deprioritised — so it
systematically lies about what application traffic sees.

### One-way delay is deliberately not reported

Without PTP-grade synchronisation, the offset between two clocks is
milliseconds and would swamp the microsecond differences that would make
one-way delay interesting. Reporting it would mean reporting a number that
is not true.

Instead both directions are measured as **separate round trips** — RTT(A→B)
timed by A, RTT(B→A) timed by B — each individually sync-free, and the two
are compared. Asymmetric routing and one-sided queueing show up clearly in
that comparison, and every number quoted is one that is actually true.

## Endpoints you cannot deploy to

A VIP, an appliance, a router, a customer address — anything you have no ssh
to. Prefix the host token with `~` and the agents reach it with `ping`
instead:

```text
src\dst,web01=10.0.0.11,db01=10.0.0.21,~gw=10.0.0.1
```

Those rows are **segregated in the report** under
`ONE-SIDED (ping only -- no receiver-side truth)` and marked `probe=ping`,
because they carry materially less:

| | agent pairs | ping-only |
|---|---|---|
| RTT p50 / p99 | full histogram | avg/max/mdev only |
| jitter | per-packet | mdev approximation |
| forward/return loss split | yes | no |
| path-MTU confirmation | yes | unsupported |
| reverse direction | yes | no |

Refusing to measure such endpoints would make the tool useless in half of
real incidents; quietly mixing their numbers in with the real ones would be
worse.

## Path MTU without root

An ordinary UDP socket with `IP_MTU_DISCOVER = IP_PMTUDISC_DO`, binary
searching the payload size, where a size counts as success **only when the
peer's echo comes back**. The kernel's own `IP_MTU` is recorded as a hint
and never as the answer — it reports a cache that can be stale or optimistic.

Four outcomes:

`confirmed`
: the search converged with an end-to-end echo

`blackhole`
: small sizes echo, large ones vanish, and **no `EMSGSIZE` came back** — a
  middlebox is dropping oversize packets *and* eating the ICMP that would
  have told the kernel. This is the classic "small requests work, large
  transfers hang" bug, and catching it justifies the whole feature.

`cached`
: the kernel already knew a smaller PMTU and the search was cut short

`unsupported`
: not Linux, or the socket option was refused — no PMTU columns are written
  at all, rather than a fabricated value

## Output

`reports/<host>.csv`, tidy long format, one row per peer per direction per
interval:

```text
ts,host,dir,peer,probe,size,target_pps,sent,recv,loss_pct,
rtt_min_us,rtt_avg_us,rtt_p50_us,rtt_p99_us,rtt_max_us,jitter_us,
path_mtu,mtu_state,agent_cpu_pct,note
```

`dir` is `tx` (this host probed peer), `rx` (this host answered peer) or
`host` (totals). Percentiles come from a constant-memory quarter-octave
histogram — about 20% wide, which is ample to tell 200 µs from 8 ms.

**A blank cell means "not measured", never zero.** A pair whose replies
dried up writes no RTT at all, because averaging blanks in as zeroes would
flatter the baseline — the one thing a baseline tool must not do.

`agent_cpu_pct` is the agent's own CPU as a share of one core. When it
approaches 100 you are measuring the tool rather than the network, and the
summary says so.

### Grids

`--grid DIR` writes N×N grids in the mesh's own shape — `rtt_p50`,
`rtt_p99`, `jitter`, `loss`, `mtu` and `asym` — matching `mx`'s grid layout,
so both tools' grids open in the same spreadsheet and read the same way:

> **A dark row is a host whose probes go out badly; a dark column is a host
> that receives badly.**

`asym_grid.csv` holds `rtt_p50(s→d) − rtt_p50(d→s)` in signed microseconds
and is antisymmetric by construction, so a positive band across one row is
that host's outbound path being slower than its inbound.

## What summarize computes

- The headline is the **median of pair p50s**, not a mean of means, so one
  sick pair neither moves the headline nor hides inside it. Worst-pair
  figures get their own columns.
- The diagnosis is computed, not canned. Three rules: slow pairs sharing a
  source → that host's egress; sharing a destination → its ingress;
  spanning a group boundary → the path between them.
- Asymmetry is flagged past `max(--asym-us, --asym-pct × min(both))`,
  defaulting to 50 µs / 30%. Both are flags because a WAN mesh and a rack
  mesh need different ones.
- **A clean run says so plainly** — `the network is not your problem` —
  because no output reads like breakage. It then points at `mx` and
  `iperf_orchestrator` for the load question.

## Verbs

| Verb | What it does |
|---|---|
| `check HOST...` | the headline: gen → deploy → probe → summarize → clean |
| `gen` | write `mesh.csv` for repeatable runs |
| `start` / `stop` | deploy and run agents / stop them, keeping reports |
| `status` | one line per host: the live ticker, or `NOT-RUNNING` |
| `summarize` | collect and diagnose; `--grid DIR` for the N×N CSVs |
| `clean` | stop and remove every trace |
| `run --for N` | start, hold, summarize, stop |
| `collect` / `logs` | fetch reports or agent logs without analysing |
| `paths` | trace the route for sick pairs; `--compare` for divergence |
| `doctor` | ssh, python, ping, tracepath, fd limits, PMTU support |
| `selftest` | two agents on loopback: no ssh, no second host, no privileges |

### paths --compare

The genuinely useful hop analysis is **divergence**: when `a→b` is sick and
`a→c` is healthy, print the two hop lists side by side and mark the first
hop where they differ. That hop is where to look.

## The mesh file

Same grammar as `mx`'s `matrix.csv`, so anyone who knows one knows the
other:

```text
# netmesh mesh v1 -- rows probe, columns answer, cells are probes/sec
# size=64 port=5310 pmtu=1 mtu_ceiling=9000 pmtu_every=300
src\dst,web01=10.0.0.11,db01=10.0.0.21,~gw=10.0.0.1
web01=10.0.0.11,,10,10
db01=10.0.0.21,10,,10
~gw=10.0.0.1,,,
```

`netmesh check` writes one into a temp directory, uses it and deletes it.
`--keep-mesh PATH` graduates a one-shot into a repeatable run.

## Leaves nothing behind

No package, no daemon, no dotdir, no sysctl. The agent is the tool's own
file, copied by scp and run from a working directory that `clean` removes.
`stop` sends SIGTERM, and the agent flushes its current interval before
exiting rather than discarding everything measured since the last one.

### When the number is the card's timer

Receive interrupt coalescing (`rx-usecs`) is the one setting that can make
`netmesh`'s own answer wrong with nothing looking wrong. A card told to wait
200 µs before raising an interrupt **cannot report a round trip faster than
that**, so a p50 near the timer is a measurement of the timer:

```text
  MEASUREMENT   rx-usecs is 200us on web03 and its fastest pair p50 is
                210us, so that number is the card's coalescing timer
                rather than the path. `ethtool -C <iface> rx-usecs 0` on
                web03 before reading latency from this run -- and put it
                back afterwards, since coalescing is there to buy
                throughput.
```

This is the *say what was done to the data* convention turned on the
instrument itself, the same way `agree` discloses its normalizations and
ping-only rows are marked `probe=ping`.

The agent reads it once at start — it is a setting, not a measurement, and
shelling out every interval would cost a subprocess a second for a number
that does not move — and records it on its own `host` row as `rx_usecs`.
There is no sysfs for it, so `ethtool` is required; where `ethtool` is
absent the value is **blank and the note is simply absent**, rather than
being taken as "coalescing is off". The note only appears when the timer is
at least half the fastest measured p50, so it stays quiet on healthy runs.

## Latency under load

`netmesh` measures an **idle** network. `iperf_orchestrator` measures TCP
bandwidth and `matrix_orchestrator` measures packets per second, both under
load. Nothing measured **what the load did to the latency** — and that is
the number everything else sharing the path actually experiences.

```bash
netmesh run --mesh mesh.txt --baseline 20 -- ./iperf_orchestrator.sh all
```

Probe idle for twenty seconds, note the split, then keep probing while the
load runs. The report gains a section comparing the two windows:

```text
  UNDER LOAD   idle -> loaded, split at 14:22:31

    web03 -> db01     p50    139us -> 8.4ms    (60x)   p99    210us -> 42.1ms  (200x)

    Latency under load rose 200x on web03 -> db01: p99 210us idle, 42.1ms
    loaded. That is the queue in front of the bottleneck filling up. The
    throughput number is real, and this is what everything else sharing
    the path paid for it -- an interactive session over this link is
    42.1ms of lag per round trip while the test runs.
```

A 9.4 Gbit/s result at 300 µs and a 9.4 Gbit/s result at 42 ms are
different results. Without this section they are the same number.

### Nothing extra is measured

The agents already write one row per interval with the timestamp on it, so
idle and loaded are two filters over rows that are on disk. That means a run
collected days ago can be re-split after the fact:

```bash
netmesh summarize --reports ./reports --load-split 1786741765
```

which is the same replay property `--from-facts` gives the diagnostic tools.

### Loss that only appears under load

A path that drops only when busy is a queue running out, not a broken link,
and it will not reproduce on an idle `check`. It gets its own finding for
that reason — it is the loss most likely to be dismissed as noise and then
rediscovered in production.

### When there is nothing to compare

A split with no rows on one side of it is reported as exactly that, rather
than as an idle baseline of zero and an infinite regression against it.
**Blank means not measured**, here as everywhere else in this package.

`--bloat-factor` moves the line (default 4×). It is a ratio rather than an
absolute, because 200 µs to 800 µs on a LAN and 20 ms to 80 ms across a WAN
are the same finding about the same queue.

## See also

- [`why-slow`](why-slow.md) — check the box before blaming the network
- [`reachable`](reachable.md) — make sure the host list is real first
- `matrix_orchestrator` (`mx`) and `iperf_orchestrator` — the same fleet
  under load, once this says the idle network is healthy
