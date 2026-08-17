# Conventions

The rules every tool here follows. Each is load-bearing: they are written
down because breaking one produces a tool that is confidently wrong, which
is worse than no tool.

## A missing measurement is `None`, never `0`

A fact that could not be measured is absent, and whatever needed it is
reported as **skipped with the reason**:

```text
  skipped   oom kills          kernel log unreadable -- rerun with sudo
  skipped   tasks waiting      no /proc/pressure -- kernel < 4.20
```

A tool that quietly checks less when run without root is worse than one that
says so, because a clean report you cannot trust is worse than no report at
all.

## Blank means not measured

The same rule in the data. A `netmesh` pair whose replies dried up writes no
RTT at all rather than a zero, and averages skip blanks rather than being
flattered by them. Zero is a measurement; blank is the absence of one.

## Causes outrank symptoms

A swapping box also looks CPU-busy. A box with dying disks also looks
I/O-bound. Reporting the symptom over the cause is how an hour gets spent
tuning the wrong thing, so `why-slow` carries an explicit precedence table
that pulls causes to the front of the verdict even when the symptom's number
is larger.

## Failures are findings

An unreachable host is a group in the report, not a line on stderr you can
scroll past. `agree` gives failed, timed-out and unreachable hosts their own
groups; `reachable` records *why* each host failed in the file itself.

> "3 hosts never answered" is usually the finding. A tool that prints those
> to stderr and moves on lets you believe you checked them.

## Say what was done to the data

`agree` prints which normalizations were active on every run, because
"47 agree" means nothing if you masked every number to get there — and it
says so outright when masking produced the agreement.

`logtriage` prints the score components, so you can see *why* a template is
ranked first. `netmesh` marks ping-only rows as `probe=ping` and segregates
them, because they carry no receiver-side truth.

## Diagnose, don't dump

Every tool ends with a **WHAT TO DO NEXT** section that reads the numbers it
just collected, names the mechanism, and gives an exact command. A clean run
says so plainly — no output reads like breakage.

## Deterministic output

Fan-out results are replayed in host-list order, never completion order.
Template ids are hashes of content. Rules evaluate in a fixed order. Two runs
over the same input produce byte-identical output, so you can diff
yesterday's against today's — and so a fleet's results can be grouped at all.

## Every knob has a flag and an environment variable

So a setting can live in a profile or a unit file instead of on every
command line. See the [CLI reference](cli.md) for the full table.

## Tidy CSV, kept separate from rendering

`--csv` emits one row per finding, host or template, with lowercase
snake_case headers and no formatting. None of these tools draw anything. The
CSV is the artifact; the human report is a view of it.

## Standard library only, no imports between modules

Each tool is a complete standalone program. This is not tidiness — it is a
requirement: `netmesh` copies itself to every host in the mesh, and
`agree script ./why_slow.py` pushes a tool to a fleet. A relative import
would break the moment it landed on a machine that has never heard of
`binnacle`.

The consequence is deliberate duplication: `agree` holds a copy of
`logtriage`'s timestamp-mask table, and `reachable` a copy of `agree`'s
range expander and address parser. Each copy carries a comment naming the
canonical one. That is the accepted cost of files that can be scp'd.

The cost has to be paid, not just accepted. Copies drift — that is the
entire reason `shared_tools` exists, and its README says so: *"the two
copies that once existed drifted apart"*. So the suite enforces it. Every
pair declared **verbatim** is compared as code with docstrings stripped, so
prose can be local to each tool while behaviour cannot diverge; a fix
applied upstream and not mirrored fails the run and names the function and
both modules. Every `canonical copy:` pointer is checked to name a file
that exists, because this package moved from `scripts/` to `binnacle/`
once already and one pointer did not follow.

Not every copy is verbatim, and the difference is written down where it
matters. `during` samples every second for minutes, so five of the `/proc`
readers it takes from `why-slow` are deliberately **trimmed** — keeping
only the fields a sample row needs, aggregating where `why-slow` keeps the
parts separate. Its provenance comment names both sets, so a parser fix
upstream is not pasted into a reader that was never the same function.

