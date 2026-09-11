# `dredge`

**Bring that answer back from every host, kept apart.**

```bash
dredge /var/log/syslog --hosts hosts.txt          # one file from every host
dredge --cmd 'ss -s' --hosts hosts.txt            # what a command says, instead
dredge /var/log/syslog --tail 200 -H 'web[01-40]' # only the last 200 lines
dredge /etc/nginx --hosts hosts.txt               # a whole directory each
dredge /var/log/app.log --since -1h --append      # only what changed, added on
dredge --cmd uptime --tag before -d audit         # labelled, to share a directory
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

## A file or a command, the same way round

Half of what you want off a fleet is in a file and half of it is only ever
printed — `ss -s`, `sysctl -a`, `rpm -q nginx`, `systemctl --failed`. `--cmd`
runs one bash command on each host and lands what it says as that host's
artifact:

```bash
dredge --cmd 'ss -s' --hosts hosts.txt
dredge --cmd 'sysctl -a' --tag sysctl -d audit --hosts hosts.txt
```

Both halves come back into the same directory under the same naming, so the
command you run afterwards — `grep -l`, `logtriage`, a `diff` between two
hosts — does not have to care which half it is reading.

Where [`agree`](agree.md) runs a command everywhere and *groups* the answers
into classes, this keeps every host's answer as its own file. That is the
difference between "which of my forty machines disagree" and "I want all forty
answers on disk to work through".

### It arrives exactly as typed

The command travels base64'd and is decoded into a variable on the far side,
so no shell parses it on the way — not the local one, not ssh, not the remote
login shell. The only shell that ever interprets it is the bash that runs it.

```bash
dredge --cmd "grep -c 'error' /var/log/app.log" --hosts hosts.txt
dredge --cmd 'echo "$(hostname -f): $(uptime -p)"' --hosts hosts.txt
```

Quotes, apostrophes, backslashes, embedded newlines, `$(...)` and backticks
all survive. Quoting would be the other way round, and it is the one that goes
wrong: the command sits inside a script that ssh hands to whatever the remote
*login* shell is, which then runs `bash -c` on it, so quoting means nesting two
levels correctly and getting both right for a shell nobody here chose. Base64
makes the count of levels zero — the payload is alphanumeric whatever the
command was, so there is nothing left for any shell to misread.

`bash` has to exist on the far side — the flag says bash, so a host without it
is reported rather than silently run under something else.

### One answer, not two

stderr is merged into stdout, in order. The artifact is what you would have
seen on the terminal: a tool that writes its headline to stderr and its table
to stdout is giving one answer, and splitting them would lose which line came
when. A command that wants them apart can say so itself — `... 2>/dev/null`.

### The status is kept, not folded away

The exit status is the one thing a file has no equivalent of, so it comes back
in its own frame rather than being inferred from the output:

```text
  NONZERO   the command exited non-zero on 2 hosts:
            web12        exit 1
            web31        exit 127
            What it said is collected either way.
```

A command that failed still has an answer worth keeping — its error text *is*
the artifact — so the collection is not a failed host, it is a finding. The
status is also a column in `--csv`, and a non-zero one makes the run exit 1.

`--head` and `--tail` cut a command's output the same way they cut a file's,
on the far side. `--since`, `--max-bytes` and `--max-files` select among files
and have nothing to select from here; `--since` is refused rather than
ignored, and `--timeout` is what bounds a command that will not finish.

## Names that stay apart

One directory per run, and everything in it is told apart by its **name**
rather than by where it sits:

```text
dredge-20260910-172845/web01~var~log~syslog
dredge-20260910-172845/web02~var~log~syslog
dredge-20260910-172845/web02~var~log~nginx~error.log
```

Rebuilding each host's directory tree locally reads well and greps badly. The
command you actually want next is `grep -l oom *`, or `logtriage
dredge-*/web*syslog`, and both of those want one directory of
distinctly-named files — not forty identical paths under forty host
directories.

### The tag

`--tag NAME` puts a label in front of every name in the run:

```text
audit/sysctl~web01                        # --cmd 'sysctl -a' --tag sysctl
audit/before-restart~web01~var~log~app.log
```

The tag leads because that is the order that makes a shared directory
readable: several runs land side by side, `ls` groups them by run, `rm
audit~*` clears one of them, and a file says which collection it belongs to
without anybody having to remember.

A `--cmd` run has no path to name itself with, so the tag is the whole of the
name. Without `--tag` it takes the command's own first word — `--cmd 'ss -s'`
lands as `ss~web01`, which is what somebody reading the directory later would
have called it anyway.

The tag is the one part of the name the caller writes freely, so it is the one
part that could carry a slash and quietly mean a directory: anything outside
`A-Za-z0-9._+-` folds to `-`, and `--tag 'a/b c:d'` lands as `a-b-c-d~web01`.

### The directory

`-d DIR` names it yourself. Without it, every run gets one of its own,
stamped with the time:

```bash
dredge /var/log/syslog --hosts hosts.txt      # -> dredge-20260910-172845/
dredge /var/log/syslog -d before-the-restart  # -> before-the-restart/
```

Collecting the same path twice an hour apart is the normal way to use this,
and the second run quietly replacing the first is not a result anybody wants
to find later. Two runs inside the same second get `-2`, `-3` rather than
sharing.

The host name is the only thing keeping one machine's files from another's, so
a list naming one host twice is **refused** rather than collected twice into
the same place — the second would overwrite the first silently, and only for
the files they had in common.

Two remote paths from one host can still want the same local name — `a~b/c`
and `a/b/c` both fold to `a~b~c`. The second is refused and named rather than
written over the first, because a file quietly replacing another looks exactly
like a successful collection.

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

The path you name is followed if it is a symlink — `dredge /var/log/current`
means the file that name points at. Links *inside* a collected tree are not:
a link is not evidence, and recreating one here is how a collection directory
grows a link out of itself.

`--max-bytes` (100 MB by default, `0` for no ceiling) refuses to carry
anything larger and names what it left behind, with its size, so a stray core
dump in the directory you asked for does not become the whole run:

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

`--max-files` (500 by default) is the other ceiling, per host. Hitting it is
reported rather than silently truncating the collection:

```text
  TRUNCATED 1 host hit --max-files 500 and there was more: web12
            raise it, or narrow what you asked for with --since.
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
| `--cmd` | framed base64 | nor is the length of what a command will say |

