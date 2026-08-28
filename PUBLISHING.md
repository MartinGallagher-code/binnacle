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

## Cutting a release

1. Update the version in **all thirteen** places — they must agree, and
   the suite fails if they do not:
   - `pyproject.toml` → `version`
   - `binnacle/__init__.py` → `VERSION`
   - each tool's own `VERSION`, in all ten modules
   - `binnacle/binnacle.py` → `VERSION`

   The ten are not redundant. A tool is routinely `scp`'d to a machine
   that has never heard of this package, where its own `VERSION` is the
   only version there is — and `agree` groups a fleet by what `--version`
   reports, so a module left behind at the old number reads as version
   skew across the fleet rather than as a release that was cut carelessly.

   `binnacle` is the thirteenth because it is the command that reports
   the other twelve: it compares each instrument's `VERSION` against its
   own and calls a disagreement `SKEW`, so a `binnacle` left behind
   would accuse the ten tools that were bumped correctly. The
   `binnacle/*.py` glob below already covers it — and running
   `binnacle` after the bump is the fastest check that all thirteen
   moved, since it exits 1 if they did not.

   ```bash
   sed -i 's/^VERSION = "0\.2\.0"$/VERSION = "0.2.1"/' \
       binnacle/*.py
   sed -i 's/^version = "0\.2\.0"$/version = "0.2.1"/' pyproject.toml
   ```
2. Move the `## [Unreleased]` items in `CHANGELOG.md` under a new
   `## [x.y.z] - YYYY-MM-DD` heading, and update the link definitions at the
   foot of the file.
3. Commit, and let CI go green on `main`.
4. Tag and push:

   ```bash
   git tag -a v0.1.0 -m "binnacle 0.1.0"
   git push origin v0.1.0
   ```

5. Publish a GitHub Release for that tag. That triggers `publish.yml`, which
   builds an sdist and a wheel, checks the metadata with `twine`, installs
   the wheel and smoke-tests every console script before uploading.

## Checking before you tag

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*

# The published artifact must be a working tool, not just valid metadata.
python -m pip install dist/*.whl
binnacle          # all thirteen versions agree, and it exits 0
netmesh selftest
```

`twine check` catches a README that PyPI will not render, which is the most
common cosmetic failure.

## Testing the whole path first

To rehearse without touching the real index, add a second trusted publisher
on [test.pypi.org](https://test.pypi.org) with the same settings and run the
publish workflow manually with the TestPyPI target. Note that TestPyPI is
periodically pruned, so a version published there is not reserved.

## Version numbers

[Semantic versioning](https://semver.org). For this project specifically:

- **patch** — a bug fix, or a rule threshold corrected
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
