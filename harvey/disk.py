"""Free disk space, as a big write would really have it.

On WSL (and in a container on it) a Linux disk is a virtual disk image
that grows inside a Windows drive: the Linux file system reports the
image's own free space (hundreds of GB) while the drive holding it may be
nearly full, and a full drive remounts the Linux disk read-only. So a
check counts the least of the target's own disk and the host drive's.

The host drive is ``HARVEY_HOST_DRIVE`` when set (a folder on it: only its
free space is read), else, on WSL, the drive Windows records for this
distro's disk: its ``BasePath`` in the registry (``Lxss``, one key per
distro, found by ``WSL_DISTRO_NAME``, or by the WSL ``init`` process's
when a service started without it), read through ``reg.exe`` once per
process (a failure is asked again after a few minutes). That can be any drive (``\\\\?\\D:\\wsl\\example`` as much as
``C:\\Users\\example\\AppData\\Local\\wsl\\{...}``), mounted under WSL's
automount root (``/etc/wsl.conf``, ``/mnt/`` by default). When it can't
be found (no ``reg.exe``, no matching distro, a disk on a network share)
a check refuses (``HostDriveUnknown``); ``HARVEY_HOST_DRIVE=none`` says the
target's own disk is the real one.

In Docker (docker-compose.yml) the container sees neither ``reg.exe`` nor
the drives: Compose mounts ``HARVEY_HOST_DRIVE_SOURCE`` read-only at
``/host-drive`` and points ``HARVEY_HOST_DRIVE`` at it. That is the
checkout by default (right on native Linux), and on WSL an empty folder of
its own on the drive that holds the WSL disk, which
``scripts/disk-check-folder.sh`` creates and puts in .env (it runs this
file's ``main`` on the WSL side), never the drive itself: a folder's free
space is its drive's, and the container, which reads untrusted websites,
sees nothing else of the drive. A host drive that is a drive's root (it
holds ``Windows``, ``Users`` or ``Program Files``, as an older release's
.env set it) is refused until it is a folder of its own. On WSL a host
drive on the same file system as the target is the virtual disk itself,
not the drive under it, so a check refuses rather than trust it, unless the
target is on a Windows drive (a checkout under ``/mnt/<drive>``): then
that drive is the real disk.

Stdlib only, and no import of the rest of Harvey: the setup script runs it
with the system's python3, outside Harvey's install.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The folder scripts/disk-check-folder.sh creates on the drive (any empty
# folder of its own the user already set is just as good).
CHECK_FOLDER = "harvey-disk-check"

# What the messages suggest for Docker on WSL: a folder of its own, never
# the drive's root (docker-compose.yml).
WSL_FOLDER_HINT = ("in Docker, run scripts/disk-check-folder.sh once from the WSL shell in the "
                   "checkout: it creates an empty folder on the Windows drive that holds your WSL "
                   f"disk ({CHECK_FOLDER}) and sets HARVEY_HOST_DRIVE_SOURCE to it in .env; by "
                   "hand, create one empty folder on that drive and set "
                   "HARVEY_HOST_DRIVE_SOURCE to it, not the whole drive")


# What a Windows drive's root holds (any one of them): a folder that does is
# the drive itself, not a folder of its own.
DRIVE_ROOT_ENTRIES = ("Windows", "Users", "Program Files")

# Where Windows records each WSL distro: a {GUID} key per distro with its
# DistributionName and BasePath (the folder holding its ext4.vhdx).
LXSS_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss"
WSL_CONF = Path("/etc/wsl.conf")
DEFAULT_AUTOMOUNT_ROOT = Path("/mnt")


class HostDriveUnknown(RuntimeError):
    """On WSL with no host drive in sight, or one that is a whole drive:
    free space can't be checked."""


def drive_root(path: Path) -> bool:
    """Whether ``path`` looks like a Windows drive's root."""
    return any((Path(path) / name).is_dir() for name in DRIVE_ROOT_ENTRIES)


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


