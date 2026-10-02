# plumb

**Does what this box reads match what is on its disk?**

Reads every file twice: once through the page cache, the way every program
reads it, and once with `O_DIRECT`, past the cache, from the disk. If the
two copies differ, it works out which one is wrong and what most likely
made it wrong, and gives the exact command to run next.

```bash
plumb                            # the files an attack would aim at
plumb /usr/bin /etc              # these; directories are walked
plumb --csv                      # one row per finding, for fleet use
plumb --explain CACHE_TAMPERED
```

## The failures it exists for

On Linux the copy of a file that programs read is the page cache's, not
the disk's. Usually the two are the same. In 2026 several things made them
differ, in both directions.

**The cache is wrong and the disk is right.** A run of local privilege
escalations each let an unprivileged user write into the page cache of a
file they could only read:

| Bug | CVE | Entry point |
|---|---|---|
| Copy Fail | CVE-2026-31431 | `algif_aead` (AF_ALG crypto sockets) |
| Dirty Frag | CVE-2026-43284, CVE-2026-43500 | `esp4`, `esp6`, `rxrpc` |
| Fragnesia | CVE-2026-46300 | `esp4`, `esp6` (ESP-in-TCP) |
| DirtyClone | CVE-2026-43503 | `esp4`, `esp6`, via `xt_TEE` |
| pedit COW | CVE-2026-46331 | `act_pedit`, with a user namespace |

The target is a setuid program such as `su`, which runs as root and so
runs the attacker's bytes. The altered page is never marked dirty, so it is
never written back: the disk stays clean, nothing that watches for writes
fires, an offline scan of the disk finds nothing, and the evidence is gone
when the page is evicted or the box reboots. Dirty Pipe (CVE-2022-0847)
worked the same way four years earlier.

**The disk is wrong and the cache is right.** Large folios, the multi-page
units the page cache now works in, brought writeback bugs with them. On
ext4, newly written ranges reached the disk as zeros: Fedora CoreOS found
kernel images in `/boot` with megabytes zeroed after an upgrade, but only
after rebooting into them. On btrfs, a 7.2 regression let data change while
it was being written back. Until the page is evicted the file reads
correctly. After that it is corrupt, and nothing records when that
happened.

**The cache is wrong for no software reason.** Memory fails. A bit that
flips in a cached page is a file that reads wrong while its disk copy is
fine.

## What it does

```text
plumb -- node07, Linux 6.12.0-124.45.1.el10_1.x86_64, 4 files compared, 40.0 MiB read both ways

  VERDICT   /usr/bin/su has been altered in memory while its disk copy is
            intact, so everything that runs it runs the altered bytes. That
            is the shape of a page-cache privilege escalation: treat the box
            as compromised.

  CRITICAL  privileged file altered  /usr/bin/su (setuid): 4 bytes altered
                                     in memory at 0x44d0, disk intact
  INFO      files not checked        2 of 6: 2 need root

  DIFFERENCES
    /usr/bin/su  (setuid, ext4, not written since boot)
      differs    4 bytes in 1 range: 0x44d0-0x44d3
      disk       f3 0f 1e fa 31 ed 49 89 d1 5e 48 89 e2 48 83 e4
      cache      90 90 90 90 31 ed 49 89 d1 5e 48 89 e2 48 83 e4
      where      the program's entry point (0x44d0)
      judged     the cache is wrong: the disk matches the sha256 util-linux
                 records for it; the cache does not
      likely     a write into the page cache -- 4 bytes rewritten in memory
                 at the program's entry point, the disk untouched -- how
                 Copy Fail, Dirty Frag, Fragnesia and pedit COW (2026) turn
                 a local user into root
                 - entry points present here: algif_aead built in (Copy
                 Fail), esp4 loaded (Dirty Frag, Fragnesia, DirtyClone),
                 esp6 loaded (Dirty Frag, Fragnesia, DirtyClone)

  WHAT TO DO NEXT
    * Treat this box as compromised until shown otherwise: anyone who ran
      /usr/bin/su since it changed ran the altered bytes. Keep the evidence
      before it goes -- `plumb --json evidence.json /usr/bin/su`, and `cat
      /usr/bin/su > ~/su.evidence` saves the altered bytes as a plain file
      -- then check who was logged in (`last`, the auth log). Then `dd
      if=/usr/bin/su iflag=nocache count=0` evicts it ...
```

