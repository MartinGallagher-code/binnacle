# skew

**Does this box know what time it is?**

Asks every configured time source directly, on the wire, compares them
against each other and against this box's clock, reads the hardware clock
alongside — and reports which of those is the problem, with the exact
command to run next.

```bash
skew                             # check the configured sources
skew --server 10.0.0.1           # ask one source instead
skew --csv                       # one row per finding, for fleet use
skew --explain NO_SYNC_SOURCE
```

## The failure it exists for

A crashed time daemon is already caught. `why-slow`'s `SYSTEMD_FAILED` rule
names it and says outright that every timestamp on the box has become a lie.

The failure nothing catches is the daemon running perfectly while the clock
is still wrong:

- its sources are unreachable, and it has nothing to steer towards
- no source was ever selected, because they all answer with stratum 16
- the machine resumed from a snapshot and the correction was never made
- someone ran `timedatectl set-ntp false` two years ago

`systemctl status chronyd` reports `active (running)` through every one of
those. Nothing on the box looks broken, and the clock is minutes out.

## Why a wrong clock is hard to see

Because it is never reported as a wrong clock. It arrives as:

- **logins failing** — Kerberos and AD reject a ticket more than five
  minutes out, so an authentication outage looks like an authentication
  problem
- **certificates that look expired** — or not yet valid, on a box whose
  clock is ahead
- **a distributed system disagreeing with itself** — leases expiring early,
  quorum flapping, "clock skew detected" buried in a log nobody reads
- **two hosts' logs that will not line up** — the one that costs the most
  time, because the person correlating them has no reason to doubt the
  timestamps

The rest of binnacle already works around this rather than checking it.
`logtriage --split-at 14:20` trusts a timestamp. `agree` has to mask the
`ts` column before any two hosts can agree about anything. `netmesh` is
built to read only the sender's clock, precisely so it never has to trust
two at once. Every one of those is a workaround for something nothing
measured — until this.

## What it measures

Each configured source is queried over SNTP and reported separately:

```text
skew -- node07, 3 sources, chronyd, from /etc/chrony.conf

  VERDICT   No time source is reachable from this box. The daemon is
            running and has nothing to steer towards, so the clock
            drifts and nothing on the box reports a fault.

  CRITICAL  no source answered         all 3 configured sources unreachable:
                                       10.0.0.1 10.0.0.2 10.0.0.3
  WARN      the clock is wrong         4m 12s behind the 0 sources that answered

  SOURCES
    10.0.0.1                 no answer  (no reply)
    10.0.0.2                 no answer  (no reply)
    10.0.0.3                 no answer  (no reply)

  WHAT TO DO NEXT
    * Nothing is correcting this clock, whatever the daemon's status says.
      Check egress on UDP 123 first -- it is the usual cause, and a
      firewall added for something else is the usual reason.
```

Note which finding is the verdict. The offset is the larger, more alarming
number, and it is a *consequence*: a box with no reachable source is
diagnosed as having no reachable source, because that is what someone has
to fix. **Causes outrank symptoms**, the same way `why-slow` puts swapping
above the CPU number it produces.

### Offset and delay, not just "ping the server"

The offset comes from the four-timestamp NTP calculation:

```text
offset = ((T2 - T1) + (T3 - T4)) / 2
delay  = (T4 - T1) - (T3 - T2)
```

`T1` and `T4` are read from this box's clock, `T2` and `T3` from the
source's. Subtracting the source's own turnaround leaves the network path,
and halving the remainder removes it from the offset — so a slow link shows
up as a large *delay* without making the clock look wrong. The best of
`--samples` queries is kept, chosen by lowest delay, because a single
sample over a congested link measures the congestion.

### A source that answers is not necessarily a source of time

Stratum 16, or the leap indicator set to alarm, is a server reporting
*itself* unsynchronised. It answers every ping, passes every port check,
and provides no time. It sits in configs for years. `SOURCE_UNUSABLE`
separates that from both "reachable" and "dead", because it is neither.

### Sources are compared against each other