## Stateless

No daemon, no dotdir, nothing left running and nothing left behind. State is
re-derived by probing. The two exceptions are explicit and named by you:
`logtriage --save-templates` and `reachable`'s edit of the file you passed
it.

## Nothing is destructive without saying so

`reachable` is the only tool that writes to anything you did not ask for by
name, and it defaults to stdout. `agree` carries a mutation guard for
obviously destructive commands, refuses on a non-tty rather than prompting,
and is honest that it is a typo guard rather than security.

## The Python 3.6 floor

Enforced in CI under a real 3.6 interpreter, plus `vermin` for stdlib APIs
newer than the floor that a syntax check cannot see. It is load-bearing
because these files run on whatever `python3` the fleet has, and RHEL 8
ships 3.6.

## Name the codec — a byte is never worth the run

The boxes these tools land on often run `LANG=C`, where Python's default
encoding is ASCII, and the files they read were edited wherever the person
happened to be — including by Notepad and Excel, which prepend a UTF-8
byte-order mark. Neither may change a tool's answer, so every I/O boundary
names its codec instead of taking what the locale offers:

* **Reads decode `utf-8-sig` with `errors="replace"`** — byte-identical to
  UTF-8 unless the file opens with a BOM, in which case the mark is decoded
  away instead of glued to the first token; an undecodable byte costs one
  character, never the finding it appeared in. Subprocess output is decoded
  the same way, and the stdin paths strip a leading BOM themselves.
* **Writes encode plain UTF-8** — a UTF-8 read round-trips, so a comment in
  a file `reachable` rewrites comes back as the bytes it arrived as, and
  nothing here ever emits a BOM.
* **`_stdio_safe()` runs first in every `main`** — when the locale claims
  ASCII, stdout and stderr are rewrapped with `backslashreplace` so a
  report is never lost to the terminal printing it. It is one of the
  verbatim-duplicated helpers below, so the eight copies are held identical.

The failure this prevents is not hypothetical or even a crash: before the
rule, a BOM made `resolve` read a healthy box's `resolv.conf` as empty and
report **no resolvers, CRITICAL** — a missing measurement rendered as a
finding, the exact thing the first convention on this page forbids. To
reproduce that class locally you must defeat PEP 538 with
`PYTHONCOERCECLOCALE=0 PYTHONUTF8=0` as well as setting `LC_ALL=C`, because
modern Python quietly coerces the C locale to UTF-8 — which is why none of
it surfaces on a developer machine and all of it surfaces on the fleet.

## Testing

```bash
bash tests/run_tests.sh              # all nine suites, 214 checks
bash tests/run_tests.sh agree        # one suite
```

**No network and no second machine.** `ssh` and `scp` are replaced by a shim
that runs the "remote" command locally in a sandbox directory per fake host,
logging every invocation so a test can assert *what was invoked* as well as
what came back. The only real packets are on loopback: `netmesh`'s own
agents, and a small DNS responder that `resolve` is pointed at — one that
builds its replies independently of the tool's own packet code, so a shared
bug cannot cancel itself out.

`why-slow`'s rules are pure functions of a fact dictionary, so they are
driven from JSON fixtures with no machine in the state being diagnosed —
including boundary cases either side of every threshold. `--proc-root` and
`--sys-root` point the fact layer at a synthetic tree.

Writing this suite found seven real bugs, three of which looked fine when
run by hand: CRLF line endings in a CSV, an IPv6 token mis-parsed into a
nonsense address rather than refused, and an agent that discarded everything
measured since the last interval when told to stop. Extending it has kept
finding them — the [changelog](changelog.md)'s *Fixed* sections carry the
running tally — and the pattern has held throughout: degenerate inputs,
long runs, signals and hostile locales find bugs; reading the code finds
far fewer. Almost everything above exists because executing something
proved a documented claim false.

## Documentation cannot drift

Each tool's `--help` prints its own module docstring, and the
[CLI reference](cli.md) is generated from the live argparse parsers at build
time. A flag cannot exist without being documented, nor linger in the docs
after it is removed.
