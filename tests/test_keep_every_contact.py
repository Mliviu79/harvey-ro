"""Every contact Scout finds is kept; the score only orders them.

Poorly scored contacts used to be dropped before they reached the database.
Now they are saved and ranked last, and only the Writer leaves them out of
campaigns (a score of 0 means never scored, which is still written to).
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.scout import Scout
from harvey.agents.writer import Writer
from harvey.models.prospect import Prospect
from harvey.state import LOW_SCORE, StateManager, poorly_scored


class ScoringBrain:
    def __init__(self, scores):
        self.scores = scores

    def load_skills_for_agent(self, name):
        return ""

    async def think_json(self, prompt, **kw):
        return [{"index": i + 1, "score": s, "personalization": ""}
                for i, s in enumerate(self.scores)]


def _cfg():
    return SimpleNamespace(
        product=SimpleNamespace(description="D"),
        icp=SimpleNamespace(industries=["Roofing"], titles=["Owner"], company_size="1-10"),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "k.db"))
    await s.init_db()
    return s


def _person(first, email=""):
    return Prospect(first_name=first, last_name="Pop", title="Owner", company="EXEMPLU SRL",
                    industry="Roofing", email=email or f"{first.lower()}@example.com",
                    email_status="verified")


@pytest.mark.asyncio
async def test_scoring_keeps_low_scores_and_ranks_them_last(state):
    scout = Scout.__new__(Scout)
    scout.brain, scout.state, scout.config = ScoringBrain([12, 90, 29, 55]), state, _cfg()
    people = [_person(n) for n in ("Ana", "Bogdan", "Carmen", "Dan")]
    scored = await scout._score_contacts(people)
    assert [p.first_name for p in scored] == ["Bogdan", "Dan", "Carmen", "Ana"]
    assert [p.score for p in scored] == [90, 55, 29, 12]


@pytest.mark.asyncio
async def test_every_scored_contact_is_saved_past_the_cycle_cap(state, monkeypatch):
    """The pages were already read: stopping at the per-cycle cap lost the rest."""
    import harvey.agents.scout as scout_mod
    monkeypatch.setattr(scout_mod, "MAX_PROSPECTS_PER_CYCLE", 2)
    scout = Scout.__new__(Scout)
    scout.brain, scout.state, scout.config = ScoringBrain([80, 10, 50, 5]), state, _cfg()
    people = [_person(n) for n in ("Ana", "Bogdan", "Carmen", "Dan")]

    async def companies():
        return [{"name": "EXEMPLU SRL", "domain": "example.com", "industry": "Roofing"}]

    async def none():
        return []

    async def ensure_company(**kw):
        return "c1"

    async def no_signals(*a, **kw):
        return ""

    async def contacts(**kw):
        return people

    scout._find_companies_via_search = companies
    scout._find_companies_via_directories = none
    scout._ensure_company = ensure_company
    scout._enrich_company_signals = no_signals
    scout._contacts_from_company = contacts
    assert await scout._prospect_via_company_discovery() == 4
    on_file = await state.get_prospects_by_status("new")
    assert sorted(p.score for p in on_file) == [5, 10, 50, 80]


@pytest.mark.asyncio
async def test_low_scored_contacts_stay_on_file_but_are_not_writable(state):
    for first, score in (("Ana", 80), ("Bogdan", LOW_SCORE - 1), ("Carmen", 0)):
        p = _person(first)
        p.score = score
        await state.add_prospect(p)
    on_file = await state.get_prospects_by_status("new")
    assert {p.first_name for p in on_file} == {"Ana", "Bogdan", "Carmen"}
    # Bogdan was scored and scored low; Carmen was never scored.
    assert await state.count_writable_prospects() == 2
    assert poorly_scored(LOW_SCORE - 1) and not poorly_scored(0)
    assert not poorly_scored(LOW_SCORE) and not poorly_scored(None)


@pytest.mark.asyncio
async def test_the_writer_leaves_poorly_scored_contacts_out(state, monkeypatch):
    for first, score in (("Ana", 80), ("Bogdan", 10), ("Carmen", 0)):
        p = _person(first)
        p.score = score
        await state.add_prospect(p)
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="smtp", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        humanizer=SimpleNamespace(enabled=False),
    )
    writer = Writer(brain, state, cfg)
    grouped = []
    monkeypatch.setattr(writer, "_group_prospects",
                        lambda ps: grouped.extend(p.first_name for p in ps) or {})
    await writer.run()
    assert sorted(grouped) == ["Ana", "Carmen"]


@pytest.mark.asyncio
async def test_a_companys_contacts_list_ranks_low_scores_last(state):
    from harvey.models import Company
    cid = await state.add_company(Company(name="EXEMPLU SRL", domain="example.com"))
    for first, score in (("Ana", 10), ("Bogdan", 90)):
        p = _person(first)
        p.company_id, p.score = cid, score
        await state.add_prospect(p)
    rows = await state.get_contacts_for_company(cid)
    assert [r.first_name for r in rows] == ["Bogdan", "Ana"]
