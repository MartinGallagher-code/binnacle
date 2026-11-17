# agree

**Which hosts in this fleet disagree with the rest?**

Runs a command across a fleet over ssh and reports the *consensus*: 47 hosts
agree, 3 do not, here is the diff.

```bash
agree -S 'node[01-24]' -- rpm -q openssl
agree --servers prod.txt --loose -- uname -r
agree script why-slow --servers prod.txt --fleet-csv --merge-csv triage.csv -- --csv
agree hosts -S 'rack[a-c]-node[01-04]'      # expand without running anything
agree doctor --servers prod.txt               # can each host be reached and used?
```

## What it looks like

```text
agree -- 50 hosts, 3 groups, 47 agree      [normalized: trim, mask-times]
        rpm -q openssl                     11.4s, 32 jobs

  GROUP 1   47 hosts   ok        d=ab12cd34ef56              <- baseline
    openssl-3.0.13-1.el9.x86_64
    hosts: node01 node02 node03 node04 node05 ... (+42, --show-hosts)

  GROUP 2    2 hosts   ok        d=77aa11bb22cc
    hosts: node31 node47
    --- baseline
    +++ group 2
    -openssl-3.0.13-1.el9.x86_64
    +openssl-3.0.2-2.el9.x86_64

  GROUP 3    1 host    unreachable
    hosts: node09
    ssh: connect to host node09 port 22: Connection timed out

  WHAT TO DO NEXT
    * node31 and node47 differ from the other 47. If this is a CVE check,
      those are your two:
      agree -S node31,node47 -- <the fix>
    * node09 never answered. That is not "fine because it produced no
      output" -- it is unknown. Fix it or take it out of the list.
```

## Why not pssh

`pssh` and friends print N outputs and leave you to read them. At 50 hosts
that is a wall of text in which two-thirds of one screen is identical to the
next, and the thing you are looking for — the host that differs — is exactly
what scrolling past identical output makes you miss.

The question you actually have is never "what did all 50 say". It is "do
they agree, and if not, who doesn't". That is a different computation, and
it is this one.

## A failed host is a group, not an error

The most important behavioural difference from every other fan-out tool.

Unreachable, timed-out, push-failed and non-zero-exit hosts each become
their own group in the report, with their stderr shown. They are never
printed to stderr and skipped over.

> "3 hosts never answered" is usually *the finding*. A tool that prints
> those to stderr and moves on lets you believe you checked them.

That distinction is also in the exit code: `3` (something failed) outranks
`1` (divergence), because a host you could not reach is a bigger problem
than a host that disagreed.

### A command agree gives up on is stopped on the host

Past `--timeout`, or past `--max-output`, agree gives up on a host. Killing
its own ssh is not enough to stop the command: agree asks for no terminal,
so sshd does not hang the command up, and it goes on running on the host
after the report has called it `timeout` (reproduced against OpenSSH 9.6
and 10.3; pdsh and pssh behave the same way). So every command goes out
after one line on stderr naming the process group sshd started it in, and
agree takes that line off again before anything sees or counts it. When
agree gives up, a second ssh sends that group TERM, then KILL two seconds
later if any of it is left, under the same `sudo -n` as the command. The
host's error says which happened:

```text
  GROUP 2     1 host   timeout
    hosts: web07
    timed out after 60.0s; agree stopped it on the host
```

A host where the stop could not be confirmed (the host stopped answering,
or something in the group would not die) says so in its error, gets a
`NOT STOPPED` section of its own in the report, and keeps a pushed
script's directory, which is otherwise removed only once the command in it
is stopped:

```text
  NOT STOPPED  the command may still be running on 1 host, where agree could not stop it:
    web07        timed out after 60.0s, and may still be running on the host: agree could not stop it (...)
```

A shell that does not lead a process group of its own is never signalled,
because that group belongs to something else. A command that finishes in
time answers byte for byte as it did before.
`tests/check_real_sshd.sh` checks all of this against a real sshd on the
machine it runs on, and `--sudo` and `--sudo-user` too where `sudo -n`
needs no password.

One case reads as `NOT STOPPED` when the command did stop: a container
whose first process is a plain shell rather than an init that reaps (run
without `--init`). A killed command is then left as a zombie nobody
collects, and a zombie still counts as a member of its group, so agree
cannot tell it from one still running and says it may be. It never says
the opposite.

### What the login shell prints first is dropped

