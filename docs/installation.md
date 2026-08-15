# Installation

## From PyPI

```bash
pip install binnacle
```

That puts five commands on your `PATH`:

```bash
why-slow --version
agree --version
logtriage --version
netmesh --version
reachable --version
```

There are no dependencies to resolve — the whole distribution is standard
library only.

## Requirements

| | |
|---|---|
| Python | 3.6 or newer |
| OS | Linux for `why-slow` (it reads `/proc` and `/sys`); the others are POSIX |
| Root | never required; see [what you lose without it](#running-without-root) |
| Network | only what you ask for: `agree`, `netmesh` and `reachable` use ssh |

The 3.6 floor is deliberate and load-bearing. `netmesh` copies itself to
every host in the mesh and runs it with whatever `python3` is there, and a
RHEL 8 box ships 3.6 — so syntax newer than that would fail on the fleet and
nowhere else. CI enforces it under a real 3.6 interpreter.

## Without pip

Every tool is one self-contained file that can be copied to a machine and
run. This is not a fallback, it is the normal way to use them on a host you
do not want to install anything on:

```bash
scp ~/.local/lib/python3*/site-packages/binnacle/why_slow.py somehost:/tmp/
ssh somehost 'python3 /tmp/why_slow.py'
```

Or straight from a checkout:

```bash
git clone https://github.com/MartinGallagher-code/binnacle
python3 binnacle/binnacle/why_slow.py
```

`agree` automates exactly this pattern — see
[`agree script`](tools/agree.md#pushing-a-script).

## Running without root

Nothing needs root, and nothing silently checks less when it does not have
it. What you lose is listed here and reported by the tool at the time:

`why-slow`
: The kernel log is usually unreadable as a normal user
  (`kernel.dmesg_restrict=1`), which costs you OOM-kill, I/O-error and
  thermal-throttle detection. `why-slow` prints
  `skipped: kernel log unreadable -- rerun with sudo` rather than reporting
  a clean bill of health it cannot support. Per-process I/O attribution also
  needs root; D-state process *names* do not.

`netmesh`
: Nothing. Probes are ordinary UDP sockets and path-MTU discovery uses
  `IP_PMTUDISC_DO`, both unprivileged. This is why it does not use ICMP:
  raw sockets would need `CAP_NET_RAW`.

`agree`, `reachable`
: Nothing locally. `agree --sudo` runs `sudo -n` on the far side, and a host
  that needs a password becomes a visible finding rather than a hung
  fan-out.

## ssh expectations

`agree`, `netmesh` and `reachable` all shell out to your own `ssh` and
inherit your config, agent and keys. They never distribute keys, never read
a password, and always pass `BatchMode=yes`, so they cannot sit waiting at a
prompt. A host that needs a password is reported as a finding.

Check a fleet is usable before relying on it:

```bash
agree doctor --hosts prod.txt      # reachable? python3? sudo without a password?
netmesh doctor                     # ...and can it run the probe agent?
```

## Proving it works here first

`netmesh` is the only tool that needs more than one machine to do its real
job, so it can prove itself on one:

```bash
netmesh selftest
```

That starts two agents on loopback, probes for a few seconds, checks the
round-trip times and loss are sane, and removes everything. It answers "is
my Python OK, can I open a UDP socket here, does path-MTU probing work on
this kernel" before you point it at a fleet.

## From source

```bash
git clone https://github.com/MartinGallagher-code/binnacle
cd binnacle
pip install -e .
bash tests/run_tests.sh        # 74 checks, no network, no second machine
```

See [Conventions](conventions.md) for how the suite fakes ssh.
