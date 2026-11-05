"""Every signal a module that is on declares is collected: there is nothing
to confirm. What keeps paid collection in check is the run's cap (and, for
the agent, the budget of the plan the user approved), so a paid fact comes
only from a run that has one.
"""

import os
import sqlite3
import tempfile

import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import load_config
from harvey.modules import api, runs as R, switches
from harvey.signals import sync_vocabulary
from harvey.state import StateManager
from tests import slim

# The facts that cost money to collect: a paid search's ranks, a paid
# verifier's answer.
PAID = {"SERP_RANK", "MAPS_RANK", "EMAIL_STATUS"}


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "harvey.db"))
        await sm.init_db()
        await sync_vocabulary(sm)
        yield sm


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def no_geocode(state, city, radius_km=40, client=None):
        return ""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(R, "geocode_city", no_geocode)
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)


class PaidProvider(D.DiscoveryProvider):
    """A search that bills a dollar a query and ranks what it finds."""

    key = "paid"
    label = "Paid"
    cost_note = "$1 a search"

    def __init__(self):
        self.calls = 0

    def configured(self, env):
        return True

    def estimate(self, queries):
        return 1.0 * len(queries)

    async def fetch(self, client, env, query):
        self.calls += 1
        self.last_cost = 1.0
        return [D.Business(name="Exemplu Roofing", domain="exemplu-roofing.example",
                           provider="paid", rank=7)]


async def _codes(state) -> set[str]:
    out = set()
    for cid in slim.live_ids(state):
        out |= {o["signal_code"] for o in await state.get_observations(cid)}
    return out


@pytest.mark.asyncio
@pytest.mark.modules_on("web_marketing", "startups", "hiring")
async def test_a_declared_signal_of_an_enabled_module_is_collected_without_confirmation(state):
    on = await switches.enabled_ids(state)
    declared = {sig["code"] for m in switches.manifests() if m.id in on for sig in m.signals}
    # An older release's decisions are left in the column and read by nothing.
    con = sqlite3.connect(state.db_path)
    with con:
        con.execute("UPDATE signal SET status = 'rejected'")
    con.close()
    collected = await state.collected_signal_codes()
    assert declared <= collected
    assert {"TECH_STACK", "YC_BACKED", "HIRING_ROLE", "SERP_RANK"} <= collected


@pytest.mark.asyncio
async def test_a_paid_rank_is_recorded_only_inside_a_capped_run(state, claim_provider):
    paid = claim_provider(PaidProvider())
    query = [D.DiscoveryQuery(term="roofer", location="Denver")]

    # No room under the cap: no request, no rank.
    report = await R.run_discovery(state, load_config(), "paid", query, max_spend=0.0)
    assert report.stopped == "budget reached"
    assert paid.calls == 0 and report.actual_cost == 0
    assert not await _codes(state) & PAID

    # Inside a cap it covers: one billed search, and its rank is recorded.
    report = await R.run_discovery(state, load_config(), "paid", query, max_spend=1.0)
    assert paid.calls == 1 and report.actual_cost == 1.0
    assert "SERP_RANK" in await _codes(state)


@pytest.mark.modules_on("web_marketing", "startups", "hiring", "boundaries",
                        "email_outreach")
def test_what_runs_with_no_cap_never_returns_a_paid_fact():
    """The work that runs with no cap of its own (a site read, the checks a
    source's companies get, the local lookups by identifier) is free, and
    none of it returns a paid fact: removing the confirmation can't make it
    collect one."""
    every = frozenset(m.id for m in switches.manifests())

    def facts(cap):
        return set((cap.get("outputs") or {}).get("facts") or ())

    free_lookups = R.id_enrichers(every) + R.id_lookups(every)
    assert free_lookups
    for m, cap, _ in free_lookups:
        assert float((cap.get("cost") or {}).get("usd", 0) or 0) == 0, (m.id, cap["id"])
        assert not facts(cap) & PAID, (m.id, cap["id"])
    reads = [(m, cap) for m, cap in api.declared(switches.by_id()) if cap.get("op") == "read"]
    assert reads
    for m, cap in reads:
        assert not facts(cap) & PAID, (m.id, cap["id"])


def test_the_cli_lists_the_signals_and_has_nothing_to_confirm(tmp_path, monkeypatch, capsys):
    import sys

    import harvey.state as state_mod
    from harvey import cli

    monkeypatch.setattr(state_mod, "DB_PATH", str(tmp_path / "harvey.db"))
    monkeypatch.setattr(sys, "argv", ["harvey", "signals"])
    cli.main()
    out = capsys.readouterr().out
    assert "collected" in out and "SERP_RANK" in out and "awaiting" not in out
    assert "--confirm" not in out
    monkeypatch.setattr(sys, "argv", ["harvey", "signals", "--confirm", "free"])
    with pytest.raises(SystemExit):
        cli.main()


def test_no_page_or_doc_asks_for_a_signal_to_be_confirmed():
    """What a user or the agent reads never tells them to confirm a signal,
    nor points at the endpoints and commands that did it."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    files = [root / "README.md", root / "CLAUDE.md", *sorted((root / "prompts").glob("*.md")),
             *sorted((root / "skills").glob("*.md")), *sorted((root / "docs").glob("*.md")),
             *(root / "harvey" / "web" / name for name in ("app.js", "i18n.js", "index.html"))]
    stale = re.compile(r"when you confirm (?:that|those|the|this) signals?|signals --confirm"
                       r"|/api/signals/status|set_signal_status|cohort builder", re.I)
    found = [f"{path.relative_to(root)}: {m.group(0)}" for path in files
             for m in stale.finditer(path.read_text(encoding="utf-8"))]
    assert not found
