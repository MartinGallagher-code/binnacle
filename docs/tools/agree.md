# agree

**Which hosts in this fleet disagree with the rest?**

Runs a command across a fleet over ssh and reports the *consensus*: 47 hosts
agree, 3 do not, here is the diff.

```bash
agree -H 'node[01-24]' -- rpm -q openssl
agree --hosts prod.txt --loose -- uname -r
agree script why-slow --hosts prod.txt --fleet-csv --merge-csv triage.csv -- --csv
agree hosts -H 'rack[a-c]-node[01-04]'      # expand without running anything
agree doctor --hosts prod.txt               # can each host be reached and used?
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
      agree -H node31,node47 -- <the fix>
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

## Grouping

The group key is `(outcome, digest-of-normalized-output)` — **not** the
digest alone. Two hosts with identical stdout but different exit codes did
not give the same answer, and merging them would hide that.

Groups are ordered by size, then by first appearance in the host list. The
largest *successful* group becomes the baseline, and every other group is
rendered as a unified diff against it. A fleet where most hosts failed still
diffs against the ones that worked.

## Host lists

The same grammar as `servers.txt` in `iperf_orchestrator` and
`matrix_orchestrator` — `#` comments, blank lines, `name[=addr[:port]]`
tokens — so an existing fleet list works unchanged.

```bash
agree --hosts prod.txt -- uptime      # a file
agree --hosts - -- uptime             # stdin
agree -H web01,web02,db01 -- uptime   # inline
agree -H 'node[01-24]' -- uptime      # a range
agree -H 'rack[a-c]-node[01-04]' -- uptime   # cartesian
```

Ranges are worth the code: every real fleet is named this way, and typing
the list out by hand is exactly how a host gets left off the check. Commas
inside brackets belong to the range — `node[1,3]` is one spec, not two.

`agree hosts` expands and prints the list without running anything. Run it
before the scary command.

## Safety

`--dry-run` prints the exact ssh argv per host and contacts nobody. Always
available.

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
than a mystery hang. No password is ever read, passed or stored.

## Pushing a script

`--push FILE` copies a file to each host before running, `--pull GLOB`
collects files after, and `agree script PATH` is the sugar for the whole
cycle: push, `chmod +x`, run, collect, clean up.

```bash
agree script why-slow --hosts prod.txt --fleet-csv -- --csv --interval 2
```

This is the reason `agree` exists in the same distribution as the others.

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

## Exit codes

| Code | Meaning |
|---|---|
| `0` | unanimous, everything succeeded |
| `1` | divergence: more than one successful group |
| `3` | at least one host failed — outranks divergence |
| `2` | usage error |

Directly usable from cron.

## See also

- [`reachable`](reachable.md) — prune the host list first, so the fan-out
  is not half wasted on dead entries
- [`why-slow`](why-slow.md), [`logtriage`](logtriage.md) — the two tools
  designed to be fanned out by this one
