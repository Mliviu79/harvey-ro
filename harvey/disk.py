"""Free disk space, as a big write would really have it.

On WSL (and in a container on it) a Linux disk is a virtual disk image
that grows inside a Windows drive: the Linux file system reports the
image's own free space (hundreds of GB) while the drive holding it may be
nearly full, and a full drive remounts the Linux disk read-only. So a
check counts the least of the target's own disk and the host drive's.

The host drive is ``HARVEY_HOST_DRIVE`` when set (any folder on it, mounted
into a container read-only), else ``/mnt/c`` on WSL, where the disk images
live by default. On a WSL kernel with neither visible, the space can't be
known and a check refuses (``HostDriveUnknown``); ``HARVEY_HOST_DRIVE=none``
says the target's own disk is the real one.

In Docker (docker-compose.yml) the container can't see ``/mnt/c``: Compose
mounts ``HARVEY_HOST_DRIVE_SOURCE`` (the checkout by default, right on
native Linux; ``/mnt/c`` on WSL, set in .env) read-only at ``/host-drive``
and points ``HARVEY_HOST_DRIVE`` at it. On WSL a host drive on the same
file system as the target is the virtual disk itself, not the drive under
it, so a check refuses rather than trust it, unless the target is on a
Windows drive (a checkout under /mnt/c): then that drive is the real disk.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

DEFAULT_WSL_DRIVE = Path("/mnt/c")


class HostDriveUnknown(RuntimeError):
    """On WSL with no host drive in sight: free space can't be checked."""


def _existing(path: Path) -> Path:
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def on_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "microsoft" in release or "wsl" in release


def host_drive() -> Path | None:
    """The drive a virtual disk lives on, or None when the disk is real."""
    configured = os.environ.get("HARVEY_HOST_DRIVE", "").strip()
    if configured.lower() == "none":
        return None
    if configured:
        if not Path(configured).is_dir():
            raise HostDriveUnknown(f"HARVEY_HOST_DRIVE is {configured}, which isn't a folder here")
        return Path(configured)
    if on_wsl():
        if DEFAULT_WSL_DRIVE.is_dir():
            return DEFAULT_WSL_DRIVE
        raise HostDriveUnknown(
            "this runs on WSL but the Windows drive holding its disk isn't visible here: set "
            "HARVEY_HOST_DRIVE to a folder on that drive (mounted read-only into a container), "
            "or to 'none' when the disk isn't a virtual one")
    return None


def same_disk(a: Path, b: Path) -> bool:
    """Whether two folders are on one file system."""
    return os.stat(a).st_dev == os.stat(b).st_dev


# File systems WSL mounts a Windows drive with (and Docker passes on).
WINDOWS_DRIVE_TYPES = frozenset({"9p", "v9fs", "drvfs"})
MOUNTS = Path("/proc/self/mounts")


def _mount_type(path: Path) -> str:
    """The file system type of the mount holding ``path`` ("" when unknown)."""
    try:
        lines = MOUNTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    best, kind = "", ""
    target = str(Path(path).resolve())
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        point = parts[1].replace("\\040", " ")
        if (target == point or target.startswith(point.rstrip("/") + "/")) and len(point) > len(best):
            best, kind = point, parts[2]
    return kind


def on_windows_drive(path: Path) -> bool:
    """Whether ``path`` is on a Windows drive itself (WSL's /mnt/c, or a
    folder of it mounted into a container), not on the virtual disk."""
    return _mount_type(path) in WINDOWS_DRIVE_TYPES


def free_bytes(path) -> int:
    """Free bytes for a file written at ``path``: its own disk's, or the
    host drive's when that is less."""
    target = _existing(Path(path).parent)
    free = shutil.disk_usage(target).free
    drive = host_drive()
    if drive is not None:
        # (A target on the Windows drive itself is fine: its free space is
        # the drive's.)
        if on_wsl() and same_disk(drive, target) and not on_windows_drive(target):
            raise HostDriveUnknown(
                f"HARVEY_HOST_DRIVE ({drive}) is on the same disk as {target}, the virtual "
                "disk itself: on WSL it must be a folder on the Windows drive that disk lives "
                "on (in Docker, set HARVEY_HOST_DRIVE_SOURCE=/mnt/c in .env), or 'none' when "
                "the disk isn't a virtual one")
        free = min(free, shutil.disk_usage(drive).free)
    return free
