# logtriage

**Which ten lines of this log matter?**

Masks the variable parts of every line to find its *shape*, counts the
shapes, and ranks them by what is new, severe and sudden — not by what is
frequent.

```bash
logtriage /var/log/syslog                    # what matters, ranked
logtriage /var/log/syslog --split-at 14:20   # what started at 14:20
logtriage /var/log/syslog --baseline yesterday.log
logtriage --mask 'LINE'                      # why two lines did not merge
logtriage /var/log/*.log --csv               # tidy output, for fleet use
```

## What it looks like

```text
logtriage -- /var/log/syslog, 2.1M lines, 1,847 templates, 04:00 -> 16:22
             baseline: everything before 10:11, buckets 30s

  #1  score 8.4   NEW   sev=error   1,204x   ▁▁▁▁▁▁▁▁▁▂▄██▇▅▃▂▁▁▁
      kernel: EXT4-fs error (device <PATH>): ext4_find_entry:<NUM>: inode #<NUM>
      first 14:21:06   last 16:22:41   burst 41x
      not seen before 14:21:06; 1,204 since

  #2  score 6.9   NEW   sev=crit    3x       ▁▁▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁
      kernel: Out of memory: Killed process <NUM> (java) total-vm:<NUM>kB
      first 14:22:58   last 14:24:10

  #3  score 3.1         sev=error   88,204x  ▄▅▄▄▅▄▅▄▄▅▄▅▄▄▅▄▅▄▄▅
      sshd: Failed password for invalid user <STR> from <IP> port <NUM> ssh2
      first 04:00:02   last 16:22:58

  WHAT TO DO NEXT
    * #1 and #2 start within 2 minutes of each other. Read them as one
      event rather than two: the filesystem started erroring, and the
      process blocked on it got killed. Start at the device, not at java.
      Earliest is at 14:21:06 -- start there.
    * #3 is 88,204 lines and steady across the whole window, so it is
      background, not your problem -- it is only listed because of its
      volume.
```

## Frequency is the wrong ranking

The most frequent line in any log is something like `session opened for user
root`, and it has never once been the answer. Sort by count and you get the
noise, every time.

What matters is what is **new**, what is **severe**, and what **suddenly
started**. So the score is:

```text
score = 3·(severity/3) + 3·novelty + 2·burst + 1·frequency
```

with the weights adjustable (`--weights sev=3,novel=3,burst=2,freq=1`) and
every component printed so you can see why something is at the top.

The effect, measured on the test fixture: **3 OOM kills rank above 2,762
authentication failures.** That is the whole point of the tool.

## Templating

A log is not thousands of distinct events. It is a few hundred *shapes* with
the variable parts filled in differently. Masking those parts leaves the
shape, and the shape is what you want to count.

The substitutions run in a fixed order, and **the order is the algorithm**:

```text
 1. ANSI escapes
 2-6. timestamps: ISO8601, syslog, Apache/CLF, dmesg monotonic, bare clock
 7. UUID          8. MAC           9. IPv6         10. IPv4
11. URL          12. email        13. pid          14. absolute path
15. hex blob     16. quoted str   17. size+unit    18. number
19. whitespace runs
```

A UUID has to be caught before the hex rule would eat its pieces, an IPv4
before the number rule, and a timestamp before both. `--rule 'RE=>NAME'`
inserts your own patterns **ahead of all of them**, so you always win:

```bash
logtriage app.log --rule 'acct-\d+=>ACCT' --rule 'tenant=[a-z]+=>TENANT'
```

`--keep-paths`, `--keep-quoted` and `--keep-numbers` switch off individual
built-ins when the thing you care about is exactly what they mask.

### Why not Drain

Drain-style prefix-tree clustering tolerates messages whose token *count*
varies, which regex masking does not. It costs a tree, a depth parameter, a
similarity threshold, and — the deciding factor — **order dependence**: the
same log fed in a different order can produce different cluster ids.

