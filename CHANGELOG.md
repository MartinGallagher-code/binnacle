# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **`dredge` lands one directory of distinctly-named files, not a rebuilt
  tree.** Every collected file went to `collected/<host>/<the remote
  path>`, so forty machines produced forty identical paths under forty
  host directories -- which reads well and greps badly. The command you
  actually want next is `grep -l oom *`, or `logtriage
  dredge-*/web*syslog`, and both want one directory whose *names* tell
  the files apart:

  ```text
  dredge-20260910-172845/web01~var~log~syslog
  dredge-20260910-172845/web02~var~log~nginx~error.log
  ```

  `-d DIR` names the directory. Without it each run now gets one of its
  own, stamped with the time, rather than every run landing in
  `collected/` on top of the last: collecting the same path twice an hour
  apart is the normal way to use this, and the second run quietly
  replacing the first is not a result anybody wants to find later. Two
  runs inside one second get `-2`, `-3`.

  `--flat` is gone, since it selected what is now the only layout.

### Fixed

- **`netmesh paths` reported an unreachable host as a route with no hops
  in it.** The ssh exit status was captured and never read, so a trace
  that never ran came back as `0 hops` with the note `unparsed`, and the
  verb exited 0. "Unparsed" is a claim about output that arrived; nothing
  had. The note now names what happened, and a trace that could not run
  reaches the exit status.

- **`netmesh paths --compare` drew a conclusion from two routes nobody
  traced.** Two empty paths compare equal, and equality was reported as a
  finding: *"The two routes do not diverge, so the difference is not
  topological -- look at load or queueing on the shared path."* -- sending
  you to investigate load on a path that was never traced. It says what
  actually happened now. A route that is a shorter prefix of the other is
  also no longer called identical: they share every hop the shorter one
  has, and one of them carries on.

- **`netmesh paths --compare` silently used two of however many pairs it
  was given.** Its own help says "two pairs to compare hop by hop", and a
  third was traced and then dropped without a word. Refused now, naming
  what was passed.

- **`muster --stale-lock 0` broke a lock one second old.** The break test
  is `age > stale`, so a zero makes every lock breakable the instant it is
  taken -- `muster take --stale-lock 0` reported *"breaking a stale lock,
  0s old"* and took the pool out from under whoever was holding it. That
  is the one thing this tool exists to prevent: the lock is what stops two
  people being handed the same item. Refused now, along with a negative
  `--lock-timeout`; zero there stays legal, since "try once rather than
  block" is a real answer.

- **`netmesh gen` wrote a mesh netmesh's own validator refuses.**
  `CFG_NUMERIC` has bounded `port` since it was written, but only on the
  *read* path -- so `gen --port 0` exited 0, wrote the file, and every
  later command then rejected it with that same validator's message,
  "port=0 is out of range (want 1-65535)". The check is one function now,
  run when a mesh is read and when one is written.

- **`netmesh --interval 0` turned every agent in the fleet into a
  disk-filling loop.** The agent's report loop advances
  `next_report += self.interval` and falls back to `now + self.interval`,
  so a zero leaves no wait in it at all and it writes a report row per
  pass. Measured on loopback: a three-second run wrote **247,637 rows and
  11.7 MB**, against 12 rows for `--interval 1`. `start` deploys that
  agent to every host, and `collect` pulls the result back.

- **`netmesh gen --pps 0` (or negative) wrote a mesh netmesh will not
  read.** Every cell of the grid comes out empty, `gen` reports success
  while printing the nonsense back -- "2 ordered pairs, -5 probes/s
  each" -- and every later command then refuses the file it wrote, saying
  "every cell is empty, so there is nothing to measure": true, and not
  the cause. Both are checked now on the box where they were typed, which
  is the argument the `CFG_NUMERIC` comment already makes about the mesh
  config line; these are the two keys that comment's reasoning does not
  cover, since neither is clamped where it is used.

- **Five `netmesh` verbs printed a failure and exited 0 anyway.**
  `fleet.each()` counts the hosts that failed and logs "FAILED on N/M
  hosts", and `status`, `stop`, `clean`, `logs` and `doctor` each threw
  that number away and returned a hardcoded 0. `doctor` is the sharpest:
  it exists to answer "can this fleet be reached?", so
  `netmesh doctor && netmesh start` ran against a fleet doctor had just
  called broken. A `stop` that could not reach a host is worse than
  cosmetic -- the agent is still running there, and a stale agent goes on
  sending traffic into the next run's measurements. `run` and `check`
  discarded their sub-commands' statuses the same way, and `collect`
  returned a raw failure count that an exit status truncates to one byte:
  256 failed hosts came back as 0.

- **`resolve` read `options ndots:0` as `ndots:1`.** Zero is not an
  absent setting there -- it is the standard fix for the exact latency
  this tool is pointed at: try every name absolute first, never walk the
  search list. `f["res.ndots"] or 1` turned it back into 1, so every
  single-label name counted as unqualified, and `resolve` went out and
  *sent* the search-list queries to measure a cost that box does not
  pay. A machine that had already solved the problem was told it still
  had it, on the strength of queries its own resolver would never send.
  `None` -- resolv.conf unreadable -- is the only absent value, and is
  the only one that now falls back to 1.

- **`during` turned a fast benchmark's success into a failure.** When
  the wrapped command finishes inside one sampling interval there is
  nothing to report on, and the short-window path returned
  `child_status or 1` -- which reads a successful command's `0` as "no
  status at all". So `during -- make bench` exited 1 the moment the
  benchmark got fast enough to finish in under a second, and
  `during -- make bench && ./deploy` quietly stopped deploying with
  nothing in the message saying why. This is the tool's headline promise
  ("a drop-in prefix"), and the passthrough already worked for a command
  that *failed*: only success was being swallowed. The warning stays --
  it is the finding -- but the command's status is the command's,
  sampled or not. A bare `--seconds` window with no command to speak for
  still exits 1.

  The suite asserted the old behaviour, so the case that locked it in is
  corrected rather than added to, with a second case covering the other
  half: a command that fails inside a short window keeps its own status
  rather than being flattened to 1.