def automount_root(conf: Path | None = None) -> Path:
    """Where WSL mounts the Windows drives (``[automount] root``)."""
    conf = WSL_CONF if conf is None else conf
    # (As lenient as WSL: a key with no value, any case of the section.)
    parser = configparser.ConfigParser(interpolation=None, strict=False, allow_no_value=True,
                                       inline_comment_prefixes=("#",))
    root = ""
    try:
        parser.read_string(Path(conf).read_text(encoding="utf-8", errors="replace"))
        for section in parser.sections():
            if section.strip().lower() == "automount":
                root = (parser.get(section, "root", fallback="") or "").strip().strip("\"'")
    except (OSError, configparser.Error):
        root = ""
    return Path(root) if root.startswith("/") else DEFAULT_AUTOMOUNT_ROOT


def _reg_exe(root: Path) -> str | None:
    # (On PATH only when WSL appends the Windows PATH; at its usual place
    # on C: otherwise.)
    found = shutil.which("reg.exe")
    if found:
        return found
    usual = root / "c" / "Windows" / "System32" / "reg.exe"
    return str(usual) if usual.is_file() else None


def parse_lxss(text: str) -> dict[str, str]:
    """``reg.exe query <Lxss> /s`` output as {DistributionName: BasePath}."""
    found: dict[str, str] = {}
    name = base = None
    for line in text.splitlines() + [""]:
        line = line.rstrip("\r\n")
        if not line.strip() or not line.startswith(" "):
            if name and base:
                found[name] = base
            name = base = None
            continue
        parts = re.split(r"\s{2,}|\t", line.strip(), maxsplit=2)
        if len(parts) == 3 and parts[1] == "REG_SZ":
            if parts[0] == "DistributionName":
                name = parts[2].strip()
            elif parts[0] == "BasePath":
                base = parts[2].strip()
    return found


def drive_mount(base_path: str, root: Path) -> Path:
    """Where a Windows folder's drive is mounted on WSL: ``G:\\...`` (or
    ``\\\\?\\G:\\...``) is ``<root>/g``."""
    path = base_path
    if path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        path = path[4:]
    match = re.match(r"([A-Za-z]):(\\|$)", path)
    if not match:
        raise HostDriveUnknown("this WSL disk isn't on a drive letter (a network share?)")
    letter = match.group(1).lower()
    mount = root / letter
    if not mount.is_dir():
        raise HostDriveUnknown(f"this WSL disk is on {letter.upper()}:, which isn't mounted "
                               f"at {mount}")
    return mount


# How long a failed lookup is kept before reg.exe is asked again (a found
# drive is kept for the process: a distro's disk doesn't move under it).
LOOKUP_RETRY_SECONDS = 300
REG_TIMEOUT_SECONDS = 10
PROC = Path("/proc")
_lookup: dict[str, object] = {}


def _clear_lookup() -> None:
    _lookup.clear()


def _wsl_drive_lookup() -> tuple[str | None, str | None]:
    # (Two first calls at once may both run reg.exe: harmless.)
    if "path" in _lookup:
        return str(_lookup["path"]), None
    if "error" in _lookup and time.monotonic() - float(_lookup["at"]) < LOOKUP_RETRY_SECONDS:
        return None, str(_lookup["error"])
    try:
        path = str(_find_wsl_drive())
    except HostDriveUnknown as e:
        _lookup.update(error=str(e), at=time.monotonic())
        return None, str(e)
    _lookup.clear()
    _lookup["path"] = path
    return path, None


