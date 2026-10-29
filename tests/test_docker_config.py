"""Compose must preserve atomic saves without exposing the dashboard."""
from pathlib import Path

import yaml

from harvey.paths import PROJECT_ROOT


def service():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]["harvey"]


def test_compose_preserves_loopback_and_uses_dashboard():
    config = service()
    assert config["network_mode"] == "host"
    assert not config.get("ports")
    assert config["command"] == ["dashboard"]
    assert config["init"] is True
    # Credentials edited through setup/dashboard must not be masked by an
    # environment snapshot captured when the container was created.
    assert "env_file" not in config


def test_compose_mounts_directory_for_atomic_saves():
    mounts = service()["volumes"]
    root = next(mount for mount in mounts if mount["target"] == "/app")
    assert root["type"] == "bind" and root["source"] == "."
    assert root["bind"]["create_host_path"] is False
    assert not root.get("read_only", False)
    # Nested single-file mounts would prevent os.replace for these files.
    assert not any(mount["target"].startswith("/app/") for mount in mounts)


def test_setup_and_atomic_training_saves_survive_reloading(tmp_path, monkeypatch):
    from harvey import setup, dashboard
    from harvey.config import load_config
    from harvey.trainer import write_trained_config

    base = tmp_path / "harvey.yaml"
    base.write_text((PROJECT_ROOT / "harvey.yaml").read_text())
    env = tmp_path / ".env"
    env.write_text("SERPER_API_KEY=fixture_original\n")
    monkeypatch.setattr(setup, "CONFIG_FILE", base)
    monkeypatch.setattr(setup, "ENV_FILE", env)
    monkeypatch.setattr(dashboard, "ENV_FILE", env)
    wizard = setup.SetupWizard()
    wizard.env_vars = {"SERPER_API_KEY": "fixture_setup"}
    wizard.config = {"usage": {"max_daily_claude_percent": 7}}
    wizard._write_env()
    wizard._write_config()
    assert wizard.config_saved
    dashboard._write_env_file({"SERPER_API_KEY": "fixture_dashboard"})
    learned = yaml.safe_load(base.read_text())
    learned["product"]["name"] = "Fixture Product"
    local = tmp_path / "harvey.local.yaml"
    write_trained_config(learned, local)
    learned["product"]["name"] = "Fixture Updated"
    write_trained_config(learned, local)
    assert load_config(str(local)).product.name == "Fixture Updated"
    assert load_config(str(local)).usage.max_daily_claude_percent == 7
    assert "SERPER_API_KEY=fixture_dashboard" in env.read_text()


def services():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]


def test_browser_services_are_pinned_and_loopback_only():
    svc = services()
    for name in ("obscura", "chromium"):
        config = svc[name]
        assert "@sha256:" in config["image"] and ":latest" not in config["image"]
        assert all(port.startswith("127.0.0.1:") for port in config["ports"])
        assert config["cap_drop"] == ["ALL"]
        assert config["healthcheck"]["test"][0] == "CMD"
    # Obscura is the default service and needs its CDP token; Chromium is opt-in.
    assert "profiles" not in svc["obscura"]
    assert svc["obscura"]["environment"]["OBSCURA_CDP_TOKEN"] == "${BROWSER_CDP_TOKEN:-}"
    # Obscura checks the Host header's port: both sides of the mapping match.
    host_port, container_port = svc["obscura"]["ports"][0].split(":")[1:]
    assert host_port == container_port and container_port in svc["obscura"]["command"]
    assert svc["chromium"]["profiles"] == ["chromium"]
    assert svc["chromium"]["user"] != "0"


def test_harvey_image_ships_no_browser():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    assert "playwright install" not in dockerfile
    assert "ms-playwright" not in dockerfile
    assert "playwright" in (PROJECT_ROOT / "requirements.txt").read_text()


def test_chromium_reaches_the_web_only_through_the_egress_proxy():
    svc = services()
    chromium, egress = svc["chromium"], svc["chromium-egress"]
    assert "--proxy-server=http://chromium-egress:3128" in chromium["command"]
    # Without this Chromium skips the proxy for loopback addresses.
    assert "--proxy-bypass-list=<-loopback>" in chromium["command"]
    assert egress["profiles"] == ["chromium"] and "ports" not in egress
    assert "@sha256:" in egress["image"]
    conf = (PROJECT_ROOT / "docker" / "browser-egress.conf").read_text()
    for cidr in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                 "192.168.0.0/16", "fc00::/7", "::1"):
        assert cidr in conf
    assert conf.index("http_access deny nonpublic") < conf.index("http_access allow")


def test_harvey_uses_the_obscura_service_by_default():
    env = service()["environment"]
    port = services()["obscura"]["ports"][0].split(":")[1]
    assert env["HARVEY_BROWSER_CDP_DEFAULT"] == f"http://127.0.0.1:{port}"


def test_setup_writes_a_browser_token_once(tmp_path, monkeypatch):
    from harvey import setup
    env = tmp_path / ".env"
    env.write_text("BROWSER_CDP_TOKEN=\n")
    monkeypatch.setattr(setup, "ENV_FILE", env)
    wizard = setup.SetupWizard()
    wizard.env_vars = {}
    wizard._write_env()
    token = setup._read_env_file()["BROWSER_CDP_TOKEN"]
    assert len(token) == 64
    wizard._write_env()
    assert setup._read_env_file()["BROWSER_CDP_TOKEN"] == token