Two sources that differ by more than the network could explain means one of
them is lying, and nothing on the box knows which. The daemon may well have
picked the liar. `SOURCES_DISAGREE` reports the spread; the per-source
offsets above it show which one is the outlier — usually a box someone
stood up as a local time server that lost its own upstream.

### The hardware clock, separately

The RTC keeps running when the box is off, and it is what the box comes
back with. A system clock corrected daily on a machine whose RTC is an hour
out boots wrong every time.

A delta of almost exactly a whole number of hours is not drift — it is an
RTC kept in local time rather than UTC, which is normal on anything that
has dual-booted. `RTC_DRIFT` says so rather than sending you after a CMOS
battery that is fine.

## Across a fleet

The tool earns its place here. One command, and you find out that six boxes
in one rack have been quietly four minutes out for a week:

```bash
agree script ./skew.py --hosts prod.txt --fleet-csv \
    --scrub '[0-9]+[.][0-9]+' -- --csv
```

```text
  GROUP 1  40 hosts  ok        in sync
  GROUP 2   6 hosts  WARN      (+CLOCK_OFFSET)
  GROUP 3   2 hosts  CRITICAL  (+NO_SYNC_SOURCE)
```

The `--scrub` is doing real work: findings carry measured numbers, so two
boxes wrong by different amounts are different rows and would land in
different groups. Scrubbing the magnitudes collapses "4m 12s" and "3m 51s"
onto the same finding, leaving the *shape* of the fleet — in sync,
drifting, or with nothing to sync against.

Timezone is reported for the same reason. `TZ_NOT_UTC` is INFO and not a
fault; across a fleet it is how you find the one box that was built from a
different image, whose logs will never line up with the rest by eye.

## Rules

`skew --rules` prints all of them with their thresholds; `skew --explain
RULE_ID` prints why one exists. In verdict precedence order:

| Rule | Severity | Fires when |
|---|---|---|
| `NO_SYNC_CONFIGURED` | CRITICAL | no `server`, `pool` or `peer` line anywhere |
| `SYNC_DISABLED` | CRITICAL | `timedatectl` reports `NTP=no` |
| `NO_SYNC_DAEMON` | CRITICAL | sources configured, nothing running to use them |
| `NO_SYNC_SOURCE` | CRITICAL | every configured source unreachable |
| `CLOCK_OFFSET` | CRITICAL / WARN | past `--max-offset` (300s) / `--warn-offset` (1s) |
| `SOURCES_DISAGREE` | WARN | answering sources span more than a second |
| `SOURCE_UNUSABLE` | WARN | a source reports itself unsynchronised |
| `NOT_SYNCHRONIZED` | WARN | `timedatectl` reports `NTPSynchronized=no` |
| `SOURCE_DEAD` | WARN | some, not all, sources unreachable |
| `STRATUM_HIGH` | WARN | furthest answering source is stratum 10+ |
| `RTC_DRIFT` | WARN | hardware clock more than a minute from the system clock |
| `SINGLE_SOURCE` | INFO | one source, so it cannot be checked against anything |
| `TZ_NOT_UTC` | INFO | local time is not UTC |

The two offset thresholds are where they are for concrete reasons. Five
minutes is where Kerberos and AD stop accepting a ticket, so it is the
point at which the clock is breaking things rather than merely being wrong.
One second is where correlating this box's logs against another's stops
being safe. Both move with a flag or an environment variable.

## Where it does not help

- **It does not set the clock.** Nothing in binnacle changes the machine it
  is diagnosing, and a tool that stepped a clock as a side effect of being
  run would be the worst possible thing to put in a cron job.
- **It measures against the sources this box is configured to follow.** If
  the whole site's time is wrong together, every source agrees, the offset
  is near zero, and this reports a healthy clock — correctly, since it is
  answering "does this box agree with what it was told to follow". Pass
  `--server` with something external to ask a different question.
- **PTP is not NTP.** A box taking time from a PTP daemon or a hypervisor
  may legitimately have no NTP source at all. `NO_SYNC_CONFIGURED` will
  fire; the fix text says so rather than assuming a fault.