Each difference is reported with its exact byte ranges, the first sixteen
bytes of each copy, and where in the file it falls. Each is then judged
twice: which copy is wrong, and why.

### Which copy is wrong

This uses the strongest evidence available, in this order, and guesses at
nothing:

1. **The package manager's record.** dpkg's `*.md5sums` and rpm's file
   digests say what was installed. A copy that matches the record is right,
   so the other one is wrong. These are only consulted for files that
   differ, so a clean run does not pay for them.
2. **Zeros on one side where the other holds data.** If the disk reads
   zeros where the cache has data, the write never landed. If the cache
   reads zeros, the page was emptied in memory.
3. **Whether the file has been written since boot.** If it has not, its
   cache was filled by reading this same disk. A cache that now disagrees
   with the disk must have changed in memory afterwards. `ctime` is used
   rather than `mtime` alone, because any owner can set `mtime` and only
   the kernel sets `ctime`.

If none of these can place a difference, it is reported as
`CACHE_DISK_DIFFER`, with the reasons it could not be placed, and the fix
explains how to settle it: compare both copies against another box that has
the file.

### Why it went wrong

The shape of the difference, together with what the box itself knows:

| What it looks like | Likely cause | Evidence read alongside |
|---|---|---|
| exactly one flipped bit | memory | EDAC corrected and uncorrected counts, or the fact that no EDAC driver is loaded to report them; machine-check taint |
| a few bytes rewritten in a setuid program, PAM module, the loader or an auth file | a page-cache write | the 2026 bugs' entry-point modules, loaded or built in; Dirty Pipe if the version is in range; unprivileged user namespaces for pedit COW |
| …at the ELF entry point, or over the ELF header | as above, more strongly | the entry point is worked out from the program headers |
| whole pages wrong, or zeroed, in memory | a page-cache bug in the kernel | the kernel log's `VM_BUG_ON_FOLIO` and bad-page lines; taint `B` and `D` |
| zeros on disk under a write made since boot | a lost write | the filesystem and kernel version against the reported ext4 and btrfs writeback bugs; I/O and filesystem errors in the kernel log |
| two reads past the cache disagree | the storage | md `mismatch_cnt`; I/O errors |

A single flipped bit in a setuid program is still reported as memory, not
as an attack. Hardware does not pick its targets, and calling it an attack
would send someone looking for an intruder instead of replacing a DIMM.

Known bugs are named as **candidates to check against your vendor's
advisory, never as a diagnosis**. Distributions backport fixes without
changing the version number, so a version inside a reported range is a
reason to look, and a version outside one proves nothing.

### The disk copy that is about to be lost

```text
  VERDICT   /boot/vmlinuz-7.1.10-200.fc44.ppc64le is wrong on disk and right
            in memory. The page cache holds the only good copy, and a
            reboot, a cache drop or memory pressure throws it away -- rescue
            it first. Likely cause: a write that never reached the disk --
            the cache holds what was written, the disk holds zeros.
```

This is the one finding that is urgent in time, so it comes before
tampering. The fix says to copy the file out first, because `cp` reads the
cached copy, and to do nothing that empties the cache until that is done.
When the same run also finds a tampered file, the advice for that file
takes this into account: evict only the one bad file, and do not drop
caches.

### Reading past the cache, honestly

`O_DIRECT` asks the kernel to bypass the page cache, but the kernel does
not always do so. Some filesystems quietly serve the read from the cache
anyway, and a read that went through the cache always matches the cache.
That would be an all-clear that means nothing, which is the one result
this tool must never give. So everything known to fall back is refused by
name, never read, and counted under `NOT_CHECKED`:

