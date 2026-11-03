"""A mail provider Harvey no longer supports (Instantly) is refused clearly:
nothing is sent or read, the reason is said in Romanian and English, and
nothing else stops working."""

import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.handler import Handler
from harvey.agents.sender import Sender
from harvey.config import EnvConfig, HarveyConfig, provider_notice
from harvey.models.prospect import Prospect
from harvey.state import StateManager

BASE = {
    "persona": {"name": "Ana", "company": "Exemplu", "role": "Vânzări",
                "email": "ana@trimite.example.com", "linkedin": "", "tone": "direct"},
    "product": {"name": "Programări", "description": "programări online", "pricing": "",
                "key_benefits": [], "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    return state


def config(provider="instantly", enabled=True):
    return HarveyConfig(**BASE, channels={"email": {"provider": provider, "enabled": enabled}})


def test_a_config_naming_instantly_still_loads_and_says_what_to_pick():
    notice = provider_notice(config())
    # Both languages, and the three providers to choose from.
    assert "Instantly nu mai e suportat" in notice and "no longer supported" in notice
    assert all(p in notice for p in ("gmail", "smtp", "microsoft"))
    assert provider_notice(config("gmail")) == provider_notice(config("smtp")) == ""
    assert provider_notice(config("microsoft")) == ""
    # Email off: nothing to say.
    assert provider_notice(config(enabled=False)) == ""
    assert "„woodpecker”" in provider_notice(config("woodpecker"))


def test_the_default_provider_is_gmail_not_instantly():
    assert HarveyConfig(**BASE).channels.email.provider == "gmail"


@pytest.mark.asyncio
async def test_the_sender_sends_nothing_and_says_why(state, caplog):
    sender = Sender(None, state, config(), EnvConfig())
    assert sender.is_native is False and sender.provider is None
    await state.add_outbox_item(prospect_id="p", to_email="ana@alfa.example.com",
                                subject="s", body="b", send_at="2026-01-01T00:00:00",
                                status="approved")
    with caplog.at_level(logging.WARNING, logger="harvey.sender"):
        await sender.run()
    assert "no longer supported" in caplog.text
    assert [i["status"] for i in await state.get_outbox(limit=10)] == ["approved"]


@pytest.mark.asyncio
async def test_the_handler_reads_nothing_and_holds_sending(state):
    handler = Handler(SimpleNamespace(load_skills_for_agent=lambda agent: ""), state, config(),
                      EnvConfig())
    assert await handler.run() is False
    assert "Instantly" in handler.last_read_problem


@pytest.mark.asyncio
async def test_a_reply_handled_with_no_provider_is_queued_not_lost(state, monkeypatch):
    """Code that reaches the reply path anyway (a test, a script) queues the
    answer for review in the Outbox: there is no other way out."""
    from unittest.mock import AsyncMock

    pid = await state.add_prospect(Prospect(first_name="Ana", last_name="Pop", company="Alfa",
                                            title="Owner", email="ana@alfa.example.com"))
    handler = Handler(None, state, config(), EnvConfig())
    handler._classify_intent = AsyncMock(return_value="question")
    handler._generate_response = AsyncMock(return_value="Cu plăcere.")
    await handler._process_reply("ana@alfa.example.com", "Cât costă?", "")
    [item] = await state.get_outbox(limit=10)
    assert (item["prospect_id"], item["kind"], item["status"]) == (pid, "reply", "pending_review")


@pytest.mark.asyncio
async def test_the_heartbeat_does_nothing_on_instantly_and_keeps_running(tmp_path, monkeypatch,
                                                                         caplog):
    """harvey run on an Instantly config: no reply read, nothing sent, the
    reason logged, and the loop sleeps instead of crashing."""
    import asyncio

    import harvey.main as main_mod
    from harvey.modules import switches

    db = str(tmp_path / "harvey.db")
    seeded = StateManager(db)
    await seeded.init_db()
    await switches.set_enabled(seeded, switches.EMAIL_MODULE, True)
    monkeypatch.setattr(main_mod, "load_config", lambda: config())
    monkeypatch.setattr(main_mod, "StateManager", lambda: StateManager(db))
    touched = []

    async def nope(self, *a, **k):
        touched.append(type(self).__name__)

    monkeypatch.setattr(Handler, "_run_native", nope)
    monkeypatch.setattr(Sender, "_run_native", nope)
    stop = asyncio.Event()
    slept = []

    async def sleep(seconds, stop_event):
        slept.append(seconds)
        stop_event.set()
        return True

    monkeypatch.setattr(main_mod, "_interruptible_sleep", sleep)
    with caplog.at_level(logging.WARNING, logger="harvey"):
        await main_mod.heartbeat(stop)
    assert touched == [] and slept
    assert "Instantly nu mai e suportat" in caplog.text


@pytest.mark.asyncio
async def test_training_on_a_local_url_says_why_it_was_refused():
    from harvey.trainer import FallbackCrawler
    crawler = FallbackCrawler()
    assert await crawler.crawl("http://localhost:3000", max_pages=3) == {}
    assert crawler.blocked is True


def test_status_still_works_when_the_config_is_invalid(tmp_path, monkeypatch, capsys):
    # `harvey status` is what people run when something's wrong, so an
    # invalid harvey.yaml must not crash the sending-hold check.
    import harvey.config
    import harvey.state
    from harvey.cli import cmd_status
    from pydantic import ValidationError

    def invalid():
        HarveyConfig(channels={"email": {"max_daily_sends": "lots"}})

    try:
        invalid()
    except ValidationError:
        pass
    real_state = harvey.state.StateManager
    monkeypatch.setattr(harvey.state, "StateManager", lambda: real_state(str(tmp_path / "s.db")))
    monkeypatch.setattr(harvey.config, "load_config", invalid)
    cmd_status(SimpleNamespace(check=False))
    assert "Prospects:" in capsys.readouterr().out


def test_status_says_sending_is_held_on_instantly(tmp_path, monkeypatch, capsys):
    import harvey.config
    import harvey.state
    from harvey.cli import cmd_status

    real_state = harvey.state.StateManager
    monkeypatch.setattr(harvey.state, "StateManager", lambda: real_state(str(tmp_path / "s.db")))
    monkeypatch.setattr(harvey.config, "load_config", lambda *a: config())
    cmd_status(SimpleNamespace(check=False))
    out = capsys.readouterr().out
    assert "Sending:              HELD" in out and "gmail, smtp or microsoft" in " ".join(out.split())
