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
path_mtu,mtu_state,agent_cpu_pct,note,rx_usecs,flow,
reply_ttl,ttl_hops
```

`dir` is `tx` (this host probed peer), `rx` (this host answered peer) or
`host` (totals). `flow` is blank on every row except the per-source-port
breakdown described under [Which path did it take?](#which-path-did-it-take);
a row carrying one is part of the row above it, never an addition to it.
`reply_ttl` and `ttl_hops` are the hop count replies came back over and how
often it moved, described under [Did the path hold
still?](#did-the-path-hold-still). Percentiles come from a constant-memory
quarter-octave histogram — about 20% wide, which is ample to tell 200 µs
from 8 ms.

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

- **`PATH CHANGED`** — replies for a pair came back over more than one hop
  count, so the window spans more than one path and its average is an
  average of two experiments. A trust rule, not a performance one.
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

Addresses may be names: each agent resolves its peers **once at start**,
on its own box, and uses the numeric form from then on — replies are
attributed by source address, so the resolution has to happen where the
packets are. A name that does not resolve on the agent's box is a visible
per-row note (`cannot resolve ...`) with nothing sent, never silence.

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

## Which path did it take?

Every probe so far uses one source port, so it presents one 5-tuple to the
fabric and takes **one path** through any LAG or ECMP bundle. If a bundle
has four members and one of them is sick, a test either hashes onto it or
does not, and which one is luck.

That is the classic fault that will not reproduce. Some flows slow, most
fine, every retest disagreeing with the last — and the switch counters look
healthy, because three of the four members are.

`--flows N` sweeps the source port across `N` buckets and keeps each one's
results apart:

```bash
netmesh check web03 db01 --flows 8 --pps 80
```

```text
  PATH SPREAD  source-port buckets per pair, widest spread first

    web03 -> db01          8 flows  median p50 142us  slowest :40008 4.1ms (28.9x)  loss 3.20%
    web01 -> db01          8 flows  median p50 138us  slowest :40003 145us (1.1x)

    One path through the fabric is 28.9x slower than the rest on web03 ->
    db01: source port 40008 sees p50 4.1ms where the median flow sees
    142us. Same pair, same interval, same probe -- only the 5-tuple
    differs, so what differs is which member of a LAG or ECMP bundle the
    traffic hashes onto.
```

The reference is the **median** bucket rather than the fastest: with one
sick member out of eight the median is a healthy one, so the ratio says how
much worse that member is than the fabric's normal. `--flow-factor` moves
the line (default 3×).

Three is not timid. Two buckets of the *same* healthy loopback, 30 replies
each on a busy machine, came out 1.9× apart in testing — per-bucket p50s
are noisier than whole-pair p50s because each one rests on a fraction of
the samples. A finding at 2× would be that noise. Lower the factor only
alongside a longer run or a higher rate, so the buckets it compares are
built from enough replies to mean it.

### The rate is split, not multiplied

The buckets take turns within the rate you asked for. Eight flows at
`--pps 80` is eight buckets of 10/s, not 640 packets a second — a
measurement tool that multiplied its own load by eight when you asked it to
look harder would be causing the congestion it then reported.

The cost is per-bucket sample count, and it is why this is opt-in rather
than the default. A bucket needs 30 replies before its p50 is compared at
all; below that it is reported as thin rather than ranked, because saying
nothing would read as "the flows agree". Raise `--pps` or `--for` when the
report says so.

### One socket per bucket, not per pair

The 5-tuple already differs per peer in the destination address, so the
source port is the only part that has to vary. Eight buckets cost eight
sockets on the agent whatever the size of the mesh.

The ports are ephemeral, so they differ between runs. That is the honest
behaviour: what is sick is a member of the bundle, not a port number. The
port a bucket got is stable for the length of the run and recorded on every
row, so a finding can still be lined up against a switch's per-member
counters while the run is fresh.

### It composes with the baseline

With `--baseline`, a sick member gets split the same way everything else
does. A member that is slow idle *and* loaded is faulty — a bad optic, a
duplex mismatch, a dirty fibre. One that is only slow under load is not
broken at all: it is carrying more than its share, which is what an uneven
hash does to an otherwise healthy bundle. The two want different things
done about them, so they are reported as different findings.

## Did the path hold still?

`--flows` answers *which* path a probe took. This answers a different
question: whether the path a pair was measured over was the **same path
throughout**.

An ECMP rehash or a route flap partway through a run means the two halves
of a window went over different physical links. The window is still
reported as one measurement, so its average is an average of two
experiments — and the p50 belongs to neither.

Every reply carries the hop count it survived, in its IP TTL. `netmesh`
records it, and a change in it means the route moved:

```text
  PATH CHANGED  replies came back over more than one hop count

    b -> a                   TTL 58                 2 change(s)
    a -> b                   TTL 58, 60             1 change(s)
```

Two ways it is seen, and they are the same finding. A TTL that differs
**between** intervals is a route that moved during the run. One that
changed **within** an interval is counted by the agent as it happens, in
the `ttl_hops` column, because by report time the interval has already been
averaged.

**This is a trust rule, not a performance one.** Nothing in it says the
path got worse — `PATH CHANGED` says the numbers above it describe more
than one path, so read that pair per interval before believing its p50.
It is shaped like [`during`](during.md)'s `PEER_NOT_CONCURRENT`: a finding
about whether a measurement means what it appears to.

It needs no traceroute and no root — just `IP_RECVTTL` on the socket. Where
the platform does not have it, both columns are blank and the rule skips,
because blank means *not measured* and is not the same as *the route held*.

The TTL is read on **every** socket, including each `--flows` bucket. They
are the receive path too, and reading it off the main socket alone would
record the hop count for one probe in N and report it as the path.

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