- **no disk behind it:** tmpfs, ramfs, procfs and the other in-memory
  filesystems
- **its own rules about the cache:** NFS, CIFS and the other network
  filesystems, FUSE, and ZFS (which has its own cache, the ARC)
- **the filesystem serves O_DIRECT from the page cache:** ext4 mounted
  `data=journal`, files with data journalling, compressed or inline
  extents, fscrypt and fs-verity files
- **no page cache at all:** DAX

Extents are read with `FIEMAP` and inode flags with `FS_IOC_GETFLAGS`. The
ioctl numbers are worked out per architecture, because powerpc, mips, sparc
and alpha encode them differently from x86 and arm, and powerpc is where
the ext4 writeback bug was first seen. If a filesystem will not answer the
ioctls, the file is still compared.

Before believing a difference, both copies are read a second time. A file
whose size, inode or timestamps changed during the read is read again, up
to three times. If it never holds still it is reported as
`CHANGED_WHILE_READ` and not judged. If the two copies agree on the second
read, that is the same finding. If two reads straight from the disk
disagree with each other, that is `DISK_UNSTABLE`. Pending writes cannot
show up as differences either: an `O_DIRECT` read writes back any dirty
pages in its range before it reads.

### Evicting a bad copy, and when not to

`plumb` never changes the machine it is diagnosing. It tells you how:

- `dd if=FILE iflag=nocache count=0` evicts one file's clean pages, with no
  root needed. The next read comes from the disk.
- **A page that a process has mapped cannot be evicted**, not by
  `fadvise`, not by `drop_caches`, and not as root. For each differing
  file, `plumb` scans `/proc/*/maps` and names the processes that have it
  mapped. Without root it can only see your own processes, and it says so.
  For a library every process maps, such as libc, the only real eviction
  is a reboot.
- **Never `drop_caches` while any file's only good copy is in memory.** No
  fix here suggests a cache drop, and the ones that evict a file say outright
  not to when another file's only good copy is still in memory.

## Across a fleet

```bash
agree script plumb --servers prod.txt --fleet-csv -- --csv
```

Hosts are grouped by what is wrong with them. After a page-cache advisory,
the one box whose `su` was rewritten in memory lands in a group of its own,
named by `CACHE_TAMPERED`. The boxes that merely have the vulnerable module
loaded stay with the clean ones, because a loaded module is context for a
difference, never a finding by itself.

Before rebooting a fleet into a kernel that has just been installed, run
`plumb /boot` on every host. That is the moment the Fedora CoreOS
corruption could have been caught: the new image's good copy was still in
the page cache.

## Rules

`plumb --rules` prints all of them, and `plumb --explain RULE_ID` prints why
one exists. In verdict precedence order:

| Rule | Severity | Fires when |
|---|---|---|
| `DISK_UNSTABLE` | CRITICAL | two reads past the cache return different bytes |
| `DISK_UNREADABLE` | CRITICAL | one copy cannot be read while the other can |
| `DISK_WRONG` | CRITICAL | the disk copy is wrong and the cache holds the good one |
| `CACHE_TAMPERED` | CRITICAL | a privileged file is altered in memory, its disk copy intact |
| `CACHE_CORRUPT` | CRITICAL | any other file wrong in memory, its disk copy intact |
| `CACHE_DISK_DIFFER` | CRITICAL | the copies differ and nothing can say which is right |
| `NOTHING_COMPARED` | WARN | every file was skipped |
| `MEMORY_ERRORS` | CRITICAL / WARN | EDAC reports uncorrected / corrected errors |
| `KERNEL_TAINT` | WARN | the kernel is tainted by a machine check, a bad page or an oops |
| `KERNEL_LOG` | WARN | memory, page-cache, filesystem or storage errors in the kernel log |
| `CHANGED_WHILE_READ` | INFO | a file would not hold still long enough to compare |
| `NOT_CHECKED` | INFO | files that could not be read past their cache, counted by reason |

