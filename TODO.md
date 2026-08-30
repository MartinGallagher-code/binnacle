# What to do next

Refreshed 2026-08-30, after 0.6.0 was published. Everything here is enough
to pick the work up cold.

## Where things stand

**0.6.0 is published** — on PyPI and installable, with all ten instruments
and the `binnacle` index answering `0.6.0` from the wheel. `main` is at
**`ff40a91`**.

Since the previous handover (written against 0.3.0) three releases have
shipped and two tools have been added:

- **0.4.0** — the `binnacle` index command, and a real `netmesh` fix: a peer
  answering from a different address than the one it was probed at had every
  reply discarded, reported as 100% loss in one direction on a healthy
  network.
- **0.5.0** — `muster` (hand out work from a pool, once each, under a lease)
  and `manifest` (turn a datacenter layout into a server list).
- **0.6.0** — `manifest --sample`.

**351 checks across twelve suites** (was 234 across nine). The verification
gate in section 4 is what was actually run before each of those releases,
not a description of what ought to be run.

No open pull requests, no open issues, no stale branches.

---

## 1. `netmesh` 2b: path change mid-run, via reply TTL — **next**

**The fault:** an ECMP rehash or route flap partway through a window means
the two halves measured **different physical paths**, so averaging them
averages two experiments. This is a **trust** rule, shaped exactly like
`PEER_NOT_CONCURRENT` in `during`.

**Shape:** record the IP TTL of each reply, per interval. A hop-count change
means the route moved. No traceroute and no root.

**Where to work:** the agent's receive path. This needs `IP_RECVTTL` plus
`recvmsg()` to get the ancillary data — the fiddliest part of what is left.
Then one appended column on the `tx` row, and a rule in `summarize`.

**Notes:**
- Slots straight into the idle/loaded split: a path change *at* the split is
  a different story from one mid-window.
- Where `IP_RECVTTL` is unavailable the column is blank and the rule skips —
  blank means not measured, as everywhere else.
- **The flow sockets are the receive path now too.** `--flows N` gave each
  bucket its own socket, and `recvmsg()` has to be wired into every one of
  them, not just `self.sock`, or the TTL is recorded for an
  unrepresentative slice of the probes. This is the one place where the
  flow-hash work made this item bigger rather than smaller.
- Nothing has been started: there is no `IP_RECVTTL` or `recvmsg` anywhere
  in `netmesh.py` today.

## 2. `netmesh` 2c: locate the queue

**The fault:** we know *that* latency rose under load, and (from the
flow-hash work) *which path*; we still do not know *where* along it. The
difference between "the network got slow" and "the queue on the second hop
is the one to fix".

**Shape:** probe with increasing TTL during the loaded window; the hop where
the latency delta appears is the buffer that filled.

**Where to work:** a new probe mode in the agent, plus reporting. Largest of
the three, and the natural sequel to `under_load()` and to 2b.

---

## 3. Release mechanics that still need a person

Two things about publishing are worth knowing before the next release,
because neither is visible from the repository alone.

**No GitHub Release has ever been created, for any version.** All three
successful publishes were manual `workflow_dispatch` runs — they put 0.3.0,
0.4.0 and 0.6.0 on PyPI, which is why the index skips 0.5.0. The
`release: [published]` trigger has therefore never fired, and `publish.yml`
now checks the tag against `pyproject.toml` before building so that the
first time it does fire, a tag that disagrees with the tree fails on the
mismatch rather than on its consequence.

**Only `v0.5.0` and `v0.6.0` are tagged.** `v0.1.0` through `v0.4.0` were
released and published without tags, so the compare links at the foot of
`CHANGELOG.md` point at tags that do not exist. Each version resolves
cleanly to the merge commit on `main` that carries it, verified by reading
`pyproject.toml` at that commit:

```bash
git tag -a v0.1.0 -m "binnacle 0.1.0" 1b55dec
git tag -a v0.2.0 -m "binnacle 0.2.0" f6f5e42
git tag -a v0.2.1 -m "binnacle 0.2.1" 8a256e7
git tag -a v0.3.0 -m "binnacle 0.3.0" eb9e210
git tag -a v0.4.0 -m "binnacle 0.4.0" 033a122
git push origin v0.1.0 v0.2.0 v0.2.1 v0.3.0 v0.4.0
```

That has to be run by a person: **the sandbox cannot push tags.** A branch
push succeeds and a `refs/tags/*` push is refused with HTTP 403, so any
tagging step in a handover like this one is the human's.

---

## 4. Things learned the hard way

Worth reading before touching the suite — each of these cost a red run.

- **The test box's own state can leak into a report the suite asserts
  about.** `test_skew.sh` ran the tool with no `--rtc-path`, so every
  live-socket case read the real `/sys/class/rtc` of whatever machine the
  suite was on. A CI runner whose hardware clock was ten minutes out failed
  a case that injects a 600s offset and asserts the report never says
  `10m` — the rejection worked, and the `10m 08s` was the runner's own RTC.
  Exactly one of five matrix jobs failed, because each gets its own VM.
  When a case asserts on a report, pin every input the report can read.
- **Assertions must use short fragments.** Reports wrap at **76 columns**, so
  any `assert_contains` string longer than a line will straddle a break and
  never match. This bit four separate cases. Assert on a distinctive short
  phrase, not a sentence. `_wrap_text` breaks between words and never inside
  one, so a single distinctive token is always safe.
- **`assert_contains` is a substring test, not a regex.** `\|` alternation
  matches literally.
- **A test that exercises a nonzero exit needs `set +e` around it.**
  `run_test` runs the body under `set -e`, so `out="$(cmd)"` on a command
  that exits 1 kills the case before its assertion runs — and the failure
  prints an empty message, which looks like a harness bug rather than a
  test that never ran.