- **`agree` called it unanimous when the normalizations had removed
  every answer.** Hosts holding the same empty string group together, so
  a filter that matches nothing collapses a fleet that genuinely differs
  into one group -- digest `e3b0c44298fc`, which is the SHA-256 of the
  empty string -- under the words *"Every host gave the same answer.
  Nothing to chase here."* Three hosts that came back as three groups
  became "3 hosts, 1 group, 3 agree" with one `--grep`, and `--grep` for
  a line that turns out to be absent everywhere is not a typo: it is the
  ordinary case where its absence is the finding.

  This is the failure the tool exists to prevent, arrived at from the
  other side -- its own docstring says a tool that moves on quietly
  "lets you believe you checked them". A host whose output *was there*
  and was normalized away is now named, never counted as unanimous, and
  reflected in the exit status. A command that genuinely prints nothing
  everywhere is untouched: that is an answer, and hosts agreeing on it
  agree.

  The arguments that could only ever produce it are refused too.
  `--field` is 1-based, so `--field 0` was an off-by-one that returned an
  empty string for every line; a negative `--head`/`--tail` sliced from
  the wrong end, `--tail -1` dropping the *first* line rather than
  keeping the last N.

- **`agree`'s two fleet guards disabled themselves on a zero.** `--first`
  is the canary you run before the fleet and `--limit` is the refusal
  that stops a wide fan-out; both were written `if args.first` / `if
  args.limit`, so a zero read as "not given". `--first 0` -- an unset
  variable in a script, usually -- ran the *whole fleet* instead of one
  host, and `--limit 0` waved the fan-out through instead of refusing
  it. A negative was quieter still: `--first -1` slices to `hosts[:-1]`,
  which is every host but the last and looks exactly like it worked.
  Both now want at least 1 and say so, and the other numbers that cannot
  mean anything (`--jobs 0`, `--max-output 0`, `--timeout 0`) are
  refused with them.

- **`resolve --timeout 0` manufactured a DNS outage.** A zero timeout is
  not a fast query, it is no query -- so every resolver "failed to
  answer" and the tool announced, CRITICAL, that *no resolver on this
  box is answering. Every timeout you are chasing downstream starts
  here*, about a box whose DNS was fine. `skew`, which is the same shape
  of tool with the same two flags, has refused both since it was
  written; `resolve` now carries the same check in the same words.

- **`logtriage --split` accepted a fraction outside the log.** It is a
  fraction of the log, and outside 0..1 it put the baseline boundary
  outside the log's own time range -- printing a perfectly plausible
  "baseline: everything before 23:59:36" for a log that ends at
  13:59:48. The whole file then counted as baseline, so every `NEW` mark
  disappeared and the findings that matter quietly lost the score that
  comes with being new. It is refused now.

- **`agree --pull` failed silently.** The whole point of `--pull` is to
  bring the files back, and `pull_files`'s exit status was discarded: a
  glob that matched nothing, an scp that failed, a `--pull-dir` that
  could not be made -- all three looked exactly like success. The report
  said the fleet agreed and the results directory was empty. A host that
  brought nothing back is now named, with the reason, and counted in the
  exit status. It is deliberately not folded into the host's *outcome*:
  the command ran and its answer is real, so grouping it with the
  failures would move it into a group it does not belong in.

  Making the per-host directory could also take the run down with it. It
  happens in a worker thread, where the OSError travels up through the
  pool and replaces the entire report with a traceback -- over one
  host's directory.

- **The suite's fake NTP responder could be killed by the check waiting
  for it.** Readiness was tested by trying to bind the responder's port
  and treating failure as "it has it". A UDP port takes one owner, so a
  checker that wins that race holds the port for the instant before it
  closes -- and the responder binding in that window dies of EADDRINUSE,
  leaving the checker to wait out its whole loop for a port nobody will
  ever hold. Bound was also less than it looked: every use of this
  responder cares whether it *answers*. It now writes a ready file once
  it has the socket, and nothing contends for the port.

- **`dredge` reported a path it had collected as a host with nothing to
  send.** `find "$rel" -type f` does not follow a symlink, but the
  existence check in front of it does -- so `dredge /var/log/current`,
  where that name is a link, passed the check, matched nothing, and the
  host came back under EMPTY for a file that is plainly there. The named
  path is followed now (`find -H`, `tar -h`); a link *inside* a collected
  tree still is not, because a link is not evidence and recreating one
  here is how a collection directory grows a link out of itself.

- **`dredge --max-files` turned a deliberate partial collection into a
  failed host.** Stopping at the ceiling means closing the stream, which
  kills the far side with SIGPIPE; that status was then read as the
  host's outcome, so the report said `FAILED web01: exit 141` (or, on the
  framed transport, `xargs: sh: terminated by signal 13`) and counted the
  host among those that did not answer -- with the files sitting in the
  collection directory all the while. Stopping on purpose is now its own
  outcome, and the report says which hosts had more.

- **One unwritable local path ended the whole `dredge` run.** The write
  happens in a worker thread, where `die()`'s SystemExit does not end the
  process it was raised in: it travelled up through the pool and ended
  the run with exit 2, a usage error, printing no report and throwing
  away every other host's collection. A file that cannot be landed is now
  that host's failure and nothing else's.

- **`dredge --timeout` bounded the connection but not the transfer.**
  Waiting on the child only begins once the stream has been read to its
  end, so a far side that goes quiet mid-transfer -- what a saturated
  link produces, and the reason to have a timeout at all -- was bounded
  by nothing, and the run hung on that host for ever. A watchdog now ends
  the transfer at `--timeout`, and each host's ssh gets a session of its
  own so ending it takes anything the remote command left holding the
  connection with it.

- **`dredge` could deadlock letting go of a host.** The thread draining
  stderr is joined with a five-second timeout, precisely because the read
  might not come back; the next line closed that stream anyway, taking
  the lock the thread was still holding. Anything that outlived the
  remote command and kept the pipe open -- a daemonising remote command,
  say -- therefore hung the run at the exact point that timeout existed
  to prevent. The stream is only closed if the join actually finished.

- **`dredge --flat` could silently overwrite one file with another.**
  Folding `/` into `~` maps `a~b/c` and `a/b/c` onto the same local name,
  and the second landing on the first looks exactly like a successful
  collection. The second is refused and named now -- which is what the
  comment beside the separator already claimed happened.

- **`dredge` accepted ceilings that cannot mean anything.**
  `--max-bytes -1` reached `find` as `-size --0c`, and `--timeout 0` made
  every host time out instantly. Both are refused up front now, and 0 is
  documented as "no ceiling" for `--max-bytes`.