sshd runs the command through your login shell, and that shell reads its
startup files -- `~/.bashrc`, `/etc/bash.bashrc`, `~/.zshenv` -- before it
runs anything. Whatever they print lands in the same stdout and stderr as
the command's answer: a greeting, a maintenance notice, `bash: warning:
setlocale: LC_ALL: cannot change locale`. Kept, one chatty host splits the
fleet into two groups that differ by nothing but the greeting, and the
report tells you to fix the hosts that were right; a setlocale warning
becomes a failed host's error.

So the line agree sends before every command goes to stdout as well as
stderr, and on each stream everything before it is dropped. bash and zsh
read every startup file before they run the command, so nothing the command
prints can come before the line. What was dropped is in no group, diff,
report, CSV, JSON or `watch` baseline, and the host is not flagged: it keeps
the group and the outcome its command earned. One line on stderr says which
hosts' shells printed, so the startup file can be quieted, and nothing is
said when none did:

```text
[agree] 1 host's login shell printed before the command (web07): dropped from its answer (--keep-preamble keeps it)
```

The usual fix is to print only in an interactive shell, at the top of
`~/.bashrc`: `case $- in *i*) ;; *) return ;; esac`. `agree doctor` says the
same line, so it is the quick way to find such hosts.

- **No line, no cut.** Where the line never comes -- a startup file that
  exits, or `exec`s something else, so the command never ran -- or more than
  64 KiB comes first, the stream is kept exactly as it came. The line itself
  is still taken off when it comes, so it never shows in an answer.
- The line carries the run's random id, so nothing a host prints can be
  taken for it, and only what comes before it is looked at: the command's own
  output is never cut, whatever it prints.
- agree's own commands on a host (making the run's directory, chmod, the
  cleanup, the stop) go after the line too, so a failed push's error is the
  push's, not the greeting.
- **`--keep-preamble`** (`AGREE_KEEP_PREAMBLE=1`) keeps everything, byte for
  byte as 0.11.0 did, and sends the command exactly as 0.11.0 sent it.

`tests/check_real_sshd.sh` checks this against a real sshd and a real bash
reading `~/.bashrc` (one of its own: it sets `HOME` for the sshd it starts).
Output a startup file prints *after* the command, from an `EXIT` trap, is
not cut.

## Normalization

Raw outputs almost never match byte for byte. A hostname, a timestamp or a
pid in the output is enough to make all 50 hosts look unique, and then the
tool tells you nothing.

So there is an ordered normalization pipeline. The order is fixed and
documented, because it matters:

1. strip ANSI *(on by default)*
2. `--vgrep RE` drop lines, then `--grep RE` keep lines
3. `--head N` / `--tail N`
4. `--field N`
5. `--mask-hosts` — each host's own name and address → `%HOST%`
6. `--mask-times` — timestamps → `<TS>`, including bare epoch
   seconds, which is what every binnacle tool stamps its CSV with
7. `--scrub RE` (repeatable) → `%X%`
8. `--mask-numbers` — every number → `#`
9. `--ignore-case`
10. `--squeeze-ws`
11. `--trim` *(on by default)*
12. `--drop-empty`
13. `--sort-lines`

`--fleet-csv` is the preset for grouping the CSV these tools emit: exactly
`--mask-hosts --mask-times`, because every one of them writes rows beginning
`host,ts` and both columns are per-host by construction. It is a preset
rather than a default because the normalizations are still disclosed with
the result — a reader has to be able to see that the host column was masked
to get the grouping. It contradicts `--strict`, and says so rather than
silently picking one.

Two presets: `--loose` (trim + squeeze-ws + mask-times + mask-hosts +
mask-numbers) and `--strict` (nothing at all, byte-exact).

`--mask-hosts` is per host and applied **before** hashing, which is what
makes it work: without it, anything that echoes the machine's own name can
never agree with anything.

### The active normalizations are always printed

```text
agree -- 50 hosts, 1 group, 50 agree      [normalized: trim, mask-numbers]
```

Because *"47 agree"* means nothing if you masked every number to get there —
two hosts on different package versions look identical under
`--mask-numbers`. The tool goes further and says so outright when a single
group was produced with numbers masked:

```text
    * Every host agrees -- but you masked numbers to get there, so hosts
      differing only in a version or a count would look identical. Re-run
      with --strict to check that is what you wanted.
```

### Agreeing on nothing is not agreeing

A normalization that removes every line leaves every host holding the same
empty string — and hosts holding the same empty string group together. Three
machines that genuinely differ:

```text
agree.py -- 3 hosts, 3 groups, 1 agree      [normalized: strip-ansi, trim]
```

