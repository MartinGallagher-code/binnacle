# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.3.0] - 2026-08-17

### Added

- **`netmesh` measures latency under load — the number neither half of the
  toolchain had.** `netmesh` measured an *idle* network; `iperf_orchestrator`
  and `matrix_orchestrator` measure throughput and packets per second *under*
  load. Nothing measured what the load did to the latency, which is what
  everything else sharing the path actually experiences. A 9.4 Gbit/s result
  at 300 µs and a 9.4 Gbit/s result at 42 ms are different results, and
  without this they are the same number.

  `netmesh run --baseline 20 -- ./iperf_orchestrator.sh all` probes idle for
  twenty seconds, notes the split, then keeps probing while the load runs,
  and the report gains an idle-versus-loaded section per pair with the
  finding attached: *"latency under load rose 200x: p99 210us idle, 42.1ms
  loaded — that is the queue in front of the bottleneck filling up."*

  Nothing extra is measured and nothing twice. The agents already write one
  row per interval with the timestamp on it, so idle and loaded are two
  filters over rows that are on disk — which also means a run collected days
  ago can be re-split with `summarize --load-split`, the same replay
  property `--from-facts` gives the diagnostic tools.

  Loss that appears only under load gets its own finding, because a path
  that drops only when busy is a queue running out rather than a broken
  link and will not reproduce on an idle `check`. A split with nothing on
  one side of it is reported as exactly that rather than as an idle
  baseline of zero with an infinite regression against it: **blank means
  not measured**, here as everywhere else. `--bloat-factor` moves the line
  (default 4×) and is deliberately a ratio, since 200 µs to 800 µs on a LAN
  and 20 ms to 80 ms across a WAN are the same finding about the same
  queue.

- **`during --peer-samples`: one verdict from both ends of a test.** A
  network test has two machines in it and every tool here watches one. The
  composing guide has described the consequence since before anything
  computed it — *"a generator that reports cpu or one core while the target
  reports not bound means the benchmark measured the generator, which is
  the most common way a load test lies, and neither machine's own numbers
  say so on their own."* That stayed a thing you had to notice by reading
  two reports side by side.

  Both ends now go through the same `analyse()`, so the peer's facts are
  derived exactly as this run's are, and four rules read across them.
  `PEER_WAS_THE_LIMIT` fires when this box was at no ceiling and the far
  end was at one, and the verdict names that machine outright.
  `NEITHER_END_BOUND` is the finding that most needs two: both ends with
  capacity to spare means the limit is between them or inside the
  application — the path, a lock, or a single flow that cannot fill the
  link. `PEER_DROPPED` reports loss at the far end's card, which from this
  end is indistinguishable from a lossy path and is not the path's fault.

  `PEER_NOT_CONCURRENT` is a trust rule and leads the verdict, because two
  windows that never coincided are two experiments and every conclusion
  drawn from comparing them is meaningless. It is also where two of these
  tools meet: two machines that disagree about the time report windows that
  did not overlap when they did, and `skew` is what says that is what
  happened.

- **`during` now watches the receive path, which is where a network test
  actually fails.** Everything it sampled before was whole-box, and a
  whole-box average is structurally unable to show the failure that matters
  most under load: a machine that cannot pick packets up fast enough **is
  not busy**. Its work is in softirq on a single core, and every aggregate
  number reports it as almost idle while throughput sits at a third of line
  rate.

  Five new sampled columns, all procfs and sysfs, no root and no `ethtool`:
  `softirq_max_core_pct` from per-CPU `/proc/stat`, `softnet_drop_per_s` and
  `time_squeeze_per_s` from `/proc/net/softnet_stat`, `net_rx_missed_per_s`
  from the interface's sysfs statistics, and `net_cc` / `net_rmem_max_kb` /
  `net_numa` for whether the run could have reached line rate at all. They
  aggregate **worst-of, not mean-of**: a ring that overflowed for ten
  seconds of a five-minute run dropped packets, and averaging that towards
  zero would report a clean run.

  Six rules on top of them. `SOFTIRQ_BOUND` separates one core saturated in
  receive processing from a box that is genuinely busy — half a core in
  softirq only means something while the rest idle. `RING_OVERFLOW` and
  `BACKLOG_DROPS` are kept apart because they are opposite problems: the
  card had nowhere to put a packet, versus the card kept up and the host's
  own backlog did not. `TIME_SQUEEZE` catches NAPI polls cut off with work
  still queued. `NIC_NUMA` names the node the card is on, on multi-socket
  boxes only.

  The verdict follows the same discipline as the rest of the package: **a
  receive-path cause leads over the state it produced.** A box pinned in
  softirq is *why* the run looks network bound, and reporting "network" as
  the verdict sends someone to the switch — the same mistake `why-slow`
  refuses to make when it puts swapping ahead of the CPU number swapping
  caused. The verdict also says outright that packets dropped on this box
  are losses the network never caused, because every network-side
  measurement will otherwise blame the path for them.