- **A failing assertion in the test suite could lose its own reason.**
  Nine cases across three suites end a hand-rolled check with `fail
  "why"`, on the strength of a helper that was never defined. The case
  still failed -- on 127, "command not found" -- but the sentence saying
  what had gone wrong went with it, which is exactly the moment you need
  it. `fail` is now a helper beside `_fail`.

- **The `during` counter-reset case raced its own fixture.** It rewrote
  the disk and network counters 1.2s into a 2s run and required a sample
  to straddle that; on a loaded runner the rewrite slips past the last
  sample and there is nothing to straddle. The reset now lands a second
  into a four-second run, and the `/proc/net/dev` fixture is built whole
  and moved into place rather than appended to under the sampler's nose.

- **`logtriage --since -30m` was refused by the tool that recommends it.**
  A value beginning with `-` looks like an option to argparse, so the
  relative spelling every error message in this tool suggests was
  rejected with "expected one argument" -- and `--since=-30m`, the form
  that works, is not the one anybody types. `--since`, `--until` and
  `--split-at` now glue a bare relative value to their option before
  parsing. `dredge` carries the same fix, since it takes `--since` in
  the same spellings.

- **`during` called a quiet moment a change of bottleneck.** `SHIFTED`
  means the *kind* of limit moved -- CPU-bound early, I/O-bound late --
  and its advice is to benchmark the phases separately. The fact behind it
  counted *stretches* of non-idle samples rather than distinct limits, so
  one sample dipping under every threshold in the middle of a CPU-bound
  run read as cpu -> free -> cpu, and the finding fired one line below a
  verdict that named a single bottleneck. It now counts distinct limits.

- **`netmesh` let a typo in the mesh config line reach the whole fleet.**
  The `# key=value` line above the grid is the one thing every host
  shares, and its numbers were cast where they were used -- `int()` inside
  `Agent.__init__`, which runs on each host after netmesh copies itself
  there. `size=big` therefore killed every agent with a traceback in its
  own log, leaving `status` to report a fleet that simply would not start,
  while every other malformed thing in that file gets a clean refusal.
  The numeric keys are now checked once when the mesh is read, on the box
  of whoever edited it, and `port` is range-checked like the host tokens.

- **`logtriage` applied `--since`/`--until` to some records and not
  others, silently.** A window can only be applied to a record that
  carries a time; untimestamped ones are kept, because dropping them would
  lose the stack traces and the dmesg tail that are the reason to run this
  at all. Kept silently, they outranked the records the window did apply
  to -- top of the report, marked `NEW` against a baseline they were never
  in, under a header claiming a one-second span. The report now says how
  many records the window could not be applied to. It only said anything
  before when *every* record lacked a timestamp.

- **Two netmesh tests shared a port pair with a test that needs it
  silent.** An agent is bounded by `--duration` rather than killed, so it
  can outlive the test that started it. `t_loss_columns_go_blank...`
  asserts that nothing answers on 5431 while the flow-bucket test ran a
  live agent on the same port, and the reply-TTL test shared 5450 with the
  unresolvable-peer test. Both read as flakes and were fixture
  collisions; every agent test now has its own pair.

- **`agree`, `reachable` and `netmesh` accepted a port that cannot
  exist.** All three validate the address in a host token rather than
  letting a bad one fail later as a connection error naming the wrong
  cause -- but the port was only checked for being digits, so
  `web01=10.0.0.1:99999` went through, and netmesh's `int()` took `-5` as
  well. It matters most in `reachable`, which rewrites the file it is
  given on the strength of the probe: every entry carrying that port would
  have been commented out for a reason that was never on the network. All
  three now refuse anything outside 1-65535.

- **`logtriage` split one message into several templates over an
  address.** Two lines that differ only in an IPv6 address have to reach
  the same template -- that is the whole job -- but the pattern could only
  start at a `::`, so `2001:db8::1` masked as `<NUM>:db8<IP6>` with its
  leading group falling through to the number mask. The same pattern also
  matched `12:34:56`, turning a duration the time masks had missed into an
  address. It now covers the textual forms from RFC 4291, zone included.

- **`logtriage` masked a MAC address as a timestamp.** The time masks ran
  ahead of the MAC and IPv6 masks, and `clock` is `\d{2}:\d{2}:\d{2}` --
  which the first three octets of a MAC very often look like, 08:00:27
  being VirtualBox's own prefix. `08:00:27:aa:bb:cc` templated as
  `<TS>:aa:bb:cc`, so two machines' MACs were two templates. The masks for
  whole identifiers now run first; nothing is lost the other way round,
  since a timestamp has no `::` and never eight colon-separated groups.

- **`skew` printed an unsigned offset where the sign was the finding.**
  `human_seconds` documented itself as signed and then took `abs()`, so the
  SOURCES table -- the one place the direction is not also said in words --
  rendered a source 40ms ahead and one 40ms behind identically as `40ms`.
  That column exists to show which source disagrees and which way. The
  helper now keeps the sign, and the lines that say the direction in words
  of their own (`40ms fast`, `UTC-5h`) pass the magnitude in, so it is
  stated once rather than twice.

- **`netmesh` could name a dead interface as the egress.** `default_iface`
  took the first row in `/proc/net/route` with a zero destination, though
  its own comment said it checked the flags. A downed interface keeps its
  entry and a box on two uplinks has a default per uplink, so the first row
  is not necessarily the route in use -- and the coalescing timer read off
  that interface then belonged to a card carrying nothing. Routes that are
  not UP are skipped and the lowest metric wins. A point-to-point default
  (`default dev tun0`), which carries no GATEWAY flag, still counts.

- **`during` and `why-slow` reported negative disk and network rates.**
  Both modules guard their counters against a reset -- "unknown is None,
  never a negative rate" -- everywhere except the disk and netdev deltas,
  which were subtracted inline. A device removed and re-added, or a veth
  recreated mid-run, keeps its name and starts again from zero, which put a
  negative utilisation and a negative MB/s into the series; `during`'s
  analysis then read that sample as the quietest moment of the run, and
  `why-slow` picked its busiest disk between numbers that were not
  measurements. Both now leave the pair blank, as the CPU and swap
  counters already did.

- **`muster release` handed a live lease to the next worker.** Releasing
  through a ticket refuses to touch an item held by somebody else, which is
  the one guarantee the lease exists to give. Releasing the same item by
  `--item` or from a hand-written list skipped the check entirely and freed
  it silently, because the check keyed on a lease id that a bare name does
  not carry. With no ticket the holder recorded on the row now decides.
  `reset` remains the way to put an item back regardless of who holds it,
  and the "a longer --lease is nearly always the fix" hint is no longer
  printed for a conflict that has nothing to do with lease length.

