#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""plumb.py -- check that what this box reads is what is on its disk.

Usage: plumb.py                      the files that matter most on this box
       plumb.py PATH...              these files; directories are walked
       plumb.py --csv                one row per finding, for fleet use
       plumb.py --rules              every rule and what it needs
       plumb.py --explain RULE_ID    why one rule exists

Each file is read twice -- through the page cache, the way every program
reads it, and with O_DIRECT, past the cache, from the disk -- and a
difference is diagnosed: which copy is wrong, and what most likely did it.

Options:
  --max-mb N         skip files over N MiB, 0 for no ceiling
                     (default 256)                          (PLUMB_MAX_MB)
  --one-fs           do not cross into other filesystems when walking
  --min-severity L   info | warn | critical -- hide findings below L
  --all              also list skipped rules and unchecked files, and why
  --quiet            the verdict and the findings, nothing else
  --exit-code        exit 0 ok / 10 warn / 20 critical (default: always 0)
  --csv [PATH]       findings as CSV (stdout if no path)
  --json [PATH]      meta + facts + findings + skipped
  --facts            print the fact dictionary and exit
  --from-facts FILE  run the rules against a saved fact file, reading
                     nothing -- this is how the rules are tested
  --proc-root DIR    read /proc from here                   (PLUMB_PROC)
  --sys-root DIR     read /sys from here                    (PLUMB_SYS)
  --dpkg-info DIR    where dpkg keeps its *.md5sums         (PLUMB_DPKG_INFO)
  --disk-view DIR    read each file's disk side from DIR/<path> instead of
                     past the cache -- how the comparison is tested
                                                            (PLUMB_DISK_VIEW)
  --no-exec          never run a subprocess (no rpm, no dmesg)
  --no-color         plain output (also honours NO_COLOR)

Exit status: 0, or under --exit-code 10 for a warning and 20 for anything
critical.  2 is a usage error.

Full manual: https://binnacle.readthedocs.io/en/latest/tools/plumb.html
"""

import argparse
import csv
import errno
import fcntl
import fnmatch
import glob
import hashlib
import io
import json
import mmap
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from collections import namedtuple

VERSION = "0.8.0"
PROG = os.path.basename(sys.argv[0]) or "plumb.py"

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
SEVERITY_ORDER = {CRITICAL: 3, WARN: 2, INFO: 1}

PROC_ROOT = "/proc"
SYS_ROOT = "/sys"
DPKG_INFO = "/var/lib/dpkg/info"
NO_EXEC = False

DEFAULT_MAX_MB = 256

# Read in megabyte steps: a multiple of every logical block size O_DIRECT
# can demand, so every read but the last starts aligned.
CHUNK = 1 << 20
PAGE = 4096
# Past this many differing pages the bytes inside each are no longer
# walked one at a time; the rest are counted a page at a time, and the
# report says the count is approximate.  64 pages is a quarter of a
# megabyte of Python loop, which is the price of an exact answer for
# every difference a person would read byte by byte.
BYTE_PAGES = 64
# How many differing ranges are kept in the facts.  The count is exact
# whatever this is; only the list is cut.
KEEP_RANGES = 16
# Differences closer together than this are one range.  A zeroed region
# over data that happens to hold zeros of its own is one region with
# some bytes that match by coincidence, not hundreds of ranges.
RANGE_GAP = 32


def _stdio_safe():
    """Never lose a report to a character the locale cannot spell.

    On a box with LANG=C -- a RHEL 8 default, and the floor this package
    targets -- stdout is ASCII, so one UTF-8 file name in a report turns
    the whole run into a UnicodeEncodeError.  Degrading the character is
    the same bargain the rest of these tools already make: the run is
    worth more than the byte.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "buffer"):
            continue
        enc = (getattr(stream, "encoding", None) or "").lower()
        if enc.replace("-", "_") in ("ascii", "ansi_x3.4_1968", "us_ascii"):
            setattr(sys, name, io.TextIOWrapper(
                stream.buffer, encoding=stream.encoding,
                errors="backslashreplace", line_buffering=True))


def die(msg, code=2):
    sys.stderr.write("%s: %s\n" % (PROG, msg))
    raise SystemExit(code)


class _WriteGuard(object):
    """Full disk, quota, missing directory: name the file and stop.

    Wraps a block writing an output the user asked for by path.  Only
    OSError becomes the message, and only when a path was really given,
    so stdout keeps its ordinary pipe semantics.  A traceback here would
    bury the one fact that matters: which file could not be written.

    The partial file is removed too -- a half-written CSV that parses is
    worse than none -- except when appending, where what was already
    there predates this failure and is still good.
    """

    def __init__(self, path, appending=False):
        self.path = path
        self.appending = appending

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.path and exc_type is not None and issubclass(exc_type, OSError):
            if not self.appending:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            die("cannot write %s: %s" % (self.path, exc))
        return False


def _env(name, default=None):
    v = os.environ.get("PLUMB_" + name)
    return v if v not in (None, "") else default


def _read(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return default


def _read_int(path, default=None):
    txt = _read(path)
    if txt is None:
        return default
    txt = txt.strip()
    if txt == "max":
        return None
    try:
        return int(txt.split()[0])
    except (ValueError, IndexError):
        return default


def _run(cmd, timeout=5):
    """Subprocess, or nothing at all under --no-exec."""
    if NO_EXEC:
        return None
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             universal_newlines=True,
                             encoding="utf-8-sig", errors="replace")
        out, _ = p.communicate(timeout=timeout)
        return out if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Which files
# ---------------------------------------------------------------------------
#
# The default set is the files a page-cache write would be aimed at, and
# the ones whose corruption costs the most: what runs as root without
# being asked (setuid and setgid programs), what decides who you are
# (passwd, shadow, sudoers, PAM and its modules), what is loaded into
# every process (the loader, libc, ld.so.preload), and what the next boot
# will load -- but only the boot files written since this boot, because
# those are the only ones whose disk copy came from this kernel's
# writeback.

AUTH_FILES = (
    "/etc/passwd", "/etc/group", "/etc/shadow", "/etc/gshadow",
    "/etc/sudoers", "/etc/ld.so.preload", "/etc/ld.so.cache",
    "/etc/nsswitch.conf", "/etc/profile", "/etc/bash.bashrc", "/etc/bashrc",
    "/etc/environment", "/etc/crontab", "/etc/ssh/sshd_config",
)
AUTH_DIRS = ("/etc/pam.d", "/etc/sudoers.d", "/etc/profile.d", "/etc/cron.d")
SUID_FLAT = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin",
             "/usr/local/sbin")
SUID_DEEP = ("/usr/libexec", "/usr/lib/openssh", "/usr/lib/dbus-1.0",
             "/usr/lib/polkit-1", "/usr/lib/snapd")
