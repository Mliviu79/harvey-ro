from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.agents.scout import Scout

STRATEGIES = (
    "hunter", "known_companies", "linkedin",
    "hiring_boards", "profile_search", "company_discovery",
)


def _scout(linkedin_enabled: bool, linkedin_email: str = "") -> Scout:
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    state = SimpleNamespace(log_action=AsyncMock(), settings_with_prefix=AsyncMock(return_value={}))
    config = SimpleNamespace(
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=linkedin_enabled))
    )
    env = SimpleNamespace(linkedin_email=linkedin_email)
    scout = Scout(brain, state, config, env)
    for name in STRATEGIES:
        setattr(scout, f"_prospect_via_{name}", AsyncMock(return_value=0))
    return scout


@pytest.mark.asyncio
@pytest.mark.modules_on("linkedin")
async def test_profile_search_rides_the_linkedin_switch():
    """site:linkedin.com/in searches yield names with no company or email,
    which Harvey can never verify or send to, so they are off by default."""
    scout = _scout(linkedin_enabled=False)
    await scout.run()
    scout._prospect_via_profile_search.assert_not_awaited()
    scout._prospect_via_company_discovery.assert_awaited_once()

    scout = _scout(linkedin_enabled=True, linkedin_email="me@example.com")
    await scout.run()
    scout._prospect_via_profile_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_search_needs_a_linkedin_login_like_the_linkedin_strategy():
    """Switched on but with no LinkedIn account, nothing could use the
    profiles, so the search would only spend the cycle's query budget."""
    scout = _scout(linkedin_enabled=True)
    await scout.run()
    scout._prospect_via_profile_search.assert_not_awaited()
    scout._prospect_via_linkedin.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategies_of_a_module_that_is_off_never_run():
    """LinkedIn, hiring and Hunter strategies belong to their modules: with
    the module off (linkedin and hiring ship off) they don't run, whatever
    the config says."""
    scout = _scout(linkedin_enabled=True, linkedin_email="me@example.com")
    await scout.run()
    scout._prospect_via_linkedin.assert_not_awaited()
    scout._prospect_via_profile_search.assert_not_awaited()
    scout._prospect_via_hiring_boards.assert_not_awaited()
    scout._prospect_via_hunter.assert_awaited_once()

    scout = _scout(linkedin_enabled=False)
    scout.state.settings_with_prefix = AsyncMock(return_value={"modules.hunter.enabled": "off"})
    await scout.run()
    scout._prospect_via_hunter.assert_not_awaited()
    scout._prospect_via_known_companies.assert_awaited_once()


@pytest.mark.asyncio
async def test_what_a_cycle_skips_is_said_once_and_kept_in_activity(caplog):
    """Nothing is skipped in silence: one warning per skipped part per
    cycle, and the cycle's Activity entry lists them with their modules."""
    scout = _scout(linkedin_enabled=True, linkedin_email="me@example.com")
    with caplog.at_level("WARNING", logger="harvey.agents.scout"):
        await scout.run()
        assert not await scout._uses("hiring", "Job-board search")   # asked again: no new line
    lines = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert lines == [
        "Scout: LinkedIn prospecting skipped: the linkedin module is off "
        "(harvey modules enable linkedin).",
        "Scout: Job-board search skipped: the hiring module is off "
        "(harvey modules enable hiring).",
    ]
    details = scout.state.log_action.await_args.kwargs["details"]
    assert details["skipped"] == [
        {"what": "LinkedIn prospecting", "module": "linkedin"},
        {"what": "Job-board search", "module": "hiring"},
    ]


@pytest.mark.asyncio
async def test_no_web_search_module_is_one_warning_per_cycle(caplog):
    scout = _scout(linkedin_enabled=False)
    scout._modules = {"website"}
    scout.env = SimpleNamespace(serper_api_key="", tinyfish_api_key="")
    with caplog.at_level("WARNING", logger="harvey.agents.scout"):
        assert await scout._web_search("roofing example") == []
        assert await scout._web_search("roofing example two") == []
    assert sum("Web search skipped" in r.getMessage() for r in caplog.records) == 1
    assert scout._skipped == {"Web search": "web_search_scrape"}


@pytest.mark.asyncio
@pytest.mark.modules_on("hiring")
async def test_the_job_board_sweep_detects_tools_only_with_web_marketing_on(monkeypatch):
    import sys

    class Jobs:
        empty = False

        def to_dict(self, kind):
            return [{"company": "Exemplu SRL", "title": "Engineer", "job_url": ""}]

    monkeypatch.setitem(sys.modules, "jobspy", SimpleNamespace(scrape_jobs=lambda **k: Jobs()))
    homepage = '<script src="https://js.hs-scripts.com/1.js"></script>'

    async def run(modules):
        scout = _scout(linkedin_enabled=False)
        scout._modules, scout._skipped, scout._seen_domains = modules, {}, set()
        scout.config.icp = SimpleNamespace(geography=[], industries=["software"])
        scout.state.company_exists = AsyncMock(return_value=False)
        scout.state.update_company_signals = AsyncMock()
        monkeypatch.setattr(scout, "_signal_role_keywords", lambda: ["engineer"])
        monkeypatch.setattr(scout, "_guess_domain", AsyncMock(return_value="exemplu.example.com"))
        monkeypatch.setattr(scout, "_is_candidate_domain", lambda d: True)
        monkeypatch.setattr(scout, "_scrape_company_info",
                            AsyncMock(return_value={"_homepage_html": homepage}))
        monkeypatch.setattr(scout, "_ensure_company", AsyncMock(return_value="c1"))
        monkeypatch.setattr(scout, "_contacts_from_company", AsyncMock(return_value=[]))
        await Scout._prospect_via_hiring_boards(scout)   # _scout() stubs the strategies
        return [c.kwargs for c in scout.state.update_company_signals.await_args_list
                if "tech_stack" in c.kwargs]

    assert await run({"hiring", "web_marketing"})
    assert await run({"hiring"}) == []
