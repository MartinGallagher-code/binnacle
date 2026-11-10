# Publishing

`binnacle` publishes to PyPI through **trusted publishing** (OIDC), so no
API token is stored as a repository secret. The workflow in
`.github/workflows/publish.yml` runs on a published GitHub Release, or
manually via `workflow_dispatch`.

## One-time PyPI setup

Do this **before** the first release, or the first publish run fails OIDC
with `invalid-publisher`.

1. Sign in at [pypi.org](https://pypi.org) and go to
   *Your projects → Publishing* (or *Account settings → Publishing* for a
   project that does not exist yet).
2. Add a **GitHub** trusted publisher:

   | Field | Value |
   |---|---|
   | Owner | `MartinGallagher-code` |
   | Repository | `binnacle` |
   | Workflow name | `publish.yml` |
   | Environment | `pypi` |

3. In the GitHub repository, create an environment named `pypi`
   (*Settings → Environments*). Adding required reviewers there gives you a
   manual approval gate before anything is pushed to PyPI.

The workflow name and environment must match exactly. A mismatch is the
usual cause of `invalid-publisher`.

## The version moves in every pull request

Every change moves the version, however small — code, tests, docs or CI
(shared_tools BN-002). A pull request into `main` carries its own bump and
its own section in `CHANGELOG.md`; nothing waits for a release to be
numbered. The bump is not a release: publishing is the separate step below.

1. Raise the version in **all seventeen** places — they must agree, and
   `tests/check_version.sh` and the suite fail if they do not:
   - `pyproject.toml` → `version`
   - `binnacle/__init__.py` → `VERSION`
   - each tool's own `VERSION`, in every module
   - `binnacle/binnacle.py` → `VERSION`

   The fourteen tool modules are not redundant. A tool is routinely `scp`'d to a machine
   that has never heard of this package, where its own `VERSION` is the
   only version there is — and `agree` groups a fleet by what `--version`
   reports, so a module left behind at the old number reads as version
   skew across the fleet rather than as a change that was numbered carelessly.

   `binnacle` is the one that reports the other fourteen: it compares
   each instrument's `VERSION` against its own and calls a disagreement
   `SKEW`, so a `binnacle` left behind would accuse the fourteen tools
   that were bumped correctly. The `binnacle/*.py` glob below already
   covers it — and running `binnacle` after the bump is the fastest check
   that they all moved, since it exits 1 if they did not.

   ```bash
   sed -i 's/^VERSION = "0\.9\.0"$/VERSION = "0.9.1"/' \
       binnacle/*.py
   sed -i 's/^version = "0\.9\.0"$/version = "0.9.1"/' pyproject.toml
   ```
2. Open `CHANGELOG.md` with a new `## [x.y.z] - YYYY-MM-DD` section for
   that version, holding this pull request's entries, and add its link
   definition at the foot of the file. There is no `## [Unreleased]`
   heading: `tests/check_version.sh` refuses one.
3. `bash tests/check_version.sh origin/main` passes when all seventeen
   agree, the changelog opens with that version, and it is greater than
   `main`'s. CI's `version` job runs the same check against the base
   branch, and again on the push to `main`; if `main` moves while the pull
   request is open, rebase and bump again.

The number follows *Version numbers*, below. A number that
is never published is fine; a change with no number is not.

## Cutting a release

A release publishes the version `main` already says. It is a person's
call, and it changes no number.

1. Let CI go green on the `main` commit to be published.
2. Tag it with the version its `pyproject.toml` says, and push the tag:

   ```bash
   git tag -a v0.9.0 -m "binnacle 0.9.0"
   git push origin v0.9.0
   ```

3. Publish a GitHub Release for that tag. That triggers `publish.yml`, which
   checks the tag against `pyproject.toml`, builds an sdist and a wheel,
   checks the metadata with `twine`, installs the wheel and smoke-tests
   every console script before uploading.

## Checking before you tag

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*

# The published artifact must be a working tool, not just valid metadata.
python -m pip install dist/*.whl
binnacle          # every version agrees, and it exits 0
netmesh selftest
```

`twine check` catches a README that PyPI will not render, which is the most
common cosmetic failure.

## Running the publish twice

Harmless. The upload passes `skip-existing: true`, so a file PyPI already
holds is skipped rather than failing the run.

That matters because **PyPI never lets a filename be reused**. Re-running
the publish against a version that is already up could otherwise only ever
fail, and it is an easy run to start by accident: the version comes from
the tree, not from the dispatch form, so nothing about starting the run
tells you which version it is about to upload.

What it does not do is hide a mistake. Bumping the version is what makes an
upload new; an unbumped one has nothing to publish, and is now skipped
quietly instead of ending the run red. If you expected new files and see
every one skipped, the version was not bumped.

Two related things are worth knowing when reading a publish log:

- **A rebuilt wheel is not byte-identical to the one already published,**
  even from the same commit, because wheels embed file timestamps. A
  different hash in the log is not evidence that the wrong thing was built.
- **A dispatch builds from whatever the branch says, a Release builds from
  the tag.** They agree only while the tag is the branch head. Publishing
  the Release is the safer of the two for exactly that reason.

## Testing the whole path first

To rehearse without touching the real index, add a second trusted publisher
on [test.pypi.org](https://test.pypi.org) with the same settings and run the
publish workflow manually with the TestPyPI target. Note that TestPyPI is
periodically pruned, so a version published there is not reserved.

## Version numbers

[Semantic versioning](https://semver.org). For this project specifically:

- **patch** — a bug fix, a rule threshold corrected, or a change to docs,
  tests or CI alone
- **minor** — a new tool, a new flag, a new rule, or a new output column
- **major** — a changed CSV schema, a removed flag, or a changed exit code

The CSV schemas and the exit codes are the public interface as much as the
flags are: people build cron jobs and dashboards on them, so a change there
is breaking even when no flag moved.

## Read the Docs

`.readthedocs.yaml` configures the build; importing the repository once on
readthedocs.org is all the setup needed. `ci.yml` builds the same docs with
`-W` (warnings as errors) on every pull request, so a docs break fails the
PR rather than surfacing later on RTD.

The CLI reference is generated from the live argparse parsers during that
build, so a new flag that would render badly also fails CI.