become unanimous the moment a filter matches nothing on any of them:

```text
agree.py -- 3 hosts, 1 group, 3 agree       [normalized: strip-ansi, trim, grep]

  GROUP 1     3 hosts  ok           d=e3b0c44298fc   <- baseline
```

That digest is the SHA-256 of the empty string, and `--grep` for a line that
turns out to be absent everywhere is not a typo — it is the ordinary case
where its absence is the finding.

So a host whose output *was there* and was normalized away is named, and the
run is never reported as unanimous:

```text
  NOTHING LEFT  3 hosts said something and the normalizations removed all of it.
    web01 web02 web03
    Those hosts are grouped on an empty answer, so they agree with each other
    whatever they actually said.  Active: strip-ansi, trim, grep
```

A command that genuinely prints nothing on every host is a different thing —
that is an answer, hosts agreeing on it agree, and the run stays exit 0 with
nothing said about it.

`--field` is 1-based, so `--field 0` was an off-by-one that returned an empty
string for every line; a negative `--head` or `--tail` sliced from the wrong
end (`--tail -1` drops the *first* line rather than keeping the last N). Both
are refused rather than quietly emptying the comparison.

## Grouping

The group key is `(outcome, digest-of-normalized-output)` — **not** the
digest alone. Two hosts with identical stdout but different exit codes did
not give the same answer, and merging them would hide that.

Groups are ordered by size, then by first appearance in the host list. The
largest *successful* group becomes the baseline, and every other group is
rendered as a unified diff against it. A fleet where most hosts failed still
diffs against the ones that worked.

Output is replayed in host-list order, never completion order, so two runs
over the same list diff cleanly however the ssh connections happened to
race.

## Host lists

The same grammar as `servers.txt` in `iperf_orchestrator` and
`matrix_orchestrator` — `#` comments, blank lines, `name[=addr[:port]]`
tokens — so an existing fleet list works unchanged.

```bash
agree --servers prod.txt -- uptime      # a file
agree --servers - -- uptime             # stdin
agree -S web01,web02,db01 -- uptime   # inline
agree -S 'node[01-24]' -- uptime      # a range
agree -S 'rack[a-c]-node[01-04]' -- uptime   # cartesian
```

Ranges are worth the code: every real fleet is named this way, and typing
the list out by hand is exactly how a host gets left off the check. Commas
inside brackets belong to the range — `node[1,3]` is one spec, not two.

`agree hosts` expands and prints the list without running anything. Run it
before the scary command.

## Safety

`--dry-run` prints the ssh argv per host and contacts nobody. Always
available. It leaves out the line agree sends before every command to name
its process group and mark where the command's own output starts, so the
command can be read.

A **mutation guard** refuses obviously destructive commands — `rm -rf`,
`mkfs`, `dd of=`, `shutdown`, `systemctl stop`, package removal, recursive
chmod/chown, firewall flushes, fork bombs — unless you pass `--yes`. On a
non-tty (cron, CI) it refuses rather than prompts, because nobody is there
to answer.

This is a **typo guard, not security**. `--yes` defeats it and so does any
quoting cleverness. It exists because `agree -- rm -rf /var/log` across 400
hosts at 3am is a specific kind of bad day.

Also: `--first N` runs against only the first N hosts — the canary before
the fleet — and `--limit N` refuses runs wider than N without `--yes`.

## sudo

`--sudo` prefixes `sudo -n`, always with `-n`. A password prompt would hang
a fan-out, and a host without NOPASSWD becomes a **visible finding** (it
lands in an `exit:1` group saying `sudo: a password is required`) rather
than a mystery hang. No password is ever read, passed or stored, and no
key is distributed.

## Pushing a script

`--push FILE` copies a file to each host before running, `--pull GLOB`
collects files after, and `agree script PATH` is the sugar for the whole
cycle: push, `chmod +x`, run, collect, clean up.

```bash
agree script why-slow --servers prod.txt --fleet-csv -- --csv --interval 2
```

This is the reason `agree` exists in the same distribution as the others.

### Where pushed files go, and what is removed

Each run pushes into a directory of its own under `--remote-dir`
(`/var/tmp/agree` by default): `agree.<run id>`, made fresh for the run
and holding a `.agree-owned` file with that id from the moment it exists.
The command finds it as `$AGREE_DIR`, and still runs in the login
directory, so a relative path among its arguments means what it always
did:

```bash
agree -S web01,web02 --push check.sh -- '"$AGREE_DIR"/check.sh --quick'
agree script check.sh -S web01,web02 -- --quick    # the same, named for you
```

