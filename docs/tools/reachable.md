# reachable

**Which entries in this server list are still real?**

Pings and ssh's every entry in a host list and comments out the ones that
failed, leaving a file the other tools can still read.

```bash
reachable prod.txt                 # report; the result goes to stdout
reachable prod.txt -o pruned.txt   # write it somewhere new
reachable prod.txt -i              # rewrite in place (keeps a .bak)
reachable prod.txt --recheck-only  # only retry the ones already commented out
reachable prod.txt --csv           # one row per host, for a record
```

## What it does to the file

Given this:

```text
# production web tier
web01
db07
cache02  # the slow one

# odds and ends
auth01
ghost
noicmp   # ICMP blocked here by policy
node[01-03]
```

you get this:

```text
# production web tier
web01
# db07  #[unreachable] pings but ssh does not answer - 2026-08-15
cache02  # the slow one

# odds and ends
# auth01  #[unreachable] ssh refused your key - 2026-08-15
# ghost  #[unreachable] name does not resolve - 2026-08-15
noicmp   # ICMP blocked here by policy
# node01  #[unreachable] no route to host - 2026-08-15
node02
# node03  #[unreachable] no route to host - 2026-08-15
```

Blank lines, your comments, inline notes and the order of entries all
survive. The only changes are a leading `#` on failures and a marker saying
why.

## Why it exists

A server list rots. Boxes get decommissioned, renamed, rebuilt without your
key, or moved behind a firewall — and the list goes on naming them until
some later run fans out to 200 hosts and quietly does nothing on eleven of
them. You do not find out from the fan-out, because a tool that skips a host
looks exactly like a tool that had nothing to say about it.

## ssh is the gate, ping is the explanation

The two probes answer different questions, and conflating them gets the
answer wrong.

**ssh decides.** It is what the fleet tools actually need. A host that
answers ssh is kept **even when it does not answer ping**, because plenty of
networks drop ICMP by policy and commenting those out would throw away
working machines. The example above has `noicmp` for exactly this.

**ping explains.** It separates *pings but ssh does not answer* — a key or
firewall problem, on a machine that is up — from *no ping, no ssh*, which is
off or gone. That difference is the difference between a ten-minute fix and
a decommissioning, and it is written into the file so a later reader knows
which they are looking at.

Outcomes are distinguished rather than lumped into "failed":

| Outcome | Meaning |
|---|---|
| `auth` | up, sshd running, **your key is not on it** — a provisioning gap |
| `refused` | up, but sshd refused the connection — check the service |
| `dns` | the name never resolved, so nothing was contacted |
| `no-route` | no route to the host |
| `timeout` | ssh timed out |
| `down` | no ping and no ssh |

`--require` changes the gate: `ssh` (default), `ping`, `both`, or `any`.
`--keep-auth` keeps hosts whose only problem is your key, since those are a
provisioning gap rather than dead boxes.

## Running it again is the point

Entries this tool commented out are **re-tested on the next run and
uncommented when they come back**:

```bash
reachable prod.txt -i     # db07 is down, gets commented out
# ... db07 is repaired ...
reachable prod.txt -i     # db07 comes back, comment removed
```

The restored line is exactly what you originally wrote, inline comment and
all. So the list **converges on the truth** instead of decaying in one
direction, which makes it safe to run on a schedule. Running it twice with
no change to the fleet produces a byte-identical file.

`--recheck-only` narrows a run to just the already-commented entries, which
is the cheap version to run often.

## Your comments are never touched

Only lines carrying the `#[unreachable]` marker are managed by this tool.
A note you wrote by hand:

```text
# db07 is decommissioned, do not re-add
```

is left exactly alone — never re-checked, never uncommented, never annotated.
That is a tested guarantee, because the alternative (a tool that resurrects
a host you deliberately retired) would make it unusable on a schedule.

## Ranges

`node[01-24]`, `node[1,3,5-8]` and `rack[a-c]-node[01-04]` are expanded and
checked. If a range comes back **mixed**, the line is expanded into one line
per host so the failures can be commented individually:

```text
node[01-03]     ->    # node01  #[unreachable] no route to host - 2026-08-15
                      node02
                      # node03  #[unreachable] no route to host - 2026-08-15
```

A range cannot be half-commented, and the alternatives are both wrong:
leaving it live keeps dead hosts in the fan-out, and commenting the whole
line drops a working one. If every host in the range is fine, the line stays
compact.

## Writing safely

- Default output is **stdout**, so nothing is modified unless you ask.
- `-o FILE` writes elsewhere; `-i` rewrites in place and keeps a `.bak`
  (`--no-backup` to skip it).
- All writes are atomic — temp file plus rename. A half-written server list
  is worse than none, and a crash mid-run leaves the original intact.

## Output and exit codes

`--csv` gives one row per host:

```text
host,addr,ping,ssh,usable,outcome,detail,duration_s
```

`--json` adds a summary block. Exit `0` if everything is usable, `1` if
anything was not, `2` for a usage error — so cron can tell the difference.

The summary groups failures by outcome and says what to do about each:

```text
  4 of 10 usable
    dns       ghost
    no-route  node01 node03
    auth      auth01

  WHAT TO DO NEXT
    * Hosts under 'auth' are up and running sshd -- your key is not on them.
      They are a provisioning gap, not dead boxes; --keep-auth keeps them.
    * 1 host answered ssh but not ping -- kept, because ICMP is blocked on
      plenty of healthy networks.
```

## In a pipeline

`reachable` reads and writes the same `servers.txt` grammar that
[`agree`](agree.md), `netmesh`, `iperf_orchestrator` and `matrix_orchestrator`
consume, so pruning is a step in the same pipeline rather than a separate
chore:

```bash
reachable prod.txt -i
agree script ./why_slow.py --hosts prod.txt --mask-hosts --mask-times \
    --merge-csv triage.csv -- --csv
```

## See also

- [`agree doctor`](agree.md) — a deeper check (python3, sudo, writable
  `/var/tmp`) on hosts you already know are reachable
- [`netmesh`](netmesh.md) — when the question is not "is it up" but "is the
  path between them healthy"