- **`fail` does not exist in the harness — it is `_fail`.** `test_compose.sh`
  uses `fail` and gets away with it only because a missing command also
  returns non-zero under `set -e`.
- **`netmesh` has no `_read` helper.** The other modules do. Calling it
  there is a `NameError` that crashes the agent at startup and takes out
  four tests — read the file directly with `io.open`.
- **The report header is pinned by the suite as an interface.** Adding a
  column means updating `t_agent_writes_documented_columns`. That assertion
  is the guard working; append new columns at the end.
- **A breakdown row must not be summed with the row it breaks down.**
  `flow` rows are a decomposition of the `tx` row above them, so
  `build_pairs` skips any row with a `flow` on it. Get that wrong and every
  count on the page doubles while still looking plausible.
- **Per-bucket p50s are much noisier than pair p50s**, because each rests on
  a fraction of the samples. Two buckets of the *same* healthy loopback came
  out **1.9x** apart on a busy machine. That is why `--flow-factor` defaults
  to 3 and not 2, and why any future per-subset statistic needs its
  threshold set against measured noise rather than against what looks like a
  big number.
- **A filter is not a list.** `manifest`'s range selectors expand to a set of
  wanted ids and then match every element against them; written as a scan
  that was ten million regex comparisons for one query on the largest
  published layout (13.4s, now 2.4s). A range that names the things to
  create is bounded by intent; a range that filters is bounded by the size
  of what it filters, so it needs an index and a cap.
- **A build requirement is resolved in an isolated environment, not from
  the machine.** `pip` installs an sdist's `build-system.requires` fresh
  from the index into a throwaway environment, so the setuptools already
  installed has no bearing on whether the build succeeds -- a box with
  setuptools 78 on it still fails if the isolated environment cannot get
  the version the project asked for. That is why `requires` must name a
  floor that is installable on every Python in `requires-python`:
  `setuptools>=77` (PEP 639's SPDX `license` string) needs Python>=3.9 and
  quietly made the package unbuildable from source on 3.6 to 3.8. Check a
  new build requirement against the *bottom* of the supported range, not
  against the machine it was written on.
- **The metadata version follows the setuptools doing the building**, not
  the licence form in `pyproject.toml`: the same tree gives
  `Metadata-Version: 2.1` under setuptools 64 and `2.4` under 80. Worth
  knowing before blaming a metadata version for an install failure --
  2.4 installs fine on pip as old as 21.3.1, which was measured.
- **Never sort on tuples that carry objects.** `sorted(asym, reverse=True)`
  over `(delta, PairStat, PairStat)` worked until two pairs tied on delta,
  at which point the comparison reached the `PairStat`s, which have no
  ordering, and `summarize` died on a run it had already measured
  correctly. Ranked lists want an explicit `key=` that returns only
  scalars, with a tiebreak that makes the order stable between runs. An AST
  sweep is the cheap way to find the rest: look for `sorted`/`max`/`min`
  with no `key=` whose argument is a list built by `.append((...))`.
- **`docs/conf.py` carries an explicit tool list.** A new tool added
  everywhere else will still be silently missing from the generated CLI
  reference, and the docs build will pass anyway.
- **A hand-written sample of a tool's output goes stale silently.**
  `docs/tools/binnacle.md` shows a transcript of the index; it listed eight
  instruments at an old version for two releases because nothing checks it.
  Diff it against the real command when the output changes.
- **The version lives in thirteen places** — `pyproject.toml`,
  `binnacle/__init__.py`, and each of the ten modules, plus
  `binnacle/binnacle.py`. The compose suite's `every declared version
  agrees` case enforces it, and running `binnacle` after a bump is the
  fastest check, since it exits 1 if one was left behind.
- **Live-machine assertions need margin, not luck.** `--exit-code opts into
  severity` asked for 5-6 samples and asserted WARN, but `SHORT_RUN` is WARN
  only below 5 samples and `NOT_BOUND` only at 80% free share — two boundary
  conditions, both of which a loaded CI runner falls the wrong side of. It
  passed for months and then failed on a commit that only added a markdown
  file. If a case asserts on a live run, pick parameters that put the
  expected finding well inside its threshold, and reproduce under load
  (`nproc`×2 busy loops) before believing it. The flow tests follow this:
  the live one asserts only on structure (buckets summing to the aggregate,
  distinct ports), and every threshold assertion runs off a fixture.
- **Thresholds are policy, not measurement.** Do not collect them into a
  fact dictionary, or a saved file's values silently win under
  `--from-facts` and the flag does nothing. Resolve flag → fact file →
  default, then stamp back into the facts.

## 5. The full verification gate

Everything CI runs, in the order worth running locally:

```bash
bash tests/run_tests.sh                                  # twelve suites
shellcheck -e SC1091 tests/*.sh tests/test_helper.bash
bash tests/check_python_compat.sh
vermin --eval-annotations --violations --target=3.6- binnacle/*.py
sphinx-build -W --keep-going -b html docs docs/_build/html
reuse lint
python -m build && python -m twine check dist/*
# then install the wheel into a venv and check all eleven console scripts
# answer --version, that `binnacle` exits 0, and that `netmesh selftest`
# passes
rm -rf dist build ./*.egg-info docs/_build docs/cli.md   # before committing
```

`shellcheck`, `vermin` and `reuse` are not in the dev container by default;
`pip install shellcheck-py vermin reuse` puts all three on the path.

**The real Python 3.6 container job cannot run in the dev container** —
`docker` is present but there is no daemon. CI covers it, and it is the job
most likely to catch what local runs miss.
