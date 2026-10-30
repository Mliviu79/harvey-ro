"""Shared test setup.

Brain's Claude path checks the installed CLI's ``--help`` before sending a
prompt (harvey/claude_runtime.py). Unit tests stub the subprocess itself, so
they also get a stand-in CLI here: found as plain ``claude``, with a help
text listing every isolation flag. Tests of the check itself override these,
and ``@pytest.mark.real_claude_cli`` tests use the real installed CLI. This
lives in tests/ only; the shipped package has no way to skip the check.
"""

import pytest

from harvey import claude_runtime

FAKE_HELP = "\n".join(f"  {flag} <value>   test" for flag in claude_runtime.REQUIRED_FLAGS)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_claude_cli: runs the installed Claude CLI (no stand-in)")
    config.addinivalue_line(
        "markers", "modules_on(*ids): these modules count as switched on")


@pytest.fixture(autouse=True)
def _modules_on(request, monkeypatch):
    """``@pytest.mark.modules_on("hiring", ...)``: the test runs as if the
    user had switched those modules on (several of the original Harvey's,
    such as hiring, startups and web_marketing, ship off)."""
    marker = request.node.get_closest_marker("modules_on")
    if marker is None:
        return
    from harvey.modules import switches

    real = switches.resolve
    forced = {switches.KEY.format(m): switches.ON for m in marker.args}
    monkeypatch.setattr(switches, "resolve",
                        lambda stored, mans=None: real({**stored, **forced}, mans))


@pytest.fixture(autouse=True)
def _no_checkout_env_file(monkeypatch, tmp_path_factory):
    """Upgrading a database looks for .env key names: never the checkout's
    real .env, so no test depends on (or reads) the developer's keys."""
    from harvey.modules import switches

    monkeypatch.setattr(switches, "ENV_FILE", tmp_path_factory.mktemp("env") / ".env")


@pytest.fixture(autouse=True)
def _private_dashboard_token(tmp_path_factory):
    """Never create or read the checkout's real data/dashboard-token."""
    import harvey.dashboard as dash

    dash.token_store.configure(tmp_path_factory.mktemp("token") / "dashboard-token")
    yield
    dash.token_store.configure(dash.TOKEN_FILE)


@pytest.fixture(autouse=True)
def _stand_in_claude_cli(request, monkeypatch):
    claude_runtime.clear_cache()
    if request.node.get_closest_marker("real_claude_cli"):
        yield
        claude_runtime.clear_cache()
        return
    monkeypatch.setattr(claude_runtime, "find_claude", lambda env=None: "claude")
    monkeypatch.setattr(claude_runtime, "_probe_help", lambda exe: FAKE_HELP)
    yield
    claude_runtime.clear_cache()


import harvey.humanize  # noqa: E402


@pytest.fixture(autouse=True)
def _no_installed_humanizer(monkeypatch, tmp_path):
    """Tests never pick up the operator's real ~/.claude Humanizer skill."""
    monkeypatch.setattr(harvey.humanize, "SKILL_PATH", tmp_path / "no-humanizer.md")


@pytest.fixture(autouse=True)
def _no_browser_services(monkeypatch):
    """Tests never reach a real browser service from a developer's .env."""
    for name in ("BROWSER_CDP_URL", "BROWSER_CDP_FALLBACK_URL", "BROWSER_CDP_TOKEN",
                 "HARVEY_BROWSER_CDP_DEFAULT"):
        monkeypatch.delenv(name, raising=False)
