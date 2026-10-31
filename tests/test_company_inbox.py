"""Company-inbox fallback: a business that names nobody is reached at its published inbox."""

from types import SimpleNamespace

import pytest

import harvey.integrations.email_finder as ef
from harvey.agents.sender import Sender
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect


def inbox(**kw):
    base = dict(email="info@acme.com", company="Acme Roofing", title="General inbox",
                source=COMPANY_INBOX_SOURCE)
    return Prospect(**{**base, **kw})


# ── model ──

def test_inbox_prospect_is_valid_without_a_name():
    p = inbox()
    assert p.is_company_inbox and p.is_valid()
    assert p.full_name() == "Acme Roofing team"


def test_inbox_needs_an_address_and_named_people_still_need_names():
    assert not inbox(email="").is_valid()
    assert not Prospect(email="a@b.com", title="Owner").is_valid()


def test_render_greets_an_inbox_as_there():
    sender = Sender.__new__(Sender)
    assert sender._render("Hi {{first_name}}, {{company}}", inbox()) == "Hi there, Acme Roofing"
    named = Prospect(first_name="Jo", last_name="Li", title="Owner", company="Acme")
    assert sender._render("Hi {{first_name}}", named) == "Hi Jo"


# ── verification ──

@pytest.mark.parametrize("verdict, expected", [
    ({"status": "safe"}, "verified"),
    ({"status": "valid"}, "verified"),
    ({"status": "spamtrap"}, "invalid"),
    ({"status": "disposable"}, "invalid"),
    ({"status": "unknown"}, None),
    ({"status": "safe", "catch_all": True}, "risky"),
])
def test_reoon_verdicts(verdict, expected):
    assert ef._status_from_verdict(verdict) == expected


@pytest.mark.parametrize("graded, expected", [
    ("verified", "verified"),
    ("risky", "risky"),        # catch-all stays risky: send_to_risky decides
    ("invalid", "invalid"),
    (None, "guess"),           # inconclusive stays a guess: never sent
])
@pytest.mark.asyncio
async def test_published_address_grading(monkeypatch, graded, expected):
    async def mx(domain):
        return "smtp.google.com"

    async def check(email, mx_type, env, enabled=None):
        return graded, graded == "risky"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", check)
    assert await ef.verify_published_address("info@acme.com") == expected


@pytest.mark.asyncio
async def test_published_address_without_mail_server_is_invalid(monkeypatch):
    async def no_mx(domain):
        return None

    monkeypatch.setattr(ef, "get_mx_host", no_mx)
    assert await ef.verify_published_address("info@acme.com") == "invalid"


@pytest.mark.parametrize("deliverable, expected", [(True, "valid"), ("false", "unknown"), (None, "unknown")])
@pytest.mark.asyncio
async def test_reoon_role_account_needs_a_real_deliverable_flag(monkeypatch, deliverable, expected):
    class Resp:
        status_code = 200

        def json(self):
            return {"status": "role_account", "is_deliverable": deliverable}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return Resp()

    monkeypatch.setattr(ef.httpx, "AsyncClient", Client)
    verdict = await ef.verify_reoon("info@acme.com", "key")
    assert verdict["status"] == expected


# ── writer ──

def test_inbox_prospects_get_their_own_campaign():
    from harvey.agents.writer import Writer
    writer = Writer.__new__(Writer)
    writer.config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(provider="gmail")))
    named = Prospect(first_name="Jo", last_name="Li", title="Owner", industry="Roofing", email="jo@a.com")
    batches = writer._group_prospects([named, inbox(industry="Roofing")])
    assert set(batches) == {"Roofing-outreach", "Roofing-inbox-outreach"}


class SequenceBrain:
    def __init__(self, steps):
        self.steps = steps

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        return self.steps


def sequence_writer(steps):
    from harvey.agents.writer import Writer
    writer = Writer.__new__(Writer)
    writer.brain = SequenceBrain(steps)
    writer.skills = ""
    writer.config = SimpleNamespace(
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    return writer


@pytest.mark.parametrize("var", [
    "{{first_name}}", "{{last_name}}", "{{title}}",
    "{{ first_name }}", "{{First_Name}}", "{{firstName}}", "{{Title}}",
])
@pytest.mark.asyncio
async def test_inbox_sequence_using_a_person_variable_is_rejected(var):
    # "as General inbox at Acme" must never be rendered and sent.
    steps = [
        {"step": 1, "subject": "quick one", "body": f"Hi {var}, who answers at {{{{company}}}}?", "delay_days": 0},
        {"step": 2, "subject": "following up", "body": "Still curious about {{company}}?", "delay_days": 3},
    ]
    assert await sequence_writer(steps)._write_sequence([inbox(industry="Roofing")]) == []


@pytest.mark.asyncio
async def test_inbox_sequence_with_company_only_is_kept():
    steps = [
        {"step": 1, "subject": "quick one", "body": "Hi there, who answers at {{company}}?", "delay_days": 0},
    ]
    out = await sequence_writer(steps)._write_sequence([inbox(industry="Roofing")])
    assert [s.body for s in out] == ["Hi there, who answers at {{company}}?"]