- **`resolve` printed a timeout budget that did not multiply out.** The
  budget is counted from the configured nameserver list, duplicates
  included, because that is the list the stub works down -- but the finding
  printed the count of *distinct* servers probed. A resolv.conf naming one
  server twice therefore read `timeout:5 x attempts:2 x 2 servers` beside a
  total of 30. The line now shows the count the arithmetic used and names
  the repeated line, which was the reason for the wait.

- **`reachable` reformatted an inline comment it was asked only to
  comment out.** When a range line has mixed results it is expanded to one
  line per host, and the comment carried across lost the space after its
  `#` -- so `# the slow one` came back as `#the slow one` on every host the
  range split into, a diff in a file this tool promises only to comment and
  uncomment. Everything after the first `#` is now carried over verbatim.

### Added

- **`tests/check_numeric_args.py`, so this class of bug cannot come back
  quietly.** Five bugs in one audit were the same shape: a number a user
  can type that the tool then acts on as if it made sense -- `agree
  --first 0` running the whole fleet instead of the canary, `during --
  true` exiting 1 because a successful `0` read as "no status",
  `resolve --timeout 0` announcing a DNS outage, `netmesh --interval 0`
  writing 247,637 report rows in three seconds onto every host, `muster
  --stale-lock 0` breaking a live lock. Fixing five instances does not
  stop the sixth.

  The check finds every `type=int`/`type=float` option on every parser in
  the package -- 65 of them -- and requires each to be classified: either
  the tool must refuse a zero, which it verifies by running the tool, or
  the flag is listed with the reason zero means something there. A new
  numeric flag fails the check until somebody decides which it is. It runs
  from `tests/run_tests.sh`.

- **`dredge`, an eleventh instrument: bring that file back from every
  host, kept apart.** Something is wrong on some of forty machines and
  the evidence is in a file on each of them; collecting it by hand is
  forty `scp` commands whose results all land on top of each other,
  because every one of them is called `syslog`. `dredge` runs the copy
  once, in parallel, and lands each host's files under that host's name.
  It is the gathering half of what `agree` does with commands.

  `--head N` / `--tail N` cut on the far side, so what crosses the
  network is two hundred lines and not four gigabytes -- the difference
  between a run that takes a second and one that saturates the link it
  is meant to be diagnosing. `--since` filters by modification time,
  also remotely, and goes over as an epoch second so the window means
  the same thing on a box in another timezone. `--append` / `--prepend`
  add what came back to a local copy rather than replacing it, with
  `--mark` writing a line at the seam.

  Nothing a remote host says is used as a local path: names are rebuilt
  here from the path that was asked for, so a host answering with
  `../../etc/cron.d/x` writes inside the collection directory or not at
  all. A host list naming one host twice is refused rather than
  collected twice into the same place, because the host name is the only
  thing keeping one machine's files from another's.

- **`binnacle copy TOOL` puts an instrument's file in your hand.** Every
  tool here is one standalone file precisely so it can be carried to a
  box that has never heard of this package -- and after `pip install`
  that file is under a site-packages directory nobody has memorised.
  `binnacle copy netmesh` writes `netmesh.py` here, executable, ready to
  `scp`; `--all` brings the lot, `-d DIR` puts them somewhere else.

  It lands under the name the package uses, which matters more than it
  looks: `agree script why-slow` goes looking for `why_slow.py`, so a
  copy renamed on the way out stops matching. It refuses to write over a
  file already there whose contents differ, because what is most likely
  sitting under that name is your own edited copy; `--force` says
  otherwise, and a byte-identical file already there is reported and
  left alone rather than counted as trouble.

- **`manifest --explain` says what each selector named.** A count that is
  not the one you expected is the normal way this tool goes wrong, and
  working it out used to mean re-running with `--csv` and an awk over the
  room column. `--explain` prints, per selector, the elements it actually
  named and how many were still standing after it, so the answer is read
  off rather than investigated. It goes to stderr, so the host list still
  pipes, and it is asked for outright, so `--quiet` does not silence it.

- **`manifest` reports an id that answered to more than one spelling.**
  Forgiving id matching is what makes `rack[1]` find `R01` from memory, but
  a bare number ignores the letters in front of an id entirely: `rack[1]`
  is `r01` in each hall *and* `g01` in the GPU room, and `room[1]` is
  `wr01` *and* `gpu1`. That is now said on stderr, naming the spellings
  that answered and the `rack[r01]`-or-a-path way to mean one of them.

  It stays a report rather than a refusal -- one number answering to two
  spellings is often exactly what was meant -- but quietly returning a
  second room's worth of machines is how a fan-out reaches a rack nobody
  meant to touch.

- **`netmesh --hops N` locates the queue.** `--baseline` says a pair got
  slower under load; this says *where* along the path. A queue forms in
  front of one interface and everything downstream inherits the wait, so
  knowing the path got slow does not say which buffer filled — the
  difference between "the network got slow" and "the egress queue on the
  second hop is the one to fix".

  The first `N` hops of each path are timed alongside the normal probes,
  and `summarize` reports the hop whose own delay rose between the idle
  and loaded windows as `QUEUE AT HOP`.

  **The hop named is the first riser, not the worst.** Every hop past a
  full buffer inherits its delay, so the largest riser is usually the far
  end reporting somebody else's queue. A hop that stopped answering under
  load is not listed at all: it has been shown to stop answering, not to
  be slow.

  **No root and no raw socket.** A probe sent with a small TTL dies at a
  router, which says so with an ICMP Time Exceeded; `IP_RECVERR` queues
  that to the very socket that sent it, readable with `MSG_ERRQUEUE` —
  what `tracepath` does, and what this package's no-`CAP_NET_RAW` rule
  requires. One socket carries the whole sweep, because the kernel reports
  the destination port of the offending datagram, so the TTL is encoded
  there (`33434 + ttl`) rather than held open as thirty sockets.

  **Off by default**, because the sweep is extra probes on the fabric
  being measured, and a tool that quietly adds traffic to look harder
  causes the congestion it then reports.