"Privileged" means setuid or setgid, a PAM module, the loader or libc
(including `libpam`, `libcrypt` and `libnss`), or an auth file: `passwd`,
`shadow`, `group`, `sudoers`, `ld.so.preload`, `pam.d`, the shell profiles
and cron.

## The default set

With no arguments, `plumb` checks the files an attack would aim at, and
the ones whose corruption costs the most:

- every setuid and setgid program in the `bin` and `sbin` directories,
  `/usr/libexec`, and the helper directories under `/usr/lib`
- the auth files above, `/etc/pam.d`, `/etc/sudoers.d`, `/etc/profile.d`
  and `/etc/cron.d`
- PAM modules, the loader, libc, `libpam`, `libcrypt`, `libnss_files`,
  `libselinux` and `libaudit`, found by globbing the library directories
  and also from this process's own mappings, so layouts the globs do not
  know are still covered
- kernel images, initramfs and boot loader entries in `/boot`, **only if
  written since boot**. Those are the only boot files whose disk copy came
  from this kernel's writeback.

## What it needs, and what it guarantees

- **No root for most of it.** `O_DIRECT` needs only read permission.
  `/etc/shadow`, `/etc/gshadow` and, on some distributions, the boot images
  need root, and are counted as `need root` rather than silently left out.
- **No packages.** Standard library only, Python 3.6+. `rpm` is run only
  for files that differ, and `--no-exec` turns it off along with `dmesg`.
- **A fact that could not be measured is `None`, never `0`.** Any rule that
  needed it is reported as skipped, with the reason (`--all` lists them,
  and lists every unchecked file too).
- **Every judgement is a pure function of the fact dictionary**, so
  `--from-facts` reproduces any diagnosis without reading a disk. That is
  also how the rules are tested. `--disk-view DIR` reads each file's disk
  side from a copy under `DIR` instead, which is how the comparison itself
  is tested without a broken disk.
- **Exit status is 0 unless you ask for `--exit-code`.**

## Where it does not help

- **A bug that writes through to the disk.** Dirty COW (CVE-2016-5195)
  marked its pages dirty, so writeback put the attacker's bytes on disk and
  both copies agree. A baseline check catches that, and this does not:
  `rpm -Va`, `debsums`, AIDE.
- **A page altered and already evicted.** The evidence of the 2026 bugs
  lives only as long as the page does. Run this when the advisory lands,
  not after the reboot.
- **What it was not asked about.** The default set is what an attack would
  aim at. A corrupted database file is only found if you name it.
- **Filesystems it refuses.** These are listed above, and every refused
  file is counted. A btrfs root with compression on, the Fedora default,
  has a share of its files refused for that reason.
- **Reading is not free.** Comparing a file brings it into the page cache,
  as any read would, and a dirty range is written back before the direct
  read, as the kernel would do within half a minute anyway.
- **It does not patch anything.** The fix text names the modules to block
  and how, including the boot parameter a built-in one needs, because
  `install … /bin/false` does nothing to a module that is not a module.

## Sources

- Copy Fail: [CERT-EU advisory 2026-005](https://cert.europa.eu/publications/security-advisories/2026-005/)
- Dirty Frag: [Edera](https://edera.dev/stories/dirty-frag-the-linux-kernel-exploit-that-turns-your-page-cache-against-you)
- Fragnesia: [SOC Prime](https://socprime.com/blog/cve-2026-46300-fragnesia-linux-kernel-flaw/)
- DirtyClone and pedit COW: [The CyberSec Guru](https://thecybersecguru.com/news/linux-lpe-pedit-cow-dirtyclone-cve-2026-46331-cve-2026-43503/)
- The ext4 writeback bug: [fedora-coreos-tracker#2234](https://github.com/coreos/fedora-coreos-tracker/issues/2234)
- The btrfs 7.2 regression: [linux-btrfs](https://ratatoskr.run/linux-btrfs/2026/09/17489069/t)
- The same comparison as a blocking guard: [pagecache-guard](https://github.com/0xlane/pagecache-guard)