- **`during --rtt-ms`, and a `WINDOW_LIMITED` rule.** Throughput on a single
  TCP flow cannot exceed window ÷ round trip whatever the link can do, so a
  test whose ceiling was the receive buffer measured the buffer and reports
  a number the network had no part in. That is **trust outranks
  attribution** applied to the network, so it leads the verdict rather than
  sitting among the findings. A 10 Gbit link at 40 ms is a ~49 MB
  bandwidth-delay product against the common 6 MB `tcp_rmem` ceiling, which
  caps one flow near 1.2 Gbit/s — wrong by a factor of eight if recorded as
  what the path can carry. The round trip is not measured here, because
  `during` watches one box and a round trip needs two; it arrives by flag
  from `netmesh` or anything else that measured it, and without it the rule
  **skips and names the flag** rather than assuming a number.

- **`skew` — an eighth tool: does this box know what time it is?** A
  crashed time daemon was already caught, by `why-slow`'s failed-unit rule,
  which says outright that a failed sync unit makes every timestamp on the
  box a lie. What nothing caught was the daemon running *perfectly* while
  the clock stayed wrong — sources unreachable, none ever selected, or a
  machine resumed from a snapshot. `systemctl status chronyd` reports
  `active (running)` through every one of those, so nothing on the box
  looks broken.

  It ignores what the daemon says about itself and queries each configured
  source over SNTP, built here rather than shelled out to `ntpdate`, since
  these files land on machines that have neither. Offset and delay come
  from the four-timestamp calculation, so a slow path lands in the delay
  instead of being charged to the clock, and the best of `--samples`
  queries is kept so a single sample over a congested link measures the
  congestion rather than the time. A reply that does not echo the transmit
  timestamp it was sent is discarded and the wait continues — the same
  discipline `resolve` applies to a DNS query id, and for the same reason.

  Thirteen rules, causes ahead of symptoms as everywhere else: a box with
  no reachable source is diagnosed as having **no reachable source**, not
  as being four minutes fast, because the drift is what that produced and
  the firewall is what someone has to fix. Sources are compared against
  each other, since two that disagree means the daemon may have picked the
  liar and no single query can see it. Stratum 16 and the leap alarm are
  separated from both *alive* and *dead*: a source reporting itself
  unsynchronised answers every reachability check and provides no time, and
  conflating the two is how a box has three working servers and no clock.
  The hardware clock is read alongside, because it is what the box comes
  back with after a reboot — and a delta of almost exactly a whole number
  of hours is reported as an RTC kept in local time rather than as drift,
  which saves chasing a CMOS battery that is fine.

  It composes like the rest: `agree script ./skew.py --fleet-csv` groups a
  fleet by what its clocks are doing, which is how six boxes in one rack
  that have been quietly four minutes out for a week become one line
  instead of a discovery. The reason it belongs here is that the rest of
  the package was already working around a wrong clock without ever
  checking it — `logtriage --split-at` trusts a timestamp, `agree` has to
  mask the `ts` column before two hosts can agree about anything, and
  `netmesh` reads only the sender's clock precisely so it never has to
  trust two at once.

### Fixed

- **A threshold flag was ignored under `--from-facts`.** `--max-offset` and
  `--warn-offset` were being collected into the fact dictionary, so a
  saved file's thresholds silently won and the flag did nothing on a
  replay. Thresholds are policy rather than measurement: they now resolve
  as flag, then whatever the fact file recorded — which is what makes
  `--from-facts` reproduce the run it came from — then the default, and
  are stamped back into the facts either way, so a saved `--json` says
  which line each finding was judged against instead of leaving a reader
  to assume the defaults were in force. Found by testing that the flags
  moved a finding's severity, rather than by testing that they parsed.

## [0.2.1] - 2026-08-16

### Fixed

- **Rewriting a host list did not give the file back as itself.**
  `reachable -i` is the one operation in this package that edits a file
  you already had, and temp-file-plus-rename loses two things unless they
  are carried across. A `0600` inventory came back **`0644`** — a tool run
  to tidy a server list quietly published the name of every box in it. And
  a **symlink was replaced by a regular file**: an inventory pointing into
  a shared checkout lost the link, while the file everyone else reads
  stayed stale, so the edit appeared to work and changed nothing for
  anybody. The target is now resolved before writing, and mode and
  ownership are copied onto the replacement. `netmesh`'s mesh writer got
  the same treatment: a mesh carries hand edits — the `~` ping-only prefix
  is a human's mark — so regenerating it must not widen its mode either.
