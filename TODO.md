# What to do next

Written 2026-08-17, refreshed after the 0.3.0 merges. Everything here is
enough to pick the work up cold.

## Where things stand

`main` is at **`7eed30f`** and is **release-ready at 0.3.0**, unpublished.
It carries `skew` (eighth tool), `during`'s receive-path, two-ended and
`--expect-mbps` work, and `netmesh`'s latency-under-load and rx-usecs
disclosure. Nothing is open: PRs #21 (release prep) and #22 (the two
follow-on features) are both merged, and no branch is ahead of `main`.

**227 checks across nine suites.** The merged combination was verified
directly rather than trusted, because #21 and #22 were each tested against
the *previous* `main` and nothing had tested them together: full suite,
shellcheck, the 3.6 floor, `sphinx-build -W`, `reuse lint`, both artifacts
through `twine check`, all eight console scripts reporting `0.3.0` from the
installed wheel, and `netmesh selftest`.

One thing that went right by luck rather than design and is worth knowing:
#22 branched from `main` *before* the `Unreleased` section was rolled into
`0.3.0`, so its two changelog entries could have been stranded. Git placed
them inside the `0.3.0` `### Added` block instead, which is where they
belong — the release section covers all six items. If a future branch is
long-lived across a release roll, check that by hand rather than assuming.

---

## 1. Publish 0.3.0

The tree is ready and verified; only the publishing steps remain, and none
of them can be done from a Claude Code session — they need repo settings,
tags, releases and PyPI. **Order matters**: `publish.yml` fires the moment
the GitHub Release is published, so the PyPI side has to exist first or the
run dies on OIDC with `invalid-publisher`.

1. **PyPI trusted publisher** (one-time, and this is the *first* publish):
   - On PyPI: *Your projects → Publishing* → add a GitHub publisher with
     owner `MartinGallagher-code`, repository `binnacle`, workflow
     `publish.yml`, environment `pypi`.
   - On GitHub: create an environment named `pypi` (*Settings →
     Environments*). A required reviewer there gives a manual approval gate
     before anything reaches PyPI.
   - The workflow name and environment must match exactly; a mismatch is the
     usual cause of `invalid-publisher`.
2. **Tag and push:**
   ```bash
   git checkout main && git pull
   git tag -a v0.3.0 -m "binnacle 0.3.0"
   git push origin v0.3.0
   ```
3. **Publish a GitHub Release** for `v0.3.0`. That triggers the build, which
   re-runs the rehearsal before uploading.
4. **Import the repo on Read the Docs** — one-off; `.readthedocs.yaml`
   already configures the build.

Until step 3 lands, `pip install binnacle` 404s and the README's PyPI and
Read the Docs badges promise things that do not resolve.

**Why 0.3.0 and not 0.2.1:** `PUBLISHING.md`'s own rule makes a new tool, a
new flag, a new rule or a new output column a minor bump. This release has
all four. Nothing was removed or changed incompatibly — the shared CSV
header is untouched, no exit code moved, and the new `during` columns are
additive, which `agree`'s merged CSV accepts as a superset.

**After publishing**, the next change starts a fresh `Unreleased` section;
it currently reads "Nothing yet." and the compare links are already correct.

---

## 2. Three remaining network-testing features

All three are `netmesh` work. They are listed in the order I would take
them, which is by value rather than by size.

### 2a. Vary the flow hash (highest value left)

**The fault:** every probe uses one 5-tuple, so it takes **one path** through
any LAG or ECMP fabric. If a bundle has four members and one is sick, a test
either hits it or does not, and which is luck. That is the classic
unreproducible fault — some flows slow, most fine, every retest disagreeing
with the last, and the switch counters looking fine because three members
are healthy. **Nothing in the package can currently see this.**

**Shape:** sweep the probe's **source port** and keep per-bucket results.

```text
  PATH SPREAD  web03 -> db01, 8 source ports

    ports 1-6         p50   142us    loss 0.00%
    port  7           p50   139us    loss 0.00%
    port  8           p50  4.1ms     loss 3.2%   <- one member of the bundle
```

**Where to work:** the prober's socket setup and per-peer stats in
`binnacle/netmesh.py`. The report needs a hash-bucket dimension —
either a new column on the `tx` row or a bucket suffix on `peer`, and the
column is the cleaner of the two. `build_pairs()` keys on `(host, peer)`
today, so bucketing means either a wider key or a parallel accumulator.

**Notes:**
- A finding wants at least two buckets that disagree by more than the
  run-to-run noise; one slow bucket out of eight is the shape to look for.
- It composes with `--baseline`: a sick member that only misbehaves under
  load is a different finding from one that is always sick.
- Keep the default modest (8 buckets) — this multiplies probe count.

### 2b. Path change mid-run, via reply TTL

**The fault:** an ECMP rehash or route flap partway through means the two
halves of a window measured **different physical paths**, so averaging them
averages two experiments. This is a **trust** rule, shaped exactly like
`PEER_NOT_CONCURRENT` in `during`.

**Shape:** record the IP TTL of each reply, per interval. A hop-count change
means the route moved. No traceroute and no root.

**Where to work:** the agent's receive path. This needs `IP_RECVTTL` plus
`recvmsg()` to get the ancillary data — **the fiddliest part of the three**,
and the reason this is 2b rather than 2a. Then one appended column on the
`tx` row, and a rule in `summarize`.

**Notes:**
- Slots straight into the idle/loaded split already merged: a path change
  *at* the split is a different story from one mid-window.
- Where `IP_RECVTTL` is unavailable the column is blank and the rule skips —
  blank means not measured, as everywhere else.

### 2c. Locate the queue

**The fault:** we now know *that* latency rose under load; we do not know
*where*. The difference between "the network got slow" and "the queue on the
second hop is the one to fix".

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
  phrase, not a sentence.
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
- **`docs/conf.py` carries an explicit tool list.** A new tool added
  everywhere else will still be silently missing from the generated CLI
  reference, and the docs build will pass anyway.
- **The version lives in ten places** — `pyproject.toml`,
  `binnacle/__init__.py`, and each of the eight modules. The compose suite's
  `every declared version agrees` case enforces it.
- **Live-machine assertions need margin, not luck.** `--exit-code opts into
  severity` asked for 5-6 samples and asserted WARN, but `SHORT_RUN` is WARN
  only below 5 samples and `NOT_BOUND` only at 80% free share — two boundary
  conditions, both of which a loaded CI runner falls the wrong side of. It
  passed for months and then failed on a commit that only added a markdown
  file. If a case asserts on a live run, pick parameters that put the
  expected finding well inside its threshold, and reproduce under load
  (`nproc`×2 busy loops) before believing it.
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

**The real Python 3.6 container job cannot run in the dev container** —
`docker` is present but there is no daemon. CI covers it, and it is the job
most likely to catch what local runs miss.