Afterwards that directory, and nothing else, is removed — after `--pull`,
which reads from it — and `--remote-dir` too if the run made it and it is
empty again. The checks run on the host, under the same `sudo` as the
command: the path must end in this run's own `agree.<run id>`, hold no
`..`, be a real directory rather than a symlink, and hold `.agree-owned`, a
plain file, with this run's id. Anything else is refused and reported:

```text
  NOT CLEANED UP  1 host still holds this run's directory:
    web02        could not remove /var/tmp/agree/agree.3f9c…: agree: refusing to remove …: its .agree-owned is another run's
```

and the run exits 3. So `--remote-dir ~/bin`, or any directory already
holding files, keeps every one of them, and two runs at once on the same
host never remove each other's files. `--keep-remote` leaves the run's
directory and says where it is.

`why-slow` there is a **name, not a path**. A bare name — no directory
part — is looked up among the tools installed alongside `agree` itself,
hyphen or underscore, `.py` optional, so `why-slow`, `why_slow` and
`why_slow.py` all find the same file. That matters after
`pip install binnacle`, where the sources live in whichever
`site-packages` directory pip chose and nobody should have to go looking
for it. An explicit path always wins, and a name that matches neither a
file nor a bundled tool fails with the list of tools that would have
matched.

### --merge-csv: fleet-wide triage

`--merge-csv PATH` treats each host's stdout as a CSV, drops the duplicate
headers, prepends an `ssh_host` column and writes one tidy file:

```text
ssh_host,host,ts,rule_id,severity,title,detail,fix
node01,node01,1786741765,OK,INFO,nothing wrong,no rule fired,
node31,node31,1786741765,SWAP_THRASH,CRITICAL,swap thrashing,...
```

Both `ssh_host` and the tool's own `host` column are kept deliberately: they
differ (a login alias versus what the machine calls itself), and knowing
which is which matters when they disagree.

And the consensus view still applies on top, because `why-slow --csv` is
deterministic — so hosts with the same problem land in the same group:

```text
  GROUP 1  44 hosts  ok    <- baseline      (nothing wrong)
  GROUP 2   4 hosts  ok                     (+SWAP_THRASH, +MEM_EXHAUSTED)
  GROUP 3   2 hosts  unreachable
```

That is fleet-wide triage in one command.

## IPv6

Supported, in the bracket form when a port is involved:

```text
::1                    a bare literal, no port
[fe80::1]:2222         bracketed, because the port needs a separator
v6=[2001:db8::1]:22    named, like any other entry
```

A host token is `name[=addr[:port]]`, so the last colon means a port — which
is why a bare `::1` once became the address `:` on port 1, and why the
brackets are not optional once a port is involved. Addresses are validated
with `inet_pton` where they are written, so `2001:db8::1::2` is refused
there rather than surfacing later as a connection error naming the wrong
cause. `ssh` is given the address bare and `scp` gets it bracketed, since
`scp` splits its argument on the last colon to find the path.

`[::1]` is an address, not a range: the expander leaves any bracketed group
containing a colon alone.

**`netmesh` still refuses IPv6** — its agents bind sockets, echo UDP and
walk path MTU, so the family reaches much further into it than a host list.

## watch

`agree run` answers *who disagrees, right now?* once, for one command.
`agree watch` makes it a standing check of what the fleet is running —
firmware, kernel, NIC driver and settings — that a cron job or a CI step can
run every night. `binnacle watch` is the same command.

```bash
agree watch record -b fleet.json --servers prod.txt   # save the answers
agree watch check  -b fleet.json                      # later: what moved?
```

`record` runs a set of probes on every host, all of them in one ssh, and
saves each host's raw answers in the baseline file, with the probe set and
the host list. `check` runs the same probes again, on the same hosts unless
`--servers`/`-S` names others, and reports two different things:

- **CHANGED SINCE THE BASELINE** — a host's answer is not what it was when
  the baseline was recorded. This is the regression watcher, and the only
  way to see a change that the whole fleet took at once: if every host
  moved to the same new kernel, nobody disagrees with anybody.
- **DISAGREES WITH ITS PEERS** — a host's answer is not the majority's, by
  exactly the grouping `agree run` uses (the largest successful group is
  the majority). This is the drift checker, and it does not care what the
  baseline says: a host that was already the odd one out when it was
  recorded is still named.