- **`netmesh` reports when the route moved mid-run.** An ECMP rehash or a
  route flap partway through a window means the two halves of it went over
  different physical links — and the window is still reported as one
  measurement, so its average is an average of two experiments and the p50
  belongs to neither. Nothing in the package could see it.

  Every reply carries the hop count it survived in its IP TTL, so the agent
  records it: `reply_ttl` is the hop count the interval ended on, and
  `ttl_hops` how many times it moved within that interval. A change either
  way raises `PATH CHANGED` in `summarize`.

  **A trust rule, not a performance one**, shaped like `during`'s
  `PEER_NOT_CONCURRENT`: it does not say the path got worse, it says the
  numbers above it describe more than one path. No traceroute and no root —
  just `IP_RECVTTL` on the socket.

  The TTL is read on **every** receiving socket, including each `--flows`
  bucket. Those buckets are the receive path too, and reading it off the
  main socket alone would have recorded the hop count for one probe in N
  and reported it as the path.

  Two details that had to be measured rather than assumed: Python exposes
  no `IP_RECVTTL` constant on Linux, and the option you set (12) is not the
  type the kernel tags the ancillary data with (`IP_TTL`, 2). The payload
  is a native int on Linux and a single byte elsewhere; both are read,
  because guessing wrong yields a plausible hop count rather than an error.
  Where the platform lacks it, both columns are blank and the rule skips —
  blank means *not measured*, which is not *the route held*.

### Fixed

- **A Release whose tag disagrees with the version is refused before
  anything is built.** A Release builds from its tag, so the tag is a claim
  about what is in the tree — and the two can disagree. Tagging `v0.7.0` on
  a tree that still says 0.6.0 would publish 0.6.0 under a v0.7.0 release,
  or collide with the 0.6.0 already on PyPI; neither failure says what
  actually went wrong. `publish.yml` now compares the tag against
  `pyproject.toml` as its first step on a release-triggered run, so the run
  fails on the mismatch itself. A manual dispatch has no tag and skips the
  check.

- **Running the publish workflow twice is no longer a failure.** The upload
  now passes `skip-existing: true`, so a file PyPI already holds is skipped
  rather than ending the run red.

  PyPI never lets a filename be reused, so a second run against a version
  that is already up could only ever fail — and it is an easy run to start
  by accident, because the version comes from the tree rather than from the
  dispatch form, so nothing about starting the run says which version it is
  about to upload. Both failures this workflow has had were exactly that.

  It does not hide a real mistake: bumping the version is what makes an
  upload new, an unbumped one has nothing to publish, and every file being
  skipped is the visible tell that the bump was missed.

  `PUBLISHING.md` gains a section on this, with two things worth knowing
  when reading a publish log: a rebuilt wheel is not byte-identical to the
  published one even from the same commit, because wheels embed file
  timestamps; and a dispatch builds from the branch while a Release builds
  from the tag, which agree only while the tag is the branch head.

## [0.6.0] - 2026-08-28

### Added

- **`manifest --sample` prints a layout to start from.** The tool reads a
  `.dc` file, which is not much help the first time, when you have no `.dc`
  file and the format is a paragraph of prose in the help text. `--sample`
  writes a commented layout that uses every construct the parser
  understands — nested rooms, rows and racks, `[01..04]` ranges and an
  `[01..08x2]` step, inherited attributes and tags, `name={room}{rack}{id}`
  placeholders, and `net`/`link` cabling lines that are read past — so it
  is a worked example of the format and a file to edit into your own floor.

  It takes no layout, since having none is the reason to reach for it, and
  writes only the file: the summary stays on stderr where `> floor.dc`
  cannot catch it. The sample states its own totals (328 servers, 18
  switches) and the suite asserts the tool agrees with them, so a sample
  that stopped parsing, or quietly changed shape, fails CI rather than
  teaching the format wrong.

## [0.5.0] - 2026-08-28

### Added

- **`manifest` — which servers are those, in the layout?** A fleet list is
  a datacenter fact — these racks, in that room, hold these machines — but
  it is almost always kept as a text file somebody maintains by hand, so it
  is always slightly wrong. The machines decommissioned last quarter are
  still in it, the row added in March is not, and nobody finds out until a
  fan-out quietly skips a rack.

  The building already knows, and if that description exists as a file then
  the host list is a *query* against it rather than a second copy that has
  to be kept in step. `manifest floor.dc 'rack[1-3]'` prints the servers in
  those racks, one per line, which is already the input to `muster add -`,
  `agree --hosts` or `netmesh gen --servers -`.

  It reads the `.dc` format from the layout_visualizer project: indentation
  nests, `room`/`row`/`rack`/`node` with `[01..20]` ranges, `key=value`
  attributes and `+tag`s that inherit downward, and `name={room}{rack}{id}`
  placeholders that give every machine a flat hostname. `net` and `link`
  lines are cabling rather than machines and are read past.

  **A selector names elements; the answer is the machines at or under
  them.** That single rule is what makes `rack[1-3]` mean what a reader
  expects: a rack is not a server, so matching the rack elements and then
  filtering for `role=server` would leave nothing at all. Each machine is
  tested against the selector and against every ancestor it has, so naming
  a container names its contents — and `room[1]`, `row[A,C]`, `+gpu`,
  `model=hgx*`, a path, a glob and a bare hostname all work the same way,
  with `!` to negate. Arguments AND together, commas inside one OR.

  Ids are matched forgivingly, because a rack called `R01` is the rack you
  mean when you type `rack[1]` and having to remember the zero padding
  would make the tool useless from memory. Both range spellings are
  accepted — `rack[1-3]` as the rest of this package writes one, `rack[1..3]`
  as the layout file does — as is a plural kind, so `racks[1-3]` is the same
  question.

  `--role` decides what counts as a server (default `server`, `--role any`
  turns it off); a layout that sets no `role=` anywhere gets its leaf nodes
  and is told so, rather than silently including the switches. `--csv`
  adds where each machine is, `--path` prints paths, `--count` prints the
  number. It reads and prints: nothing is contacted, nothing is written,
  and the layout file is never modified.

