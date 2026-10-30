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
    config.addinivalue_line(
        "markers", "live_anaf_client: the ANAF request code runs (against a stubbed client)")


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


@pytest.fixture(autouse=True)
def _no_live_anaf(monkeypatch, tmp_path_factory, request):
    """No test reaches ANAF's real web service (a fictional CUI could be a
    real business's), keeps its answers in the checkout's data/, or waits
    on the checkout's lane file. A test of the request itself stubs the
    HTTP client and marks itself ``live_anaf_client``."""
    from harvey.modules import lanes
    from harvey.modules.ro.anaf import service

    monkeypatch.setattr(lanes, "LANE_DIR", tmp_path_factory.mktemp("lanes"))
    monkeypatch.setattr(service, "CACHE_PATH", tmp_path_factory.mktemp("anaf") / "anaf.db")
    from harvey.modules.ro.mf_taxpayers import store as taxpayers

    monkeypatch.setattr(taxpayers, "TAXPAYERS_PATH",
                        tmp_path_factory.mktemp("taxpayers") / "taxpayers.db")
    if request.node.get_closest_marker("live_anaf_client"):
        return

    async def refuse(client, cuis, **kw):
        raise service.AnafError("ANAF is not asked in tests")

    monkeypatch.setattr(service, "ask", refuse)


# Every fact a discovery result can give (discover.business_facts).
_SEARCH_FACTS = ["FOUND_IN_SERP", "NO_WEBSITE", "UNCLAIMED_LISTING", "REVIEW_RATING",
                 "REVIEW_COUNT", "SERP_RANK", "IN_LOCAL_PACK", "MAPS_RANK"]


@pytest.fixture
def claim_provider(monkeypatch):
    """``claim_provider(provider)``: a test's stand-in discovery source,
    claimed by a module of its own (``stub_<key>``), as a real one is. A
    source no module claims is refused, so a stand-in needs one too."""
    import sys
    import types
    from pathlib import Path

    from harvey.collectors import discover as D
    from harvey.modules import switches
    from harvey.modules.loader import Manifest

    def install(provider, module_id: str | None = None):
        key = provider.key
        module_id = module_id or f"stub_{key}"
        monkeypatch.setitem(D.PROVIDERS, key, provider)
        entry = f"harvey.modules._stub_{module_id}"
        code = types.ModuleType(entry)
        code.CAPABILITIES = D.provider_capabilities(module_id, {"web_search": key})
        monkeypatch.setitem(sys.modules, entry, code)
        manifest = Manifest(
            id=module_id, set="original", kind="search", version="0.0.1",
            path=Path("tests") / module_id / "module.toml", name=f"Stub {key}",
            default_enabled=True, entry=entry,
            capabilities=({"id": "web_search", "op": "query", "provider": key,
                           "outputs": {"records": "companies", "ids": ["domain"],
                                       "facts": list(_SEARCH_FACTS)}},))
        mans = tuple(m for m in switches.manifests() if m.id != module_id) + (manifest,)
        monkeypatch.setattr(switches, "manifests", lambda: mans)
        return provider

    return install
