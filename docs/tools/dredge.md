# `dredge`

**Bring that file back from every host, kept apart.**

```bash
dredge /var/log/syslog --hosts hosts.txt          # one file from every host
dredge /var/log/syslog --tail 200 -H 'web[01-40]' # only the last 200 lines
dredge /etc/nginx --hosts hosts.txt               # a whole directory each
dredge /var/log/app.log --since -1h --append      # only what changed, added on
```

## The problem it solves

Something is wrong on some of forty machines and the evidence is in a file on
each of them. Collecting it by hand is forty `scp` commands whose results all
land on top of each other, because every one of them is called `syslog`.

This runs the copy once, in parallel, and lands the results under a name that
says which machine each came from — so the next command can be `grep -r`, or
`agree`, or `logtriage` over the lot.

It is the gathering half of what [`agree`](agree.md) does with commands: agree
runs one command everywhere and groups the answers; dredge brings one *file*
back from everywhere and keeps them apart.

## Names that stay apart

Every collected file lands under the name of the host it came from, with the
remote directory structure kept:

```text
collected/web01/var/log/syslog
collected/web02/var/log/syslog
collected/web02/var/log/nginx/error.log
```

`--flat` puts everything in one directory instead, folding the path into the
name — which is what you want when the next step is a glob rather than a walk:

```text
collected/web01~var~log~syslog
collected/web02~var~log~syslog
```

The host name is the only thing keeping one machine's files from another's, so
a list naming one host twice is **refused** rather than collected twice into
the same place — the second would overwrite the first silently, and only for
the files they had in common.

Nothing a remote host says is used as a local path. Names are rebuilt here
from the path you asked for, so a host answering with `../../etc/cron.d/x`
writes inside the collection directory or not at all.

## Only the part you need

A log is usually gigabytes and the interesting part is the end of it.

```bash
dredge /var/log/huge.log --tail 200 --hosts hosts.txt
```

The `tail` runs **on the far side**, so what crosses the network is two
hundred lines and not four gigabytes. That is the difference between a run
that takes a second and one that saturates the link it is meant to be
diagnosing. `--head` is the same at the other end of the file.

`--max-bytes` (100 MB by default) refuses to carry anything larger and names
what it left behind, with its size, so a stray core dump in the directory you
asked for does not become the whole run:

```text
  OVERSIZE  1 file larger than --max-bytes (100.0MB), left where they are:
            web12         4.1GB  /var/log/app/core.20260910
            --tail N brings back the end of one without the rest of it.
```

## Only if it changed

```bash
dredge /var/log --since -1h --hosts hosts.txt
```

`--since` filters by modification time, also on the far side, so a directory
of a thousand files hands back the six that moved. It takes the same three
spellings as [`logtriage`](logtriage.md): a relative `-30m`, a clock time
`14:20`, or an ISO stamp.

The timestamp goes over as an epoch second rather than a wall-clock string, so
the window means the same thing on a box in another timezone.

It still means *that box's* idea of the time. A machine whose clock is wrong
will hand you the wrong files and nothing here can tell — [`skew`](skew.md) is
the instrument for that question, and worth a run across the fleet before
trusting a tight `--since` window.

A host with nothing to send is reported rather than left blank:

```text
  EMPTY     3 hosts had nothing to send: web04 web18 web22
            nothing under /var/log changed since -1h there
```

## Collecting the same thing again

`--append` adds what came back to the end of the local file instead of
replacing it, and `--prepend` adds it to the beginning. That is how a local
copy grows over a week of runs. `--mark` writes a line saying which host and
when, at the seam:

```text
hello from web01
===== dredge web01 2026-09-10T15:21:23 =====
hello from web01
```

**Nothing is de-duplicated.** Appending a whole file twice gives you it twice.
The pairing that makes sense is `--tail` or `--since` with `--append`, where
each run brings back a slice the last one did not have — and even then two
runs an hour apart with `--tail 200` will repeat whatever both ends saw. If
you need exactly-once continuation, this is the wrong shape of tool: see
**Why this pulls** below.

## How it goes over the wire

One ssh per host, and one round trip. A `find` on the far side selects the
files and the whole selection comes back down the same connection — forty
hosts with a thousand files each is forty connections, not forty thousand.

| Mode | Transport | Why |
|---|---|---|
| whole files | `find … \| tar` streamed | the bytes and nothing else; tar frames itself |
| `--head` / `--tail` | framed base64 | a slice's length is not known until it is cut |

The second one needs explaining. Framing by length would mean measuring the
slice and then reading it, and on the growing log this tool is pointed at
those two answers differ — the stream desynchronises and every file after it
is garbage. So a slice comes back inside a frame delimited by a token drawn
fresh for each run, with the payload base64'd so no byte in it can be mistaken
for that token. The 33% that costs is nothing when the payload is two hundred
lines.

`--dry-run` prints the remote command and contacts nothing, which is the way
to see exactly what will run on your fleet before it does.

## Why this pulls rather than being pushed

An agent on each host, streaming to a collector, would be the other way to
build this. It is the right shape for a *continuous* feed and the wrong shape
for this job:

- There is nothing to install, start, supervise or clean up. The same argument
  [`muster`](muster.md) makes about leases — no daemon, no reaper — applies to
  collection.
- ssh is already installed, already authenticated, already allowed through the
  firewall. A listener needs a port opened *inbound to the collector*, which is
  usually where the idea dies.
- Failure is unambiguous. A host that did not answer is named in the report. On
  a push model, "no data from web31" is down, or crashed, or firewalled, or
  simply not finished — and telling those apart needs heartbeats and state.
- Pull is naturally rate-limited by the collector: it asks for what it can
  handle. Two hundred hosts pushing at once can bury both the collector and the
  link being diagnosed.

Where an agent genuinely wins is *following* a file live, with a byte offset
remembered so nothing is duplicated. That is a different tool and it does not
need a new port either: `ssh host 'tail -F file'` is a pusher that runs over
the connection already open.

## Reading the result

```text
dredge -- /var/log/syslog   [tail 200]
        38 files from 38 of 40 hosts, 1.2MB in 2.4s -> collected/

  UNREACHABLE db07: connect to host db07 port 22: Connection timed out
  MISSING     web31: no such path

  collected/web01/var/log/syslog
  collected/web02/var/log/syslog
  ... and 36 more
```

`--csv PATH` writes one row per collected file —
`host,remote_path,local_path,bytes,outcome` — including a row for the hosts
that returned nothing, so the record says who was asked as well as what came
back.

## Options

| Option | Meaning |
|---|---|
| `-H, --host TOKEN` | hosts, repeatable; ranges expand (`web[01-40]`) |
| `--hosts FILE` | a server list — [`reachable`](reachable.md)'s output works, its comments included |
| `-d, --dir DIR` | where collected files land (default `collected`) |
| `--flat` | one directory, the host in each name |
| `--head N` / `--tail N` | only that many lines, cut on the far side |
| `--since T` | only files modified since T |
| `--append` / `--prepend` | add to what is here rather than replacing it |
| `--mark` | write a marker line where old meets new |
| `--max-bytes N` / `--max-files N` | ceilings, per file and per host |
| `-j, --jobs N` | hosts contacted at once (default 20) |
| `--csv [PATH]` | one row per file |
| `--dry-run` | print the remote command and stop |

## Exit status

| Code | Meaning |
|---|---|
| `0` | every host answered and something came back |
| `1` | a host failed, a path was missing, or nothing was collected |
| `2` | usage error |

Exit 1 on an empty collection is deliberate: a script that fans out to gather
evidence and gathers none should stop, not carry on with an empty directory.
