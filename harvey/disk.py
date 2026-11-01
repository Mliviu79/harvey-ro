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


def free_bytes(path) -> int:
    """Free bytes for a file written at ``path``: its own disk's, or the
    host drive's when that is less."""
    free = shutil.disk_usage(_existing(Path(path).parent)).free
    drive = host_drive()
    if drive is not None:
        free = min(free, shutil.disk_usage(drive).free)
    return free