- **A host printing an error to stdout could become a *column*.** Aligning
  the merged CSV by column name (above, this release) meant every host's
  header joined the merged one — so a host that complained on stdout and
  exited 0 contributed its complaint as a column heading. Version skew is
  a *partial* overlap with the schema; sharing nothing with it is a
  different tool's output, and a single-field header is not a header at
  all. Both are now reported as *not merged, output is not this CSV* and
  left out, while genuine skew still merges. Found by probing the merge
  change from this same release rather than by trusting it.
- **Three bad arguments answered with the wrong thing entirely.** An
  invalid regex to `agree --grep`/`--vgrep`/`--scrub` surfaced as a raw
  traceback from inside the per-host normalizer once the fan-out was
  already underway; it is now refused at startup naming the flag, the
  pattern and the reason. A typo'd `--hosts` path fell through to being a
  *hostname* — a fleet of one bogus host that then failed as
  "unreachable", blaming the network for a typo; nothing with a path
  separator is a valid hostname, so it is refused by name. And
  `during --from-samples` on a CSV with none of the columns a sample
  carries classified every row as "not bound" and printed a confident
  *"this box was not the bottleneck"* from data that measured nothing —
  the clean-report-you-cannot-trust failure; a foreign CSV is now refused
  with the question it raises. A wider sweep in the same pass came back
  healthy: `agree doctor` exits 3 when a host never answered, the
  template round-trip marks only the genuinely new shape as NEW,
  `--recheck-only` restores a recovered host while leaving hand-written
  comments alone, the `--limit` guard refuses on a non-tty, and every
  `--explain` of a bogus rule says "no such rule" with the fix.

## [0.2.0] - 2026-08-16

### Added

- **`agree --fleet-csv`** — the preset for grouping the CSV these tools
  emit, and exactly `--mask-hosts --mask-times`. Every one of them writes
  rows beginning `host,ts`, both per-host by construction, so without them
  no two hosts can agree about anything. A preset rather than a default
  because the normalizations are still disclosed with the result: a reader
  has to be able to see that the host column was masked to get the
  grouping. Contradicting it with `--strict` is refused rather than
  silently resolved.
- **IPv6 host lists in `agree` and `reachable`**, replacing the refusal
  added in the previous change. `::1` bare, `[fe80::1]:2222` bracketed
  when a port is involved, `name=[2001:db8::1]:22` named — validated with
  `inet_pton` where they are written, so `2001:db8::1::2` is refused there
  instead of surfacing later as a connection error naming the wrong cause.
  `ssh` is given the address bare and `scp` bracketed, since `scp` splits
  its argument on the last colon to find the path. `agree hosts` prints
  the bracket form so its output reads back as the same host — without
  that, `fe80::1:2222` round-tripped into a different and entirely valid
  address. The range expander leaves any bracketed group containing a
  colon alone, since `[::1]:2222` would otherwise expand to `::1:2222`.
  `netmesh` still refuses IPv6: its agents bind sockets, echo UDP and walk
  path MTU, so the family reaches far further into it than a host list.
  `reachable` passes `-6` to `ping` for v6 addresses, and treats a ping
  that cannot speak the family as unknown rather than as a down host.

- **`tests/test_compose.sh`** — the claim on the front page, actually run.
  Real tools pushed to fake hosts through real `agree`, asserting that
  hosts group by what is wrong with them, that `--merge-csv` keeps the
  values masking hid, that the diagnostic tools share one CSV header, and
  that a host which never answered is a group rather than a line on stderr.

- **`during`** — a seventh instrument: *what limited this run, and can you
  trust the number?* The others diagnose an instant; this samples a whole
  window and answers what a point-in-time tool structurally cannot.
  - Every sample is classified into **exactly one** state — cpu, one core,
    io, memory, network, throttled, stolen, or not bound — so the shares add
    up and "bound by two things at once" cannot be reported.
  - **"This box was not the bottleneck"** is a first-class finding: nothing
    near a ceiling means the limit was the load generator, the peer, or a
    lock inside the application.
  - **`one core` is its own state.** A serialised benchmark pins one core
    and leaves an eight-core box reading 12% busy, which every whole-box
    average calls idle — and the usual next step is a bigger instance that
    changes nothing.
  - **Trust outranks attribution.** Warmup, an interloping process, a
    falling clock, an exhausted burst balance, steal and instability all
    rank above the bottleneck in the verdict, because a bottleneck
    attributed from an invalid run is a confident wrong answer.
  - The wrapped command's output and exit status pass straight through, so
    `during -- make bench` is a drop-in prefix; its process tree is excluded
    from interloper detection, re-derived every sample so forked workers
    still count as the benchmark. `^C` still prints the report.
  - `--samples` writes the raw tidy series; `--csv` writes findings with
    why-slow's header; `--from-samples` re-runs the whole analysis over a
    saved series, which is how it is tested; `--baseline` compares two runs.

