"""Every config change is saved to harvey.local.yaml, never the tracked harvey.yaml.

harvey.yaml is the template git ships. A setup, a dashboard switch or a
training that rewrote it made the next `git pull` refuse to update. The
first change copies the config in use into harvey.local.yaml (comments and
all), and every change after that, and every read, uses that file.
"""

import fnmatch
import logging
import os
import shutil
import threading

import pytest
import yaml

import harvey.config as config_module
from harvey.config import (
    ConfigError, _find_config_file, _temp_beside, config_to_edit, live_config_file,
    live_do_not_contact, load_config, local_config_file, save_config_change,
    set_require_approval, set_yaml_values, writable_config_file,
)
from harvey.paths import PROJECT_ROOT
from harvey.trainer import write_trained_config


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", path)
    return path


def test_the_first_write_copies_the_template_byte_for_byte(template):
    before = template.read_bytes()
    assert b"#" in before                     # the template carries comments
    local = writable_config_file(template)
    assert local == template.with_name("harvey.local.yaml")
    assert local.read_bytes() == before       # comments and all
    assert template.read_bytes() == before
    assert not list(template.parent.glob(".*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_copy_keeps_the_files_permissions(template):
    template.chmod(0o640)
    assert writable_config_file(template).stat().st_mode & 0o777 == 0o640


def test_an_existing_local_file_is_written_in_place(template):
    local = template.with_name("harvey.local.yaml")
    local.write_text(template.read_text().replace("max_daily_sends: 50", "max_daily_sends: 7"))
    mine = local.read_bytes()
    assert writable_config_file(template) == local
    assert local.read_bytes() == mine         # never replaced by the template
    set_require_approval(False, template)
    assert load_config(str(local)).channels.email.require_approval is False
    assert load_config(str(local)).channels.email.max_daily_sends == 7


def test_a_file_under_another_name_is_written_in_place(tmp_path, template):
    other = tmp_path / "custom.yaml"
    shutil.copy(template, other)
    assert local_config_file(other) == other
    assert set_require_approval(False, other) == other
    assert not (tmp_path / "harvey.local.yaml").exists()


def test_reads_prefer_the_local_file(template, monkeypatch):
    monkeypatch.chdir(template.parent)
    assert _find_config_file() == str(template)
    assert live_config_file(template) == template
    written = set_require_approval(False)     # the config Harvey loads
    assert _find_config_file() == str(written) == str(template.with_name("harvey.local.yaml"))
    assert live_config_file(template) == written
    assert load_config().channels.email.require_approval is False


def test_a_config_found_in_the_parent_folder_gets_its_local_file_there(template, monkeypatch):
    sub = template.parent / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert _find_config_file() == str(template)
    written = writable_config_file()
    assert written == template.with_name("harvey.local.yaml")
    assert _find_config_file() == str(written)  # and is read from there


def test_training_into_harvey_yaml_trains_into_harvey_local_yaml(template):
    before = template.read_bytes()
    learned = yaml.safe_load(before)
    learned["product"]["name"] = "Example Product"
    write_trained_config(learned, template)
    assert template.read_bytes() == before
    local = template.with_name("harvey.local.yaml")
    assert load_config(str(local)).product.name == "Example Product"


def test_the_do_not_contact_list_follows_the_local_file(template):
    config = load_config(str(template))
    local = writable_config_file(template)
    data = yaml.safe_load(local.read_text())
    data.setdefault("compliance", {})["do_not_contact"] = ["rival.example"]
    local.write_text(yaml.safe_dump(data, sort_keys=False))
    assert live_do_not_contact(config) == ["rival.example"]


# ── a failed or racing change, leftovers, links ──


def test_a_failed_switch_leaves_no_copy_behind(tmp_path):
    template = tmp_path / "harvey.yaml"
    template.write_text("# t\npersona: {company: Example Co}\n")
    with pytest.raises(ConfigError):
        set_require_approval(False, template)        # no channels.email section
    assert not (tmp_path / "harvey.local.yaml").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_a_failed_edit_writes_nothing(template):
    def edit(data, name):
        raise ValueError("no")
    with pytest.raises(ValueError):
        save_config_change(edit, template)
    assert not template.with_name("harvey.local.yaml").exists()


def test_a_local_file_made_meanwhile_gets_the_change_not_replaced(template, monkeypatch):
    """Someone else makes harvey.local.yaml between the read and the write:
    the change is applied to theirs."""
    local = template.with_name("harvey.local.yaml")
    theirs = template.read_text().replace("max_daily_sends: 50", "max_daily_sends: 7")
    real = config_module._create_new
    raced = []

    def create(target, data, mode_from):
        if not raced:
            raced.append(1)
            local.write_text(theirs)
        return real(target, data, mode_from)
    monkeypatch.setattr(config_module, "_create_new", create)
    set_require_approval(False, template)
    config = load_config(str(local))
    assert config.channels.email.require_approval is False
    assert config.channels.email.max_daily_sends == 7


def test_threads_making_the_first_copy_at_once_all_succeed(template):
    errors = []
    barrier = threading.Barrier(8)

    def go():
        try:
            barrier.wait()
            writable_config_file(template)
        except Exception as e:                       # pragma: no cover - the failure
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert template.with_name("harvey.local.yaml").read_bytes() == template.read_bytes()


def test_temp_files_are_named_so_gitignore_covers_a_leftover(template):
    patterns = [line.strip() for line in (PROJECT_ROOT / ".gitignore").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    tmp = _temp_beside(template.with_name("harvey.local.yaml"))
    try:
        assert any(fnmatch.fnmatch(tmp.name, pat) for pat in patterns), tmp.name
    finally:
        tmp.unlink()


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_dangling_local_link_is_a_config_error(template):
    local = template.with_name("harvey.local.yaml")
    local.symlink_to(template.parent / "nowhere.yaml")
    with pytest.raises(ConfigError, match="link"):
        set_require_approval(False, template)
    assert not (template.parent / "nowhere.yaml").exists()


# ── hand edits ──


def test_a_one_key_local_file_says_it_must_hold_every_setting(template, caplog):
    local = template.with_name("harvey.local.yaml")
    local.write_text("brain:\n  provider: claude\n")
    with caplog.at_level(logging.ERROR, logger="harvey.config"):
        with pytest.raises(Exception):
            load_config(str(local))
    assert "read instead of harvey.yaml" in caplog.text


def test_messages_say_to_copy_the_template_until_there_is_a_local_file(template, monkeypatch):
    monkeypatch.chdir(template.parent)
    monkeypatch.setattr(config_module, "PROJECT_ROOT", template.parent)
    assert config_to_edit() == "harvey.local.yaml (copy harvey.yaml to it first)"
    writable_config_file(template)
    assert config_to_edit() == "harvey.local.yaml"


# ── the in-place key editor ──


def test_set_yaml_values_keeps_comments_and_adds_missing_keys():
    raw = ("# top\nusage:\n  # how much\n  max_daily_claude_percent: 80\n"
           "  quiet_hours:\n    start: \"22:00\"\n\n# next\nchannels:\n  email:\n"
           "    enabled: true\n")
    out = set_yaml_values(raw, [
        (("usage", "max_daily_claude_percent"), 60),
        (("usage", "quiet_hours", "end"), "07:00"),
        (("channels", "email", "max_daily_sends"), 20),
        (("brain",), {"provider": "claude"}),
    ])
    assert yaml.safe_load(out) == {
        "usage": {"max_daily_claude_percent": 60, "quiet_hours": {"start": "22:00", "end": "07:00"}},
        "channels": {"email": {"enabled": True, "max_daily_sends": 20}},
        "brain": {"provider": "claude"},
    }
    for comment in ("# top", "# how much", "# next"):
        assert comment in out


def test_set_yaml_values_keeps_a_changed_scalars_note():
    out = set_yaml_values('a:\n  country: ""   # GB or US\n', [(("a", "country"), "GB")])
    assert out == "a:\n  country: GB   # GB or US\n"
    assert yaml.safe_load(out) == {"a": {"country": "GB"}}
