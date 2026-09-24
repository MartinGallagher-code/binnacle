# `dredge`

**Bring that answer back from every host, kept apart.**

```bash
dredge /var/log/syslog --servers hosts.txt          # one file from every host
dredge --cmd 'ss -s' --servers hosts.txt            # what a command says, instead
dredge /var/log/syslog --tail 200 -S 'web[01-40]' # only the last 200 lines
dredge /etc/nginx --servers hosts.txt               # a whole directory each
dredge /var/log/app.log --since -1h --append      # only what changed, added on
dredge --cmd uptime --tag before -d audit         # labelled, to share a directory
dredge /var/log/app.log --follow -d out           # only what is new since last time
dredge /var/log/app.log --daemon -d out           # ... and again every five minutes
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
dredge --cmd 'ss -s' --servers hosts.txt
dredge --cmd 'sysctl -a' --tag sysctl -d audit --servers hosts.txt
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
dredge --cmd "grep -c 'error' /var/log/app.log" --servers hosts.txt
dredge --cmd 'echo "$(hostname -f): $(uptime -p)"' --servers hosts.txt
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

### Silence is not an artifact

A command that printed nothing at all — on stdout or stderr — has said nothing
to collect, so no file is written for that host and it is reported as empty
instead:

```text
  EMPTY     2 hosts had nothing to send: web04 web18
            the command printed nothing there, on stdout or stderr, so there was no artifact to keep
```

`dredge --cmd ls --servers hosts.txt` across a fleet whose login directories
hold nothing visible collects nothing, and one line saying so is worth more
than forty zero-byte files that look like a broken transport. The host still
has a row in `--csv`, carrying its exit status, and a run where *no* host said
anything exits 1 — nothing was collected.

A zero-byte *file* is the other way round: it exists on the far side, and a
faithful copy of it is empty. Only a command has nothing to land when it says
nothing.

[`--follow`](#a-remote-tail) is left to its own accounting. There an empty
pass is what `UNCHANGED` already means, the mark has to be kept either way for
the next pass to resume from, and a stream that is empty the first time it is
looked at is a stream rather than a failed collection.

Where a host came back with nothing and its ssh wrote something on stderr,
that line is shown too — for an empty host it is usually the whole answer:

```text
  EMPTY     1 host had nothing to send: web04
            the command printed nothing there, on stdout or stderr, so there was no artifact to keep
            web04        stderr: bash: base64: command not found
```

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

### The suffix

`--suffix EXT` goes on the end of every file a run creates:

```bash
dredge --cmd 'ss -s' --suffix .txt        # ss~web01.txt
dredge /var/log/syslog --suffix .log      # web01~var~log~syslog.log
```

That is how a collection gets an extension the rest of your tooling
recognises. A `--cmd` artifact has no path, so it has no extension at all, and
an editor opening `ss~web01` is left guessing where `ss~web01.txt` is not.

A bare word gains a dot — `--suffix log` and `--suffix .log` both mean
`.log` — and one that already starts with `.`, `_`, `-`, `+` or `~` is
appended as typed. A leading dash needs the joined spelling `--suffix=-raw`,
because a separate `-raw` is something argparse has to read as an option. Like
the tag, it is cleaned before it is used: anything outside `A-Za-z0-9._+-`
folds to `-`, and a suffix with no letter or digit left in it is refused rather
than put on the end of every name in the run.

It is part of the name, so changing it between two `--follow` passes makes the
local copy the last pass wrote unfindable, and that file is collected again from
the start under the new name — reported as a `RESYNC` rather than silently, but
worth knowing before changing a suffix mid-follow.

### The directory

`-d DIR` names it yourself. Without it, every run gets one of its own,
stamped with the time:

```bash
dredge /var/log/syslog --servers hosts.txt      # -> dredge-20260910-172845/
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
dredge /var/log/huge.log --tail 200 --servers hosts.txt
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
dredge /var/log --since -1h --servers hosts.txt
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

