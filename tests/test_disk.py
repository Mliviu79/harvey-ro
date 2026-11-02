"""The Windows drive that holds a WSL distro's disk, found from the registry,
and the one-time folder setup for Docker on WSL (harvey/disk.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harvey import disk

ROOT = Path(__file__).resolve().parent.parent

# What `reg.exe query <Lxss> /s` prints, CRLF line ends included: fictional
# distros, one per BasePath spelling, plus a network share.
LXSS = (
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\r\n"
    "    DefaultVersion    REG_DWORD    0x2\r\n"
    "    DefaultDistribution    REG_SZ    {11111111-2222-3333-4444-555555555555}\r\n"
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\Notifications\r\n"
    "    DrvFsPerfDisabled    REG_DWORD    0x1\r\n"
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\"
    "{11111111-2222-3333-4444-555555555555}\r\n"
    "    State    REG_DWORD    0x1\r\n"
    "    DistributionName    REG_SZ    Ubuntu\r\n"
    "    BasePath    REG_SZ    "
    "C:\\Users\\example\\AppData\\Local\\wsl\\{11111111-2222-3333-4444-555555555555}\r\n"
    "    VhdFileName    REG_SZ    ext4.vhdx\r\n"
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\"
    "{66666666-7777-8888-9999-000000000000}\r\n"
    "    BasePath    REG_SZ    \\\\?\\D:\\wsl\\example distro\r\n"
    "    DistributionName    REG_SZ    Example Distro\r\n"
    "    Version    REG_DWORD    0x2\r\n"
    "\r\n"
    "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss\\"
    "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}\r\n"
    "    DistributionName    REG_SZ    Shared\r\n"
    "    BasePath    REG_SZ    \\\\fileserver\\wsl\\shared\r\n"
)


def test_parse_lxss_reads_every_distro_with_crlf():
    assert disk.parse_lxss(LXSS) == {
        "Ubuntu": "C:\\Users\\example\\AppData\\Local\\wsl\\"
                  "{11111111-2222-3333-4444-555555555555}",
        "Example Distro": "\\\\?\\D:\\wsl\\example distro",
        "Shared": "\\\\fileserver\\wsl\\shared",
    }
    assert disk.parse_lxss("") == {}


def test_drive_mount_from_either_base_path_form(tmp_path):
    for letter in ("c", "d"):
        (tmp_path / letter).mkdir()
    assert disk.drive_mount("C:\\Users\\example\\AppData\\Local\\wsl\\{x}", tmp_path) \
        == tmp_path / "c"
    assert disk.drive_mount("\\\\?\\D:\\wsl\\example", tmp_path) == tmp_path / "d"
    assert disk.drive_mount("d:", tmp_path) == tmp_path / "d"
    for share in ("\\\\fileserver\\wsl\\shared", "\\\\?\\UNC\\fileserver\\wsl"):
        with pytest.raises(disk.HostDriveUnknown, match="network share"):
            disk.drive_mount(share, tmp_path)
    with pytest.raises(disk.HostDriveUnknown, match="E:, which isn't mounted"):
        disk.drive_mount("E:\\wsl", tmp_path)


def test_automount_root_from_wsl_conf(tmp_path):
    conf = tmp_path / "wsl.conf"
    assert disk.automount_root(conf) == Path("/mnt")
    conf.write_text('[boot]\nsystemd=true\n\n[automount]\noptions = "metadata"\n')
    assert disk.automount_root(conf) == Path("/mnt")
    conf.write_text('[automount]\r\nroot = "/windir/"  # drives here\r\n')
    assert disk.automount_root(conf) == Path("/windir")
    conf.write_text("[automount]\nroot = /\n")
    assert disk.automount_root(conf) == Path("/")
    conf.write_text("not an ini file at all\n[[[")
    assert disk.automount_root(conf) == Path("/mnt")
    # As lenient as WSL: a key with no value doesn't hide the rest, and the
    # section's name matches in any case.
    conf.write_text("[boot]\nsystemd\n\n[Automount]\nenabled = true\nroot = /drives/\n")
    assert disk.automount_root(conf) == Path("/drives")


@pytest.fixture
def fake_wsl(tmp_path, monkeypatch):
    """A WSL host: drives mounted under a custom automount root, reg.exe at
    its usual place on C: (a script printing LXSS), not on PATH."""
    root = tmp_path / "windir"
    for letter in ("c", "d"):
        (root / letter).mkdir(parents=True)
    conf = tmp_path / "wsl.conf"
    conf.write_text(f"[automount]\nroot = {root}/\n")
    out = tmp_path / "lxss.txt"
    out.write_bytes(LXSS.encode("utf-8"))
    reg = root / "c" / "Windows" / "System32" / "reg.exe"
    reg.parent.mkdir(parents=True)
    reg.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path / 'calls'}\ncat {out}\n")
    reg.chmod(0o755)
    monkeypatch.setattr(disk, "WSL_CONF", conf)
    monkeypatch.setattr(disk, "on_wsl", lambda: True)
    monkeypatch.setattr(disk.shutil, "which", lambda name: None)
    # (No init process to fall back on unless a test makes one.)
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(disk, "PROC", proc)
    monkeypatch.delenv("HARVEY_HOST_DRIVE", raising=False)
    disk._clear_lookup()
    yield root
    disk._clear_lookup()


def test_the_drive_is_found_for_this_distro_and_cached(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    assert disk.wsl_drive() == fake_wsl / "d"
    assert disk.host_drive() == fake_wsl / "d"
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == ["query HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss /s"]
    disk._clear_lookup()
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert disk.wsl_drive() == fake_wsl / "c"
    # A HARVEY_HOST_DRIVE folder wins over the registry.
    own = tmp_path / "own"
    own.mkdir()
    monkeypatch.setenv("HARVEY_HOST_DRIVE", str(own))
    assert disk.host_drive() == own
    monkeypatch.setenv("HARVEY_HOST_DRIVE", "none")
    assert disk.host_drive() is None


def test_free_space_is_the_found_drive_s(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    real = disk.shutil.disk_usage
    drive = str(fake_wsl / "d")

    def fake(path):
        free = 7 * 10**9 if str(path) == drive else 900 * 10**9
        return real(tmp_path)._replace(free=free)

    monkeypatch.setattr(disk.shutil, "disk_usage", fake)
    monkeypatch.setattr(disk, "same_disk", lambda a, b: False)
    monkeypatch.setattr(disk, "on_windows_drive", lambda p: False)
    assert disk.free_bytes(tmp_path / "out.db") == 7 * 10**9
    # Written to a Windows drive itself, the virtual disk doesn't grow: no
    # lookup needed, the target's own space counts.
    monkeypatch.setattr(disk, "on_windows_drive", lambda p: True)
    monkeypatch.setattr(disk, "_wsl_drive_lookup", lambda: (None, "unused"))
    assert disk.free_bytes(tmp_path / "out.db") == 900 * 10**9


@pytest.mark.parametrize("distro, reason", [
    ("Missing", "no WSL distro named Missing"),
    ("Shared", "network share"),
    ("", "WSL_DISTRO_NAME isn't set"),
])
def test_a_failed_lookup_refuses_and_says_what_to_set(fake_wsl, monkeypatch, distro, reason):
    monkeypatch.setenv("WSL_DISTRO_NAME", distro)
    with pytest.raises(disk.HostDriveUnknown) as e:
        disk.free_bytes(fake_wsl / "out.db")
    message = str(e.value)
    assert reason in message
    assert "HARVEY_HOST_DRIVE" in message and "scripts/disk-check-folder.sh" in message


def test_no_reg_exe_refuses(fake_wsl, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    (fake_wsl / "c" / "Windows" / "System32" / "reg.exe").unlink()
    with pytest.raises(disk.HostDriveUnknown, match="reg.exe isn't reachable"):
        disk.host_drive()
    with pytest.raises(disk.HostDriveUnknown, match="reg.exe isn't reachable"):
        disk.wsl_drive()


def test_a_failed_lookup_is_asked_again_after_a_while(fake_wsl, tmp_path, monkeypatch):
    # A long-running dashboard or worker isn't stuck with one bad moment
    # (interop down, reg.exe timing out) until it restarts.
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    reg = fake_wsl / "c" / "Windows" / "System32" / "reg.exe"
    hidden = tmp_path / "reg.exe.hidden"
    shutil.move(reg, hidden)
    now = [1000.0]
    monkeypatch.setattr(disk.time, "monotonic", lambda: now[0])
    with pytest.raises(disk.HostDriveUnknown, match="reg.exe isn't reachable"):
        disk.wsl_drive()
    shutil.move(hidden, reg)
    now[0] += disk.LOOKUP_RETRY_SECONDS - 1
    with pytest.raises(disk.HostDriveUnknown, match="reg.exe isn't reachable"):
        disk.wsl_drive()
    assert not (tmp_path / "calls").exists()
    now[0] += 2
    assert disk.wsl_drive() == fake_wsl / "d"
    # A found drive is kept: reg.exe isn't asked again.
    now[0] += 10 * disk.LOOKUP_RETRY_SECONDS
    assert disk.wsl_drive() == fake_wsl / "d"
    assert len((tmp_path / "calls").read_text().splitlines()) == 1


def test_reg_exe_gets_a_short_timeout(fake_wsl, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    seen = {}

    def slow(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(disk.subprocess, "run", slow)
    with pytest.raises(disk.HostDriveUnknown, match="reg.exe didn't run"):
        disk.wsl_drive()
    assert seen["timeout"] <= 10


def _init_process(proc: Path, pid: int, comm: str, environ: bytes) -> None:
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "comm").write_text(comm + "\n")
    (proc / str(pid) / "environ").write_bytes(environ)


def test_a_service_without_the_variable_reads_it_from_wsl_s_init(fake_wsl, monkeypatch):
    # systemd units, cron and sudo start without WSL_DISTRO_NAME; WSL's own
    # init processes carry the name (as WSL2_DISTRO_NAME).
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    proc = disk.PROC
    _init_process(proc, 1, "systemd", b"PATH=/usr/bin\0")
    _init_process(proc, 2, "init", b"WSL2_PID=1\0WSL2_DISTRO_NAME=Example Distro\0")
    (proc / "self").mkdir()
    assert disk.distro_name() == "Example Distro"
    assert disk.wsl_drive() == fake_wsl / "d"
    # Not readable (a service not run as root): refused, saying what to set.
    (proc / "2" / "environ").unlink()
    (proc / "2" / "environ").mkdir()
    disk._clear_lookup()
    assert disk.distro_name() == ""
    with pytest.raises(disk.HostDriveUnknown) as e:
        disk.host_drive()
    assert "WSL_DISTRO_NAME isn't set" in str(e.value) and "HARVEY_HOST_DRIVE" in str(e.value)


def test_distro_names_match_in_any_case_and_odd_bytes_refuse_cleanly(fake_wsl, tmp_path,
                                                                     monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "EXAMPLE distro")
    assert disk.wsl_drive() == fake_wsl / "d"
    # reg.exe writes the OEM code page: a name that isn't ASCII doesn't
    # decode as UTF-8, so it doesn't match and is refused like a missing one.
    disk._clear_lookup()
    odd = (LXSS + "\r\nHKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion"
           "\\Lxss\\{bbbbbbbb-0000-0000-0000-000000000000}\r\n").encode("utf-8")
    odd += "    DistributionName    REG_SZ    Débian\r\n".encode("cp850")
    odd += b"    BasePath    REG_SZ    \\\\?\\D:\\wsl\\other\r\n"
    (tmp_path / "lxss.txt").write_bytes(odd)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Débian")
    with pytest.raises(disk.HostDriveUnknown) as e:
        disk.host_drive()
    assert "no WSL distro named Débian" in str(e.value)
    assert "HARVEY_HOST_DRIVE" in str(e.value)
    # The other distros still read.
    disk._clear_lookup()
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert disk.wsl_drive() == fake_wsl / "c"


def test_reg_exe_on_path_is_used_first(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    usual = fake_wsl / "c" / "Windows" / "System32" / "reg.exe"
    on_path = tmp_path / "bin" / "reg.exe"
    on_path.parent.mkdir()
    shutil.move(usual, on_path)
    monkeypatch.setattr(disk.shutil, "which",
                        lambda name: str(on_path) if name == "reg.exe" else None)
    assert disk.wsl_drive() == fake_wsl / "d"


def test_setup_creates_the_folder_and_appends_env_once(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    env = tmp_path / ".env"
    env.write_text("SOME_KEY=1\n# HARVEY_HOST_DRIVE_SOURCE=/mnt/<drive>/harvey-disk-check")
    code, lines = disk.make_check_folder(env)
    folder = fake_wsl / "d" / "harvey-disk-check"
    assert code == 0 and folder.is_dir() and not any(folder.iterdir())
    assert env.read_text().splitlines()[-1] == f"HARVEY_HOST_DRIVE_SOURCE={folder}"
    assert env.read_text().startswith("SOME_KEY=1\n# HARVEY")
    assert any("docker compose up -d harvey" in line for line in lines)
    assert not any("SOME_KEY" in line for line in lines)
    before = env.read_text()
    code, lines = disk.make_check_folder(env)
    assert code == 0 and env.read_text() == before
    assert "already set" in lines[0]
    assert env.read_text().count("\nHARVEY_HOST_DRIVE_SOURCE=") == 1


def test_setup_keeps_a_folder_already_set(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    env = tmp_path / ".env"
    old = fake_wsl / "d" / "my-old-folder"
    old.mkdir()
    env.write_text(f"HARVEY_HOST_DRIVE_SOURCE={old}\n")
    code, lines = disk.make_check_folder(env)
    assert code == 0 and "left as it is" in lines[-1] and "D:" in lines[-1]
    assert env.read_text() == f"HARVEY_HOST_DRIVE_SOURCE={old}\n"
    assert not (fake_wsl / "d" / "harvey-disk-check").exists()
    # A drive's root set before is reported, not changed.
    (fake_wsl / "d" / "Users").mkdir()
    env.write_text(f"HARVEY_HOST_DRIVE_SOURCE={fake_wsl / 'd'}\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "whole drive" in lines[1]
    assert env.read_text() == f"HARVEY_HOST_DRIVE_SOURCE={fake_wsl / 'd'}\n"
    # Set with no value: compose would use the default, so it says to fix it.
    env.write_text("HARVEY_HOST_DRIVE_SOURCE=\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and env.read_text() == "HARVEY_HOST_DRIVE_SOURCE=\n"


def test_setup_refuses_a_folder_set_on_another_drive(fake_wsl, tmp_path, monkeypatch):
    # An install that followed the old instructions (a folder on C:) while
    # this distro's disk is on D: is told to change it, not that all is well.
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    old = fake_wsl / "c" / "harvey-free"
    old.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"SOME_KEY=1\nHARVEY_HOST_DRIVE_SOURCE={old}\n")
    before = env.read_text()
    code, lines = disk.make_check_folder(env)
    text = "\n".join(lines)
    assert code == 1 and env.read_text() == before
    assert "a folder on C:" in text and "this WSL disk is on D:" in text
    assert str(fake_wsl / "d" / "harvey-disk-check") in text
    assert "docker compose up -d harvey" in text and "SOME_KEY" not in text
    assert not (fake_wsl / "d" / "harvey-disk-check").exists()
    # One on the virtual disk itself, not on any Windows drive: refused too.
    linux = tmp_path / "linux-folder"
    linux.mkdir()
    env.write_text(f"HARVEY_HOST_DRIVE_SOURCE={linux}\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "isn't on a Windows drive" in "\n".join(lines)


def test_setup_refuses_a_folder_set_that_is_missing_or_not_empty(fake_wsl, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Example Distro")
    folder = fake_wsl / "d" / "mine"
    env = tmp_path / ".env"
    env.write_text(f"HARVEY_HOST_DRIVE_SOURCE={folder}\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "doesn't exist" in lines[-1] and not folder.exists()
    folder.mkdir()
    (folder / "photo.jpg").write_text("x")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "isn't empty" in lines[-1]
    assert env.read_text() == f"HARVEY_HOST_DRIVE_SOURCE={folder}\n"
    (folder / "photo.jpg").unlink()
    assert disk.make_check_folder(env)[0] == 0


def test_setup_refuses_a_folder_set_it_cant_check(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Shared")
    folder = fake_wsl / "d" / "mine"
    folder.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"HARVEY_HOST_DRIVE_SOURCE={folder}\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "can't be checked" in lines[-1] and "network share" in lines[-1]


def test_env_setting_drops_an_inline_comment_like_compose(tmp_path):
    env = tmp_path / ".env"
    for line, value in [
        ("HARVEY_HOST_DRIVE_SOURCE=/mnt/g/x # note", "/mnt/g/x"),
        ("HARVEY_HOST_DRIVE_SOURCE=/mnt/g/x\t# note", "/mnt/g/x"),
        ("HARVEY_HOST_DRIVE_SOURCE=/mnt/g/a#b", "/mnt/g/a#b"),
        ('HARVEY_HOST_DRIVE_SOURCE="/mnt/g/my folder # kept" # note', "/mnt/g/my folder # kept"),
        ("export HARVEY_HOST_DRIVE_SOURCE='/mnt/g/x'", "/mnt/g/x"),
        ("HARVEY_HOST_DRIVE_SOURCE= # nothing", ""),
    ]:
        env.write_text(line + "\n")
        assert disk._env_setting(env) == value, line


def test_setup_creates_env_when_missing_and_refuses_a_full_folder(fake_wsl, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    folder = fake_wsl / "c" / "harvey-disk-check"
    folder.mkdir()
    (folder / "notes.txt").write_text("x")
    env = tmp_path / ".env"
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "isn't empty" in lines[0] and not env.exists()
    (folder / "notes.txt").unlink()
    code, lines = disk.make_check_folder(env)
    assert code == 0 and env.read_text() == f"HARVEY_HOST_DRIVE_SOURCE={folder}\n"
    assert env.stat().st_mode & 0o077 == 0


def test_setup_refuses_when_the_drive_cant_be_found(fake_wsl, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Shared")
    env = tmp_path / ".env"
    env.write_text("SOME_KEY=1\n")
    code, lines = disk.make_check_folder(env)
    assert code == 1 and "network share" in lines[0] and "By hand" in lines[1]
    assert env.read_text() == "SOME_KEY=1\n"


def test_the_setup_script_runs_disk_py_with_the_system_python(tmp_path):
    # The script runs outside Harvey's install: disk.py alone, stdlib only.
    # On WSL (in a container, with no reg.exe, or with no such distro) it
    # refuses and leaves .env alone.
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "harvey").mkdir()
    shutil.copy(ROOT / "scripts" / "disk-check-folder.sh", checkout / "scripts")
    shutil.copy(ROOT / "harvey" / "disk.py", checkout / "harvey")
    (checkout / ".env").write_text("SOME_KEY=1\n")
    script = checkout / "scripts" / "disk-check-folder.sh"
    assert os.access(ROOT / "scripts" / "disk-check-folder.sh", os.X_OK)
    # A distro no registry has: on a real WSL host the script must not
    # create a folder on a real drive. The system's python3 when there is
    # one, else this interpreter.
    env = dict(os.environ, WSL_DISTRO_NAME="No Such Distro (test)")
    system = "/usr/bin:/bin"
    if not shutil.which("python3", path=system):
        system = f"{Path(sys.executable).parent}:{system}"
    env["PATH"] = system
    done = subprocess.run(["sh", str(script)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert (checkout / ".env").read_text() == "SOME_KEY=1\n"
    if disk.on_wsl():
        assert done.returncode == 1, done.stderr
        assert "By hand" in done.stderr and "SOME_KEY" not in done.stderr + done.stdout
    else:
        assert done.returncode == 0 and "isn't WSL" in done.stdout
