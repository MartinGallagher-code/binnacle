# resolve

**Is it DNS, and which resolver is wrong?**

Queries every configured nameserver *separately*, on the wire, compares what
they say, times the path your application actually takes, and reports which
of those is the problem — with the exact command to run next.

```bash
resolve                          # check the configured resolvers
resolve db01.example.com         # ...and what each of them says about a name
resolve --server 10.0.0.53 api   # ask one resolver instead
resolve --csv                    # one row per finding, for fleet use
resolve --explain NS_DEAD
```

## Why not dig

`dig`, `host` and `nslookup` ask **one** resolver **one** question and print
what came back. That is not the shape of the problem.

A box with three nameservers where the first one is dead resolves everything
correctly, and slowly, for ever. The stub tries the nameservers in order, so
every lookup waits out a full timeout before the second server answers
properly. Nothing is broken; everything is late. And every `dig` you run
against the working server says DNS is fine — because it is, on that server.

That failure is invisible to any tool that asks one question, and it is the
single most common DNS fault on a long-lived machine.

## What it looks like

```text
resolve -- node07, 3 resolvers, 1 name, from /etc/resolv.conf

  VERDICT   A configured resolver is dead and the others are covering for
            it. Everything still works, and every lookup pays a timeout to
            find that out.

  CRITICAL  a resolver not answering    1 of 3 resolvers did not answer:
                                        10.0.0.53 -- including the first one tried
  WARN      getaddrinfo is the slow part getaddrinfo took 5002ms where the
                                        fastest resolver answered in 2ms
  ok        resolvers disagree, a name does not resolve, tcp 53 blocked

  RESOLVERS
    10.0.0.53              no answer  (timed out after 2.0s) <- tried first
    10.0.0.54                 1.9ms  NOERROR
    10.0.0.55                 2.1ms  NOERROR
                           search example.com  ndots:1

  NAMES
    db01.example.com       10.0.0.54          10.0.0.7
    db01.example.com       10.0.0.55          10.0.0.7
    db01.example.com       getaddrinfo        10.0.0.7  (5002ms)

  WHAT TO DO NEXT
    * Resolution still works, which is exactly why this survives for months.
      The dead server is first in the list, so every lookup on this box
      waits 5 seconds for it before asking the one that works. That is your
      latency, and no query against the working server will ever show it.
      Remove it from /etc/resolv.conf, or fix it -- and if that file is
      generated, fix whatever generates it.
```

The `5002ms` on the last line is the point: the resolvers that answered are
fast, and the library is still five seconds slow. That gap is the cost of
the configuration rather than of the network.

## The rules

Thirteen, each a pure function of a fact dictionary, same as
[`why-slow`](why-slow.md). `--rules` prints them all with their reasoning;
`--explain ID` prints one.

| Rule | Fires when | Why it matters |
|---|---|---|
| `NO_NAMESERVERS` | resolv.conf lists none | nothing resolves; usually a container whose runtime failed to write the file |
| `NS_ALL_DEAD` | no resolver answered | every downstream timeout starts here |
| `NS_DEAD` | some answered, some did not | **critical if it is the first one tried** — every lookup pays its timeout |
| `NS_DISAGREE` | resolvers return different answers | split horizon or a stale cache; which answer you get is a race |
| `NAME_FAILS` | a name resolves nowhere, or only somewhere | the partial case is the one that reads as flakiness |
| `TCP53_BLOCKED` | truncated reply, TCP retry refused | only names with large answers fail; everything else works |
| `AAAA_STALL` | A answers, AAAA never does | glibc waits for both, so every lookup is right and slow |
| `NS_SLOW` | slowest resolver ≥ 100 ms | paid before every request the box makes |
| `TIMEOUT_BUDGET` | timeout × attempts × servers ≥ 20 s | longer than any client will wait, so DNS faults surface as app timeouts |
| `SEARCH_COST` | wasted queries before the real one | measured, not assumed — Kubernetes ships `ndots:5` |
| `HOSTS_SHADOW` | hosts file disagrees with DNS | beats DNS silently, on this box alone |
| `STUB_SLOW` | getaddrinfo ≫ the fastest resolver | the gap is configuration cost, not network cost |
| `RESOLV_STUB` | resolv.conf points at a local stub | says what was *not* measured |

## Measured, not assumed

Two of those deserve calling out, because the tool goes and checks rather
than reasoning from the configuration file:

- **The search list.** An unqualified name is tried against every search
  domain in turn. `resolve` walks that list against a live server and counts
  the NXDOMAINs that come back before the real answer, so the finding is
  *"2 wasted queries, costing 41ms"* rather than *"your search list is long"*.
- **AAAA.** A dropped AAAA query and a name with no AAAA record look
  identical in every log. They are distinguished here by asking for both and
  seeing whether one never comes back at all.

## Round-robin is not disagreement

Answer sets are compared **sorted**. A resolver rotating its records is
normal, healthy load balancing, and comparing them in wire order would
report every load-balanced name in the estate as a disagreement — burying
the handful that are real.

## A local stub is named as one

If `resolv.conf` points at `127.0.0.53`, everything measured describes the
`systemd-resolved` cache on this box and says nothing whatever about the
servers behind it. A fast stub in front of a sick upstream looks perfect
here until the cache misses, so the report says so outright and offers the
upstreams to measure directly:

```text
  INFO  measuring a local stub  127.0.0.53 is systemd-resolved -- the upstreams
                                behind it were not measured; it forwards to 10.0.0.53
```

## For fleets

`--csv` emits the same header as `why-slow`, so the two stack in one file:

```bash
agree script ./resolve.py --hosts prod.txt --merge-csv dns.csv -- --csv db01.example.com
```

That groups hosts by **what they think a name means** — which is how a
single resolver with a stale zone, or one rack pointed at the wrong server,
shows up as its own group instead of as intermittent application errors.

## Exit codes

`0` always, unless the tool itself failed (`2` = usage). Severity is opt-in
via `--exit-code`: `0` ok / `10` warn / `20` critical.

## Testing

The rules are driven from JSON fixtures with nothing resolving:

```bash
resolve --facts > facts.json
resolve --from-facts facts.json
```

The packet code cannot be tested that way, so the suite starts a small UDP
responder on loopback and queries it for real — including a server that
drops AAAA, one that truncates and refuses the TCP retry, and two that
disagree. The responder builds its replies independently of the tool's own
packet code, which is the point of writing it twice.

## See also

- [`why-slow`](why-slow.md) — when the resolvers are fine and the box may
  not be
- [`netmesh`](netmesh.md) — when the resolver is slow and you need to know
  whether the path to it is
- [`agree`](agree.md) — to run this across a fleet