- **`muster` — hand out work from a pool, once each, and know what is
  left.** Some jobs are a list and a promise: forty hosts to patch, nine
  hundred files to re-encode, every switch in a rack to walk up to. The
  work goes to whoever is free, it must happen once each, and the thing
  that actually goes wrong is never the work — it is the bookkeeping. Two
  people take the same host. A laptop shuts and eleven items are held by
  nobody, forever. At the end nobody can say which twelve of the forty are
  left, so the whole list gets re-walked to be sure.

  `muster` is that bookkeeping and nothing else. It does not do the work,
  does not know what the work is, and never touches the items — they are
  strings to it. `add` fills a pool (a file, stdin, or a `web[01-40]`
  range, expanded the way `agree` and `reachable` already expand one),
  `take` leases items and writes a ticket, `done` closes them, `status`
  says what is left.

  **The lease is the whole idea, and nothing runs to enforce it.** `take`
  marks items held until a deadline; if you never finish, the lease simply
  runs out and the items are available again. An expired lease is not a
  lease, worked out from the timestamps as the pool is opened — so there is
  no daemon, no reaper and no cleanup step, and the only thing left behind
  is the pool file you named. That is what keeps a stateful tool inside
  this package's stateless rule.

  **Finishing late is accepted and reported, not swallowed or refused.**
  If your lease lapsed and someone else now holds the item, `done` still
  marks it complete — the work happened, and refusing it would send the
  pool out to have it done twice more — but prints a `CONFLICT` naming both
  holders, because two people on one host is precisely what the lease
  existed to prevent. A lapse with nobody else holding it is a quieter
  `LATE`. Either makes the exit status 1.

  **`attempts` is what makes a bad item visible.** An item taken four times
  and finished none of them is not a scheduling problem, it is a host
  nobody can work on, and it would absorb the pool forever; `status` lists
  it under `STUCK` and exits 1, which makes the command a cron check.

  The completed share leads the report and is rounded honestly: 399 of 400
  prints as `>99%` rather than `100%`, and 1 of 400 as `<1%` rather than
  `0%`, because either would report the job finished, or never started,
  when neither is true — and that number is the one that gets pasted into a
  status mail. `status --csv` gives the same figures as one row for
  something that will do arithmetic on them.

  **A pool is meant to be shared between machines.** Locking is an
  `O_EXCL` sentinel beside the pool rather than `flock`, since flock over
  NFS is not dependable while `O_EXCL` create is the one primitive NFS has
  always had to get right; every write is a tempfile renamed into place, so
  a reader never sees half a pool; and a lock whose holder died is broken
  after `--stale-lock` rather than blocking the fleet. Twelve concurrent
  takes over one pool were verified to hand out 120 items with no overlap.
  Leases are wall-clock deadlines, so workers have to roughly agree about
  the time — which is what `skew` is for, and the docs say so.

  Forty-two suite cases cover it, including the concurrency, the expiry,
  both late-completion paths, stale-lock breaking, and the rounding.

  Eight bugs found by going at the edges of it after it was written, each
  with a case that fails against the code before the fix:

  - **A pool error stranded the lock.** The caller's `finally` is not in
    force until `_open` has returned, so anything that failed while
    reading the pool left the lock file behind — and every later run then
    waited out `--stale-lock` before it could do anything. On a shared
    pool that is the whole fleet stopped by one bad row. `_open` now
    hands the lock back itself on any failure.
  - **A ticket that could not be written left the items leased.** The pool
    is written before the ticket, so a full disk or a typo'd `-o` left
    items held by somebody with no record of which ones — unreachable for
    the whole lease. They are handed straight back now: a lease nobody can
    read is worse than no lease.
  - **A `#` inside an item name truncated it.** Comments were stripped by
    splitting on a bare `#`, so `file#1` and `file#2` both became `file`:
    one item where there were two, and the other silently never worked
    on. A comment now has to open the line or follow whitespace.
  - **A held row carrying no deadline was held forever**, since expiry
    needs a deadline to compare against — and `status` printed the epoch
    as a countdown, `next expires in -20692d23h`. Such a row is not a
    lease either, and is now reclaimed.
  - **Two rows for one item were counted as two items.** `add` cannot
    make these, but a hand edit or a merge can; the rows disagree about
    state, every count is wrong, and only one would ever be updated
    again. Refused, naming the item.
  - **`--pool` and the other shared flags only worked after the verb**, so
    `muster --pool p.csv status` — which is what people type — failed with
    argparse blaming the verb for an invalid choice. Accepted in both
    places now, with the later one winning.
  - **A malformed duration raised a traceback.** `.` and `1.2.3` are runs
    of digits and dots that are not numbers; both reached `float()` and
    came back as a bare `ValueError` where a one-line refusal belongs.
  - **`add somedir` added the directory as an item.** A directory is not a
    list of items and is now refused, like a mistyped file path already
    was.
  - **Every lease was up to a second shorter than it was asked for.** The
    deadline was `int(now + lease)`, which threw the fraction away — a 1s
    lease measured 0.93s, and a take could come back already expired.
    Short is the dangerous direction: a lease that ends early hands the
    item to somebody else while the first worker is still on it, which is
    the duplicate work this exists to prevent. Rounded up now, so a lease
    is never shorter than asked and at most a second longer. Found by the
    3.6 container, where the margin a fast machine hid was gone.


- **`binnacle` — one command that says what is installed and prints every
  tool's help.** Until now the package put eight commands on your PATH and
  nothing that named them. `pip install binnacle` gave you `binnacle` as a
  distribution name that was not a command, so the obvious first thing to
  type did nothing, and finding out what you had meant reading the README
  or listing a `site-packages` directory. Recovering the eight names was a
  precondition for using any of them.

  `binnacle` is the housing rather than a ninth instrument: it lists what
  is beside it, and `binnacle help` concatenates every tool's `--help`,
  verbs included, so one page (~1,400 lines) is the whole manual for the
  package *as installed* rather than as documented somewhere else.
  `binnacle help TOOL` narrows it to one, accepting `why-slow`, `why_slow`
  or `why_slow.py` since a reader who has only seen the file should not
  have to guess the command.

  The version column is per-tool on purpose, and that is the part worth
  reading twice. Each file carries its own `VERSION` because each is
  routinely copied to a machine that has never heard of this package, and
  `agree` groups a fleet by what `--version` reports — so one module left
  behind at an older number reads as version skew across the whole fleet
  rather than as a bad install on one box, and the hunt starts in the
  wrong place. Every instrument is therefore asked separately, a
  disagreement prints as `SKEW` naming the odd file, an unimportable file
  prints as `BROKEN` without costing the other seven their row, and either
  makes the exit status 1. `binnacle >/dev/null` is a usable post-install
  check, and `PUBLISHING.md` now uses it as the fastest proof that a
  version bump reached all eleven places.

  It runs nothing to answer: formatting a tool's help builds its argparse
  parser, which does not execute the tool, touch the network or read
  `/proc`. `--paths` names the file each instrument was loaded from, for
  when two installs shadow each other. This is the one module allowed to
  read its siblings — it is not an instrument, its whole subject is which
  instruments are present, and copying it somewhere on its own would be
  meaningless — and it still does not `import binnacle`: it loads each
  file from the directory it lives in, so it reports the tools actually
  beside it rather than whichever ones a `sys.path` search found first.