- **`resolve`** — a sixth instrument: *is it DNS, and which resolver is
  wrong?* Queries every configured nameserver directly on the wire rather
  than through the stub, because the stub is what hides the fault.
  - **A dead resolver earlier in the list outranks the latency it causes.**
    A box whose first nameserver is dead resolves everything correctly and
    slowly for ever, and every `dig` against the working server says DNS is
    fine — the fault is invisible to any tool that asks one question.
  - Answers are compared across resolvers to catch split horizon and stale
    caches, **sorted**, so a resolver rotating records round-robin is never
    mistaken for a disagreement.
  - The search-domain cost is measured by walking the list and counting the
    NXDOMAINs, not inferred from `ndots`; `getaddrinfo` is timed alongside,
    and the gap between it and the fastest resolver is the cost of the
    configuration rather than of the network.
  - A local stub (`127.0.0.53`) is named as one, because timing it says
    nothing about the upstreams behind it.
  - DNS packets are built and parsed in-module — no `dig`, standard library
    only. Replies whose id or question does not match are discarded rather
    than counted, and query ids come from `os.urandom`.
  - Thirteen rules, `--csv` with the same header as `why-slow` so `agree
    --merge-csv` stacks them, plus `--rules`, `--explain`, `--facts` and
    `--from-facts`.

### Changed

- **`why-slow`** now runs 30 rules rather than 23. The seven new ones
  measure **ceilings rather than rates**: `CONNTRACK_FULL`,
  `FD_EXHAUSTION`, `PID_EXHAUSTION`, `CGROUP_PIDS`, `EPHEMERAL_PORTS`,
  `ARP_TABLE_FULL`, and `LIMIT_HITS`.
  - A box can be idle and still refusing work. These limits have no
    gradient to watch — they work until abruptly they do not — so they are
    reported as ratios against their ceilings before the wall is reached,
    and an `EVIDENCE` line shows every ratio whether or not a rule fired.
  - `LIMIT_HITS` reads the kernel log for a table that has *already*
    overflowed, and ranks with `OOM_KILLS`: the table has drained since, so
    every ratio measured now looks healthy and only that line survives.
  - The exhaustion rules sit above the network symptoms they cause in the
    verdict precedence — a full conntrack table produces the drops and
    retransmits, so naming the drops would be the same mistake as naming
    the CPU on a swapping box.
  - A healthy box now suggests `resolve` alongside `netmesh` and
    `logtriage`.

### Fixed

- **A counter that reset between snapshots became a negative rate.**
  `why-slow` and `during` compute rates from paired `/proc` reads, and on
  a live box counters go backwards — a container restart, a module
  reload, a wrap. `(b - a) / dt` with `b < a` reported "-99990 pg/s in"
  and CPU percentages past 100. A pair the kernel reset between reads is
  not a measurement: every rate and every CPU component now checks the
  delta's sign, and an inconsistent pair leaves the field blank — the
  first convention on the conventions page, applied to the sampler
  itself. The per-core path already guarded CPU hotplug; the aggregate
  path now matches it.
- **A fleet running mixed tool versions could silently misfile its merged
  CSV.** These tools get scp'd to machines and stay there, so version skew
  across a fleet is the expected state, not the exception — and
  `agree --merge-csv` stacked every host's rows positionally under the
  first host's header. A host running an older copy with a missing column
  produced ragged rows; one with *reordered* columns filed its values
  under the wrong headers with nothing to show for it. Rows are now
  aligned by column name: a value stays under its own column wherever the
  emitting host put it, a column a host does not have is left blank, and
  the skew is reported to stderr with which host lacks or adds what —
  mixed versions are exactly the disagreement this tool exists to
  surface.
- **`resolve --server` took a typo for a dead fleet.** A resolver written
  as a hostname or a malformed address (`999.1.2.3`) was probed anyway
  and reported as *no resolver answered, CRITICAL* — a usage error
  laundered into a finding. Worse, a hostname there would have to be
  resolved by the very stub this tool exists to diagnose. `--server` now
  requires an IP address (v4, v6, bracketed-with-port all fine) and is
  refused at startup with the reason, matching where `agree` and
  `reachable` validate their host tokens.