The second one needs explaining. Framing by length would mean measuring the
slice and then reading it, and on the growing log this tool is pointed at
those two answers differ — the stream desynchronises and every file after it
is garbage. So a slice comes back inside a frame delimited by a token drawn
fresh for each run, with the payload base64'd so no byte in it can be mistaken
for that token. The 33% that costs is nothing when the payload is two hundred
lines.

`--dry-run` prints the remote command and contacts nothing, which is the way
to see exactly what will run on your fleet before it does.

`--timeout` (120s) bounds the whole transfer and not just the connection, so
a host that goes quiet halfway through is one row in the report rather than a
run that never returns. Each host's ssh gets a session of its own, so ending
one takes anything the remote command left holding the connection with it.

```text
  COLLISION 1 file folded onto a name already taken and was left behind:
            web03        ./a/b/c
```

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
        38 files from 38 of 40 hosts, 1.2MB in 2.4s -> dredge-20260910-172845/

  UNREACHABLE db07: connect to host db07 port 22: Connection timed out
  MISSING     web31: no such path

  dredge-20260910-172845/web01~var~log~syslog
  dredge-20260910-172845/web02~var~log~syslog
  ... and 36 more
```

`--csv PATH` writes one row per collected file —
`host,source,local_path,bytes,outcome,exit_status` — including a row for the
hosts that returned nothing, so the record says who was asked as well as what
came back. `source` is the path that was collected, or the command that was
run; `exit_status` is that command's status, and empty for a file.

## Options

| Option | Meaning |
|---|---|
| `-c, --cmd CMD` | a bash command to run on each host; its output is the artifact |
| `-t, --tag NAME` | label this run's artifacts so several runs can share a directory |
| `-H, --host TOKEN` | hosts, repeatable; ranges expand (`web[01-40]`) |
| `--hosts FILE` | a server list — [`reachable`](reachable.md)'s output works, its comments included |
| `-d, --dir DIR` | where collected files land (default: a `dredge-<timestamp>` of this run's own) |
| `--head N` / `--tail N` | only that many lines, cut on the far side |
| `--since T` | only files modified since T |
| `--append` / `--prepend` | add to what is here rather than replacing it |
| `--mark` | write a marker line where old meets new |
| `--max-bytes N` / `--max-files N` | ceilings, per file and per host; hitting either is reported |
| `--timeout S` | bounds the whole transfer per host (default 120s) |
| `-j, --jobs N` | hosts contacted at once (default 20) |
| `--csv [PATH]` | one row per file |
| `--dry-run` | print the remote command and stop |

## Exit status

| Code | Meaning |
|---|---|
| `0` | every host answered, everything asked for came back, and any command exited zero |
| `1` | a host failed, a path was missing, a command exited non-zero, a ceiling was hit, a name collided, or nothing was collected |
| `2` | usage error |

Exit 1 on an empty collection is deliberate: a script that fans out to gather
evidence and gathers none should stop, not carry on with an empty directory.
