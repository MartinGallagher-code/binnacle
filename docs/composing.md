# Composing them

Each tool is useful alone. The reason to have seven is that they fit
together: four of them produce deterministic CSV, one of them fans things
out across a fleet, and one keeps the host list honest so the fan-out is not
half wasted.

## Fleet-wide triage

The composition that motivates the whole set:

```bash
agree script ./why_slow.py --hosts prod.txt --merge-csv triage.csv -- --csv
```

That pushes `why-slow` to every host, runs it, collects the CSV, and groups
the hosts by **what is wrong with them**:

```text
agree -- 50 hosts, 3 groups, 44 agree      [normalized: trim, scrub x1]

  GROUP 1   44 hosts   ok    <- baseline
    host,ts,rule_id,severity,title,detail,fix
    %X%,1786741765,OK,INFO,nothing wrong,no rule fired,

  GROUP 2    4 hosts   ok
    hosts: node31 node47 node52 node53
    +%X%,...,SWAP_THRASH,CRITICAL,swap thrashing,...
    +%X%,...,MEM_EXHAUSTED,CRITICAL,memory exhausted,...

  GROUP 3    2 hosts   unreachable
    hosts: node09 node22
```

*44 healthy, 4 agreeing that they are swapping, 2 unreachable* — in one
command. Nothing else does this, and it works because `why-slow --csv` is
deterministic: two hosts with the same problem emit byte-identical rows, so
they land in the same group.

`triage.csv` holds the merged rows with an `ssh_host` column prepended, so
the detail is there when you want to sort or filter it:

```bash
awk -F, '$5=="CRITICAL"' triage.csv
```

## The same trick for DNS

```bash
agree script ./resolve.py --hosts prod.txt -- --csv db01.example.com
```

Hosts get grouped by **what they think a name means**. One rack pointed at a
resolver with a stale zone is one group, and it is a group you would
otherwise meet as intermittent application errors on a third of the fleet.

## The same trick for logs

```bash
agree script ./logtriage.py --hosts prod.txt -- --csv /var/log/syslog
```

Hosts get grouped by **which templates they are emitting**. This works for
the same reason: `logtriage` template ids are a hash of the masked shape, so
the same log line produces the same id on every machine.

That is precisely why `logtriage` uses ordered regex masking rather than
Drain-style clustering — see
[why not Drain](tools/logtriage.md#why-not-drain).

## The order to reach for them

There is a natural sequence when something is wrong and you do not yet know
what:

```bash
# 1. Is the list I am about to fan out to even real?
reachable prod.txt -i

# 2. What is wrong, and where?
agree script ./why_slow.py --hosts prod.txt --merge-csv triage.csv -- --csv

# 3. why-slow says the boxes are fine. Is it DNS?
resolve db01.example.com

# 4. DNS is fine too. Is it the network?
netmesh check web03 db01

# 5. The network is fine too. What changed, and when?
logtriage /var/log/syslog --split-at 14:20
```

Each step either finds the problem or rules out a layer. The tools
cross-reference each other in their own output, so the next command is
usually printed for you: `why-slow` on a healthy box suggests `resolve`,
`netmesh` and `logtriage`; `resolve` on healthy DNS suggests `why-slow` and
`netmesh`; and `netmesh` on a healthy network suggests `mx` and
`iperf_orchestrator` for the load question.

## Prune before you fan out

```bash
reachable prod.txt -i && agree --hosts prod.txt -- uptime
```

`reachable` is safe to run on a schedule because it converges: hosts it
comments out are re-tested next run and restored when they come back. A
weekly cron keeps the list true, so every fan-out afterwards is measuring
what you think it is.

## Benchmarking rather than firefighting

The tools above answer "what is wrong now". Under a benchmark the question
is different -- what limited the run, and can the number be believed -- and
that is [`during`](tools/during.md), which watches a window rather than an
instant:

```bash
# One box, wrapping the benchmark: its output and exit status pass through
during -- ./benchmark.sh

# The whole fleet, while something else drives the load
agree script ./during.py --hosts prod.txt -- --seconds 60 --csv
```

Run it on the **load generator** as well as the target. A generator that
reports `cpu` or `one core` while the target reports `not bound` means the
benchmark measured the generator, which is the most common way a load test
lies -- and neither machine's own numbers say so on their own.

## With the rest of the toolchain

`binnacle` measures an **idle** network and a **single** box's health. When
those come back clean, the question has moved to load, and that is a
different pair of tools -- with `during` watching while they run:

| Question | Tool |
|---|---|
| What does the network do when idle? | `netmesh` |
| How much TCP bandwidth under load? | [`iperf_orchestrator`](https://github.com/MartinGallagher-code/iperf_orchestrator) |
| How many packets per second under load? | [`matrix_orchestrator`](https://github.com/MartinGallagher-code/matrix_orchestrator) |
| What was each box doing while that ran? | `during` |
| Which settings actually matter? | [`doehelper`](https://doehelper.com) |

`netmesh`'s grid CSVs deliberately use the same `src\dst` shape as `mx`'s,
so both open in the same spreadsheet and read the same way: a dark row is a
sick sender, a dark column a sick receiver.

## Everything is CSV, and rendering is separate

Every tool emits tidy CSV with `--csv`, and none of them draw anything. That
is a deliberate split: the CSV is the artifact, and it goes to whatever you
normally plot with. It also means the human report can be opinionated
without that opinion contaminating the data.