- **New Year's Eve reversed a syslog file.** Syslog lines carry no year,
  and `logtriage` stamped every one with the file's year — so a log
  crossing midnight on December 31st, read in January, put its December
  lines eleven months into the *future*. The span printed backwards, the
  midnight incident fell into the baseline, and a routine heartbeat
  outranked the actual emergency — inverted triage on exactly the night
  nobody wants it. Two facts pin the year down and both are now used: no
  line can be written after the file's last write, so anything "later"
  than the mtime belongs to the year before; and a half-year jump
  backwards between adjacent lines is a rollover, not time travel. The
  test pins the absolute year as well as the ordering, because the
  relative order can be repaired while every December line still sits in
  the wrong year — each rule was disabled separately to prove the test
  catches its absence.
- **A mangled DNS reply could become an answer instead of an error.** A
  record whose `rdlength` runs past the end of the packet — a broken
  middlebox, a truncating proxy — sliced quietly and handed back a
  one-byte "address", because Python slicing never complains. That bogus
  value then fed the cross-server comparison, which could have reported
  *your nameservers disagree* off corruption. The parser now refuses the
  record. The rest of the parser held up under crafted packets —
  compression-pointer loops, self-referencing pointers, pointers past the
  packet, truncated headers, 5 KB names — all already clean `DNSError`s,
  and a reply that fails to parse keeps the query waiting on its budget
  rather than killing it, so a spoofed or late packet cannot deny the
  real answer.
- **One runaway host could sink a whole `agree` fan-out.** Everything a
  child printed was buffered in memory, so a single box caught in a log
  storm — exactly the kind of box this tool gets pointed at — grew the
  process by the size of whatever it printed, multiplied by `--jobs`:
  200 MB of output cost 593 MB of RSS, before multiplying. Output is now
  read incrementally and cut at `--max-output` (default 16 MB,
  `AGREE_MAX_OUTPUT`); a host past the cap is killed and reported as its
  own finding, not grouped, because a truncated flood agreeing with
  anything means nothing. The reader uses `selectors`, not `select()`,
  so a wide `--jobs` cannot trip the 1024-descriptor ceiling.
- **Timeouts and durations were measured on the wall clock.** `resolve`'s
  query deadlines and latency numbers, `reachable`'s and `agree`'s
  per-host durations, and `netmesh`'s CPU-rate denominator all used
  `time.time()`, so an NTP step mid-run could stretch a timeout, produce
  a negative latency, or corrupt a rate. All measurement clocks are now
  `time.monotonic()`, matching `during`, which already did this
  correctly; wall time remains only in displayed timestamps, where it is
  the point.
- **A disk that filled up was answered with a stack trace.** Every tool
  that writes an output the user names — `--csv`, `--json`,
  `--save-templates`, `--samples`, `--merge-csv`, `netmesh`'s mesh, report
  and hops files — met ENOSPC, a quota, or a typo'd directory with a raw
  traceback, burying the one fact that matters. Each write is now wrapped
  in a guard that says `cannot write <path>: <why>` and exits 2, and
  removes the partial file it leaves behind — an empty CSV that parses is
  worse than a missing one — except when appending, where what was already
  there predates the failure and is still good. stdout is deliberately not
  guarded, so `tool | head` keeps ordinary pipe semantics. The guard is
  duplicated into all seven modules and held identical by the drift check,
  which now compares classes as well as functions. Found by mounting a
  64 KB filesystem and pointing every writer at it; the test stages the
  same failure with a missing directory and a zero file-size ulimit, which
  need no root.
- **`reachable -i` could leave a truncated `.bak` behind.** If the disk
  filled while the backup was being copied, the run stopped correctly and
  the user's file was untouched — but a partial `.bak` stayed, and a
  truncated backup is a trap for whoever restores from it. The failed
  backup is now removed before the tool dies. (`netmesh`'s mesh writer had
  the same gap with its temp file, fixed the same way; `reachable`'s own
  `write_atomic` already cleaned up after itself.)
- **A file saved by Notepad or exported from Excel changed its meaning.**
  Both prepend a UTF-8 byte-order mark, and every reader here glued it to
  the first token. `reachable` probed `<BOM>web01` — a name that cannot
  resolve — and would have commented a live machine out of the user's own
  host file; `agree` fanned out to the same wrong name; `resolve` read a
  healthy box's `resolv.conf` as having **no resolvers at all** and said so
  as CRITICAL; `during --from-samples` saw the CSV's `host` column as
  `<BOM>host` and reported the host as `?`; `logtriage` silently lost the
  first line's timestamp. Every file read now decodes `utf-8-sig` — byte-
  identical to `utf-8` unless the file starts with a BOM, in which case
  the BOM is decoded away — and the two stdin paths strip it explicitly,
  since a pipe can carry it too. Writes still never emit one, so a file
  `reachable` rewrites comes out BOM-free: converging on the plain form is
  the point, the same as its comment markers. CRLF endings, the other
  thing Windows adds, already worked everywhere — the new test pins both
  so neither survival depends on an accident of `.strip()`.