A `--cmd` run that printed nothing counts as the same thing — see [Silence is
not an artifact](#silence-is-not-an-artifact).

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
runs an hour apart with `--tail 200` will repeat whatever both ends saw.

`--follow`, the next section, is that pairing without the overlap.

## A remote tail

`--follow` brings back only what is new. Each file is resumed from the byte
the last pass stopped at, so a second pass over a log that has grown by forty
lines carries forty lines — not the file, and not a `--tail 200` window that
repeats whatever the last pass already had.

```bash
dredge /var/log/app.log --follow -d out --servers hosts.txt
dredge /var/log/nginx   --follow -d out --servers hosts.txt
dredge --cmd 'dmesg -T' --follow -d out --servers hosts.txt
```

A **file** is resumed at its offset. A **directory** brings back the files
that grew, and a file that appeared since the last pass comes back whole —
that is what new means. A **command** has no byte offset to resume from, so
what it says is compared against what it said last time:

| What the command said this time | What comes back |
|---|---|
| the same thing | nothing |
| the same thing and more | the part that was added |
| something different from the first byte | all of it |

The comparison is a `cksum` of exactly the bytes we already have, run on the
far side against the same prefix of this answer — POSIX, on every box this
will land on, and asked only whether what we carried is still the start of
what is there now. A host with no `cksum` sends the whole answer every pass,
which is the safe way to be wrong.

### Where it lands is still yours to say

What arrives lands as `--append`, `--prepend` or `--replace` says. Under
`--follow` the default is `--append` — the local file is the log, growing here
as it grows there. `--replace` under `--follow` is the other useful shape: the
local file holds only what the last pass brought back.

```bash
dredge /var/log/app.log --follow --append  -d out   # the log, accumulating here
dredge /var/log/app.log --follow --replace -d out   # only the latest slice
dredge /var/log/app.log --follow --mark    -d out   # a line at every seam
```

`--tail N` decides where a *first* sight starts: the last N lines, exactly as
`tail -n N -f` does. Without it a file that has not been seen before comes
back whole. `--head` cannot mean anything here — the first N lines of a file
never change — and is refused rather than quietly ignored.

### Where the marks live

A pass has to know where the last one stopped, so `--follow` keeps a small
JSON file — one offset per host per file — in the collection directory:

```bash
dredge /var/log/app.log --follow -d out     # marks in out/dredge-state.json
```

This is the one piece of state this package keeps on its own, and it is named
by you. No `-d` means a new stamped directory every run and nothing to resume
from, so `--follow` **asks for one** rather than quietly starting over.
`--state FILE` puts the marks somewhere else; deleting the file starts the
follow again from scratch. Several collections can share one directory and one
state file — each stream is kept apart by its tag and by what it collects.

A state file that cannot be read is refused rather than started over: starting
over means every host sending every file again, which on the fleet this is
pointed at is the event `--follow` exists to avoid.

### When the file is not the file it was

A log that was rotated is not a log that was truncated, and neither is a log
that grew. A file whose **inode changed**, or whose **size went backwards**,
comes back from byte zero and is reported:

```text
  ROTATED   1 file was not the file it was and came back as a new one:
            web02        /var/log/app.log
            A new inode, or a size that went backwards: resuming at the old
            offset would have handed you the middle of a different file.
            The follow carries on from the new one -- this is a seam, not a stop.
```

**A rotation is a seam, not a stop.** The new file is followed from there
exactly as the old one was, and the next pass carries only what was added to
it. The warning explains the seam in the local copy; it does not mean that
file was abandoned.

Both halves matter: `logrotate` moving the file aside changes the inode, and
its `copytruncate` mode keeps the inode and puts the size back to zero.

What this cannot see is a file replaced **in place**, keeping its inode and
ending up longer than the old one. That is the same blind spot `tail -f` has,
and it is worth knowing rather than being surprised by.

If the local copy is gone — you deleted it, or something cleaned the
directory — the mark goes with it rather than being believed:

```text
  RESYNC    1 local copy is gone, so the mark went with it:
            web01        /var/log/app.log
            The next pass brings each of them back whole.
```

"Nothing new" about a file that is no longer here is the most confidently
wrong thing this could say, so it does not say it.

### When more arrives than one pass can carry

`--max-bytes` means **the new part** under `--follow`, not the whole file, and
it bounds a pass rather than ending one. When more than the ceiling was added
since the last pass — a busy log, or a rotation, where the whole new file *is*
the new part — the newest `--max-bytes` come back and the follow resumes from
the end of the file:

```text
  GAP       1 artifact grew by more than --max-bytes (500B) in one pass:
            web01           2.4KB not carried  logs/app.log
            The newest 500B came back and the follow is at the end of the file
            again, so this is one hole rather than a stop.  Raise --max-bytes,
            or pass more often, to stop it happening again.
```

What did not fit is a hole in the local copy that will not fill, so it is named
with its size. It is deliberately not a refusal: refusing would leave the mark
where it was, the next pass would have *more* to carry and would be refused for
the same reason, and that artifact would never be collected again — losing the
whole of the rest of the log to protect the part of it that did not fit.

**A gap does not change the exit status.** A log busy enough to outrun its
ceiling does it on most passes, and a daemon whose every pass reported failure
for working exactly as designed is a daemon whose exit status stops being read.
`gap_bytes` in `--csv` is the machine-readable half of the finding, and it
exists for that reason — read it, not `$?`, if a script needs to know that part
of a log is missing.

## Daemon mode

`--daemon` does that on a timer: a pass, a wait, another pass, until something
stops it.

```bash
dredge /var/log/app.log --daemon -d out --servers hosts.txt
dredge --cmd 'systemctl --failed' --daemon --every 30s -d out -S 'web[01-40]'
```

```text
17:50:46  pass 1    1 new, 33B from 1 of 1 host in 0.0s
17:50:47  pass 2    0 new, 0B from 0 of 1 host in 0.0s
  UNCHANGED 1 host had nothing new: web01
            1 artifact checked and unchanged there

17:50:48  pass 3    1 new, 16B from 1 of 1 host in 0.0s
```

Each pass prints one line, and anything worth reading — a host that failed, a
file that rotated, a command that exited non-zero — prints its findings under
it in the same words the one-off report uses. A pass whose hosts all failed is
still just a pass: the loop keeps going, because a fleet that is unreachable
for ten minutes is not a reason to stop watching it.

`--every` takes `30s`, `5m`, `2h` or a plain number of seconds and defaults to
**five minutes**. It is the gap between the end of one pass and the start of
the next, so a pass that runs long cannot stack up behind itself — one that
outlasts the interval is reported and the next begins immediately.

`--daemon` implies `--follow`, because a timer that re-fetched every file in
full every five minutes would be a denial of service against the fleet it is
watching.

`--passes N` stops after N of them. Without it the run continues until SIGINT
or SIGTERM, which are answered by finishing the pass in hand — hosts already
contacted are finished and their marks written — and then saying what the run
did. Asked twice, it is not asked a third time: the second signal puts the
default handler back.

It does **not** fork, detach, write a pidfile or leave anything behind but the
files it collected and its state file. `&`, `tmux` or a systemd unit is how it
becomes a background service:

```ini
[Service]
Environment=DREDGE_EVERY=2m
ExecStart=/usr/local/bin/dredge /var/log/app.log --daemon -d /srv/collected \
          --servers /etc/fleet.txt --quiet
Restart=on-failure
```

`--quiet` there means the same thing it means everywhere else: say nothing
unless something went wrong. A failed host is still a finding and still
printed, because a daemon that swallows an unreachable host is a daemon you
cannot leave running.

## A table of what the fleet said

`--csv` answers "did the collection work" — a row per file, how big it was,
how it went. `--tsv` answers the other question, the one you actually pointed
dredge at the fleet to ask: **what were the numbers?**

```bash
dredge --cmd 'echo "load1=$(cut -d" " -f1 /proc/loadavg)"
              echo "procs=$(ls /proc | grep -c "^[0-9]")"' \
       --servers hosts.txt --tsv metrics.tsv
```

```text
date                   host    load1   procs
2026-09-18T09:14:02    web01   0.41    212
2026-09-18T09:14:02    web02   1.93    318
```

One row per host per pass: the time the pass ran, the host it came from, then
a column per variable. Appended to one file across runs, because that is the
shape a week of passes has to have for a spreadsheet, a plot or an `awk`
one-liner to read it as a time series rather than as forty files.

The date is the clock **here** — `%Y-%m-%dT%H:%M:%S`, local, no offset and no
fraction, sortable as text. Every row in a pass carries the same stamp however
far apart the hosts' own clocks are; [`skew`](skew.md) is the instrument for
the question of whose clock is wrong, and this column deliberately does not
pretend to answer it.

### The shape the variables arrive in

`--parse` says how to read what the command printed. Four shapes, because
different probes already speak different ones:

| `--parse` | The command prints | Notes |
|---|---|---|
| `kv` (default) | `name=value` per line | Self-describing: `sysctl -a`, `/etc/os-release`, most probes. Blank lines and `#` comments are skipped; the first `=` separates |
| `json` | `{"name": value}` | One level deep — a nested object or list has no column to go in and is refused by name |
| `row` | a header line, then a values line | The command names its own columns. Tabs where there are tabs, whitespace where there are not |
| `values` | bare values in a fixed order | The one shape that is not self-describing, so `--columns` is required |

`kv` is the default because it is the shape that describes itself. The columns
come from the data rather than from a flag you have to keep in step with it,
and a host missing one is a visible blank rather than a row whose columns have
all shifted along by one.

`--parse values` is refused without `--columns`: `3 41 0.7` says nothing about
which is which. A host that prints a different *number* of values is refused
too, rather than filled in — a short row there is not a missing value, it is
every column after the gap holding the wrong one.

### The header does not move

The header is written when the file is created, and every row after it is
counted from that header. Rebuilding it from each pass would renumber every
column the first time a host answered differently, and the week of rows behind
it would quietly start meaning something else.

So a name that shows up later has nowhere to go, and that is said rather than
dropped:

```text
  NEWVAR    1 name the table has no column for: swap_free
            The header was written when metrics.tsv was created and every row since
            is counted from it.  Start a new table, or name the columns
            up front with --columns.
```

`--columns a,b,c` pins the header up front, which is how you leave room for a
variable a host is not printing yet. Without it the header is the names the
fleet actually used, first-seen order — and host order is fixed by the server
list, so two runs over one list build the same header.

A variable a host does not have is an empty cell. A host whose answer will not
parse gets **no row**, and a `UNPARSED` line saying which host and what was
wrong with what it printed — a host quietly missing from a table reads as a
machine that was fine. Its answer is still collected whole either way; only its
row is missing. A host that printed nothing gets no row for the same reason: an
empty line under a timestamp claims the fleet reported zero, which is a
different and much worse claim than saying nothing.

Values are escaped, not truncated: a tab ends a column and a newline ends a
row, so either would turn one row into two or shift every column after it.
They travel as `\t` and `\n`.

### A time series

```bash
dredge --cmd 'cat /proc/pressure/cpu | head -1' --servers hosts.txt \
       --tsv pressure.tsv --parse kv --daemon --every 5m
```

`--daemon` normally implies [`--follow`](#a-remote-tail), so a timer cannot
re-fetch every file in full every five minutes. With `--tsv` it does not: a
table wants the whole answer each pass, and re-running a command is not a
re-transfer. `--follow` and `--tsv` together are refused outright — a pass
where nothing changed would write no row, which reads as a host that was down.


## A jump box

The fleet is behind a bastion: **A** can reach **B**, and only B can reach
**C–Z**.

```bash
dredge --cmd 'uptime' --servers hosts.txt --relay bastion -d out
```

```text
A  --ssh-->  B  --ssh--> C, D, E ... Z
   <--tar--     <--------
```

The tunnelling answer is `ssh -J`, and it works — `--ssh 'ssh -J bastion'`
needs nothing from dredge at all. This is the other answer, and the one that
**depends on no configuration**: `--relay` sends *this file* to B, runs the
whole collection from there, and brings it back.

B needs a Python and a shell. It does not need dredge installed, an agent, a
package, a cron entry or a line of config, because the copy that runs there is
the copy that was running here — sent over the same connection that carries
the answer back, and removed when the run ends.

One transfer crosses the A–B link instead of forty, which is the reason to
prefer this over a tunnel when that link is the slow one. The fan-out, and the
connections it opens, happen on B.

The names the collection lands under are the ones dredge would have built
without a jump box in the way, so `grep -l oom *` reads the same either way.
The server list goes over **already expanded** — `web[01-40]` became forty
names here — so B collects from exactly the fleet that was asked for rather
than re-expanding a range against its own idea of the syntax.

### What it leaves behind

Nothing, by default. The spool is a **named path** rather than a `mktemp -d`,
specifically so that it can be removed even when the run that made it was
killed: `--timeout` ends the session with `SIGKILL`, and a killed shell runs no
`trap`. `--keep-relay` leaves it, for when the question is what went wrong over
there:

```bash
dredge --cmd 'uptime' --servers hosts.txt --relay bastion -d out --keep-relay
# then: ssh bastion 'cat /var/tmp/dredge-relay-*/errors'
```

The cost is worth saying plainly: **the collection exists on B, in the clear,
for as long as the run lasts.** On a bastion somebody else owns, that is a
disclosure — and `--ssh 'ssh -J bastion'` is the shape that does not make it,
because there the bytes pass through B's sshd encrypted end to end and B cannot
read them.

### The spool is dredge's, and only dredge's

dredge **clears the spool before it uses it and removes it after** — that is
how a run killed mid-flight does not leave a half-collection on the bastion,
and how the next run starts clean. It means the spool has to be a path dredge
*owns*: point `--relay-dir` (or `DREDGE_RELAY_DIR`) at an existing tree and
dredge would delete it, because to dredge that path is last run's scratch to
clear.

So it will not. A spool dredge makes carries a marker file inside it, and
dredge removes only a directory that has one:

- On the jump box, a `--relay-dir` that already exists **without** the marker
  is taken to be your own directory, handed over by mistake. dredge refuses
  the run and deletes nothing:

  ```text
  dredge: /home/me/project already exists here and dredge did not make it --
  dredge: refusing to touch it, so nothing in it is deleted. Point
  dredge: --relay-dir at a path dredge can own, or omit it for the default.
  ```

- The tidy-up sweep at the end is guarded by the same marker, so a run that
  refused is never "cleaned up" by deleting the tree it refused to touch.
- A `--relay-dir` that resolves to a root or a home directory is refused on
  this side, before any host is contacted, for the message alone.

The safe course is the default: omit `--relay-dir` and dredge names a path
under `/var/tmp` for the run. Give one of your own only when you have a reason
to, and give a path dredge can have to itself — not a directory with anything
in it you would miss.

### The far side's report is the report

The run that actually happened is the one over there, so its report is what is
shown, indented under one line of this side's own. Rewriting it here would be
one more place for the two to disagree. Its exit status is this run's exit
status.

```text
dredge.py -- 12 hosts via bastion, 12 files (1.4MB) in 6.2s
  dredge.py -- [uptime] uptime
          12 files from 12 of 12 hosts, 1.4MB in 5.8s -> collect/
```

### What a relay cannot do

[`--follow`](#a-remote-tail) is refused. Its marks would live in the spool,
which is removed when the run ends, so every pass would be a first sight and
would carry the whole collection again. `--daemon` with `--cmd` and
[`--tsv`](#a-table-of-what-the-fleet-said) repeats the command rather than
resuming a file, and that does work — the timer stays on this side, one round
trip per pass:

```bash
dredge --cmd 'echo "load1=$(cut -d" " -f1 /proc/loadavg)"' \
       --servers hosts.txt --relay bastion --tsv metrics.tsv \
       --daemon --every 5m -d out
```

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

### A login that talks first

Plenty of hosts greet a command before running it: a banner from
`/etc/bashrc`, a MOTD, a "last login" line, the compliance notice a bastion
prints at every login. All of it lands on the command's own stdout, mixed in
front of the answer.

The framed transports step over any line they do not recognise, so a banner
costs them nothing. A tar cannot do that — the first byte of the stream is the
first byte of a header, so one line of welcome in front of it makes the whole
tar unreadable. So the far side announces its tar with a line of its own, and
everything before that line is the login talking and is dropped.

If the mark never arrives, what the host did say is quoted in the report,
because that is the whole diagnosis:

```text
  FAILED    web31: the host answered, but not with a collection: *** This bastion requires an interactive session. ***
```

This matters most under `--relay`, where the banner is on the jump box and
the answer being wrecked is the entire fleet's.

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

Following a file live, with a byte offset remembered so nothing is
duplicated, is the thing an agent is usually installed for — and `--follow`
does it by pulling, because the offset is the only state involved and there is
no reason it has to live on the far side. It lives here, in the collection
directory, and each pass carries it over and asks for the rest. What an agent
would still win is *latency*: a pass is as fresh as `--every`, where a process
sitting on the far side can push a line the moment it is written. `ssh host
'tail -F file'` is that pusher, over the connection already open, for the one
host you are watching right now.

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
`host,source,local_path,bytes,outcome,exit_status,pass,unchanged,gap_bytes` —
including a row for the hosts that returned nothing, so the record says who was
asked as well as what came back. `source` is the path that was collected, or the
command that was run; `exit_status` is that command's status, and empty for a
file. `bytes` is what came **over the wire**, not the size of the local file
after it landed. `pass` is 1 for a one-off run and counts up under `--daemon`,
which appends its rows rather than replacing them; `unchanged` is what a
`--follow` pass checked and did not have to carry; `gap_bytes` is what it could
not carry and nothing will bring back.

## Options

| Option | Meaning |
|---|---|
| `-c, --cmd CMD` | a bash command to run on each host; its output is the artifact |
| `-t, --tag NAME` | label this run's artifacts so several runs can share a directory |
| `--suffix EXT` | put EXT on the end of every file the run creates; a bare word gains a dot |
| `-S, --server TOKEN` | servers, repeatable; ranges expand (`web[01-40]`) |
| `--servers FILE` | a server list — [`reachable`](reachable.md)'s output works, its comments included |
| `-d, --dir DIR` | where collected files land (default: a `dredge-<timestamp>` of this run's own) |
| `--head N` / `--tail N` | only that many lines, cut on the far side |
| `--since T` | only files modified since T |
| `--append` / `--prepend` / `--replace` | where the new bytes land; `--replace` is the default, except under `--follow` |
| `--mark` | write a marker line where old meets new |
| `-f, --follow` | only what is new since the last pass |
| `--daemon` | keep going, a pass at a time (implies `--follow`, except with `--tsv`) |
| `--every T` | how long between passes: `30s`, `5m`, `2h` (default 5m) |
| `--passes N` | stop after N passes (`0`: until stopped) |
| `--state FILE` | where a `--follow` run keeps its marks |
| `--max-bytes N` / `--max-files N` | ceilings, per file and per host; hitting either is reported |
| `--timeout S` | bounds the whole transfer per host (default 120s) |
| `-j, --jobs N` | hosts contacted at once (default 20) |
| `--relay HOST` | a jump box: send this file there, run the collection from there, bring it back |
| `--relay-dir DIR` | where the copy and the collection live on the jump box while the run lasts |
| `--relay-python P` | the python to run it with over there (default: `python3`, then `python`) |
| `--keep-relay` | leave the spool on the jump box instead of removing it |
| `--csv [PATH]` | one row per file |
| `--tsv [PATH]` | a table: one row per host per pass, of the variables its command printed |
| `--parse HOW` | the shape those variables arrive in: `kv` (default), `json`, `row`, `values` |
| `--columns A,B,C` | name the variable columns; required by `--parse values`, elsewhere it pins the header |
| `--dry-run` | print the remote command and stop |

## Exit status

| Code | Meaning |
|---|---|
| `0` | every host answered, everything asked for came back, and any command exited zero |
| `1` | a host failed, a path was missing, a command exited non-zero, a ceiling was hit, a name collided, or nothing was collected |
| `2` | usage error |

Exit 1 on an empty collection is deliberate: a script that fans out to gather
evidence and gathers none should stop, not carry on with an empty directory.

Under `--follow` that one rule is inverted: a pass that brought nothing back
exits **0**, because nothing new is the answer a tail spends most of its time
giving, and a script that polls one would otherwise read a quiet fleet as a
broken run. A `GAP` is **0** as well — see above; `gap_bytes` in `--csv` is
what says so instead. A `--daemon` stopped by a signal exits 0 as well — it was asked to
stop — and one that ran out its `--passes` exits 1 if any pass in it had a
failure.