### Changed

- **`muster` takes a list of items however it is delimited.** A newline, a
  space and a comma all separate one item from the next, so `muster add
  hosts.txt`, `muster add 'web01 web02'` and `muster add web01,web02` are
  the same command, and `--item` accepts a list as well. That is possible
  because an item name now never contains whitespace or a comma — they are
  the delimiters, which is also what makes a ticket unambiguously one item
  per line. Commas are split outside brackets only, using `agree`'s own
  splitter, so `node[1,3,5]` is still one range; a space *inside* a range
  is refused rather than half-expanded, since `web[01-04, 06]` would
  otherwise leave a literal item called `web[01-04,` behind.

### Fixed

- **No path argument may be the pool.** Every verb takes a filename next
  to `--pool`, so naming the pool by mistake is one keystroke away — and
  each way of doing it went wrong differently and quietly:

  - `take -o POOL` overwrote the pool with the ticket while reporting
    `took 5 item(s)`, taking every item and every completion with it,
    unrecoverably.
  - `take -o POOL.lock` left the lock holding ticket text, so it was
    never released and the pool was jammed until `--stale-lock` ran out.
  - `add POOL` read the pool's own schema back in as work: `item`,
    `state`, `holder`, `lease_id` became items to be handed out.
  - `done POOL` **completed the entire pool at once** — every row begins
    with its item name, and a comma is a delimiter — under a wall of
    `UNKNOWN` lines for the header fields.

  All are refused now, before anything can happen. Found by soaking the
  tool rather than by a report; the first two were fixed on their own
  first, which left the other two, so the guard is on the class rather
  than on `-o`.

- **`netmesh` reported 100% loss against a peer that answers from a
  different address than the mesh names.** The symptom is unmistakable
  once you know it: forward loss near zero and total loss at 100% on the
  same pair, on a link that is carrying every packet in both directions.
  `summarize` printed it as `loss 100.00% (fwd 1.2%)`, and the LOSS line
  split it as almost all "on the way back".

  Replies were attributed by matching the reply's source address against
  the address the mesh gave for that peer. A peer does not get to choose
  that address: it answers from whichever of its own the return route
  selects, and on a multi-homed box — or one whose name resolves to a
  different interface than the route back picks — that is not the address
  we probed. Every reply then matched no peer and was dropped, so `recv`
  stayed at zero while `sent` climbed.

  What made it read as a network fault rather than a bug is that the
  *other* direction was accounted correctly: the receiver's own `rx`
  count is attributed by mesh index, not by address, so it showed every
  probe arriving. Forward loss is computed from that count, so the pair
  reported "your packets arrive, their answers do not" — which is a real
  network failure mode, and sends you looking at return-path routing on a
  fabric that is fine.

  The reply now carries the responder's own mesh index instead of echoing
  the requester's back, which only ever said who the reply was *for* —
  something the receiver already knew — and left the source address as
  the sole clue to who it was *from*. Attribution keys on that index; the
  address match remains as a fallback so a reply from an older agent is
  still counted, and since that reply echoes our own index it can never
  name one of our peers, nothing is misattributed.

  The path-MTU echo matched on the source address the same way, so the
  same peer never converged: every size read as a black hole and the pair
  reported no path MTU at all. It is now resolved by index too. Both are
  covered by suite cases that fail against the previous code.

- **The test box's own hardware clock leaked into skew's reports.** The
  skew suite's `sk()` helper ran the tool with no `--rtc-path`, so every
  live-socket test read the real `/sys/class/rtc` of whatever machine the
  suite was on. A CI runner whose hardware clock was ten minutes out then
  failed *a reply not echoing the query* — a test that injects a 600s NTP
  offset and asserts the report never says `10m`. The offset was rejected
  correctly; the `10m 08s` in the report was the runner's own RTC, in the
  CLOCK section and in an `RTC_DRIFT` warning. Exactly one of five matrix
  jobs failed, because each gets its own VM. The helper now defaults the
  hardware clock to a path that does not exist, so the box underneath
  cannot appear in a report the tests assert about; the two tests that
  mean to exercise the RTC pass their own `--rtc-path` and win.

- **CI shipped three console scripts it never ran.** The wheel smoke-test
  in both `ci.yml` and `publish.yml` drove five of the eight entry points,
  so `resolve`, `during` and `skew` could have been broken on PATH by a
  packaging change and gone out anyway — `PUBLISHING.md`'s own checklist
  says every one of them. Both workflows now loop over all nine and then
  run `binnacle`, which additionally fails the build if the versions
  disagree.

## [0.4.0] - 2026-08-27

### Fixed

- **`netmesh` no longer reports 100% loss when the mesh holds hostnames.**
  A bare host token is its own address, and nothing ever resolved it: the
  probes went out (`sendto` resolves per packet) and the echoes came back,
  but replies are attributed by comparing the mesh's address string
  against the reply's source address -- and `recvfrom` only ever reports
  numbers, so a name matched no reply and every pair read as 100% loss
  while the network carried every packet. Any run generated from names
  rather than IPs (`netmesh check web01 db01`, or a `--servers` file of
  names) was affected; the `name=10.0.0.11` form from the docs was not,
  which is how it hid.

  The agent now resolves each peer's address once at start and uses the
  numeric form for both sending and attribution -- which also takes a DNS
  lookup per probe out of the send path. A name that does not resolve on
  the agent's box becomes a visible per-row note (`cannot resolve ...`)
  with nothing sent, rather than silence or invented loss. Reproduced
  with two loopback agents on a `localhost` mesh (100% loss before, clean
  after) and covered by two new suite cases.

- **`summarize` no longer dies when two pairs are equally asymmetric.**
  The ASYMMETRY list was sorted on `(delta, PairStat, PairStat)` tuples
  directly. Where two pairs tied on delta the comparison fell through to
  element 1, met two `PairStat` objects that define no ordering, and raised
  `TypeError: '<' not supported between instances of 'PairStat' and
  'PairStat'` -- killing a run whose measurements were already complete and
  correct. A tie is not exotic: the p50s are rounded to whole microseconds
  before they get here.

  Now sorted by an explicit key, ties broken on the slow leg's names so the
  order is stable between runs as well. An AST sweep over all eight modules
  confirmed this was the only bare sort over a list of tuples carrying
  objects.

