# Notes for Claude

binnacle is fourteen standalone instruments for Linux boxes, the fleets they
belong to and the networks between them (`why-slow`, `agree`, `logtriage`,
`netmesh`, `reachable`, `resolve`, `during`, `skew`, `muster`, `manifest`,
`dredge`, `rig`, `reckon`, `plumb`), plus `binnacle`, the index that lists
them and the version each says it is. Published on PyPI as `binnacle`, with
the manual on Read the Docs from `docs/`. Each tool is one file under
`binnacle/` with no dependencies beyond the standard library and a Python 3.6
floor, because tools are routinely `scp`'d alone to machines that have never
heard of this package. `README.md` is the front page, `PUBLISHING.md` says
how the version moves and how a release is published, and `TODO.md` holds
what was learned the hard way.

## Every task is a ticket

Everything done here goes through a ticket in `MartinGallagher-code/shared_tools`,
which keeps every project's tickets as files and moves each one Backlog → In Progress
→ Review → Done (Adversarial Test only when the owner asks). Before any change:

1. Attach `MartinGallagher-code/shared_tools` with `add_repo` and push access, and
   clone it. If a clone is already here, pull it instead
   (`git -C <shared_tools clone> pull --ff-only`): a stale clone has a stale
   `CLAUDE.md` and a stale `ticket.py`.
2. Read the "Every task is a ticket" section of its `CLAUDE.md`. It sets out what each
   column requires, and none of it is optional.
3. Open the ticket with the shared_tools clone's script, not a path in this
   repository:

   ~~~bash
   <shared_tools clone>/scripts/ticket.py new binnacle "Imperative title" --request "the request, in the user's own words"
   ~~~

**Every change moves the version**, however small: a pull request into `main` raises
it in every place it is written and opens `CHANGELOG.md` with that version's own
dated section, never an "Unreleased" heading left for later. CI fails a pull request
whose version is not greater than its base branch's. This file's own section on the
version says where it is written.

The board: https://claude.ai/artifact/HrB3RpVpqGGQS7UZgZsMJm

## Checks

What `.github/workflows/ci.yml` runs on every pull request and every push to
`main`. The suite needs nothing installed (ssh and scp are faked by a shim);
`pip install shellcheck-py vermin reuse build twine -r docs/requirements.txt`
in a venv puts the rest on the path.

```bash
bash tests/run_tests.sh                     # every suite; CI: Python 3.9-3.13
bash tests/check_python_compat.sh           # the 3.6 floor (CI also runs the
vermin --eval-annotations --violations --target=3.6- binnacle/*.py
                                            # suite in a python:3.6-slim container)
sphinx-build -W --keep-going -b html docs docs/_build/html
shellcheck -e SC1091 tests/*.sh tests/test_helper.bash
reuse lint
python -m build && python -m twine check dist/*
python -m pip install dist/*.whl && binnacle   # every tool on one version: exit 0
bash tests/check_version.sh origin/main     # the version job
rm -rf dist build ./*.egg-info docs/_build docs/cli.md   # before committing
```

The build job also runs every console script's `--version` and a short
smoke test of each; `TODO.md` section 4 has the whole gate in order.

## Every change moves the version

The owner's rule (shared_tools BN-002): **whenever this project changes, its version
changes with it**, however small the change: code, tests, docs or CI. A pull request
into `main` carries its own bump and its own `CHANGELOG.md` section, never an
"Unreleased" heading left for later.

- Patch for fixes, docs, tests and CI; minor for anything new a user can use; major
  only when something that worked stops working the way it did.
- In the same pull request: all seventeen places -- `pyproject.toml` (`version`),
  and `VERSION` in `binnacle/__init__.py`, `binnacle/binnacle.py` and each of the
  fourteen tool modules (`agree`, `dredge`, `during`, `logtriage`, `manifest`,
  `muster`, `netmesh`, `plumb`, `reachable`, `reckon`, `resolve`, `rig`, `skew`,
  `why_slow`) -- and a `## [X.Y.Z] - YYYY-MM-DD` section at the top of
  `CHANGELOG.md`, with its link definition at the foot. `tests/check_version.sh`
  fails if any of them disagree; `binnacle` itself reports a module left behind as
  `SKEW` and exits 1. `PUBLISHING.md` has the `sed` that moves all seventeen.
- The **version** job in CI fails a pull request whose version is not greater than its
  base branch's, and a push to `main` whose version is not greater than what `main` had
  before it. If `main` moves while a pull request is open, rebase and bump again.
- Publishing is a separate step, and a person's call: a `v<version>` tag on `main` and
  a GitHub Release, which `publish.yml` uploads to PyPI (`PUBLISHING.md`). A number
  that is never published is fine; a change with no number is not.

## Bundling this repo

Use the canonical scripts in `MartinGallagher-code/shared_tools` and read its
`CLAUDE.md` first. `scripts/handover.sh` reads `repos/binnacle.conf`, which
sends `tests/`, `docs/`, `binnacle/why_slow.py` and `pyproject.toml` as base64
(an upload scanner refused them in the clear). Export with `git archive`, never
a copy of a working tree: `.git/` never goes into a bundle.
