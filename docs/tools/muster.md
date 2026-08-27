# `muster`

**Who has which of these, and what is still outstanding?**

A muster list assigns every hand to a station and accounts for who is
present. This is that list, for work: a pool of items, handed out under a
lease, once each, with an honest answer to *how much is left*.

```bash
muster add 'web[01-40]'        # put the work in the pool
muster take 10 -o mine.txt     # lease 10, write the ticket
# ... do the work on the hosts in mine.txt ...
muster done mine.txt           # they are finished
muster status                  # what is done, held, left, stuck
```

## The problem it solves

Some jobs are a list and a promise: forty hosts to patch, nine hundred
files to re-encode, every switch in a rack to walk up to. The work gets
handed out to whoever is free, it must happen once each, and the thing
that actually goes wrong is never the work — it is the bookkeeping.

Two people take the same host. Somebody's laptop shuts and eleven items are
held by nobody, forever. At the end nobody can say which twelve of the forty
are left, so the whole list gets re-walked to be sure.

`muster` is the bookkeeping and nothing else. It does not do the work, does
not know what the work is, and never touches the items — they are strings to
it, hostnames or filenames or ticket numbers.

## The lease is the whole idea

`take` marks items held by you until a deadline. Finish, and `done` closes
them. Don't — the job died, the laptop shut, you went home — and the lease
simply runs out and the items are available again.

**Nothing has to notice this and nothing has to run.** An expired lease is
not a lease, and every command works that out from the timestamps as it
opens the pool. There is no daemon, no reaper, no cleanup step, which is
what keeps this inside the package's [stateless](../conventions.md) rule:
the only thing left behind is the pool file you named.

```bash
muster take 10 --lease 4h -o mine.txt
```

## Finishing late

If your lease lapsed while you were working, someone else may already hold
the item. Reporting it done is still accepted — the work did happen, and
refusing it would send the pool out to have it done twice more — but it is
reported as a `CONFLICT` naming both holders:

```text
  CONFLICT  web06 held by bob@box:222 since 2026-08-27T20:00:19Z -- your
            lease had lapsed; marked done anyway, their work is duplicate
[muster] a longer --lease is nearly always the fix for a CONFLICT
```

If the lease lapsed but nobody else took the item, that is a quieter
`LATE` — worth knowing, but nothing was done twice.

## Stuck items

`attempts` counts every time an item was taken. An item taken four times and
finished none of them is the thing `status` puts in front of you:

```text
  STUCK (taken and never finished)
    web07                    4 attempts   last held by bob@box:9930
```

That is not a scheduling problem. It is a host nobody can actually work on,
and it will absorb the pool forever unless someone is told.

## The percentage

`status` leads with it:

```text
  PROGRESS   12 of 40 done (30%), 3 held, 25 available
```

The rounding is deliberate. 399 of 400 prints as `>99%`, not `100%`, and 1
of 400 prints as `<1%`, not `0%` — either would report the job finished, or
never started, when neither is true, and this is exactly the number that
gets pasted into a status mail. `100%` is reserved for actually complete.

For something that will do arithmetic on it:

```bash
$ muster status --csv
pool,total,done,held,available,done_pct,stuck
muster.csv,40,12,3,25,30.00,1
```

## Sharing a pool between machines

The pool is one file. Put it on a shared filesystem and workers on any
number of hosts draw from it:

```bash
export MUSTER_POOL=/shared/patching.csv
host-a $ muster take 5 -o mine.txt
host-b $ muster take 5 -o mine.txt      # never the same five
```

Locking is an `O_EXCL` sentinel beside the pool rather than `flock`, because
flock over NFS is not dependable while `O_EXCL` create is the one primitive
NFS has always had to get right. Every write is a temporary file renamed into
place, so a reader never sees half a pool. A lock whose holder died is broken
after `--stale-lock` seconds rather than blocking the fleet forever.

Leases are wall-clock deadlines, so they assume the workers roughly agree
about the time. If they do not, leases expire early on the fast box and late
on the slow one — [`skew`](skew.md) is the tool for that question.

## The files

The pool is a CSV you can read, diff, and commit:

```text
item,state,holder,lease_id,taken_ts,expires_ts,done_ts,attempts,note
web01,done,alice@box:4211,,1787853404,,1787853500,1,
web07,held,bob@box:9930,646aff6a,1787853500,1787857100,4,
web12,available,,,,,,0,
```

A ticket is one item per line, so it is already the input to whatever does
the work:

```bash
for h in $(grep -v '^#' mine.txt); do patch "$h"; done
```

with the lease recorded in comments above it, which `done` reads to tell your
completion from somebody else's. A hand-written list of names works too; only
the conflict detection gets quieter.

## Verbs

| Verb | What it does |
|---|---|
| `add` | put items in the pool — a file, `-` for stdin, or a name/range |
| `take` | lease `N` items, or `--item NAME`; writes the ticket |
| `done` | the work on these is finished |
| `release` | give them back, unfinished |
| `reset` | force items back to available, including completed ones |
| `status` | the page, or `--csv` for one row of numbers |
| `list` | the rows, as CSV, filtered by `--state` |

## Exit status

| Code | Meaning |
|---|---|
| `0` | nothing wrong |
| `1` | something worth seeing: a conflict, an unknown item, a stuck item |
| `2` | usage error, or the pool could not be locked |

`muster status` exiting 1 on a stuck item makes it usable as a cron check.
An expired lease going back in the pile is routine and is reported in the
page rather than in the exit code.