- **One non-ASCII byte anywhere in the input lost the whole run, in all
  seven tools.** The CI failure that prompted this was in the suite rather
  than a tool — the drift check read source with a bare `open()` and died on
  the sparkline characters in `logtriage.py` — but the same assumption was
  in every tool, and a survey under `LC_ALL=C` found three distinct ways it
  broke. `logtriage` crashed on *output*, having read a log fine and then
  failed printing its own sparkline. `reachable`, `during`, `agree` and
  `netmesh` crashed on *input*: a UTF-8 hostname comment, a `make` target
  echoing a typographic quote, one accented word in a log. Worst was
  `resolve`, which crashed nowhere and instead read a perfectly good
  `resolv.conf` as **empty** — reporting "0 resolvers" for a box that had
  one, the failure mode the conventions exist to prevent, and caught only
  because the `NOTHING_CHECKED` guard added earlier refused to call that
  health.

  Every file read, every file write and every subprocess pipe now names
  `utf-8` explicitly rather than taking whatever the locale offers, with
  `errors="replace"` on the way in so an undecodable byte costs one
  character rather than the finding it appeared in. Naming the codec
  matters twice over: `errors="replace"` alone stops the crash but
  substitutes U+FFFD, which is itself unencodable on the way back out, so
  `reachable -o` read a host list correctly, judged it correctly, and then
  died writing it — leaving no output file at all, for a tool whose job is
  editing the user's own file. A comment it does not understand now
  round-trips as the bytes it arrived as. A `_stdio_safe()` helper rewraps
  `stdout`/`stderr` with `backslashreplace` when the locale claims ASCII,
  so a report is never lost to the terminal it is being printed on.
  Reproducing any of this needs `PYTHONCOERCECLOCALE=0 PYTHONUTF8=0` as
  well as `LC_ALL=C`: PEP 538 quietly coerces the C locale to UTF-8 on
  modern Python, which is exactly why none of it showed up until a RHEL 8
  container ran the suite. `_stdio_safe` is now on the verbatim-copy list,
  so the seven copies this fix created are held identical by the check
  added alongside it.
- **Nothing checked that the deliberate duplication had not drifted** —
  the arrangement `shared_tools` exists because of, its README naming *"the
  two copies that once existed drifted apart"* as the reason. The suite now
  compares every pair declared verbatim as code with docstrings stripped,
  so per-tool prose stays local while behaviour cannot diverge, and checks
  that every `canonical copy:` pointer names a file that exists —
  `reachable` still pointed at `scripts/agree.py`, from before this package
  moved to `binnacle/`. Both halves were verified by breaking a copy and a
  pointer on purpose and watching the suite fail.
- **`during`'s provenance comment claimed more than was true.** It named
  `why-slow` as the canonical copy of ten `/proc` readers when five are
  deliberately trimmed — narrower fields, aggregated where `why-slow` keeps
  the parts separate — so a parser fix applied upstream would have been
  pasted into functions that were never the same. The comment now names
  both sets and says which is which.

- **Seven flags existed without appearing in their tool's `--help`**,
  including `--fleet-csv` itself the moment it was added. Each tool's
  `--help` prints its module docstring and the CLI reference is generated
  from the live parsers, but nothing checked that the hand-written
  docstring lists every flag the parser accepts — so a flag could work,
  be documented on the site, and still be invisible to the person holding
  the terminal. `agree --sudo-user/--pull-dir/--keep-remote/--no-trim/
  --no-strip-ansi`, `logtriage --max-template-len` and `during --explain`
  are now written down, and a test enforces it across every tool.
  `netmesh` is exempt by its own wording — its docstring lists "common
  options" and its `help` verb prints every flag of every verb — and the
  test checks that escape hatch actually works.

- **`^C` did not print `during`'s report if the command ignored it.** The
  run blocked in `wait()` until the wrapped command finished, so a
  benchmark that traps `SIGINT` — `make`, a JVM, most test harnesses —
  held the report hostage for as long as it liked, which is the
  twenty-minutes-lost-to-a-keystroke failure the tool's own docstring
  promises to prevent. The command now gets two seconds to exit so its
  real status can still be collected, and is then reported as *still
  running* rather than killed.