```text
agree watch -- 48 hosts, 5 probes, against fleet.json of 2026-11-17 02:10:04 +0000      [normalized: strip-ansi, trim]

CHANGED SINCE THE BASELINE  1 host
  node31
    bios:  (- baseline, + now)
      -2.1.0
      -03/04/2025
      +2.3.1
      +09/10/2026

DISAGREES WITH ITS PEERS  1 host
  bios: 46 agree on "Dell Inc." (+2 more lines)
    node31:  (- the 46, + this host)
      -2.1.0
      -03/04/2025
      +2.3.1
      +09/10/2026

UNREACHABLE  1 host, not compared:
  node09         unreachable: ssh: connect to host node09 port 22: Connection timed out

DRIFT: 1 host changed since the baseline; 1 host disagrees with its peers; 1 host not checked
```

A host that gave no answers at all — ssh failed, `sudo -n` was refused, the
run timed out — is listed under **UNREACHABLE** and never counted as drift.
A host that was unreachable when the baseline was recorded stays in it with
no answers, so a later check still goes to it, and compares it with its
peers only (**NOT IN THE BASELINE**).

### The probes

| Probe | Asks |
|---|---|
| `bios` | `/sys/class/dmi/id/bios_vendor`, `bios_version`, `bios_date` |
| `microcode` | the CPU microcode revision, from `/proc/cpuinfo` |
| `kernel` | `uname -r` |
| `nic-driver` | every interface with a device behind it: `ethtool -i`'s driver, version and firmware-version (the driver from `/sys` without ethtool) |
| `nic-settings` | the same interfaces: MTU, and with ethtool the speed, duplex and autonegotiation, ring sizes (`-g`), channels (`-l`) and offloads (`-k`) |

None of these needs root. A probe's stderr is part of its answer and so is
its exit status: a probe that fails the same way on every host is
agreement, and one that starts working on one host is a change.

The set is yours to change. `agree watch probes` prints it as a file that
`--probes FILE` reads back — one `NAME = COMMAND` per line, run by `sh`,
`#` comments — so the usual way to make your own is to start from it:

```bash
agree watch probes > probes.txt          # edit it
agree watch record -b fleet.json --servers prod.txt --no-defaults --probes probes.txt
agree watch record -b fleet.json --servers prod.txt \
      --probe 'bmc = ipmitool mc info | grep -i firmware' --sudo --skip nic-settings
```

`--probe NAME=COMMAND` adds one, or replaces the probe of that name in its
place; `--skip NAME` leaves one out; `--no-defaults` starts from nothing.
These are `record`'s (and `probes`'s): **`check` runs exactly the probes
saved in the baseline** and refuses the flags, so a check can never compare
answers to two different questions. Record again to change the set.

### Normalization

The baseline holds every answer raw. The normalizations `agree run` has —
`--loose`, `--strict`, `--sort-lines`, `--mask-numbers`, `--scrub RE` and the
rest — apply at `check`, to every probe, and are printed in the heading as
always. The slices (`--grep`, `--head`, `--tail`, `--field`) are not
offered: they pick lines out of one command, and a check runs several.

`--json PATH` writes the findings as well: `changed`, `disagrees`,
`unreachable`, `not_in_baseline` and `not_checked`.

### Exit codes of watch

| Code | `record` | `check` |
|---|---|---|
| `0` | every host answered | no host changed, none disagrees |
| `1` | — | a host changed since the baseline or disagrees with its peers |
| `3` | some hosts could not be recorded (none at all: the file is left as it was) | no drift, but some hosts could not be checked |
| `2` | usage error | usage error, or a file that is not a baseline |

Drift outranks an unreachable host here, unlike `agree run`: the finding a
nightly check exists for is the change, and the unreachable hosts are
listed beside it either way.

### Why it is a verb of agree

It runs on agree's runner, groups with agree's grouping and normalizes with
agree's normalizations, and every tool here is one standalone file that
imports nothing from its siblings. So `watch` lives in `agree.py`, beside
the code it is built from, rather than in a copy of that code. The baseline
is [`reckon --baseline`](reckon.md)'s idea: an earlier run's own output,
refused if it is anything else, and named with when it was taken.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | unanimous, everything succeeded |
| `1` | divergence: more than one successful group, **or** nothing left to compare (see below) |
| `3` | at least one host failed — outranks divergence |
| `2` | usage error |

Directly usable from cron.

## See also

- [`reachable`](reachable.md) — prune the host list first, so the fan-out
  is not half wasted on dead entries
- [`why-slow`](why-slow.md), [`logtriage`](logtriage.md) — the two tools
  designed to be fanned out by this one