def distro_name() -> str:
    """This distro's name: ``WSL_DISTRO_NAME``, or, when it isn't set (a
    systemd service, cron, sudo), the one WSL's ``init`` processes carry,
    when they can be read (as root)."""
    name = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if name:
        return name
    try:
        pids = [p for p in PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return ""
    for pid in sorted(pids, key=lambda p: int(p.name)):
        try:
            if (pid / "comm").read_text(encoding="utf-8").strip() != "init":
                continue
            environ = (pid / "environ").read_bytes()
        except OSError:
            continue
        for entry in environ.split(b"\0"):
            key, _, value = entry.partition(b"=")
            if key in (b"WSL_DISTRO_NAME", b"WSL2_DISTRO_NAME") and value.strip():
                return value.decode("utf-8", errors="replace").strip()
    return ""


def _find_wsl_drive() -> Path:
    distro = distro_name()
    if not distro:
        raise HostDriveUnknown("WSL_DISTRO_NAME isn't set (and WSL's init process can't be "
                               "read from here), so this distro's disk can't be looked up")
    root = automount_root()
    reg = _reg_exe(root)
    if not reg:
        raise HostDriveUnknown("Windows' reg.exe isn't reachable from here, so this distro's "
                               "disk can't be looked up")
    try:
        done = subprocess.run([reg, "query", LXSS_KEY, "/s"], capture_output=True,
                              timeout=REG_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise HostDriveUnknown(f"reg.exe didn't run ({e})") from None
    if done.returncode != 0:
        raise HostDriveUnknown("reg.exe couldn't read the WSL distros from the registry")
    # (reg.exe writes the OEM code page: a name that isn't ASCII may not
    # match, and is then refused like a missing one. Names match in any case,
    # as Windows treats them.)
    distros = parse_lxss(done.stdout.decode("utf-8", errors="replace"))
    base = {name.casefold(): path for name, path in distros.items()}.get(distro.casefold())
    if not base:
        raise HostDriveUnknown(f"the registry has no WSL distro named {distro}")
    return drive_mount(base, root)


def wsl_drive() -> Path:
    """The mount of the Windows drive that holds this WSL distro's disk."""
    path, error = _wsl_drive_lookup()
    if error:
        raise HostDriveUnknown(error)
    return Path(path)


def host_drive() -> Path | None:
    """The drive a virtual disk lives on, or None when the disk is real."""
    configured = os.environ.get("HARVEY_HOST_DRIVE", "").strip()
    if configured.lower() == "none":
        return None
    if configured:
        if not Path(configured).is_dir():
            raise HostDriveUnknown(f"HARVEY_HOST_DRIVE is {configured}, which isn't a folder here")
        if drive_root(Path(configured)):
            # (Mounted into the container, which reads untrusted websites,
            # the whole drive would be readable there.)
            raise HostDriveUnknown(
                f"HARVEY_HOST_DRIVE ({configured}) is a whole drive, not a folder of its own: "
                f"{WSL_FOLDER_HINT}, then recreate the container (docker compose up -d harvey)")
        return Path(configured)
    if on_wsl():
        try:
            return wsl_drive()
        except HostDriveUnknown as e:
            raise HostDriveUnknown(
                f"this runs on WSL but the Windows drive that holds its disk can't be found "
                f"({e}): set HARVEY_HOST_DRIVE to a folder on that drive ({WSL_FOLDER_HINT}), "
                "or to 'none' when the disk isn't a virtual one") from None
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
    """Whether ``path`` is on a Windows drive itself (a WSL drive mount, or
    a folder of one mounted into a container), not on the virtual disk."""
    return _mount_type(path) in WINDOWS_DRIVE_TYPES


def free_bytes(path) -> int:
    """Free bytes for a file written at ``path``: its own disk's, or the
    host drive's when that is less."""
    target = _existing(Path(path).parent)
    free = shutil.disk_usage(target).free
    if (on_wsl() and not os.environ.get("HARVEY_HOST_DRIVE", "").strip()
            and on_windows_drive(target)):
        # (Written to a Windows drive itself, the virtual disk doesn't grow:
        # the target's own free space is the real one.)
        return free
    drive = host_drive()
    if drive is not None:
        # (A target on the Windows drive itself is fine: its free space is
        # the drive's.)
        if on_wsl() and same_disk(drive, target) and not on_windows_drive(target):
            raise HostDriveUnknown(
                f"HARVEY_HOST_DRIVE ({drive}) is on the same disk as {target}, the virtual "
                "disk itself: on WSL it must be a folder on the Windows drive that holds that "
                f"disk ({WSL_FOLDER_HINT}), or 'none' when the disk isn't a virtual one")
        free = min(free, shutil.disk_usage(drive).free)
    return free


ENV_KEY = "HARVEY_HOST_DRIVE_SOURCE"


def _env_setting(env_file: Path) -> str | None:
    """``HARVEY_HOST_DRIVE_SOURCE`` as .env sets it ("" when set empty), or
    None when it isn't set (a commented line doesn't count)."""
    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    value = None
    for line in lines:
        match = re.match(rf"\s*(?:export\s+)?{ENV_KEY}\s*=(.*)$", line)
        if match:
            raw = match.group(1).strip()
            quoted = re.match(r"""(["'])(.*?)\1""", raw)
            # (Unquoted, " #" starts a comment, as Compose reads it.)
            if quoted:
                value = quoted.group(2)
            else:
                value = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
    return value


def _drive_letter(path: Path, root: Path) -> str | None:
    """The drive ``path`` is on, as WSL mounts it (``<root>/g/...`` is G),
    or None when it isn't under a drive mount."""
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None
    first = rel.parts[0] if rel.parts else ""
    return first.upper() if re.fullmatch(r"[A-Za-z]", first) else None


def _check_current(current: str) -> tuple[int, list[str]]:
    """A ``HARVEY_HOST_DRIVE_SOURCE`` .env already sets: kept when it is an
    empty folder of its own on the drive that holds this WSL disk."""
    path = Path(current)
    lines = [f"{ENV_KEY} is already set in .env ({current})."]
    if path.is_dir() and drive_root(path):
        lines.append(f"That is a whole drive: change it to an empty folder of its own on "
                     f"it (e.g. {path / CHECK_FOLDER}), then run docker compose up -d harvey.")
        return 1, lines
    if not on_wsl():
        lines.append("Left as it is.")
        return 0, lines
    try:
        drive = wsl_drive()
    except HostDriveUnknown as e:
        lines.append(f"It can't be checked against the Windows drive that holds this WSL disk "
                     f"({e}): make sure it is an empty folder on that drive.")
        return 1, lines
    fix = (f"change it to {drive / CHECK_FOLDER} (or delete that line and run this again, "
           "which creates it), then run docker compose up -d harvey.")
    letter = _drive_letter(path, automount_root())
    want = drive.name.upper()
    if letter != want:
        where = f"a folder on {letter}:" if letter else "a folder that isn't on a Windows drive"
        lines.append(f"That is {where}, but this WSL disk is on {want}:, so the check would "
                     f"read the wrong free space: {fix}")
        return 1, lines
    if not path.is_dir():
        lines.append(f"That folder doesn't exist: create it (mkdir {path}), or {fix}")
        return 1, lines
    if any(path.iterdir()):
        lines.append("That folder isn't empty: the container would see its files. Empty it, "
                     f"or {fix}")
        return 1, lines
    lines.append(f"An empty folder on {want}:, the drive that holds this WSL disk: left as it is.")
    return 0, lines


def make_check_folder(env_file: Path) -> tuple[int, list[str]]:
    """Docker on WSL, run once on the WSL side: an empty folder on the drive
    that holds this distro's disk, set as ``HARVEY_HOST_DRIVE_SOURCE`` in
    ``env_file`` unless it already sets one (then that one is checked).
    Returns (exit code, lines to print); never prints the rest of .env."""
    env_file = Path(env_file)
    current = _env_setting(env_file)
    if current:
        return _check_current(current)
    if current == "":
        return 1, [f"{ENV_KEY} is in .env with no value: delete that line and run this again."]
    if not on_wsl():
        return 0, ["This isn't WSL: the default (the checkout) is the right folder, nothing "
                   "to do."]
    try:
        drive = wsl_drive()
    except HostDriveUnknown as e:
        return 1, [f"The Windows drive that holds this WSL disk can't be found: {e}.",
                   f"By hand: create one empty folder on that drive (mkdir "
                   f"<drive mount>/{CHECK_FOLDER}) and add {ENV_KEY}=<that folder> to .env, "
                   "then run docker compose up -d harvey."]
    folder = drive / CHECK_FOLDER
    lines = []
    if folder.is_dir():
        if any(folder.iterdir()):
            return 1, [f"{folder} already exists and isn't empty: the container would see "
                       "its files. Empty it, or delete it, and run this again."]
        lines.append(f"{folder} is already there, empty.")
    else:
        folder.mkdir()
        lines.append(f"Created {folder}, an empty folder on the Windows drive that holds this "
                     "WSL disk: only its free space is read.")
    tail = env_file.read_bytes()[-1:] if env_file.exists() else b""
    fd = os.open(env_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(("\n" if tail and tail != b"\n" else "") + f"{ENV_KEY}={folder}\n")
    lines.append(f"Added {ENV_KEY}={folder} to .env.")
    lines.append("Recreate the container so it mounts it: docker compose up -d harvey")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_file = Path(args[0]) if args else Path(".env")
    code, lines = make_check_folder(env_file)
    for line in lines:
        print(line, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