Deterministic ids are what let a fleet be compared. Because the template id
is a hash of the masked shape, the same line produces the same id on every
machine, so [`agree`](agree.md) can group hosts by *which templates they are
emitting*. A tree cannot promise that.

The honest cost: `retrying in 3s (attempt 4 of 10)` and `retrying now` stay
separate templates. That is stated rather than hidden, and
`logtriage --mask 'LINE'` shows you exactly which rules fired on any line,
so you can see why two lines did not merge.

## Novelty needs a baseline

By default the log is **split in half** and the first half is the baseline.
That answers "what changed" with no state, no config file and no second
file — which is the common case at 3am.

```bash
logtriage syslog                       # first half is the baseline
logtriage syslog --split 0.75          # move the cut
logtriage syslog --split-at 14:20      # what started at 14:20
logtriage syslog --split-at -30m       # what started in the last half hour
logtriage syslog --baseline yesterday.log
logtriage syslog --save-templates today.csv
logtriage syslog --baseline-templates yesterday.csv
```

`--split-at` is the sharp one, and worth reaching for: give it the time the
pager went off and the report tells you what began then.

The template-digest files (`--save-templates` / `--baseline-templates`) are
opt-in explicit state — a file you name, in a place you chose. Not a dotdir,
and never automatic.

## Multi-line records

A Java or Python traceback is **one event**, not forty. Left alone it
manufactures forty fake templates, and that does not merely add noise — it
destroys the ranking, because every fragment looks novel.

So continuation lines (`\tat com.example...`, `Caused by:`,
`File "...", line N`, `... 14 more`, frame addresses) attach to the record
above, and the template is built from the first line plus a frame
signature. Two `NullPointerException`s from different call sites stay
separate; 400 from one site are one finding.

`--multiline off` disables it, mostly to demonstrate the damage.

## Scale

One streaming pass, bounded memory, fine on a multi-gigabyte file.

- Time buckets start at 1 second and **double** whenever the span outgrows
  4096 buckets, rebucketing what is already counted. A 30-day log lands on
  ~1024-second buckets after eleven doublings, all cheap. You never have to
  know the span in advance.
- `--max-templates` (default 20,000) caps memory. On overflow the coldest
  tenth are pooled into `<OTHER>` — and the tool **keeps admitting new
  ones**, because the late arrival is exactly what you came for.
- Eviction is reported, never silent:

```text
  12,403 templates were evicted into <OTHER> (48,102 records) -- raise
  --max-templates to see them
```

## Input

Files, globs, `-` for stdin, and `.gz` / `.bz2` / `.xz` read directly.
Eight prefix formats are parsed: syslog, RFC5424, journalctl, dmesg,
`dmesg -T`, Apache/CLF, JSON lines, and bare (no prefix at all).

With no timestamps anywhere, ordering falls back to line number and the
report says so once, at the top, rather than silently pretending.

By default templates from different programs are kept apart (`--by program`):
"connection refused" from `sshd` and from `postfix` are different findings.

## Output

`--csv` gives one row per template:

```text
template_id,rank,count,pct,first_seen,last_seen,severity,program,novel,
burst_factor,score,template,example
```

`--json` adds the per-bucket counts. Both are deterministic, so
[`agree`](agree.md) can group hosts by which templates they emit:

```bash
agree script ./logtriage.py --hosts prod.txt --fleet-csv -- /var/log/syslog --csv
```

## Debugging the tool itself

Two seams, documented rather than hidden, because they are also how you work
out why two lines did not merge:

```bash
logtriage --mask 'Jan  7 12:34:56 web01 sshd[1234]: Failed password for bob'
# <TS> web01 sshd<PID>: Failed password for bob
# id: 190cfb3b96

logtriage --parse 'Jan  7 12:34:56 web01 sshd[1234]: Failed password'
# format:  syslog
# ts:      12:34:56
# program: sshd
# pid:     1234
```

## See also

- [`why-slow`](why-slow.md) — the machine's state now, rather than its history
- [`agree`](agree.md) — to fan this out and group hosts by what they log
