# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
