# `reckon`

**What should this run have reached, and where did it fall short?**

Dead reckoning is working out where a ship should be from its speed and
heading; the fix is where it actually is, and the gap between the two is the
thing worth explaining. This does that for a network test. The layout says
what was built — NIC speeds, rack uplinks — and a run of
[matrix_orchestrator](https://github.com/MartinGallagher-code/matrix_orchestrator)
or [iperf_orchestrator](https://github.com/MartinGallagher-code/iperf_orchestrator)
says what was achieved. `reckon` works out what every flow could have had on
that hardware, compares, and says which part fell short and why.

```bash
reckon floor.dc --mx reports/                    # an mx run against the hardware
reckon floor.dc --iperf results/latest           # an iperf run against it
reckon --nic-gbps 25 --mx reports/               # no layout: the NICs alone
reckon floor.dc --mx reports/ --idle idle/       # and queueing, against netmesh
reckon floor.dc --mx reports/ --overlay r.tsv    # paint it on the floor plan
reckon floor.dc --mx reports/ --baseline tue.json # what changed since Tuesday
```

## The question the other tools leave open

`mx summarize` says a host sent 61% of its target. `iperf_rel_median` says a
direction ran at 45% of the run's median. Both are relative: to what was
asked for, or to the rest of the fleet. Neither can say whether the fleet as
a whole is where its hardware puts it — when every host runs at half its NIC,
every host sits at 100% of a median that is itself wrong — and neither can
say *which* part of the hardware a shortfall is against.

An absolute answer needs the hardware, and the hardware is already written
down: the [datacenter viewer](https://github.com/MartinGallagher-code/datacenter_visualization)'s
`.dc` layout holds the racks and what is in them, and [`manifest`](manifest.md)
already reads it. `reckon` reads the same file, with the speeds added as
ordinary attributes.

## Declaring the hardware

| Attribute | On | Meaning |
|---|---|---|
| `nic_gbps=` | a server, or any container above it | the NIC's line rate, Gb/s |
| `nic_mpps=` | the same | the NIC's packet rate, Mpps — optional, for small packets |
| `mtu=` | the same | the MTU its TCP segments are cut to — optional |
| `uplink_gbps=` | a rack | the speed of each uplink, Gb/s |
| `uplinks=` | a rack | how many — default 1 |

```text
dc MX1
  room wr01
    row A nic_gbps=25 uplinks=8 uplink_gbps=100
      rack r[01..04]
        node tor role=tor name={room}{rack}tor
        node u[01..06] role=server name={room}{rack}{id}
```

Attributes inherit the way every `.dc` attribute does, so `nic_gbps=25` on a
row is every server in it, and `uplinks=8 uplink_gbps=100` on a row means
*every rack in this row has eight 100 G uplinks*. A rack's capacity is read
off the rack itself; `--group-kind pod` reads it off another container kind
instead, for a layout that groups servers some other way.

**A value that is not a number is reported, never guessed at.** `nic_gbps=25g`,
`uplinks=eight`, `nic_gbps=nan`, `nic_gbps=0` and `uplinks=2` with no
`uplink_gbps=` each become a finding naming the element, and the value is
treated as not declared. A typo silently read as a number would be the
figure every flow on that host is graded against.

`--nic-gbps` gives a speed to every host the layout leaves without one, and
with no layout at all it is the whole of the hardware: NICs, and a fabric
between them taken as non-blocking.

### What the hosts say about themselves

The layout is what somebody wrote down. What the link actually negotiated is
in `/sys/class/net/IFACE/speed`, and `--speeds` reads it — a `host Mb/s` file,
or a headed table such as [`dredge`](dredge.md) appends:

```bash
dredge --cmd 'echo speed=$(cat /sys/class/net/eth0/speed)' --parse kv \
       --tsv speeds.tsv --servers hosts.txt
reckon floor.dc --mx reports/ --speeds speeds.tsv
```

The negotiated speed is what the packets met, so it is the one the model
uses; the disagreement is a finding, not a silent substitution. Lower than
declared is **CRITICAL** — a link that came up at 10 G in a 25 G fabric caps
everything through it — and higher is a **WARN** that the layout is stale. A
host reporting `-1` is a link that is down, and is said to be.

## The runs it reads

**`--mx`** takes matrix_orchestrator's `reports/` directory, or its CSVs. The
window is the one `mx summarize --window` uses — the last 60 seconds before
the newest row in any report — so the two tools average the same intervals.
A flow's achieved rate is its **replies received per second**: the round
trips that completed, which is what the fabric delivered both ways. Its
demand is its `target_pps`; a blank target is an unpaced flow, asking for
whatever it can get.

**`--iperf`** takes `iperf-orchestrator export-overlay`'s file, or a run
directory holding `iperf_results.csv`. How the tests were driven decides what
shared the fabric, so the mode is read from the run:

| Mode | What shared the fabric |
|---|---|
| `parallel` | every test, at once |
| `sequential-host` | one sender's tests to all its peers |
| `sequential-pair` | nothing — each test had the fabric to itself |

A file that does not say gets `--iperf-mode`, and is refused without it
rather than assumed. A **rolling** run is refused outright: its probes overlap
at random, so which tests met is not recorded and no fair share can be worked
out. An overlay exported with `--overlay-reduce` is refused too, because it
keeps one median per host and has lost which flow was which. An appended
overlay holding several runs is read for its last run only, and says so.

**`--idle`** takes netmesh reports from a run with nothing else on the
fabric. Each pair's idle p50 RTT is the floor the loaded RTT is measured
against, and a confirmed path MTU sets the MTU of an iperf flow. Rows that
fall inside the measured run's own time span were taken under load, and are
left out and counted rather than averaged in.

`--names` maps the names a run used onto the layout's, one `measured layout`
pair per line, for a fleet whose test names are not its hostnames.

## How the expectation is worked out

Every flow crosses a handful of links: its sender's NIC out, its receiver's
NIC in, and, when it leaves its rack, that rack's uplinks out and the far
rack's uplinks in. An mx flow is a request and a reply, so the replies cross
the same links the other way.

**Each flow's expected rate is its max-min fair share.** Every flow rises at
the same rate until a link it crosses is full or it reaches its own target,
and whatever stopped it is its *limit*. That is the most any flow can be
promised without taking from another, which is what "this fabric should have
delivered X" means.

Worked through for the smallest interesting floor — three racks of two 10 G
servers, one 10 G uplink per rack, an unpaced mx all-to-all of 1434 B
requests and 34 B replies (12,000 and 800 bits on the wire):

- A rack's uplinks out carry its 8 cross-rack requests and the replies to
  the 8 cross-rack flows coming in: 102,400 bits per request/s, full at
  **97,656.25** requests/s. A NIC would allow 156,250, so the uplinks fill
  first and every cross-rack flow stops there, on `UPLINK`.
- Each rack's two internal flows then rise alone, into the half of each NIC
  the cross flows left: 5×10⁹ / 12,800 = **390,625**, on `NIC`.

Those two numbers are worked by hand in the test suite and the tool is held
to them.

The arithmetic on the wire is the tools' own:

- **mx**: a packet is its payload plus 66 B — preamble and SFD 8, MAC 14,
  FCS 4, inter-frame gap 12, IPv4 20, UDP 8 — the figure matrix_orchestrator
  converts with.
- **iperf**: goodput is the wire rate × (MTU − 52) / (MTU + 38) — IPv4, TCP
  and the timestamp option against Ethernet's framing. At a 1500 MTU that is
  94.1%, so a 25 G NIC reading 23.5 Gb/s in iperf is at line rate, not 6%
  short of it. ACKs are not counted.
- **Layered mx runs** (`--peers K --dwell`) share capacity only within a
  layer, because only one layer is on the wire at a time; reading the layers
  as concurrent would expect a third as much of a three-layer rotation.

When two links fill at the same moment, a flow is charged to the one it
loads most — its request leg, not a reply crossing the same NIC — and of the
two ends of that leg, the sending side. A flow out of a rack whose uplinks
are full is that rack's problem, not the one it was going to.

**What is not modelled is said, every time.** The layers above the racks are
taken as non-blocking; a rack that declares no uplinks is limited by its
NICs alone, and its expectations are upper bounds. Both are printed under
**ASSUMED**, and written into the overlay's header.

## What it looks like

```text
reckon -- 6 hosts, 30 flows, mx run, 1434 B requests, 34 B replies, last 60s

  VERDICT  The shortfall is on a host, not the fabric: b2 reaches 45% of what
           its hardware allows (median of its flows; limit r02 uplinks out)
           while the rest of r02 is at 98%.

  CRITICAL  host short      b2 reaches 45% of what its hardware allows (median
                            of its flows; limit r02 uplinks out) while the
                            rest of r02 is at 98%

  HARDWARE   6 hosts at 10 Gb/s: NIC speed 6 from the layout
             3 racks: 3 declare uplinks (1 x 10 Gb/s)
  EXPECTED   30 of 30 flows compared; expected 4.69 Mpps, achieved 3.77 Mpps
             (80%); median host 98%

  HOSTS, worst first
    host      expected      achieved  median  limit
    b2     781.25 kpps   351.56 kpps     45%  r02 uplinks out
    a1     781.25 kpps   713.87 kpps     98%  r01 uplinks out
    b1     781.25 kpps   558.59 kpps     98%  r02 uplinks out

  FLOWS, worst first
    flow          expected      achieved    eff    loss      +rtt  limit
    a1 -> b2    97.66 kpps    43.95 kpps    45%       -         -  r01 uplinks out
    b1 -> b2   390.62 kpps   175.78 kpps    45%       -         -  b1 NIC 10 Gb/s out

  WHAT TO DO NEXT
    * The fault is on b2 or its link, not the fabric: `why-slow --ssh b2` for
      the box, `during` around a rerun for what limited it, and `ethtool -S`
      on its NIC for errors and drops -- then the cable or optic.
```

A host's figure is the **median of every flow it is an end of**, in either
direction, not its total over its expectation. A host that is itself slow
has all of its flows slow; a host with one slow peer has one. That is *I am
slow* against *I have a slow peer* — `b1` above sends a quarter of its
traffic to `b2` and is still at 98%, because it is not the problem. It is
the reading `mx_rel_median` makes, for the same reason.

## The rules

They run in this order, and a host one of them explains is not reported
again by a later one: a link that came up slow, or an agent out of CPU, is
the cause, and "short" is its symptom.

| Rule | Fires when |
|---|---|
| `LINK_SPEED` | `--speeds` disagrees with the layout, or a host reports no speed |
| `NO_REPORT` | a host in the run wrote nothing, or an iperf test failed |
| `UNMODELLED` | a host has no NIC speed, or an attribute is not a number |
| `NOT_IN_LAYOUT` | a measured host the layout does not name |
| `ABOVE_HARDWARE` | a flow carried more than 105% of its expectation |
| `IDLE_OVERLAP` | netmesh rows from inside the run were left out of the baseline |
| `CPU_BOUND` | a short host whose agent ran out of CPU first |
| `HOST_SHORT` | a host below `--short` while the rest of its rack is not |
| `GROUP_SHORT` | half a rack below `--short` while the other racks are not |
| `FLEET_SHORT` | the median host below `--short` |
| `HOST_REGRESSED` | a host fell `--drop` points since `--baseline` while its rack held |
| `GROUP_REGRESSED` | half a rack fell `--drop` points while the other racks held |
| `FLEET_REGRESSED` | the median host fell `--drop` points, still above `--short` |
| `FLOW_SHORT` | flows below `--short` between hosts that are otherwise fine |
| `LOSS_BELOW_CAPACITY` | ≥1% loss on flows the hardware has room for |
| `QUEUEING` | loaded RTT ≥ `--bloat` × idle, and 50 µs more |

`reckon --rules` prints each with its reasoning, and `--explain RULE` one.

**`ABOVE_HARDWARE` is not good news.** A flow cannot beat the hardware, so
one that did means the declaration is wrong — a speed set too low, an uplink
count short of what is cabled, a host placed in the wrong rack so traffic
that never leaves one is charged to its uplinks — and every other
expectation is suspect until it is fixed. Efficiency is never clipped to
100%; the overshoot is the finding.

**A rack is only a rack fault if the other racks are fine**, the same test a
host is put to against its rack-mates. When every rack is short together
that is the fleet, and `FLEET_SHORT` says so once, with what most of the
fleet's flows were limited by. `GROUP_SHORT` then splits on where the
shortfall is: flows across the rack's boundary worse than flows inside it
put it on the uplinks; both alike put it on the switch, or on something the
rack's hosts share.

`CPU_BOUND` uses matrix_orchestrator's own lines — busiest core 85%, busiest
worker 75% of a core — and for iperf, the overlay's peak CPU and `during`'s
90% softirq line, so this never calls a host CPU-bound that the tool which
measured it called fine.

`QUEUEING` splits too. A queue where the model says a link is full is
expected, and reported as INFO. A queue on a path the declared hardware has
room for is traffic this test did not send, or a bottleneck the layout does
not declare.

## Comparing runs

Raw rates do not compare across runs: a new target, packet size or window
moves every one of them. **Efficiency does**, because each run is graded
against its own expectation. So `reckon` keeps no history of its own; the
reckoning you kept from an earlier run is the history:

```bash
reckon floor.dc --mx reports/ --run tue --json runs/tue.json
# ... a week later, after a firmware roll-out ...
reckon floor.dc --mx reports/ --run wed --baseline runs/tue.json
```

```text
  WARN      host fell       wr01r03u05 fell from 99% to 91% of what its
                            hardware allows since run tue, while the rest of
                            r03 held
```

`--baseline` takes either of the files an earlier reckoning writes: its
`--json`, or its `--overlay` (the file you probably kept for the viewer).
`--run` names a run, and the next one says "since run tue"; without it,
the comparison names the earlier run by its own last timestamp.

Three rules read it, in the same order as the shortfalls and for the same
reason — one fault that moved a whole rack is one finding, not one per
host:

- **`HOST_REGRESSED`** — a host fell `--drop` points (default 5) while the
  rest of its rack, or with no rack the fleet, held. This is the one the
  comparison exists for: a host at 91% is not short, and nothing else
  would name it, but a host that was at 99% last week has started to go
  wrong. The default is 5 rather than 10 for that reason: a healthy host
  sits near 100%, and one that has fallen 10 points from there is already
  below `--short`, where `HOST_SHORT` has it.
- **`GROUP_REGRESSED`** — half a rack fell together while the other racks
  held. A fall in the flows crossing its boundary, more than in the flows
  inside it, is capacity out of the rack that went away between the runs: a
  LAG member, an optic, ECMP. A fall inside and out alike is the switch or
  something the rack's hosts share.
- **`FLEET_REGRESSED`** — the median host fell. Every host at once is the
  test's settings, a roll-out, or the switches; when the two runs were
  described differently (another packet size, another window) it says so.

A host already below `--short` is not reported a second time as fallen.
Its shortfall finding carries its old figure instead — *"…; it was 98% in
run tue"* — because a host at 61% that was at 98% last week is a new fault,
and one that was at 60% is an old one.

**Only what both runs compared is compared.** A host one of the two runs
could not model has no change, not a fall to nothing. A host in the
baseline that is missing from this run is listed. A host modelled at
another NIC speed than last time is listed too, because part of its change
is the declaration, and which declaration was right is not something
`reckon` can know. A value in the baseline that is not a number (a
hand-edited NaN, a string, a negative efficiency) is counted and left out,
never read as a fall. A baseline from the other workload — an iperf
reckoning against an mx run — is refused: UDP request rates and TCP goodput
grade different things.

## Painting it on the floor

`--overlay` writes the viewer's own results format — the one `mx export` and
`iperf-orchestrator export-overlay` write — so it loads beside theirs with no
importer:

| Overlay | Per | What it is |
|---|---|---|
| `reckon_efficiency` | host | median flow, achieved vs expected, % |
| `reckon_expected` / `reckon_achieved` | host | pps for mx, Mb/s for iperf |
| `reckon_limit` | host | `TARGET`, `NIC`, `NIC_PPS`, `UPLINK` or `UNMODELLED` |
| `reckon_verdict` | host | `OK`, `WARN`, `FAIL` or `NO-DATA` |
| `reckon_nic_gbps` | host | the speed the model used, and `source=` where it came from |
| `reckon_added_rtt` | host | loaded RTT minus idle, worst peer (with `--idle`) |
| `reckon_peer_efficiency` | flow | one flow, with `peer=`, so the viewer draws it |
| `reckon_peer_added_rtt` | flow | the same for RTT (with `--idle`) |
| `reckon_change` | host | efficiency now minus then, in points, with `then=` (with `--baseline`) |
| `reckon_peer_change` | flow | the same for one flow (with `--baseline`) |

Efficiency arrives on a diverging ramp pinned at 0–200%, for the reason
`mx_achieved` does: 100% is *what this hardware should do*, the midpoint
rather than the top, and a host above it is as much a finding as one below.
The change since a baseline is on the same ramp centred on *no change*, from
−50 to +50 points: a fall of a few points is visibly coloured, and a
50-point one is already a fault. `--prefix` renames the
tests, and `--run LABEL` tags every sample so several runs can be kept in
one file.

## Other outputs

- **`--csv`** — the findings, one per line, in the header `why-slow`,
  `during`, `resolve` and `skew` share: `host,ts,rule_id,severity,title,
  detail,fix`. The timestamp is the run's own, so two reckonings of one run
  are byte-identical.
- **`--flows`** — one row per flow: demand, expected, achieved, sent,
  efficiency, the limit and the link it is on, loss, RTT against idle, and
  the MTU with where it came from.
- **`--json`** — all of it, with the hardware as the model used it and where
  each figure came from, the `--run` label, and with `--baseline` each
  host's and flow's figure then and the change since, and a `baseline`
  block with who fell, who rose, and what could not be compared.

Blank means not measured in all of them. A flow with no expectation has an
empty `expected`, not a zero, and its limit says `UNMODELLED`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | compared, whatever was found |
| 1 | nothing to compare: no flow has both a measurement and an expectation |
| 2 | usage error, or an input could not be read |

`--exit-code` makes it 0 / 10 / 20 for the worst finding — ok, warn,
critical — clear of 1 and 2 so a severity is never mistaken for a failure to
run.

## With the rest of the toolchain

```bash
netmesh check --reports idle/ $(cat hosts.txt)     # the idle floor, first
mx run --for 120                                   # the load; collects reports/
reckon floor.dc --mx reports/ --idle idle/ --overlay reckon.tsv
dcviz serve --layout floor.dc --results reckon.tsv # and look at it
```

`netmesh check` deletes its reports when it is done unless `--reports` names
somewhere to keep them, and it has to run before the load: reports taken
while the load is on are not a floor, and `reckon` leaves them out.

The overlay paints beside `mx export`'s: `mx_rel_median` says which hosts are
unlike the rest, `reckon_efficiency` says whether the rest are where the
hardware puts them. A host `reckon` names goes to [`why-slow`](why-slow.md) and
[`during`](during.md); a path goes to [`netmesh`](netmesh.md).

## See also

- [`manifest`](manifest.md) — the same layout, read for which machines
- [`netmesh`](netmesh.md) — the idle floor, and which hop a queue is at
- [`during`](during.md) — what limited one host's run