LIB_DIRS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/lib/*-linux-gnu",
            "/usr/lib/*-linux-gnu")
LOADER_NAMES = (
    "ld-linux*.so*", "ld-musl-*.so*", "libc.so.6", "libc.musl-*",
    "libpam.so.*", "libpam_misc.so.*", "libcrypt.so.*", "libnss_files.so.*",
    "libnss_systemd.so.*", "libselinux.so.*", "libaudit.so.*",
)
BOOT_GLOBS = (
    "/boot/vmlinuz*", "/boot/vmlinux*", "/boot/initrd*", "/boot/initramfs*",
    "/boot/ostree/*/vmlinuz*", "/boot/ostree/*/initramfs*",
    "/boot/grub/grub.cfg", "/boot/grub2/grub.cfg",
    "/boot/loader/entries/*.conf",
)

# Roles where an altered page means someone else's code running with
# privileges they did not have.
PRIVILEGED = ("setuid", "setgid", "auth", "pam", "loader")


def role_of(path, st):
    """What this file is to the box, which is what a difference costs."""
    if st.st_mode & stat.S_ISUID:
        return "setuid"
    if st.st_mode & stat.S_ISGID:
        return "setgid"
    if path.startswith("/boot/"):
        return "boot"
    if path in AUTH_FILES or any(path.startswith(d + "/") for d in AUTH_DIRS):
        return "auth"
    base = os.path.basename(path)
    if os.path.basename(os.path.dirname(path)) == "security" \
            and base.endswith(".so"):
        return "pam"
    if any(fnmatch.fnmatch(base, pat) for pat in LOADER_NAMES):
        return "loader"
    return "file"


def _files_in(d):
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(d, n)
        try:
            if stat.S_ISREG(os.lstat(p).st_mode):
                out.append(p)
        except OSError:
            continue
    return out


def _privileged_in(d, deep):
    """Every setuid or setgid regular file in d (and below, if deep)."""
    out = []
    walk = os.walk(d) if deep else [(d, [], None)]
    for dirpath, _dirs, _files in walk:
        for p in _files_in(dirpath):
            try:
                mode = os.lstat(p).st_mode
            except OSError:
                continue
            if mode & (stat.S_ISUID | stat.S_ISGID):
                out.append(p)
    return out


def _self_mapped(proc_root):
    """The loader and libc as this very process has them mapped.

    A fallback for layouts the globs do not know -- NixOS, a vendor
    prefix -- since whatever loaded this interpreter is by definition the
    loader this box uses.
    """
    out = []
    for line in (_read(os.path.join(proc_root, "self", "maps")) or "") \
            .splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith("/"):
            base = os.path.basename(parts[5])
            if any(fnmatch.fnmatch(base, pat) for pat in LOADER_NAMES):
                out.append(parts[5])
    return out


def changed_since(st, btime):
    """Written, renamed into place or re-permissioned since boot.

    ctime rather than mtime alone: mtime can be set by anyone who owns
    the file, ctime only by the kernel.
    """
    if btime is None:
        return None
    return max(st.st_mtime, st.st_ctime) >= btime


def default_targets(proc_root, btime):
    out = [p for p in AUTH_FILES if os.path.lexists(p)]
    for d in AUTH_DIRS:
        out.extend(_files_in(d))
    for d in SUID_FLAT:
        out.extend(_privileged_in(d, False))
    for d in SUID_DEEP:
        out.extend(_privileged_in(d, True))
    for pattern in LIB_DIRS:
        for d in sorted(glob.glob(pattern)):
            for name in LOADER_NAMES:
                out.extend(sorted(glob.glob(os.path.join(d, name))))
            out.extend(sorted(glob.glob(os.path.join(d, "security", "*.so"))))
    out.extend(_self_mapped(proc_root))
    for pattern in BOOT_GLOBS:
        for p in sorted(glob.glob(pattern)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if changed_since(st, btime) is not False:
                out.append(p)
    return out


def explicit_targets(paths, one_fs):
    """The paths named, with directories walked; symlinks inside a walk
    are not followed, since what they point at is checked where it is."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            out.append(p)            # reported as gone or unreadable
            continue
        if not stat.S_ISDIR(st.st_mode):
            out.append(p)
            continue
        for dirpath, dirs, _files in os.walk(p):
            if one_fs:
                keep = []
                for d in dirs:
                    try:
                        if os.lstat(os.path.join(dirpath, d)).st_dev \
                                == st.st_dev:
                            keep.append(d)
                    except OSError:
                        continue
                dirs[:] = keep
            dirs.sort()
            out.extend(_files_in(dirpath))
    return out


def dedupe(paths):
    """Each file once, by where it really is: a symlink and its target,
    and two hard links to one inode, are one comparison."""
    seen_real, seen_ino, out = set(), set(), []
    for p in paths:
        real = os.path.realpath(os.path.abspath(p))
        if real in seen_real:
            continue
        seen_real.add(real)
        try:
            st = os.stat(real)
            key = (st.st_dev, st.st_ino)
            if key in seen_ino:
                continue
            seen_ino.add(key)
        except OSError:
            pass
        out.append(real)
    return sorted(out)


# ---------------------------------------------------------------------------
# Can this file be read past its cache at all?
# ---------------------------------------------------------------------------
#
# O_DIRECT is a request, not a guarantee.  Several filesystems take it
# and quietly serve the read from the page cache anyway, and a read that
# went through the cache compares equal to the cache every time -- the
# "all clear" that means nothing, which is the one result this tool must
# never give.  So everything known to fall back is refused up front, by
# name, and counted as not checked.

NO_DISK_FS = {"tmpfs", "ramfs", "devtmpfs", "rootfs", "proc", "sysfs",
              "devpts", "cgroup", "cgroup2", "securityfs", "debugfs",
              "tracefs", "configfs", "pstore", "efivarfs", "bpf",
              "hugetlbfs", "mqueue", "nsfs", "binfmt_misc", "autofs"}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ceph", "glusterfs",
              "9p", "virtiofs", "afs", "lustre", "gpfs", "ocfs2", "gfs2"}


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def read_mounts(proc_root):
    txt = _read(os.path.join(proc_root, "self", "mountinfo"))
    if txt is None:
        return None
    out = []
    for line in txt.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lf, rf = left.split(), right.split()
        if len(lf) < 6 or len(rf) < 2:
            continue
        out.append({"mount": _unescape(lf[4]), "opts": lf[5],
                    "fstype": rf[0], "source": rf[1],
                    "super": rf[2] if len(rf) > 2 else ""})
    return out


def mount_of(path, mounts):
    """The mount a path lives on: the longest mount point above it, and
    of two at the same point, the later -- which is the one on top."""
    best = None
    for m in mounts or []:
        mp = m["mount"]
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best["mount"]):
                best = m
    return best


def fs_refusal(m):
    """(code, reason) when nothing on this mount can be read past its
    cache, or None."""
    if m is None:
        return None
    t = m["fstype"]
    opts = set((m["opts"] + "," + m["super"]).split(","))
    if t in NO_DISK_FS:
        return ("nodisk", "%s keeps files in memory, so there is no disk "
                          "copy to compare" % t)
    if t in NETWORK_FS or t.startswith("nfs"):
        return ("netfs", "%s is a network filesystem, whose cache may "
                         "legitimately trail the server" % t)
    if t.startswith("fuse"):
        return ("fuse", "%s: the daemon behind it decides what O_DIRECT "
                        "means" % t)
    if t == "zfs":
        return ("owncache", "zfs reads through its own cache, the ARC, "
                            "which O_DIRECT does not reliably bypass")
    if t == "ext4" and "data=journal" in opts:
        return ("journal", "ext4 mounted data=journal serves O_DIRECT "
                           "through the page cache")
    if "dax" in opts or "dax=always" in opts:
        return ("dax", "mounted with DAX: there is no page cache to compare")
    return None


# The ioctl numbers are worked out rather than written down, because the
# encoding is per architecture: x86 and arm put the direction in two bits
# at 30, powerpc (where the 2026 ext4 writeback bug was seen first), mips,
# sparc and alpha put it in three at 29.  A wrong number is not a crash --
# the ioctl fails and the check is skipped -- but it would be a check
# quietly not made on exactly the boxes most likely to need it.
_MACHINE = os.uname()[4]
if _MACHINE.startswith(("ppc", "powerpc", "mips", "sparc", "alpha")):
    _IOC_SHIFT, _IOC_READ, _IOC_WRITE = 29, 2, 4
else:
    _IOC_SHIFT, _IOC_READ, _IOC_WRITE = 30, 2, 1


def _ioc(read, write, nr, size):
    d = (_IOC_READ if read else 0) | (_IOC_WRITE if write else 0)
    return (d << _IOC_SHIFT) | (size << 16) | (ord("f") << 8) | nr


FS_IOC_GETFLAGS = _ioc(True, False, 1, struct.calcsize("l"))
FS_IOC_FIEMAP = _ioc(True, True, 11, 32)

# Inode flags under which the filesystem serves O_DIRECT from the cache,
# or has no cache to compare.  Compression is decided by the extents
# where they can be read, below, since a flagged file can hold extents
# that were stored raw.
FLAG_REFUSALS = (
    (0x00100000, "verity", "fs-verity: O_DIRECT is served through the "
                           "page cache"),
    (0x00004000, "journal", "data journalling: ext4 serves O_DIRECT "
                            "through the page cache"),
    (0x10000000, "inline", "inline data: kept in the inode and read "
                           "through the cache"),
    (0x00000800, "encrypted", "encrypted with fscrypt: O_DIRECT may be "
                              "served through the cache"),
    (0x02000000, "dax", "DAX: there is no page cache to compare"),
)
FS_COMPR_FL = 0x00000004

FIEMAP_EXTENT_LAST = 0x1
EXTENT_REFUSALS = (
    (0x008, "compressed", "compressed: the filesystem serves O_DIRECT "
                          "reads of it through the cache"),
    (0x200, "inline", "inline data: kept in metadata and read through "
                      "the cache"),
    (0x400, "inline", "tail-packed: read through the cache"),
    (0x080, "encrypted", "encrypted extents: O_DIRECT may be served "
                         "through the cache"),
    (0x100, "inline", "extents not block-aligned: read through the cache"),
)
_FM_HEAD = struct.Struct("=QQIIII")
_FM_EXTENT = struct.Struct("=QQQQQIIII")


def inode_flags(fd):
    try:
        out = fcntl.ioctl(fd, FS_IOC_GETFLAGS, bytes(struct.calcsize("l")))
        return struct.unpack("i", out[:4])[0]
    except (OSError, IOError, OverflowError, ValueError, TypeError):
        return None


def extent_flags(fd, size):
    """Every extent's flags OR'd together, or None if the filesystem will
    not say.  No FIEMAP_FLAG_SYNC: delayed allocation is fine, because an
    O_DIRECT read writes dirty pages back before it reads."""
    if size <= 0:
        return 0
    count = 256
    acc, start = 0, 0
    for _ in range(4096):
        buf = bytearray(_FM_HEAD.size + _FM_EXTENT.size * count)
        _FM_HEAD.pack_into(buf, 0, start, size - start, 0, 0, count, 0)
        try:
            fcntl.ioctl(fd, FS_IOC_FIEMAP, buf, True)
        except (OSError, IOError, OverflowError, ValueError, TypeError):
            return None
        mapped = _FM_HEAD.unpack_from(buf, 0)[3]
        if mapped == 0:
            return acc
        last = None
        for i in range(mapped):
            ext = _FM_EXTENT.unpack_from(buf, _FM_HEAD.size
                                         + i * _FM_EXTENT.size)
            acc |= ext[5]
            last = ext
        if last[5] & FIEMAP_EXTENT_LAST:
            return acc
        start = last[0] + last[2]
        if start >= size:
            return acc
    return acc


def file_refusal(fd, size):
    flags = inode_flags(fd)
    for bit, code, why in FLAG_REFUSALS:
        if flags is not None and flags & bit:
            return code, why
    ext = extent_flags(fd, size)
    if ext is None:
        if flags is not None and flags & FS_COMPR_FL:
            return ("compressed", "marked compressed, and the filesystem "
                                  "will not list its extents to say which "
                                  "are")
        return None
    for bit, code, why in EXTENT_REFUSALS:
        if ext & bit:
            return code, why
    return None


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

class SideError(Exception):
    def __init__(self, side, exc):
        Exception.__init__(self, str(exc))
        self.side = side
        self.exc = exc


def _read_at(fd, off, buf):
    """One read into the aligned buffer.  readv rather than pread because
    pread allocates its own buffer, and O_DIRECT needs this one's
    alignment -- an anonymous mmap is page-aligned -- on every Python
    back to 3.6, where preadv does not exist."""
    os.lseek(fd, off, os.SEEK_SET)
    n = os.readv(fd, [buf])
    return buf[:n]


def _pread_full(fd, off, n):
    out = []
    got = 0
    while got < n:
        b = os.pread(fd, n - got, off + got)
        if not b:
            break
        out.append(b)
        got += len(b)
    return b"".join(out)


class Diff(object):
    """Where two copies of one file differ, and what the difference looks
    like -- which is most of how the cause is told."""

    def __init__(self):
        self.ranges = []
        self.last = None
        self.count = 0
        self.bytes = 0
        self.pages = 0
        self.approx = False
        self.disk_nonzero = False
        self.cache_nonzero = False
        self.bits = 0
        self.first = None

    def _span(self, s, e):
        if self.last is not None and s - self.last[1] <= RANGE_GAP:
            self.last[1] = e
            return
        self.count += 1
        self.last = [s, e]
        if len(self.ranges) < KEEP_RANGES:
            self.ranges.append(self.last)

    def _walk(self, base, dp, cp):
        n = max(len(dp), len(cp))
        start = None
        for i in range(n):
            a = dp[i] if i < len(dp) else None
            b = cp[i] if i < len(cp) else None
            if a != b:
                if start is None:
                    start = i
                    if self.first is None:
                        self.first = (base + i, dp[i:i + 16], cp[i:i + 16])
                self.bytes += 1
                if a:
                    self.disk_nonzero = True
                if b:
                    self.cache_nonzero = True
                if a is not None and b is not None:
                    self.bits += bin(a ^ b).count("1")
            elif start is not None:
                self._span(base + start, base + i)
                start = None
        if start is not None:
            self._span(base + start, base + n)

    def add(self, base, d, c):
        n = max(len(d), len(c))
        for p in range(0, n, PAGE):
            dp, cp = d[p:p + PAGE], c[p:p + PAGE]
            if dp == cp:
                continue
            self.pages += 1
            if self.pages <= BYTE_PAGES:
                self._walk(base + p, dp, cp)
                continue
            self.approx = True
            m = max(len(dp), len(cp))
            self._span(base + p, base + p + m)
            self.bytes += m
            if dp.strip(b"\0"):
                self.disk_nonzero = True
            if cp.strip(b"\0"):
                self.cache_nonzero = True

    def summary(self, size):
        # Whole pages, give or take bytes that match by coincidence at
        # either edge: the unit the page cache works in, so the shape a
        # page-cache bug leaves and a flipped bit or a patch does not.
        def whole_pages(s, e):
            head = s % PAGE
            tail = (PAGE - e % PAGE) % PAGE
            return (e - s >= PAGE - 2 * RANGE_GAP and head <= RANGE_GAP
                    and (tail <= RANGE_GAP or e >= size - RANGE_GAP))
        aligned = bool(self.ranges) and all(whole_pages(s, e)
                                            for s, e in self.ranges)
        first = None
        if self.first is not None:
            first = {"offset": self.first[0], "disk": _hex(self.first[1]),
                     "cache": _hex(self.first[2])}
        return {
            "ranges": [list(r) for r in self.ranges],
            "n_ranges": self.count,
            "bytes": self.bytes,
            "approx": self.approx,
            "disk_zero": not self.disk_nonzero,
            "cache_zero": not self.cache_nonzero,
            "single_bit": (self.count == 1 and self.bytes == 1
                           and self.bits == 1 and not self.approx),
            "page_aligned": aligned,
            "first": first,
        }


def _hex(b):
    return " ".join("%02x" % x for x in bytearray(b))


def read_both(dfd, cfd, buf, algos, diff=None):
    """Both copies, a megabyte at a time and in step, hashed as they go.

    The disk side is read first in each step: an O_DIRECT read writes back
    any dirty pages in its range before reading, so a write still on its
    way to the disk is on it by the time the two are compared, and an
    ordinary pending write is never reported as a difference.
    """
    dh = [(a, hashlib.new(a)) for a in algos]
    ch = [(a, hashlib.new(a)) for a in algos]
    off, dlen, clen, heads = 0, 0, 0, None
    while True:
        try:
            d = _read_at(dfd, off, buf)
        except OSError as exc:
            raise SideError("disk", exc)
        try:
            c = _pread_full(cfd, off, CHUNK)
        except OSError as exc:
            raise SideError("cache", exc)
        if heads is None:
            heads = (d[:65536], c[:65536])
        for _a, h in dh:
            h.update(d)
        for _a, h in ch:
            h.update(c)
        if diff is not None and d != c:
            diff.add(off, d, c)
        dlen += len(d)
        clen += len(c)
        off += CHUNK
        if len(d) < CHUNK and len(c) < CHUNK:
            break
    return ({"disk": dict((a, h.hexdigest()) for a, h in dh),
             "cache": dict((a, h.hexdigest()) for a, h in ch)},
            dlen, clen, heads)


def _usable(algo):
    """md5 is refused outright on a FIPS box, and dpkg records nothing
    else -- so the package's digest goes unused there rather than
    taking the comparison down with it."""
    try:
        hashlib.new(algo)
        return True
    except ValueError:
        return False


def _sig(st):
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def elf_info(head, ranges):
    """Where an ELF program's entry point sits in the file, and whether
    a difference touches it or the header.  A handful of bytes rewritten
    exactly where the program starts running is not what a flipped bit
    or a lost write looks like."""
    if len(head) < 64 or head[:4] != b"\x7fELF":
        return None
    cls, data = bytearray(head[4:6])
    e = "<" if data == 1 else ">" if data == 2 else None
    if e is None or cls not in (1, 2):
        return None
    try:
        if cls == 2:
            entry, phoff = struct.unpack_from(e + "QQ", head, 24)
            phentsize, phnum = struct.unpack_from(e + "HH", head, 54)
        else:
            entry, phoff = struct.unpack_from(e + "II", head, 24)
            phentsize, phnum = struct.unpack_from(e + "HH", head, 42)
        entry_off = None
        for i in range(min(phnum, 128)):
            at = phoff + i * phentsize
            if cls == 2:
                ptype, _fl, poff, vaddr, _pa, filesz = struct.unpack_from(
                    e + "IIQQQQ", head, at)
            else:
                ptype, poff, vaddr, _pa, filesz = struct.unpack_from(
                    e + "IIIII", head, at)
            if ptype == 1 and vaddr <= entry < vaddr + filesz:
                entry_off = poff + (entry - vaddr)
                break
    except struct.error:
        return None
    header = any(s < 64 for s, _e in ranges)
    at_entry = entry_off is not None and any(
        s < entry_off + 16 and e2 > entry_off for s, e2 in ranges)
    return {"entry_off": entry_off, "at_entry": at_entry, "header": header}


class Packages(object):
    """What the package manager recorded about a file when it installed
    it.  Asked only about files that differ, so a clean run never pays
    for it -- and the one question it answers is which copy is right."""

    RPM_ALGOS = {"1": "md5", "2": "sha1", "8": "sha256", "9": "sha384",
                 "10": "sha512", "11": "sha224"}

    def __init__(self, dpkg_info):
        self.dpkg_info = dpkg_info
        self._dpkg = None

    def _load_dpkg(self):
        idx = {}
        for path in sorted(glob.glob(os.path.join(self.dpkg_info,
                                                  "*.md5sums"))):
            pkg = os.path.basename(path)[:-len(".md5sums")]
            for line in (_read(path) or "").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and len(parts[0]) == 32:
                    idx["/" + parts[1].lstrip("/")] = (pkg, parts[0].lower())
        self._dpkg = idx

    @staticmethod
    def aliases(path):
        """A merged /usr means one file has two names, and the package
        database may hold either."""
        out = [path]
        if path.startswith("/usr/"):
            out.append(path[4:])
        elif path.startswith(("/bin/", "/sbin/", "/lib")):
            out.append("/usr" + path)
        return out

    def lookup(self, path):
        names = self.aliases(path)
        if self._dpkg is None:
            self._load_dpkg()
        for n in names:
            if n in self._dpkg:
                pkg, digest = self._dpkg[n]
                return {"manager": "dpkg", "name": pkg, "algo": "md5",
                        "digest": digest}
        out = _run(["rpm", "-qf", "--qf",
                    "[%{FILENAMES}\t%{=FILEDIGESTALGO}\t%{FILEDIGESTS}"
                    "\t%{=NAME}\n]", path])
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[0] in names and parts[2].strip():
                return {"manager": "rpm", "name": parts[3].strip(),
                        "algo": self.RPM_ALGOS.get(parts[1].strip(), "md5"),
                        "digest": parts[2].strip().lower()}
        return None


def _skip(rec, code, reason):
    rec["status"] = "skipped"
    rec["skip"] = code
    rec["reason"] = reason
    return rec


def _open_error(rec, exc, what):
    if exc.errno in (errno.EACCES, errno.EPERM):
        return _skip(rec, "perm", "%s: permission denied" % what)
    if exc.errno == errno.ENOENT:
        return _skip(rec, "gone", "%s: no such file" % what)
    return _skip(rec, "error", "%s: %s" % (what, exc.strerror or exc))


def check_file(path, ctx):
    """Compare one file's two copies and record what it takes to judge
    which one is wrong.  Never raises: a file that cannot be compared is
    a record saying why, not a gap in the report."""
    rec = {"path": path, "status": None}
    try:
        st = os.stat(path)
    except OSError as exc:
        return _open_error(rec, exc, "stat")
    rec.update({"size": st.st_size, "mode": "%o" % stat.S_IMODE(st.st_mode),
                "uid": st.st_uid, "role": role_of(path, st),
                "written_since_boot": changed_since(st, ctx["btime"])})
    if not stat.S_ISREG(st.st_mode):
        return _skip(rec, "notreg", "not a regular file")
    if ctx["max_bytes"] and st.st_size > ctx["max_bytes"]:
        return _skip(rec, "size", "%s, over --max-mb %d"
                     % (_size(st.st_size), ctx["max_bytes"] >> 20))
    m = mount_of(path, ctx["mounts"])
    rec["fstype"] = m["fstype"] if m else None
    rec["mount"] = m["mount"] if m else None
    disk_view = ctx["disk_view"]
    if disk_view is None:
        if ctx["o_direct"] is None:
            return _skip(rec, "platform", "this platform has no O_DIRECT")
        why = fs_refusal(m)
        if why:
            return _skip(rec, why[0], why[1])

    attempt = 0
    while True:
        attempt += 1
        out = _compare_once(path, rec, ctx)
        if out != "moved":
            return out
        if attempt >= 3:
            rec["status"] = "moving"
            rec["reason"] = "changed on every one of %d reads" % attempt
            return rec
        time.sleep(0.1 * attempt)


def _compare_once(path, rec, ctx):
    disk_view = ctx["disk_view"]
    try:
        cfd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        return _open_error(rec, exc, "open")
    dfd = None
    try:
        st0 = os.fstat(cfd)
        if disk_view is None:
            why = file_refusal(cfd, st0.st_size)
            if why:
                return _skip(rec, why[0], why[1])
            try:
                dfd = os.open(path, os.O_RDONLY | ctx["o_direct"])
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    return _skip(rec, "nodirect",
                                 "%s will not be read past its cache "
                                 "(O_DIRECT refused)"
                                 % (rec.get("fstype") or "this filesystem"))
                return _open_error(rec, exc, "open past the cache")
        else:
            try:
                dfd = os.open(os.path.join(disk_view, path.lstrip("/")),
                              os.O_RDONLY)
            except OSError as exc:
                return _open_error(rec, exc, "open in --disk-view")

        diff = Diff()
        try:
            digests, dlen, clen, heads = read_both(
                dfd, cfd, ctx["buf"], ("sha256",), diff)
        except SideError as exc:
            if exc.side == "disk" and exc.exc.errno == errno.EINVAL:
                return _skip(rec, "nodirect",
                             "%s refused a read past its cache"
                             % (rec.get("fstype") or "this filesystem"))
            rec["status"] = "unreadable"
            rec["failed"] = exc.side
            rec["reason"] = exc.exc.strerror or str(exc.exc)
            return rec
        try:
            st1 = os.stat(path)
        except OSError:
            return "moved"
        if _sig(st0) != _sig(st1) or dlen != clen:
            return "moved"
        ctx["bytes"] += dlen
        if digests["disk"]["sha256"] == digests["cache"]["sha256"]:
            rec["status"] = "ok"
            return rec

        # They differ.  Read both again before believing it: a writer
        # that slipped between the two reads is not a finding, a disk
        # that answers twice differently is a different one, and the
        # second pass is where the digest the package manager used gets
        # computed.
        pkg = ctx["packages"].lookup(path) if ctx["packages"] else None
        algos = ["sha256"]
        if pkg and pkg["algo"] not in algos and _usable(pkg["algo"]):
            algos.append(pkg["algo"])
        try:
            again, dlen2, clen2, _h = read_both(dfd, cfd, ctx["buf"],
                                                tuple(algos))
        except SideError:
            again, dlen2, clen2 = None, dlen, clen
        try:
            st2 = os.stat(path)
        except OSError:
            return "moved"
        if _sig(st0) != _sig(st2) or dlen2 != dlen or clen2 != clen:
            return "moved"
        if again is not None and \
                again["disk"]["sha256"] == again["cache"]["sha256"]:
            rec["status"] = "moving"
            rec["reason"] = "differed once, then agreed on a second read"
            return rec

        summary = diff.summary(st0.st_size)
        rec["status"] = "differ"
        rec["diff"] = summary
        rec["disk_sha256"] = digests["disk"]["sha256"]
        rec["cache_sha256"] = digests["cache"]["sha256"]
        if again is not None:
            rec["disk_unstable"] = (again["disk"]["sha256"]
                                    != digests["disk"]["sha256"])
            rec["cache_unstable"] = (again["cache"]["sha256"]
                                     != digests["cache"]["sha256"])
        if pkg:
            a = pkg["algo"]
            match = "neither"
            if again is not None and a in again["disk"]:
                if again["disk"][a] == pkg["digest"]:
                    match = "disk"
                elif again["cache"][a] == pkg["digest"]:
                    match = "cache"
            else:
                match = None
            rec["pkg"] = {"manager": pkg["manager"], "name": pkg["name"],
                          "algo": a, "match": match}
        head = heads[0] if heads[0][:4] == b"\x7fELF" else heads[1]
        rec["elf"] = elf_info(head, summary["ranges"])
        rec["script"] = (heads[0][:2] == b"#!" or heads[1][:2] == b"#!")
        return rec
    finally:
        os.close(cfd)
        if dfd is not None:
            os.close(dfd)


def mapped_by(paths, proc_root):
    """Which processes have each file mapped.  A mapped page cannot be
    evicted -- not by fadvise, not by drop_caches, not even as root --
    so this decides whether a bad cached copy can be thrown away or has
    to wait for a reboot.  Without root only this user's processes can
    be seen, and the record says so."""
    want = dict((p, []) for p in paths)
    try:
        pids = sorted((int(d) for d in os.listdir(proc_root) if d.isdigit()))
    except OSError:
        return None
    for pid in pids:
        txt = _read(os.path.join(proc_root, str(pid), "maps"))
        if not txt:
            continue
        seen = set()
        for line in txt.splitlines():
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5] in want and parts[5] not in seen:
                seen.add(parts[5])
                comm = (_read(os.path.join(proc_root, str(pid), "comm"))
                        or "?").strip()
                want[parts[5]].append({"pid": pid, "comm": comm})
    return want


# ---------------------------------------------------------------------------
# What else this box knows that bears on a cause
# ---------------------------------------------------------------------------

LOG_PATTERNS = (
    ("memory", re.compile(
        r"EDAC .*\b(?:CE|UE)\b|Hardware Error|\bmce: |Machine check|"
        r"Memory failure|HWPoison|Bad page state|BUG: Bad page map", re.I)),
    ("pagecache", re.compile(
        r"VM_BUG_ON_(?:FOLIO|PAGE)|kernel BUG at (?:mm|fs)/|"
        r"page dumped because|BUG: Bad rss-counter")),
    ("filesystem", re.compile(
        r"EXT4-fs error|EXT4-fs warning.*checksum|"
        r"XFS \(.*\): (?:Metadata )?[Cc]orruption|"
        r"XFS \(.*\): metadata I/O error|"
        r"BTRFS (?:error|warning|critical).*(?:csum|checksum|corrupt)|"
        r"F2FS-fs.*(?:inconsistent|corrupt)|SQUASHFS error")),
    ("storage", re.compile(
        r"I/O error|blk_update_request|medium error|"
        r"ata\d+\.\d+: failed command", re.I)),
)


def read_kernel_log():
    """The kernel's own reports of memory, page-cache, filesystem and
    storage trouble since boot.  Adapted from why_slow.py's
    read_kernel_log -- same sources in the same order, different
    patterns -- so not a verbatim copy.  Losing it is reported, never
    read as "nothing found"."""
    text, source = None, None
    out = _run(["dmesg"])
    if out is not None:
        text, source = out, "dmesg"
    if text is None:
        out = _run(["journalctl", "-k", "-b", "--no-pager", "-q",
                    "-n", "20000"])
        if out is not None:
            text, source = out, "journalctl"
    if text is None:
        for path in ("/var/log/kern.log", "/var/log/messages"):
            out = _read(path)
            if out is not None:
                text, source = out[-500000:], path
                break
    if text is None:
        return None
    found = {"source": source}
    for key, _rx in LOG_PATTERNS:
        found[key] = {"count": 0, "lines": []}
    for line in text.splitlines():
        for key, rx in LOG_PATTERNS:
            if rx.search(line):
                found[key]["count"] += 1
                if len(found[key]["lines"]) < 5:
                    found[key]["lines"].append(line.strip()[:200])
                break
    return found


def read_edac(sys_root):
    mcs = sorted(glob.glob(os.path.join(sys_root, "devices", "system",
                                        "edac", "mc", "mc[0-9]*")))
    if not mcs:
        return None
    ce = ue = 0
    for mc in mcs:
        ce += _read_int(os.path.join(mc, "ce_count"), 0) or 0
        ue += _read_int(os.path.join(mc, "ue_count"), 0) or 0
    return {"controllers": len(mcs), "ce": ce, "ue": ue}


def read_md(sys_root):
    out = []
    for d in sorted(glob.glob(os.path.join(sys_root, "block", "md*", "md"))):
        out.append({
            "name": os.path.basename(os.path.dirname(d)),
            "level": (_read(os.path.join(d, "level")) or "").strip() or None,
            "mismatch_cnt": _read_int(os.path.join(d, "mismatch_cnt")),
        })
    return out


# The modules that are the entry points of the 2026 page-cache write
# bugs.  An exploit loads them on demand, so one loaded on a box that
# has no IPsec or AF_ALG users is itself a fact worth reading.
ENTRY_MODULES = ("algif_aead", "esp4", "esp6", "rxrpc", "xt_TEE",
                 "act_pedit")
# What each is for, so the advice to block it says what that breaks, and
# the init function a built-in one is switched off by.
ENTRY_USES = {"algif_aead": "AF_ALG crypto sockets", "esp4": "IPsec",
              "esp6": "IPsec over IPv6", "rxrpc": "AFS",
              "xt_TEE": "the iptables TEE target",
              "act_pedit": "tc packet editing"}
ENTRY_INITCALLS = {"algif_aead": "algif_aead_init", "esp4": "esp4_init",
                   "esp6": "esp6_init", "rxrpc": "af_rxrpc_init",
                   "xt_TEE": "tee_tg_init", "act_pedit": "pedit_init_module"}


def _mod_names(text, sep):
    out = set()
    for line in (text or "").splitlines():
        name = os.path.basename(line.split(sep, 1)[0].strip())
        name = re.sub(r"\.ko(?:\.[a-z]+)?$", "", name)
        if name:
            out.add(name.replace("-", "_"))
    return out


def read_modules(proc_root, release):
    loaded_txt = _read(os.path.join(proc_root, "modules"))
    if loaded_txt is None:
        return None
    loaded = set(line.split()[0] for line in loaded_txt.splitlines()
                 if line.strip())
    base = os.path.join("/lib/modules", release or "")
    builtin = _mod_names(_read(os.path.join(base, "modules.builtin")), "\n")
    avail = _mod_names(_read(os.path.join(base, "modules.dep")), ":")
    out = {}
    for m in ENTRY_MODULES:
        if m in loaded:
            out[m] = "loaded"
        elif m in builtin:
            out[m] = "built in"
        elif m in avail:
            out[m] = "available"
        else:
            out[m] = "absent"
    return out


def read_userns(proc_root):
    """Whether an unprivileged user can make a user namespace -- which is
    how pedit COW gets the CAP_NET_ADMIN it needs."""
    k = os.path.join(proc_root, "sys", "kernel")
    clone = _read_int(os.path.join(k, "unprivileged_userns_clone"))
    maxns = _read_int(os.path.join(proc_root, "sys", "user",
                                   "max_user_namespaces"))
    aa = _read_int(os.path.join(k, "apparmor_restrict_unprivileged_userns"))
    if clone == 0 or maxns == 0 or aa == 1:
        return False
    if clone is None and maxns is None:
        return None
    return True


def read_btime(proc_root):
    for line in (_read(os.path.join(proc_root, "stat")) or "").splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def collect_facts(args):
    f = {}
    try:
        f["sys.hostname"] = socket.gethostname()
    except OSError:
        f["sys.hostname"] = None
    f["sys.kernel"] = (_read(os.path.join(args.proc_root, "sys", "kernel",
                                          "osrelease")) or "").strip() or None
    f["sys.btime"] = read_btime(args.proc_root)
    f["sys.euid"] = os.geteuid()
    f["run.targets"] = "named" if args.paths else "default"
    f["run.max_mb"] = args.max_mb
    f["run.disk_view"] = args.disk_view

    if args.paths:
        paths = explicit_targets(args.paths, args.one_fs)
    else:
        paths = default_targets(args.proc_root, f["sys.btime"])
    ctx = {
        "btime": f["sys.btime"],
        "max_bytes": args.max_mb << 20,
        "mounts": read_mounts(args.proc_root),
        "disk_view": args.disk_view,
        "o_direct": getattr(os, "O_DIRECT", None),
        "buf": mmap.mmap(-1, CHUNK),
        "packages": Packages(args.dpkg_info),
        "bytes": 0,
    }
    files = [check_file(p, ctx) for p in dedupe(paths)]
    f["run.bytes_read"] = ctx["bytes"]

    bad = [x["path"] for x in files if x["status"] in ("differ", "unreadable")]
    if bad:
        maps = mapped_by(bad, args.proc_root)
        for x in files:
            if x["path"] in bad:
                x["mapped"] = None if maps is None else maps.get(x["path"])
    f["files.all"] = files

    f["ctx.mapped_complete"] = os.geteuid() == 0
    f["ctx.edac"] = read_edac(args.sys_root)
    f["ctx.taint"] = _read_int(os.path.join(args.proc_root, "sys", "kernel",
                                            "tainted"))
    f["ctx.kernel_log"] = read_kernel_log()
    f["ctx.modules"] = read_modules(args.proc_root, f["sys.kernel"])
    f["ctx.userns"] = read_userns(args.proc_root)
    f["ctx.md"] = read_md(args.sys_root)
    return f


# ---------------------------------------------------------------------------
# Judging a difference
# ---------------------------------------------------------------------------
#
# Every rule below reads these judgements, and every judgement is a pure
# function of the fact dictionary, so --from-facts reproduces any of them
# with no disk being read.
#
# Which copy is wrong is decided by the strongest evidence available, in
# this order:
#
#   1. the package manager's recorded digest, when it matches one copy;
#   2. zeros on one side where the other has data -- a write that never
#      landed, or a page that was emptied;
#   3. whether the file was written since boot.  If it was not, its
#      cache was filled by reading this very disk, so a cache that now
#      disagrees with it changed in memory afterwards.
#
# Nothing else is guessed: a difference none of those can place is
# reported as one, with the reasons it could not be placed.

# Bugs that write into the page cache of a file the attacker may only
# read, with the modules that are their entry points.  Named when a
# privileged file is found altered in memory, as candidates: whether a
# kernel carries a fix cannot be read from its version, since
# distributions backport, so these say what to check the vendor's
# advisory for, never what happened.
#   Copy Fail     https://cert.europa.eu/publications/security-advisories/2026-005/
#   Dirty Frag    https://edera.dev/stories/dirty-frag-the-linux-kernel-exploit-that-turns-your-page-cache-against-you
#   Fragnesia     https://socprime.com/blog/cve-2026-46300-fragnesia-linux-kernel-flaw/
#   DirtyClone,
#   pedit COW     https://thecybersecguru.com/news/linux-lpe-pedit-cow-dirtyclone-cve-2026-46331-cve-2026-43503/
CACHE_WRITERS = (
    ("Copy Fail", "CVE-2026-31431", ("algif_aead",)),
    ("Dirty Frag", "CVE-2026-43284, CVE-2026-43500", ("esp4", "esp6",
                                                      "rxrpc")),
    ("Fragnesia", "CVE-2026-46300", ("esp4", "esp6")),
    ("DirtyClone", "CVE-2026-43503", ("esp4", "esp6", "xt_TEE")),
    ("pedit COW", "CVE-2026-46331", ("act_pedit",)),
)
# Dirty Pipe (CVE-2022-0847) needs no module: every kernel in these
# [first, fixed) ranges has it.
DIRTY_PIPE = (((5, 8, 0), (5, 10, 102)), ((5, 11, 0), (5, 15, 25)),
              ((5, 16, 0), (5, 16, 11)))

# Writeback bugs that leave the disk holding something other than what
# was written, by filesystem and the upstream versions they were
# reported on.  Same caveat: a distribution kernel can carry the fix at
# an older number.
#   ext4    https://github.com/coreos/fedora-coreos-tracker/issues/2234
#   btrfs   https://ratatoskr.run/linux-btrfs/2026/09/17489069/t
WRITEBACK_BUGS = (
    ("ext4", (7, 1, 0), (7, 1, 11),
     "ext4's large-folio writeback bug (written ranges left zeroed on "
     "disk)"),
    ("ext4", (7, 2, 0), None,
     "ext4's large-folio writeback bug (written ranges left zeroed on "
     "disk; seen up to at least 7.2.7)"),
    ("btrfs", (7, 2, 0), None,
     "btrfs's 7.2 writeback regression (folios no longer write-protected "
     "under writeback: checksum errors, lost writes)"),
)

TAINT_FLAGS = ((4, "M", "a machine check -- a hardware error"),
               (5, "B", "a page found in a bad state"),
               (7, "D", "an oops or a BUG"))


def kernel_version(release):
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", release or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _vstr(v):
    return "%d.%d.%d" % v


def writeback_candidates(fstype, release):
    v = kernel_version(release)
    out = []
    if v is None:
        return out
    for fs, first, fixed, what in WRITEBACK_BUGS:
        if fs != fstype or v < first or (fixed and v >= fixed):
            continue
        tail = ("; fixed upstream in %s" % _vstr(fixed)) if fixed else ""
        out.append("this kernel (%s) is in the range where %s was "
                   "reported%s -- check your vendor's advisory, since a "
                   "distribution kernel can carry the fix at an older "
                   "number" % (release, what, tail))
    return out


def writer_candidates(f):
    """The 2026 page-cache writers whose entry points this kernel has,
    and Dirty Pipe if the version is in its range."""
    mods = f.get("ctx.modules") or {}
    present, out = {}, []
    for name, cve, modules in CACHE_WRITERS:
        here = [m for m in modules if mods.get(m) in ("loaded", "built in")]
        for m in here:
            present.setdefault(m, []).append(name)
    if present:
        out.append("entry points present here: " + ", ".join(
            "%s %s (%s)" % (m, mods[m], ", ".join(present[m]))
            for m in sorted(present)))
    elif mods:
        avail = [m for m in ENTRY_MODULES if mods.get(m) == "available"]
        if avail:
            out.append("none of their modules is loaded, though %s can be "
                       "loaded on demand -- which is how an exploit gets "
                       "them" % ", ".join(avail))
    if mods.get("act_pedit") in ("loaded", "built in") \
            and f.get("ctx.userns"):
        out.append("unprivileged user namespaces are enabled, which is how "
                   "pedit COW gets CAP_NET_ADMIN")
    v = kernel_version(f.get("sys.kernel"))
    if v is not None and any(a <= v < b for a, b in DIRTY_PIPE):
        out.append("this kernel's version (%s) is in Dirty Pipe's range "
                   "(CVE-2022-0847)" % f.get("sys.kernel"))
    return out


def _taint_letters(f):
    t = f.get("ctx.taint")
    if not t:
        return []
    return [(letter, what) for bit, letter, what in TAINT_FLAGS
            if t & (1 << bit)]


def _log_hint(f, keys):
    log = f.get("ctx.kernel_log") or {}
    out = []
    for key in keys:
        ent = log.get(key) or {}
        if ent.get("count"):
            out.append("%d %s line%s in the kernel log, e.g. \"%s\""
                       % (ent["count"], key, _s(ent["count"]),
                          (ent.get("lines") or ["?"])[0][:120]))
    return out


def memory_cause(f):
    e = f.get("ctx.edac")
    if e and (e.get("ue") or e.get("ce")):
        return ("failing memory -- a single flipped bit, and the memory "
                "controller has logged %d corrected and %d uncorrected "
                "errors since boot" % (e.get("ce") or 0, e.get("ue") or 0))
    if "M" in [t[0] for t in _taint_letters(f)]:
        return ("failing hardware -- a single flipped bit, and the kernel has "
                "recorded a machine check since boot")
    if e is None:
        return ("memory, most likely -- one flipped bit is the signature of "
                "hardware, not software, and no EDAC driver is loaded "
                "here, so memory errors would go unreported")
    return ("a flipped bit, the signature of hardware -- though EDAC has "
            "logged no memory errors, so if this memory has ECC the flip "
            "happened somewhere ECC does not cover")


def judge(x, f):
    """Which copy of a differing file is wrong, on what evidence, and the
    likely cause, as plain sentences plus the supporting facts."""
    d = x.get("diff") or {}
    pkg = x.get("pkg") or {}
    role = x.get("role")
    j = {"path": x.get("path"), "wrong": None, "basis": None,
         "cause": None, "hints": []}

    if x.get("status") == "unreadable":
        side = x.get("failed")
        j["kind"] = "unreadable" if side == "disk" else "unreadable-cache"
        j["wrong"] = side
        j["basis"] = "reading %s returned %s" % (
            "past the cache" if side == "disk" else "through the cache",
            x.get("reason") or "an error")
        j["cause"] = ("the storage under it -- a read straight from the device "
                      "failed while the cached copy still reads"
                      if side == "disk" else
                      "the read through the cache failed while the disk "
                      "read did not -- the cached pages themselves are bad, "
                      "or the filesystem refused them")
        j["hints"] = _log_hint(f, ("storage", "filesystem", "memory"))
        return j

    if x.get("disk_unstable"):
        j["kind"] = "unstable"
        j["wrong"] = "disk"
        j["basis"] = "two reads past the cache returned different bytes"
        j["cause"] = ("the storage is not returning stable data -- a failing "
                      "device, mirror halves that disagree, or a device "
                      "another machine is writing to")
        md = [m for m in (f.get("ctx.md") or []) if m.get("mismatch_cnt")]
        for m in md:
            j["hints"].append("%s (%s) reports mismatch_cnt %d: its copies "
                              "disagree" % (m["name"], m.get("level") or "?",
                                            m["mismatch_cnt"]))
        j["hints"] += _log_hint(f, ("storage", "filesystem"))
        return j

    if pkg.get("match") == "disk":
        wrong = "cache"
        basis = ("the disk matches the %s %s records for it; the cache does "
                 "not" % (pkg.get("algo"), pkg.get("name")))
    elif pkg.get("match") == "cache":
        wrong = "disk"
        basis = ("the cache matches the %s %s records for it; the disk does "
                 "not" % (pkg.get("algo"), pkg.get("name")))
    elif d.get("disk_zero") and not d.get("cache_zero"):
        wrong = "disk"
        basis = "the disk reads zeros where the cache holds data"
    elif x.get("written_since_boot") is False:
        wrong = "cache"
        basis = ("it has not been written since boot, so its cache was "
                 "filled from this disk and has changed in memory since")
    elif d.get("cache_zero") and not d.get("disk_zero"):
        wrong = "cache"
        basis = "the cache reads zeros where the disk holds data"
    else:
        wrong = None
        why = []
        if not pkg:
            why.append("no package records it")
        else:
            why.append("it matches neither copy of the %s %s records"
                       % (pkg.get("algo"), pkg.get("name")))
        if x.get("written_since_boot"):
            why.append("it was written since boot")
        elif x.get("written_since_boot") is None:
            why.append("the boot time is unknown")
        basis = "nothing here can say which copy is right: " + \
            ", and ".join(why)
    j["wrong"], j["basis"] = wrong, basis

    elf = x.get("elf") or {}
    where = ""
    if elf.get("header"):
        where = " over its ELF header"
    elif elf.get("at_entry"):
        where = " at the program's entry point"

    if wrong == "cache":
        if d.get("single_bit"):
            j["kind"] = "corrupt"
            j["cause"] = memory_cause(f)
        elif role in PRIVILEGED and not (d.get("cache_zero")
                                         and d.get("page_aligned")):
            j["kind"] = "tampered"
            j["cause"] = ("a write into the page cache -- %s rewritten in "
                          "memory%s, the disk untouched -- how Copy Fail, "
                          "Dirty Frag, Fragnesia and pedit COW (2026) turn "
                          "a local user into root"
                          % (_size(d.get("bytes") or 0), where))
            j["hints"] = writer_candidates(f)
        elif d.get("cache_zero") or d.get("page_aligned"):
            j["kind"] = "corrupt"
            j["cause"] = ("a kernel bug in the page cache -- whole pages served "
                          "wrong, which is not the shape of a flipped bit or "
                          "of a targeted write")
            j["hints"] = writeback_candidates(x.get("fstype"),
                                              f.get("sys.kernel"))
            j["hints"] += _log_hint(f, ("pagecache", "memory"))
        elif (d.get("bytes") or 0) <= PAGE:
            j["kind"] = "corrupt"
            j["cause"] = ("%s rewritten in place in memory%s -- the way the "
                          "2026 page-cache write bugs work, here on a file "
                          "nothing privileged need run, so possibly a "
                          "test of one, possibly a kernel bug"
                          % (_size(d.get("bytes") or 0), where))
            j["hints"] = writer_candidates(f)
        else:
            j["kind"] = "corrupt"
            j["cause"] = ("corruption in memory whose shape does not point "
                          "at one cause")
            j["hints"] = _log_hint(f, ("memory", "pagecache"))
    elif wrong == "disk":
        j["kind"] = "disk"
        if d.get("disk_zero") and x.get("written_since_boot"):
            j["cause"] = ("a write that never reached the disk -- the cache "
                          "holds what was written, the disk holds zeros")
        elif d.get("disk_zero"):
            j["cause"] = ("the blocks were zeroed on disk after this file "
                          "was last read, by something that did not go "
                          "through this kernel's cache")
        elif x.get("written_since_boot"):
            j["cause"] = ("the disk holds something other than what was "
                          "last written -- a lost or misdirected write")
        else:
            j["cause"] = ("the disk changed under the cache without going "
                          "through this kernel -- a device shared with "
                          "another machine, a hypervisor writing the image, "
                          "a raw write to the device -- or storage "
                          "returning the wrong blocks")
        j["hints"] = writeback_candidates(x.get("fstype"),
                                          f.get("sys.kernel"))
        for m in (f.get("ctx.md") or []):
            if m.get("mismatch_cnt"):
                j["hints"].append("%s (%s) reports mismatch_cnt %d: its "
                                  "copies disagree" % (
                                      m["name"], m.get("level") or "?",
                                      m["mismatch_cnt"]))
        j["hints"] += _log_hint(f, ("storage", "filesystem"))
    else:
        j["kind"] = "differ"
        if d.get("single_bit"):
            j["cause"] = memory_cause(f)
        elif role in PRIVILEGED and (d.get("bytes") or 0) <= PAGE:
            j["cause"] = ("%s rewritten%s in a %s file is the shape of a "
                          "page-cache write" % (_size(d.get("bytes") or 0),
                                                where, role))
            j["hints"] = writer_candidates(f)
        else:
            j["cause"] = "not identifiable from the bytes alone"
            j["hints"] = _log_hint(f, ("memory", "pagecache", "storage",
                                       "filesystem"))
    return j


_JUDGED = {}


def judged(f):
    """[(file record, judgement)] for every file that differs, worked out
    once per fact dictionary however many rules ask."""
    key = id(f)
    hit = _JUDGED.get(key)
    if hit is not None and hit[0] is f:
        return hit[1]
    out = []
    for x in f.get("files.all") or []:
        if x.get("status") in ("differ", "unreadable"):
            out.append((x, judge(x, f)))
    _JUDGED[key] = (f, out)
    return out


def _of_kind(f, *kinds):
    return [(x, j) for x, j in judged(f) if j["kind"] in kinds]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

Rule = namedtuple("Rule", "id title needs level say fix why")

MISSING_REASONS = {
    "files.all": "no files were looked at",
    "ctx.edac": "no EDAC driver is loaded here, so memory errors, if any, "
                "go unreported",
    "ctx.taint": "/proc/sys/kernel/tainted could not be read",
    "ctx.kernel_log": "the kernel log could not be read -- rerun with sudo, "
                      "or without --no-exec",
}

RULES = []


def rule(rid, title, needs, why):
    def deco(fn):
        level, say, fix = fn()
        RULES.append(Rule(rid, title, needs, level, say, fix, why))
        return fn
    return deco


def _s(n):
    return "" if n == 1 else "s"


def _size(n):
    if n < 1024:
        return "%d byte%s" % (n, _s(n))
    for unit, div in (("KiB", 1 << 10), ("MiB", 1 << 20), ("GiB", 1 << 30)):
        if n < div * 1024 or unit == "GiB":
            return "%.1f %s" % (float(n) / div, unit)
    return "%d bytes" % n


def _paths(items, n=3):
    shown = [x["path"] for x, _j in items[:n]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (" (+%d more)" % more if more > 0 else "")


def _more(items):
    n = len(items) - 1
    return " (+%d more file%s)" % (n, _s(n)) if n > 0 else ""


def _where(x):
    d = x.get("diff") or {}
    first = (d.get("ranges") or [[None]])[0][0]
    if first is None:
        return ""
    s = " at 0x%x" % first
    if d.get("n_ranges", 0) > 1:
        s += " and %d more range%s" % (d["n_ranges"] - 1,
                                       _s(d["n_ranges"] - 1))
    return s


def _amount(x):
    d = x.get("diff") or {}
    return "%s%s" % ("~" if d.get("approx") else "", _size(d.get("bytes", 0)))


def _hints(hints, n):
    """Supporting facts, as sentences inside a fix."""
    if not hints:
        return ""
    text = "; ".join(hints[:n])
    return "  " + text[0].upper() + text[1:] + "."


def _has_disk_wrong(f):
    return bool(_of_kind(f, "disk", "unstable", "unreadable"))


def _evict_advice(x, f):
    """How to throw a bad cached copy away -- or why that will not work
    yet.  Never drop_caches while some other file's only good copy is in
    memory: it would throw that away too."""
    mapped = x.get("mapped")
    path = x["path"]
    if mapped:
        who = ", ".join("%s (%d)" % (m["comm"], m["pid"]) for m in mapped[:3])
        return ("%s is mapped by %s, and a mapped page cannot be evicted -- "
                "restart them, or reboot, to make the kernel read it from "
                "disk again." % (path, who))
    s = ("`dd if=%s iflag=nocache count=0` evicts it, and the next read "
         "comes from the disk." % path)
    if mapped is not None and not f.get("ctx.mapped_complete"):
        s += ("  Without root only your own processes' mappings were "
              "visible; if a root process has it mapped, the eviction "
              "will not take until it restarts.")
    return s


# 1 ------------------------------------------------------------------------
@rule("DISK_UNSTABLE", "disk gives two answers", ("files.all",),
      "Two reads straight from the device returning different bytes means "
      "nothing read from this storage can be trusted, whatever is cached. "
      "It outranks every other difference because it is a cause: a file "
      "that reads two ways from disk will disagree with its cache too.")
def _disk_unstable():
    def level(f):
        return CRITICAL if _of_kind(f, "unstable") else None

    def say(f):
        items = _of_kind(f, "unstable")
        return "%s: two reads past the cache returned different bytes" \
            % _paths(items)

    def fix(f):
        x, j = _of_kind(f, "unstable")[0]
        s = ("The storage under %s is not returning the same data twice.  "
             "`smartctl -a` on the device, and the kernel log for it, come "
             "first; if it is a mirror, compare its halves; if the device "
             "is shared, find the other machine writing to it."
             % x["path"])
        return s + _hints(j["hints"], 2)
    return level, say, fix


# 2 ------------------------------------------------------------------------
@rule("DISK_UNREADABLE", "disk cannot be read", ("files.all",),
      "A file whose disk copy cannot be read while its cached copy can is a "
      "file one eviction away from being lost: the page cache holds the "
      "only readable copy.")
def _disk_unreadable():
    def level(f):
        return CRITICAL if _of_kind(f, "unreadable", "unreadable-cache") \
            else None

    def say(f):
        items = _of_kind(f, "unreadable", "unreadable-cache")
        x, j = items[0]
        return "%s: %s" % (_paths(items), j["basis"])

    def fix(f):
        x, j = _of_kind(f, "unreadable", "unreadable-cache")[0]
        if j["kind"] == "unreadable":
            return ("Copy %s somewhere safe now -- `cp -p %s ~/` reads the "
                    "cached copy -- before anything evicts it: no reboot, no "
                    "drop_caches.  Then look at the device: `smartctl -a`, "
                    "and the kernel log for the I/O error."
                    % (x["path"], x["path"]))
        return ("The cached pages of %s cannot be read while the disk can.  "
                "Evict them -- %s" % (x["path"], _evict_advice(x, f)))
    return level, say, fix


# 3 ------------------------------------------------------------------------
@rule("DISK_WRONG", "the disk copy is wrong", ("files.all",),
      "The cache holds the right bytes and the disk does not. Everything "
      "reads correctly until the page is evicted -- by memory pressure, a "
      "cache drop, or the next reboot -- and from then on the file is "
      "corrupt, with nothing to say when it happened.")
def _disk_wrong():
    def level(f):
        return CRITICAL if _of_kind(f, "disk") else None

    def say(f):
        items = _of_kind(f, "disk")
        x, j = items[0]
        return "%s: %s wrong on disk%s, cache intact%s" % (
            x["path"], _amount(x), _where(x), _more(items))

    def fix(f):
        x, j = _of_kind(f, "disk")[0]
        pkg = x.get("pkg") or {}
        s = ("The page cache holds the only good copy of %s.  Copy it out "
             "now -- `cp -p %s ~/` reads the cached bytes -- "
             "and do not reboot, drop caches or unmount first: any of them "
             "throws that copy away." % (x["path"], x["path"]))
        if pkg.get("name"):
            s += ("  Then rewrite it -- reinstalling %s does -- and run "
                  "plumb on it again." % pkg["name"])
        else:
            s += "  Then write it back from that copy and run plumb on it again."
        s += "  Likely cause: %s." % j["cause"]
        return s + _hints(j["hints"], 2)
    return level, say, fix


# 4 ------------------------------------------------------------------------
@rule("CACHE_TAMPERED", "privileged file altered", ("files.all",),
      "A setuid program, a PAM module, the loader or an auth file whose "
      "cached copy differs from an intact disk copy is running someone "
      "else's bytes with privilege. The 2026 page-cache bugs -- Copy Fail, "
      "Dirty Frag, Fragnesia, pedit COW -- all work this way, and none of "
      "them leaves anything on disk: the evidence is in memory, and goes "
      "when the page does.")
def _cache_tampered():
    def level(f):
        return CRITICAL if _of_kind(f, "tampered") else None

    def say(f):
        items = _of_kind(f, "tampered")
        x, j = items[0]
        return "%s (%s): %s altered in memory%s, disk intact%s" % (
            x["path"], x.get("role"), _amount(x), _where(x), _more(items))

    def fix(f):
        x, j = _of_kind(f, "tampered")[0]
        s = ("Treat this box as compromised until shown otherwise: anyone "
             "who ran %s since it changed ran the altered bytes.  Keep the "
             "evidence before it goes -- `plumb --json evidence.json %s`, "
             "and `cat %s > ~/%s.evidence` saves the altered bytes as a "
             "plain file -- then check who was logged in (`last`, the auth "
             "log).  Then %s"
             % (x["path"], x["path"], x["path"],
                os.path.basename(x["path"]), _evict_advice(x, f)))
        if _has_disk_wrong(f):
            s += ("  Do not reboot or drop caches yet: another file here "
                  "has its only good copy in memory.")
        s += _hints(j["hints"], 3)
        return s + "  " + _block_advice(f)
    return level, say, fix


def _block_advice(f):
    """How to shut the entry points until the kernel is patched.  A
    modprobe rule does nothing for a module that is built in, which on
    the RHEL family algif_aead is; that one takes a boot parameter."""
    mods = f.get("ctx.modules") or {}
    loadable = [m for m in ENTRY_MODULES
                if mods.get(m) in ("loaded", "available")]
    builtin = [m for m in ENTRY_MODULES if mods.get(m) == "built in"]
    parts = []
    if loadable:
        parts.append("`install %s /bin/false` in /etc/modprobe.d, one line "
                     "each for %s, then unload the loaded ones"
                     % (loadable[0], ", ".join(loadable)))
    if builtin:
        parts.append("`initcall_blacklist=%s` on the kernel command line "
                     "for %s, which %s built in" % (
                         ",".join(ENTRY_INITCALLS[m] for m in builtin),
                         ", ".join(builtin),
                         "is" if len(builtin) == 1 else "are"))
    if not parts:
        return "Patch the kernel."
    return ("Patch the kernel; until then, close the entry points this box "
            "does not use: %s.  Each one breaks what it is for -- %s."
            % ("; and ".join(parts),
               ", ".join("%s (%s)" % (m, ENTRY_USES[m])
                         for m in loadable + builtin)))


# 5 ------------------------------------------------------------------------
@rule("CACHE_CORRUPT", "corrupted in memory", ("files.all",),
      "The disk copy is right and the copy every program reads is not. "
      "What the difference looks like says why: one flipped bit is "
      "hardware, whole pages of zeros or of the wrong data are a kernel "
      "bug, a few bytes rewritten in place is a page-cache write.")
def _cache_corrupt():
    def level(f):
        return CRITICAL if _of_kind(f, "corrupt") else None

    def say(f):
        items = _of_kind(f, "corrupt")
        x, j = items[0]
        return "%s: %s wrong in memory%s, disk intact%s" % (
            x["path"], _amount(x), _where(x), _more(items))

    def fix(f):
        x, j = _of_kind(f, "corrupt")[0]
        s = "Likely cause: %s." % j["cause"] + _hints(j["hints"], 2)
        d = x.get("diff") or {}
        if d.get("single_bit"):
            s += ("  `edac-util -v` or `ras-mc-ctl --errors` names the "
                  "DIMM, where there is ECC to report it; a memory test "
                  "is the answer where there is not.")
        s += "  To make programs read the good copy again: "
        s += _evict_advice(x, f)
        if _has_disk_wrong(f):
            s += ("  Not drop_caches: another file here has its only good "
                  "copy in memory.")
        return s
    return level, say, fix


# 6 ------------------------------------------------------------------------
@rule("CACHE_DISK_DIFFER", "cache and disk disagree", ("files.all",),
      "A file that reads one way through the cache and another from the "
      "disk is wrong in one of them, and when no package digest, no zeros "
      "and no boot time can say which, the report says so rather than "
      "guessing.")
def _cache_disk_differ():
    def level(f):
        return CRITICAL if _of_kind(f, "differ") else None

    def say(f):
        items = _of_kind(f, "differ")
        x, j = items[0]
        return "%s: %s different%s%s" % (
            x["path"], _amount(x), _where(x), _more(items))

    def fix(f):
        x, j = _of_kind(f, "differ")[0]
        return ("One of the two copies of %s is wrong, and %s.  Compare "
                "both against another box that has the same file -- `agree "
                "hosts --servers prod.txt -- sha256sum %s` -- or against the "
                "application's own checksums; whichever copy matches is the "
                "good one.  Until you know, keep the cached copy (`cp -p %s "
                "~/`) and do not drop caches.  Likely cause: %s."
                % (x["path"], j["basis"].split(": ", 1)[-1], x["path"],
                   x["path"], j["cause"]))
    return level, say, fix


# 7 ------------------------------------------------------------------------
@rule("NOTHING_COMPARED", "nothing was compared", ("files.all",),
      "A run in which every file was skipped looks exactly like a clean "
      "one, and is the opposite: it says nothing about whether this box "
      "reads what is on its disk.")
def _nothing_compared():
    def level(f):
        compared = [x for x in f["files.all"]
                    if x.get("status") in ("ok", "differ", "unreadable")]
        return WARN if not compared else None

    def say(f):
        n = len(f["files.all"])
        if not n:
            return "no files to compare"
        return "all %d file%s skipped" % (n, _s(n))

    def fix(f):
        if not f["files.all"]:
            return ("Nothing matched.  Name the files or directories to "
                    "check: `%s /usr/bin /etc`." % PROG)
        return ("Every file was skipped -- the NOT CHECKED reasons above "
                "say why.  %s" % (
                    "Run it with sudo." if f.get("sys.euid") else
                    "Point it at files on a local disk."))
    return level, say, fix


# 8 ------------------------------------------------------------------------
@rule("MEMORY_ERRORS", "memory reporting errors", ("ctx.edac",),
      "Cached file data lives in RAM. A memory controller that is "
      "correcting errors is one that will sooner or later meet one it "
      "cannot correct, and an uncorrected error in a page-cache page is a "
      "file that reads wrong with nothing on disk to show for it.")
def _memory_errors():
    def level(f):
        e = f["ctx.edac"]
        if e.get("ue"):
            return CRITICAL
        return WARN if e.get("ce") else None

    def say(f):
        e = f["ctx.edac"]
        return ("%d corrected, %d uncorrected error%s since boot (EDAC, %d "
                "controller%s)" % (e.get("ce") or 0, e.get("ue") or 0,
                                   _s(e.get("ue") or 0),
                                   e.get("controllers") or 0,
                                   _s(e.get("controllers") or 0)))

    def fix(f):
        e = f["ctx.edac"]
        s = ("`edac-util -v` or `ras-mc-ctl --errors` names the DIMM.  ")
        if e.get("ue"):
            s += ("Uncorrected errors mean data was corrupted and not "
                  "repaired: replace the module, and check what this box "
                  "wrote since they started.")
        else:
            s += ("Corrected errors are a module on its way out; plan the "
                  "replacement before they stop being corrected.")
        return s
    return level, say, fix


# 9 ------------------------------------------------------------------------
@rule("KERNEL_TAINT", "kernel flagged a fault", ("ctx.taint",),
      "The kernel taints itself when it sees a machine check, a page in a "
      "bad state, or an oops. Any of those since boot is a reason to "
      "distrust what it has cached, and the flag outlives the log line "
      "that explained it.")
def _kernel_taint():
    def level(f):
        return WARN if _taint_letters(f) else None

    def say(f):
        return "tainted %s: %s" % (
            "".join(t[0] for t in _taint_letters(f)),
            "; ".join(t[1] for t in _taint_letters(f)))

    def fix(f):
        letters = [t[0] for t in _taint_letters(f)]
        if "M" in letters:
            return ("A machine check is the hardware reporting its own "
                    "error.  `ras-mc-ctl --summary` or the mcelog says "
                    "which part.")
        return ("The kernel log since boot holds the BUG or bad-page report "
                "that set this -- `dmesg | grep -iE 'bad page|BUG|oops'` -- "
                "and the stack in it names the subsystem.")
    return level, say, fix


# 10 -----------------------------------------------------------------------
@rule("KERNEL_LOG", "kernel logged corruption", ("ctx.kernel_log",),
      "Filesystems that checksum their data, the memory controller and the "
      "page-cache code all say so in the kernel log when they find "
      "something wrong. Those lines are evidence for a cause even when "
      "every file compared here agrees.")
def _kernel_log():
    keys = ("memory", "pagecache", "filesystem", "storage")

    def counts(f):
        log = f["ctx.kernel_log"]
        return [(k, (log.get(k) or {}).get("count") or 0) for k in keys]

    def level(f):
        return WARN if any(n for _k, n in counts(f)) else None

    def say(f):
        return ", ".join("%d %s" % (n, k) for k, n in counts(f) if n) + \
            " line%s in the kernel log" % _s(sum(n for _k, n in counts(f)))

    def fix(f):
        log = f["ctx.kernel_log"]
        for k in keys:
            ent = log.get(k) or {}
            if ent.get("count"):
                return ("The first %s line (%s): \"%s\".  `%s` shows them "
                        "all." % (k, log.get("source") or "kernel log",
                                  (ent.get("lines") or ["?"])[0][:140],
                                  "dmesg -T" if log.get("source") == "dmesg"
                                  else "journalctl -k -b"))
        return ""
    return level, say, fix


# 11 -----------------------------------------------------------------------
@rule("CHANGED_WHILE_READ", "changed while read", ("files.all",),
      "A file being written while it is compared differs for an ordinary "
      "reason. It is read again and, if it will not hold still, left "
      "unjudged and named, rather than reported as corrupt.")
def _changed_while_read():
    def level(f):
        return INFO if [x for x in f["files.all"]
                        if x.get("status") == "moving"] else None

    def say(f):
        items = [(x, None) for x in f["files.all"]
                 if x.get("status") == "moving"]
        return "%s: %s" % (_paths(items), items[0][0].get("reason") or "")

    def fix(f):
        return ("Something is writing these, so the two copies could not be "
                "caught agreeing or disagreeing.  Run plumb again when "
                "it has finished.")
    return level, say, fix


# 12 -----------------------------------------------------------------------
@rule("NOT_CHECKED", "files not checked", ("files.all",),
      "A file that could not be read past its cache was not checked, and "
      "says so: a report that quietly covered less without root, or on a "
      "filesystem that serves O_DIRECT from the cache, would read as a "
      "clean bill of health it had not earned.")
def _not_checked():
    def skipped(f):
        return [x for x in f["files.all"] if x.get("status") == "skipped"]

    def level(f):
        return INFO if skipped(f) else None

    def say(f):
        groups = {}
        for x in skipped(f):
            label = _skip_label(x.get("skip"), f)
            groups[label] = groups.get(label, 0) + 1
        parts = ["%d %s" % (n, label) for label, n in
                 sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))]
        return "%d of %d: %s" % (len(skipped(f)), len(f["files.all"]),
                                 ", ".join(parts))

    def fix(f):
        codes = set(x.get("skip") for x in skipped(f))
        s = "`%s --all` lists each one with its reason." % PROG
        if "perm" in codes and f.get("sys.euid"):
            s = ("Rerun with sudo to check the ones that need root -- "
                 "/etc/shadow and the boot images usually do.  " + s)
        return s
    return level, say, fix


SKIP_LABELS = {
    "notreg": "not regular files", "size": "over --max-mb",
    "nodisk": "not on a disk", "netfs": "on network filesystems",
    "fuse": "on FUSE", "owncache": "on ZFS", "journal": "data-journalled",
    "compressed": "compressed", "inline": "stored inline",
    "encrypted": "encrypted", "verity": "under fs-verity", "dax": "on DAX",
    "nodirect": "O_DIRECT refused", "gone": "gone", "error": "unreadable",
    "platform": "no O_DIRECT here",
}


def _skip_label(code, f):
    if code == "perm":
        return "need root" if f.get("sys.euid") else "permission denied"
    return SKIP_LABELS.get(code, code or "?")


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------
#
# Causes first.  A disk that answers twice differently explains every
# other difference on it, so it leads; then the copies that are about to
# be lost, because they are the only findings with a clock on them; then
# what is wrong in memory; then the evidence that explains it.
VERDICT_PRECEDENCE = [
    "DISK_UNSTABLE", "DISK_UNREADABLE", "DISK_WRONG", "CACHE_TAMPERED",
    "CACHE_CORRUPT", "CACHE_DISK_DIFFER", "NOTHING_COMPARED",
    "MEMORY_ERRORS", "KERNEL_TAINT", "KERNEL_LOG", "CHANGED_WHILE_READ",
    "NOT_CHECKED",
]

Finding = namedtuple("Finding", "rule severity say fix")

NOTHING_CHECKED = Rule("NOTHING_CHECKED", "nothing could be checked", (),
                       None, None, None,
                       "Every rule was skipped for want of the facts it "
                       "needs, so the run says nothing about this box.")


def missing_reason(f, key):
    if key in MISSING_REASONS:
        return MISSING_REASONS[key]
    return "%s not available" % key


def evaluate(facts):
    findings, skipped, passed = [], [], []
    for r in RULES:
        missing = [k for k in r.needs if facts.get(k) is None]
        if missing:
            skipped.append((r, missing_reason(facts, missing[0])))
            continue
        try:
            sev = r.level(facts)
            if sev is not None:
                findings.append(Finding(r, sev, r.say(facts), r.fix(facts)))
            else:
                passed.append(r)
        except (TypeError, ValueError, ZeroDivisionError, KeyError,
                IndexError, AttributeError) as exc:
            skipped.append((r, "rule error: %s" % exc))
    findings.sort(key=lambda x: (-SEVERITY_ORDER[x.severity],
                                 VERDICT_PRECEDENCE.index(x.rule.id)
                                 if x.rule.id in VERDICT_PRECEDENCE else 99))
    return findings, skipped, passed


def _all_files(n):
    return "the one file compared" if n == 1 else \
        "all %d files compared" % n


def _compared(facts):
    return [x for x in facts.get("files.all") or []
            if x.get("status") in ("ok", "differ", "unreadable")]


def verdict_line(findings, facts):
    if not findings or findings[0].severity == INFO:
        n = len(_compared(facts))
        if not n:
            return ("Nothing was compared, so this is not a clean bill of "
                    "health.")
        return ("The page cache and the disk agree on %s, so what "
                "programs read here is what is stored." % _all_files(n))
    top = findings[0]
    rid = top.rule.id
    first = {
        "DISK_UNSTABLE": "unstable", "DISK_UNREADABLE": "unreadable",
        "DISK_WRONG": "disk", "CACHE_TAMPERED": "tampered",
        "CACHE_CORRUPT": "corrupt", "CACHE_DISK_DIFFER": "differ",
    }.get(rid)
    if first:
        kinds = (first, "unreadable-cache") if first == "unreadable" \
            else (first,)
        x, j = _of_kind(facts, *kinds)[0]
        p = x["path"]
        if rid == "DISK_UNSTABLE":
            return ("The disk returns different bytes each time %s is read "
                    "past the cache.  The storage under this box is not "
                    "giving stable answers, so nothing read from it can be "
                    "trusted until that is found." % p)
        if rid == "DISK_UNREADABLE":
            if j["kind"] == "unreadable":
                return ("%s can no longer be read from the disk, only from "
                        "memory.  The cached copy is the only one left -- "
                        "save it before anything evicts it." % p)
            return ("%s reads from the disk but not through the cache, so "
                    "every program that opens it gets an error." % p)
        if rid == "DISK_WRONG":
            return ("%s is wrong on disk and right in memory.  The page "
                    "cache holds the only good copy, and a reboot, a cache "
                    "drop or memory pressure throws it away -- rescue it "
                    "first.  Likely cause: %s." % (p, j["cause"]))
        if rid == "CACHE_TAMPERED":
            return ("%s has been altered in memory while its disk copy is "
                    "intact, so everything that runs it runs the altered "
                    "bytes.  That is the shape of a page-cache privilege "
                    "escalation: treat the box as compromised." % p)
        if rid == "CACHE_CORRUPT":
            return ("%s is wrong in memory and right on disk, so every "
                    "program reading it gets the wrong bytes.  Likely "
                    "cause: %s." % (p, j["cause"]))
        return ("%s reads one way through the cache and another from the "
                "disk, and nothing here can say which is right.  Likely "
                "cause: %s." % (p, j["cause"]))
    if rid == "NOTHING_COMPARED":
        return ("Nothing was compared -- every file was skipped, so this is "
                "not a clean bill of health.")
    n = len(_compared(facts))
    lead = ("The cache and the disk agree on %s, but " % _all_files(n)) \
        if n else ""
    tail = {
        "MEMORY_ERRORS": "this box's memory is reporting errors, and cached "
                         "files live in that memory.",
        "KERNEL_TAINT": "the kernel has flagged a fault since boot that can "
                        "corrupt what it caches.",
        "KERNEL_LOG": "the kernel has logged corruption since boot.",
    }.get(rid, "%s: %s" % (top.rule.title, top.say))
    if lead:
        return lead + tail
    return tail[0].upper() + tail[1:]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Colors(object):
    def __init__(self, on):
        self.on = on

    def _c(self, code, s):
        return "\033[%sm%s\033[0m" % (code, s) if self.on else s

    def crit(self, s):
        return self._c("1;31", s)

    def warn(self, s):
        return self._c("1;33", s)

    def info(self, s):
        return self._c("36", s)

    def dim(self, s):
        return self._c("2", s)

    def bold(self, s):
        return self._c("1", s)


def _wrap(text, indent, width=76):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width - indent:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    return ("\n" + " " * indent).join(lines)


def _detail_lines(x, j, f):
    out = []
    d = x.get("diff") or {}
    bits = [x.get("role") or "file"]
    if x.get("fstype"):
        bits.append(x["fstype"])
    if x.get("written_since_boot"):
        bits.append("written since boot")
    elif x.get("written_since_boot") is False:
        bits.append("not written since boot")
    out.append("    %s  (%s)" % (x["path"], ", ".join(bits)))

    def row(label, text):
        out.append("      %-9s  %s" % (label, _wrap(text, 17)))

    if x.get("status") == "unreadable":
        row("failed", j["basis"])
    else:
        rs = d.get("ranges") or []
        shown = ", ".join("0x%x-0x%x" % (s, e - 1) for s, e in rs[:4])
        if d.get("n_ranges", 0) > 4:
            shown += " (+%d)" % (d["n_ranges"] - 4)
        row("differs", "%s in %d range%s: %s" % (
            _amount(x), d.get("n_ranges", 0), _s(d.get("n_ranges", 0)),
            shown))
        first = d.get("first") or {}
        if first:
            row("disk", "%s%s" % (first.get("disk") or "(nothing)",
                                  "  <- zeros" if d.get("disk_zero") else ""))
            row("cache", "%s%s" % (first.get("cache") or "(nothing)",
                                   "  <- zeros" if d.get("cache_zero")
                                   else ""))
        elf = x.get("elf") or {}
        if elf.get("header"):
            row("where", "the ELF header")
        elif elf.get("at_entry"):
            row("where", "the program's entry point (0x%x)"
                % elf["entry_off"])
        elif x.get("script"):
            row("where", "an interpreted script")
    row("judged", ("the %s is wrong: " % j["wrong"] if j["wrong"] else "")
        + j["basis"])
    row("likely", j["cause"])
    for h in j["hints"]:
        row("", "- " + h)
    mapped = x.get("mapped")
    if mapped:
        row("mapped", ", ".join("%s (%d)" % (m["comm"], m["pid"])
                                for m in mapped[:5]))
    return out


def render_human(facts, findings, skipped, passed, args, C):
    out = []
    compared = _compared(facts)
    bits = []
    if facts.get("sys.kernel"):
        bits.append("Linux %s" % facts["sys.kernel"])
    bits.append("%d file%s compared" % (len(compared), _s(len(compared))))
    if facts.get("run.bytes_read"):
        bits.append("%s read both ways" % _size(facts["run.bytes_read"]))
    out.append("%s -- %s, %s" % (PROG, facts.get("sys.hostname") or "?",
                                 ", ".join(bits)))
    if facts.get("run.disk_view"):
        out.append("  (disk side read from %s, not past the cache)"
                   % facts["run.disk_view"])
    out.append("")

    out.append("  %s   %s" % (C.bold("VERDICT"),
                              _wrap(verdict_line(findings, facts), 12)))
    out.append("")

    shown = [f for f in findings
             if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[args.min_severity]]
    for f in shown:
        tag = {CRITICAL: C.crit("CRITICAL"), WARN: C.warn("WARN    "),
               INFO: C.info("INFO    ")}[f.severity]
        out.append("  %s  %-24s %s" % (tag, f.rule.title, _wrap(f.say, 37)))
    if passed and not args.quiet:
        titles = sorted(set(r.title for r in passed))
        names = ", ".join(titles[:5])
        if len(titles) > 5:
            names += " (+%d)" % (len(titles) - 5)
        out.append("  %s        %s" % (C.dim("ok"), C.dim(_wrap(names, 12))))
    if args.all:
        for r, why in skipped:
            out.append("  %s   %-24s %s" % (C.dim("skipped"), r.title,
                                            C.dim(_wrap(why, 37))))
    out.append("")

    pairs = judged(facts)
    if pairs and not args.quiet:
        out.append("  %s" % C.bold("DIFFERENCES"))
        for x, j in pairs:
            out.extend(_detail_lines(x, j, facts))
        out.append("")

    if args.all and not args.quiet:
        unchecked = [x for x in facts.get("files.all") or []
                     if x.get("status") in ("skipped", "moving")]
        if unchecked:
            out.append("  %s" % C.bold("NOT CHECKED"))
            for x in unchecked:
                out.append("    %-40s %s" % (x["path"], C.dim(_wrap(
                    x.get("reason") or "", 45))))
            out.append("")

    out.append("  %s" % C.bold("WHAT TO DO NEXT"))
    serious = [f for f in shown if f.severity != INFO]
    if not serious:
        if not compared:
            out.append("    %s" % _wrap(
                "Nothing was compared, so nothing here says the cache can "
                "be trusted.  Name files on a local disk, or rerun with "
                "sudo.", 4))
        else:
            out.append("    %s" % _wrap(
                "What every program reads from these files is what is on "
                "the disk.  After a kernel advisory, or before rebooting "
                "into a kernel just installed, the same check across the "
                "fleet:", 4))
            out.append("")
            out.append("    %s" % _wrap(
                "agree script ./plumb.py --servers prod.txt --fleet-csv "
                "-- --csv", 4))
        for f in shown:
            if f.severity == INFO:
                out.append("")
                out.append("    * %s" % _wrap(f.fix, 6))
    else:
        for f in shown[:4]:
            out.append("    * %s" % _wrap(f.fix, 6))
    out.append("")
    return "\n".join(out)


CSV_FIELDS = ["host", "ts", "rule_id", "severity", "title", "detail", "fix"]


def render_csv(facts, findings, path):
    fh = io.open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        ts = int(time.time())
        host = facts.get("sys.hostname", "")
        if not findings:
            w.writerow({"host": host, "ts": ts, "rule_id": "OK",
                        "severity": "INFO", "title": "nothing wrong",
                        "detail": "no rule fired", "fix": ""})
        for f in findings:
            w.writerow({"host": host, "ts": ts, "rule_id": f.rule.id,
                        "severity": f.severity, "title": f.rule.title,
                        "detail": f.say, "fix": f.fix})
    finally:
        if path:
            fh.close()


def render_json(facts, findings, skipped, path):
    doc = {
        "meta": {"tool": PROG, "version": VERSION,
                 "host": facts.get("sys.hostname"), "ts": int(time.time())},
        "facts": dict((k, v) for k, v in sorted(facts.items())),
        "judgements": [j for _x, j in judged(facts)],
        "findings": [{"rule_id": f.rule.id, "severity": f.severity,
                      "title": f.rule.title, "detail": f.say, "fix": f.fix}
                     for f in findings],
        "skipped": [{"rule_id": r.id, "reason": why} for r, why in skipped],
    }
    text = json.dumps(doc, indent=2, sort_keys=True, default=str)
    if path:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version",
                   version=(
                       "%s %s\n"
                       "Copyright (C) 2026 Martin J. Gallagher\n"
                       "License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0.html>\n"
                       "This is free software: you are free to change and redistribute it.\n"
                       "There is no warranty, to the extent permitted by law."
                   ) % (PROG, VERSION))
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.add_argument("--max-mb", type=int,
                   default=int(_env("MAX_MB", DEFAULT_MAX_MB)))
    p.add_argument("--one-fs", action="store_true")
    p.add_argument("--min-severity", default=_env("MIN_SEVERITY", "info"),
                   choices=["info", "warn", "critical"])
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--exit-code", action="store_true")
    p.add_argument("--csv", nargs="?", const="", metavar="PATH")
    p.add_argument("--json", nargs="?", const="", metavar="PATH")
    p.add_argument("--facts", action="store_true")
    p.add_argument("--from-facts", metavar="FILE")
    p.add_argument("--rules", action="store_true")
    p.add_argument("--explain", metavar="RULE_ID")
    p.add_argument("--proc-root", default=_env("PROC", PROC_ROOT))
    p.add_argument("--sys-root", default=_env("SYS", SYS_ROOT))
    p.add_argument("--dpkg-info", default=_env("DPKG_INFO", DPKG_INFO))
    p.add_argument("--disk-view", default=_env("DISK_VIEW"))
    p.add_argument("--no-exec", action="store_true")
    p.add_argument("--no-color", action="store_true")
    return p


def cmd_rules():
    out = []
    for r in RULES:
        out.append("%-20s %s" % (r.id, r.title))
        out.append("    needs: %s" % ", ".join(r.needs))
        out.append("    %s" % _wrap(r.why, 4))
        out.append("")
    sys.stdout.write("\n".join(out))
    return 0


def cmd_explain(rid):
    for r in RULES:
        if r.id == rid.upper():
            sys.stdout.write("%s -- %s\n\n%s\n\nneeds: %s\n"
                             % (r.id, r.title, _wrap(r.why, 0),
                                ", ".join(r.needs)))
            return 0
    die("no such rule: %s  (try --rules)" % rid)


def main(argv=None):
    _stdio_safe()
    global NO_EXEC
    args = build_parser().parse_args(argv)

    if args.rules:
        return cmd_rules()
    if args.explain:
        return cmd_explain(args.explain)

    NO_EXEC = args.no_exec
    args.min_severity = {"info": INFO, "warn": WARN,
                         "critical": CRITICAL}[args.min_severity]
    if args.max_mb < 0:
        die("--max-mb must be 0 (no ceiling) or more")
    if args.disk_view is not None and not os.path.isdir(args.disk_view):
        die("--disk-view %s is not a directory" % args.disk_view)
    if args.from_facts and args.paths:
        die("--from-facts reads nothing, so it takes no paths")

    if args.from_facts:
        try:
            with io.open(args.from_facts, encoding="utf-8-sig",
                         errors="replace") as fh:
                facts = json.load(fh)
        except (OSError, ValueError) as exc:
            die("cannot read facts file: %s" % exc)
        if isinstance(facts, dict) and "facts" in facts \
                and isinstance(facts["facts"], dict):
            facts = facts["facts"]
        if not isinstance(facts, dict):
            die("%s does not hold a fact dictionary (found %s) -- pass a "
                "file written by --facts or --json"
                % (args.from_facts, type(facts).__name__))
    else:
        facts = collect_facts(args)

    if args.facts:
        sys.stdout.write(json.dumps(facts, indent=2, sort_keys=True,
                                    default=str) + "\n")
        return 0

    findings, skipped, passed = evaluate(facts)
    if not findings and not passed:
        findings = [Finding(NOTHING_CHECKED, WARN,
                            "none of the %d rules could run: every fact they "
                            "need is missing" % len(RULES),
                            "The fact dictionary is empty or unrecognisable, "
                            "so this says nothing about this box.  Run %s on "
                            "the box itself, or pass a file written by "
                            "--facts." % PROG)]

    if args.csv is not None:
        with _WriteGuard(args.csv or None):
            render_csv(facts, findings, args.csv or None)
    if args.json is not None:
        with _WriteGuard(args.json or None):
            render_json(facts, findings, skipped, args.json or None)
    if args.csv is None and args.json is None:
        use_color = (not args.no_color and not os.environ.get("NO_COLOR")
                     and sys.stdout.isatty())
        sys.stdout.write(render_human(facts, findings, skipped, passed, args,
                                      Colors(use_color)) + "\n")

    if args.exit_code and findings:
        worst = max(SEVERITY_ORDER[f.severity] for f in findings)
        # 10/20 rather than 1/2, so a severity can never be mistaken for a
        # usage error by whatever is wrapping this.
        return {3: 20, 2: 10, 1: 0}[worst]
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)

# ---------------------------------------------------------------------------
# Why this file has no imports from its siblings
# ---------------------------------------------------------------------------
#
# Every module in binnacle is a complete, standalone program: standard
# library only, and nothing imported from the rest of the package. That is
# not tidiness, it is a requirement. These files get copied to machines that
# have never heard of binnacle -- `agree script ./plumb.py` pushes this file
# to a fleet -- and a relative import would break the moment it landed. A
# helper duplicated across two modules, with a comment naming the canonical
# copy, is the accepted cost of that.
#
# canonical copy: binnacle/why_slow.py  (_stdio_safe, _WriteGuard, _read,
#                                        _read_int, _run)
