# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

### Added

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

[Unreleased]: https://github.com/MartinGallagher-code/binnacle/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MartinGallagher-code/binnacle/releases/tag/v0.1.0