- **A command killed by a signal reported a status no shell would.**
  `Popen` gives `-N` for a signalled child, and passing that through hands
  the shell `256 - N` — `254` for a plain `^C`, where running the command
  directly gives `130`. Normalised to `128 + N`, since this is documented
  as a drop-in prefix.
- **An empty fact file read as a clean bill of health.**
  `why-slow --from-facts` on a `{}` document skipped all thirty rules and
  printed *"This box is not slow"* — the clean-report-you-cannot-trust
  failure one step further along: not a run that quietly checked less, but
  one that checked nothing at all and still came back healthy. Both it and
  `resolve` now report `NOTHING_CHECKED`, which carries through the CSV and
  the exit status, so a monitoring wrapper cannot read it as healthy either.
  A fact file that is not a dictionary is refused with a message rather
  than an `AttributeError` traceback.
- **A NaN or an infinity in a `during` sample column was treated as a
  measurement**, quietly distorting every mean and threshold downstream of
  it. Corrupt input now reads as blank, which is what the rest of the
  package means by "not measured". A series with no `host` column printed
  the literal `None` as the hostname; all three rule tools now print `?`
  when the name is missing or null.
- **`during`'s timeline grew without bound.** A ten-minute run at the
  default interval is 600 samples, and one character per sample is a
  600-character line that survives neither a terminal nor a paste into a
  ticket. Bucketed to 60 columns, showing each bucket's peak, with the
  samples-per-column stated so the picture cannot be misread.
- **The "ok" line listing passed rules had no bound either**, and grew as
  rules were added — 175 characters on a healthy box after the seven new
  `why-slow` rules. Wrapped, with a count of the rules not named, in all
  three rule-driven tools. `during`'s rule titles were full sentences where
  the rest of the package uses short noun phrases; shortened to match.
- **`agree` and `reachable` silently destroyed IPv6 host tokens.** A host
  list entry of `::1` parsed as the address `:` on port 1, so every v6
  entry in a list collapsed to the same nonsense host — and `reachable`
  rewrites the file it is given, so it would comment out working machines
  on the strength of a parse bug. Both now refuse IPv6 outright, which is
  what `netmesh` has always done and for the reason its own comment gives.
  `reachable` validates every token **before** it probes or writes
  anything, so a list it cannot understand stops the run rather than
  surfacing halfway through a rewrite.
- **`agree --mask-hosts` could not mask a token that does not start with a
  word character.** The word-boundary fix in the previous change was
  written with `\b`, which is defined against word characters, so
  `\b::1\b` can never match. Replaced with an explicit boundary that also
  refuses to match a fragment of a longer address — `\b10.0.0.9\b` matches
  inside `192.10.0.0.9`, because a dot is a word boundary — while still
  masking a bare name inside its own FQDN, which is the part that differs
  between hosts.
- **`resolve` miscounted a repeated `nameserver` line.** Results are held
  per server, so a duplicated entry left `srv.count` larger than the number
  of results and `NS_ALL_DEAD` could never fire, however dead every
  resolver was. Each distinct server is now probed once, while the timeout
  budget still counts the file as written, because the stub works down it
  in order.
- **`during --exit-code` hid a failed command behind a severity.**
  `during --exit-code -- make bench` returned `10` when `make` exited `7`.
  A command that failed now always wins: there is nothing worth reading in
  a verdict about a run that never completed.

- **`agree --mask-hosts` masked substrings rather than names.** A host
  called `sql` turned `%HOST%` up inside `postgresql`, and a host called `a`
  turned every word containing an *a* into `%HOST%` — corrupting the
  comparison it exists to enable, differently on each host, and so
  manufacturing exactly the spurious groups it was meant to remove. It now
  matches on word boundaries, and leaves single characters alone.
- **`agree --mask-times` did not mask epoch seconds**, which is the one
  timestamp every tool in this package stamps its CSV with. Two hosts
  answering either side of a tick landed in different groups. Narrowed to
  the 2001–2033 range so an ordinary ten-digit number is not silently
  treated as a date.
- **The documented fleet-triage command could not group hosts.**
  `agree script ./why_slow.py --hosts prod.txt --merge-csv triage.csv --
  --csv` was missing `--mask-hosts --mask-times`, and every row begins with
  the machine's own name and an epoch — so every host became its own group
  and the headline result in the README was unobtainable. Corrected
  everywhere it appears, with the reason written down in
  [Composing them](composing.md), and now covered by a test suite that runs
  the real tools through real `agree`.
- **`logtriage --csv /var/log/syslog` analysed nothing.** `--csv` takes an
  optional path, so it swallowed the log and `logtriage` read stdin instead
  — which under `ssh` or a pipeline is empty rather than a terminal, so the
  help-on-no-input guard never fired. The failure now names the actual
  mistake. The same trap in `resolve --csv db01.example.com` is refused
  outright.