- **`pip install binnacle` works again on the Pythons it advertises.**
  `pyproject.toml` declared PEP 639's `license = "GPL-3.0-or-later"` SPDX
  string, which requires `setuptools>=77` to build -- and setuptools 77
  requires Python >=3.9. A package claiming `requires-python = ">=3.6"` was
  therefore impossible to build from its own sdist on most of the range it
  claimed: pip builds an sdist in an *isolated* environment and installs the
  build requirements there from the index, so whatever setuptools is already
  on the machine is irrelevant, and where no version at or above 77 can be
  installed pip stops with `Cannot install setuptools>=77.0`.

  The `license` table and the license classifier are back, and the build
  floor is `setuptools>=64`. Verified by reproducing the failure against the
  published 0.3.0 sdist under a `setuptools<77` constraint and watching the
  same install succeed with this change.

  Only the source path was affected. The wheel was always fine: its
  `Metadata-Version: 2.4` installs on pip as old as 21.3.1, which was
  checked rather than assumed. The metadata version follows the setuptools
  doing the building, not the licence form, so no cap was added.

### Added

- **`netmesh` can take more than one path through the fabric.** Every probe
  used a single source port, so it presented one 5-tuple and took one path
  through any LAG or ECMP bundle: a sick member was hit or missed by luck,
  and which one was luck too. That is the classic fault that will not
  reproduce -- some flows slow, most fine, every retest disagreeing with the
  last, and the member counters looking healthy because the other members
  are. Nothing in the package could see it.

  `--flows N` sweeps the source port across N buckets and keeps their
  results apart, adding a `PATH SPREAD` section and a finding when one
  bucket sits at `--flow-factor` times the median bucket's p50 (default
  3x -- two buckets of the same healthy loopback came out 1.9x apart on a
  busy machine, because a bucket's p50 rests on a fraction of the samples
  a pair's does, so 2x would have reported that noise), or drops on its
  own. The reference is the median bucket rather than
  the fastest, because with one sick member out of eight the median is a
  healthy one.

  The buckets take turns within the configured rate rather than each
  running at it: a measurement tool that multiplied its own load eightfold
  when asked to look harder would cause the congestion it then reported.
  The cost lands on per-bucket sample count instead, which is why the
  feature is opt-in and why a bucket under 30 replies is reported as thin
  rather than ranked -- saying nothing would read as "the flows agree".

  The buckets are one socket each on the agent, shared across every peer,
  since the destination address already varies the rest of the tuple. The
  ports are ephemeral and so differ between runs, which is the honest
  behaviour: what is sick is a member of the bundle, not a port number.

  It composes with `--baseline`. A member slow both idle and loaded is
  faulty; one slow only under load is carrying more than its share of an
  uneven hash. They are reported as different findings.

  The report gains one appended column, `flow`, blank on every row except
  the per-port breakdown. A row carrying one is part of the aggregate row
  above it and never an addition to it, and `summarize` keys on the blank
  to avoid counting both.

- **`agree script` takes a tool name, not just a path.** The documented
  fleet-triage command began `agree script ./why_slow.py`, which assumes
  the caller knows where why_slow.py is -- and after `pip install
  binnacle` nobody does: the sources live in whichever `site-packages`
  directory pip chose. A bare name (no directory part) is now also looked
  up among the tools installed alongside `agree` itself, hyphen or
  underscore, `.py` optional, so `agree script why-slow --hosts prod.txt
  --fleet-csv -- --csv` works from any install. An explicit path still
  wins, and a name matching neither a file nor a bundled tool fails
  naming the tools that would have matched.

- **`why-slow --ssh HOST` diagnoses a remote box.** The file is fed to
  `python3 -` over ssh, so nothing is installed on the host, nothing is
  copied to it and nothing is left behind -- it works on a machine that
  has never heard of binnacle. The remote exit code is passed through,
  keeping `--exit-code`'s 0/10/20 contract across the hop, and ssh's own
  255 is called out as "the box was never diagnosed" rather than left to
  read like a clean report. `--ssh-cmd` swaps the transport
  (`WHY_SLOW_SSH`). Two contradictions are refused rather than resolved
  silently: `--no-exec` (the hop is a subprocess), and `--csv PATH` /
  `--json PATH` (the file would be written on the remote box; use the
  bare form and redirect). One host only, deliberately -- fanning out and
  grouping the answers is `agree`'s job.

## [0.3.0] - 2026-08-17

### Added

- **`netmesh` says when its own number is the card's timer.** Receive
  interrupt coalescing is the one setting that can make this tool's answer
  wrong with nothing looking wrong: a card told to wait 200 µs before
  raising an interrupt cannot report a round trip faster than that, so a
  p50 near the timer is a measurement of the timer rather than of the path.
  The agent reads `rx-usecs` once at start — it is a setting, not a
  measurement — and records it on its own `host` row; `summarize` raises a
  MEASUREMENT note when it is at least half the fastest measured p50. That
  is the *say what was done to the data* convention turned on the
  instrument itself, next to `agree` disclosing its normalizations and
  ping-only rows being marked as such. There is no sysfs for the setting,
  so `ethtool` is needed; where it is absent the value is blank and the
  note is absent, rather than being read as "coalescing is off".

  The report gains one appended column, `rx_usecs`. The header is pinned by
  the suite as an interface, and that assertion is what caught the change.

- **`during --expect-mbps`: traffic on the link that was not yours.** The
  CPU equivalent has always been checked — `INTRUDER`, something else
  running inside the window makes the result a measurement of both. The
  link never was, and it is the easier one to miss: a backup or a
  replication stream through the same interface leaves no trace in the
  benchmark's own output and looks exactly like your own traffic in every
  whole-interface number. Tell it what the test should have been pushing
  and the interface total is checked against it. Like `--rtt-ms`, the fact
  arrives from outside because `during` cannot know it, and without the
  flag the rule skips and names it rather than guessing. A quarter over is
  INFO, double is WARN, and being a trust rule it leads the verdict.

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

[Unreleased]: https://github.com/MartinGallagher-code/binnacle/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/MartinGallagher-code/binnacle/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MartinGallagher-code/binnacle/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MartinGallagher-code/binnacle/releases/tag/v0.1.0
