"""The model's reply, or its absence: a draft that comes back unusable is
told apart from no reply at all (an outage, a logout, a rate limit), so the
caller never mistakes one for the other, and a campaign's "Generează
secvența" returns nothing usable rather than a broken sequence.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.writer import Writer
from harvey.models.campaign import Campaign
from harvey.models.prospect import Prospect
from harvey.state import StateManager


class FakeBrain:
    def __init__(self, reply=None, paused=""):
        self.reply = reply
        self.paused_reason = paused
        self.calls = 0

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    def for_profile(self, profile_id):
        return self

    async def think_json(self, prompt, **kw):
        self.calls += 1
        return None if self.paused_reason else self.reply


CONFIG = SimpleNamespace(
    channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=False)),
    product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
    persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
)


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "w.db"))
    await state.init_db()
    return state


@pytest.mark.asyncio
async def test_an_unusable_sequence_is_returned_as_nothing(state):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Pop", title="Owner", company="Alfa",
        email="ana@alfa.example.com", email_status="verified"))
    brain = FakeBrain(reply="not a sequence")
    writer = Writer(brain, state, CONFIG, None)
    steps = await writer.write_templates(Campaign(id="c", name="Test"),
                                         [await state.get_prospect(pid)])
    assert steps == [] and brain.calls == 1


@pytest.mark.asyncio
async def test_the_real_brain_reports_whether_the_model_replied(state, monkeypatch):
    from harvey.brain import Brain
    from harvey.config import BrainConfig
    brain = Brain(state, BrainConfig())

    async def silent(*a, **kw):
        return ""

    async def chatty(*a, **kw):
        return "Sorry, here are some thoughts instead of JSON."

    monkeypatch.setattr(brain, "think", silent)
    assert await brain.think_json("x") is None and brain.last_reply_empty is True
    monkeypatch.setattr(brain, "think", chatty)
    assert await brain.think_json("x") is None and brain.last_reply_empty is False


class _ClaudeProcess:
    """A Claude CLI that exits 0 with the given JSON result payload."""

    returncode = 0

    def __init__(self, payload):
        self.payload = payload

    async def communicate(self, data=None):
        import json
        return json.dumps(self.payload).encode(), b""


@pytest.mark.asyncio
async def test_a_cli_reported_api_error_is_no_reply_not_model_text(state, monkeypatch):
    # The CLI can report an API error (overloaded, rate limited) inside a
    # successful exit. That text is not the model's answer: no caller should
    # treat it as a draft, and the Writer must not count it as a bad one.
    import harvey.brain as brain_mod
    from harvey.brain import Brain

    payloads = []

    async def fake_exec(*cmd, **kwargs):
        return _ClaudeProcess(payloads.pop(0))

    async def no_sleep(*a, **kw):
        return None

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(brain_mod.asyncio, "sleep", no_sleep)
    brain = Brain(state)

    payloads[:] = [{"type": "result", "is_error": True, "result": "API Error: 529 Overloaded"}] * 5
    assert await brain.think("hi", max_retries=1) == ""
    assert await brain.think_json("hi") is None and brain.last_reply_empty is True

    payloads[:] = [{"type": "result", "is_error": False, "result": '{"ok": true}'}]
    assert await brain.think_json("hi") == {"ok": True} and brain.last_reply_empty is False