## [0.1.0] - 2026-08-15

First release. Five single-file, dependency-free diagnostics, packaged
together because they compose.

### Added

- **`why-slow`** — one-shot Linux box triage. Samples `/proc` twice, adds
  the kernel log, cgroup limits and filesystem fullness, and runs 23 rules
  producing a verdict plus the exact command to run next.
  - Rules are ordered by **cause rather than symptom**: a swapping box also
    looks CPU-busy, so swap thrashing and disk errors outrank CPU saturation
    in the verdict even when the CPU number is larger.
  - Container-aware — inside a cgroup your own limit is checked first and
    the host's numbers are demoted.
  - Every rule is a pure function of a fact dictionary, so `--from-facts`
    reproduces any diagnosis with no machine in that state.
  - `--csv` / `--json`; `--rules` and `--explain` are generated from the
    rule table.
- **`agree`** — fleet consensus runner. Runs a command across hosts over ssh
  and reports which hosts differ from the majority, with a unified diff.
  - A failed, timed-out or unreachable host is **its own group**, never a
    line on stderr.
  - Thirteen ordered normalizations with `--loose` / `--strict` presets; the
    active ones are always printed with the result.
  - `agree script` pushes a tool to the fleet and collects it;
    `--merge-csv` stacks the results into one tidy file.
  - Mutation guard for obviously destructive commands, honest about being a
    typo guard rather than security.
- **`logtriage`** — log triage by masking lines into templates and ranking
  by novelty, severity and burst rather than frequency.
  - Ordered regex masking, chosen over Drain-style clustering so template
    ids are deterministic and a fleet can be compared.
  - Automatic early/late split for novelty, with `--split-at` for "what
    started at 14:20"; multi-line records attach to the line above.
  - Bounded memory with honest reporting of what was evicted.
- **`netmesh`** — idle-network RTT, jitter, loss and path-MTU mesh.
  - UDP echoes between temporary agents: no root, no `CAP_NET_RAW`, and only
    the sender's clock is ever read, so RTT is exact without clock sync.
  - Loss is split into forward and return legs from the receiver's own
    count, distinguishing a sick sender from a sick receiver.
  - Unprivileged path-MTU discovery that confirms each size end to end and
    detects the black hole where small packets echo and large ones vanish.
  - Endpoints that cannot run an agent are reachable via `ping` with a `~`
    prefix, reported separately as reduced fidelity.
  - `selftest` proves the tool works on one machine before a fleet is
    involved.
- **`reachable`** — prunes a server list by pinging and ssh-ing every entry
  and commenting out the failures.
  - **ssh is the gate, ping is the explanation**: a host answering ssh is
    kept even without ping, since ICMP is blocked on plenty of healthy
    networks.
  - Entries it commented out are re-tested and **uncommented when they come
    back**, so the list converges rather than decaying; re-running with no
    change to the fleet produces a byte-identical file.
  - Only lines carrying its own marker are ever managed, so hand-written
    comments are never touched.
- Test suite: 74 checks across five suites, requiring no network and no
  second machine.
- Documentation on Read the Docs, with a CLI reference generated from the
  live argparse parsers so it cannot drift.

### Fixed

Bugs found while writing the test suite, before any release:

- `netmesh` wrote its report with **CRLF** line endings (the `csv` module
  default), leaving a stray carriage return on the last field for every
  shell tool that read it.
- `netmesh` **mis-parsed IPv6 tokens**, splitting an address on its own
  colons into a nonsense address and port; a run would have silently
  measured the wrong thing. Now refused outright.
- `netmesh` doubled a relative `--remote-dir` in two places: the start
  script `cd`s into the directory and then joined it back onto both the
  agent path and `--dir`.
- `netmesh` **discarded everything measured since the last interval** when
  `stop` sent SIGTERM, despite documenting that reports survive it. The
  signal now sets a flag and the loop exits through its normal final write.
- `netmesh` mesh-file errors cited the grid row index while the user is
  editing a file; they now cite the file line.
- `agree` split host specs on commas **before** expanding ranges, so
  `node[1,3]` became `node[1` and `3]`.
- `agree --version` was swallowed by the verb defaulting and printed usage
  instead of the version.
- `reachable` crashed on the restore path because `Result.__slots__` omitted
  the flag it sets. The atomic write meant the host list came through
  untouched rather than half-rewritten.

[Unreleased]: https://github.com/MartinGallagher-code/binnacle/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/MartinGallagher-code/binnacle/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MartinGallagher-code/binnacle/releases/tag/v0.1.0
