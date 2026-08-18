# What to do next

Written 2026-08-18, refreshed after 0.3.0 was published and the flow-hash
work landed. Everything here is enough to pick the work up cold.

## Where things stand

**0.3.0 is published** — on PyPI and imported on Read the Docs, so
`pip install binnacle` works and the README's badges resolve. That closes
the whole publishing section of the previous handover.

`main` is at **`afdf3e4`**. Note that the commit the last handover called
`main`, `7eed30f`, differed from it only in `TODO.md` itself: the document
was written on a branch and then merged, so it named the commit it was
about to stop being. Nothing behind it moved.

On top of that, branch `claude/project-bundle-txt-4845kd` carries **2a, the
flow-hash sweep** — the highest-value item on the previous list. It is
`Unreleased` in the changelog; the version is deliberately still 0.3.0 in
all ten places, because `PUBLISHING.md` bumps at release-cut time rather
than per change.

**234 checks across nine suites** (was 227; the seven new ones are all
`netmesh`). Verified directly rather than trusted: full suite, shellcheck,
the 3.6 floor under vermin, `sphinx-build -W`, `reuse lint`, both artifacts
through `twine check`, all eight console scripts reporting `0.3.0` from the
installed wheel, `netmesh selftest`, and a live two-agent loopback run with
`--flows 4` summarized end to end from that wheel.

`claude/tool-ideas-planning-ajsvea` is stale — zero commits ahead of `main`,
branched from an old point. Safe to delete.

---

## 1. Cut 0.4.0

The flow-hash work is a new flag and a new output column, which
`PUBLISHING.md`'s own rule makes a minor bump. Nothing was removed or
changed incompatibly: `flow` is appended to the report header, blank on
every row that existed before, and old fixtures with the shorter header
still parse.

Follow `PUBLISHING.md`. The one-time PyPI and Read the Docs setup is
already done, so it is now just: bump the version in all ten places, roll
`Unreleased` into `## [0.4.0]`, update the compare links, let CI go green,
tag, and publish the Release.

---

## 2. Two remaining network-testing features

Both are `netmesh` work, and 2b is the one to take next.

### 2a. Vary the flow hash — **done**, on the branch above

Kept here because the shape of it is worth knowing before touching 2b.

Each pair's probes are swept across N source ports (`--flows N`), so they
take N paths through a LAG or ECMP fabric instead of one. `summarize` grows
a `PATH SPREAD` section and raises a finding when one bucket sits at
`--flow-factor` times the median bucket's p50, or drops on its own.

Four decisions worth not re-litigating:

- **The rate is split across the buckets, not multiplied by them.** Eight
  flows at `--pps 80` is eight buckets of 10/s. A measurement tool that
  octupled its own load when asked to look harder would cause the
  congestion it then reported.
- **The cost lands on per-bucket sample count instead**, which is why the
  feature is opt-in and why a bucket under `FLOW_MIN_SAMPLES` (30) replies
  is reported as thin rather than ranked. Saying nothing would read as
  "the flows agree".
- **The reference is the median bucket, not the fastest.** With one sick
  member out of eight the median is a healthy one, so the ratio says how
  much worse that member is than the fabric's normal.
- **One socket per bucket, shared across every peer** — the destination
  address already varies the rest of the tuple, so eight buckets cost eight
  sockets whatever the size of the mesh. The ports are ephemeral and differ
  between runs, which is honest: what is sick is a member of the bundle,
  not a port number.

### 2b. Path change mid-run, via reply TTL (next)

**The fault:** an ECMP rehash or route flap partway through means the two
halves of a window measured **different physical paths**, so averaging them
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
- 2a's flow sockets are the receive path now too. `recvmsg()` has to be
  wired into every one of them, not just `self.sock`, or the TTL is
  recorded for an unrepresentative slice of the probes. That is the one
  place where 2a made 2b bigger rather than smaller.

### 2c. Locate the queue

**The fault:** we now know *that* latency rose under load, and (with 2a)
*which path*; we still do not know *where* along it. The difference between
"the network got slow" and "the queue on the second hop is the one to fix".

**Shape:** probe with increasing TTL during the loaded window; the hop where
the latency delta appears is the buffer that filled.

**Where to work:** a new probe mode in the agent, plus reporting. Largest of
the three. The natural sequel to `under_load()`.

---

## 3. Things learned the hard way

Worth reading before touching the suite — each of these cost a red run.

- **Assertions must use short fragments.** Reports wrap at **76 columns**, so
  any `assert_contains` string longer than a line will straddle a break and
  never match. This bit four separate cases. Assert on a distinctive short
  phrase, not a sentence. `_wrap_text` breaks between words and never inside
  one, so a single distinctive token is always safe.
- **`assert_contains` is a substring test, not a regex.** `\|` alternation
  matches literally.
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
- **`docs/conf.py` carries an explicit tool list.** A new tool added
  everywhere else will still be silently missing from the generated CLI
  reference, and the docs build will pass anyway.
- **The version lives in ten places** — `pyproject.toml`,
  `binnacle/__init__.py`, and each of the eight modules. The compose suite's
  `every declared version agrees` case enforces it. `PUBLISHING.md` said
  nine and seven until this branch fixed it; it had not been updated when
  `skew` was added.
- **Live-machine assertions need margin, not luck.** `--exit-code opts into
  severity` asked for 5-6 samples and asserted WARN, but `SHORT_RUN` is WARN
  only below 5 samples and `NOT_BOUND` only at 80% free share — two boundary
  conditions, both of which a loaded CI runner falls the wrong side of. It
  passed for months and then failed on a commit that only added a markdown
  file. If a case asserts on a live run, pick parameters that put the
  expected finding well inside its threshold, and reproduce under load
  (`nproc`×2 busy loops) before believing it. The new flow tests follow this:
  the live one asserts only on structure (buckets summing to the aggregate,
  distinct ports), and every threshold assertion runs off a fixture.
- **Thresholds are policy, not measurement.** Do not collect them into a
  fact dictionary, or a saved file's values silently win under
  `--from-facts` and the flag does nothing. Resolve flag → fact file →
  default, then stamp back into the facts.

## 4. The full verification gate

Everything CI runs, in the order worth running locally:

```bash
bash tests/run_tests.sh                                  # nine suites
shellcheck -e SC1091 tests/*.sh tests/test_helper.bash
bash tests/check_python_compat.sh
vermin --eval-annotations --violations --target=3.6- binnacle/*.py
sphinx-build -W --keep-going -b html docs docs/_build/html
reuse lint
python -m build && python -m twine check dist/*
# then install the wheel into a venv and check all eight console scripts
# answer --version, and that `netmesh selftest` passes
rm -rf dist build ./*.egg-info docs/_build docs/cli.md   # before committing
```

`shellcheck`, `vermin` and `reuse` are not in the dev container by default;
`pip install shellcheck-py vermin reuse` puts all three on the path.

**The real Python 3.6 container job cannot run in the dev container** —
`docker` is present but there is no daemon. CI covers it, and it is the job
most likely to catch what local runs miss.
