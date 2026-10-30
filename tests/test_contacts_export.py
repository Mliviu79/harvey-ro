"""The phone/location and per-company exports."""

import csv
import io

import pytest
import pytest_asyncio

from harvey.export import (
    COMPANY_CSV_COLUMNS, CSV_COLUMNS, collect_companies, collect_prospects,
    export_companies_csv,
)
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


# ── exports ──

@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "contacts.db"))
    await state.init_db()
    return state


async def _company(state, name, domain, phone="", location=""):
    return await state.add_company(Company(
        name=name, domain=domain, website=f"https://{domain}",
        industry="dentist", location=location, phone=phone, source="discover:osm",
    ))


async def _prospect(state, company_id, first, email, email_status="verified",
                    status="new", score=50, phone=""):
    await state.add_prospect(Prospect(
        first_name=first, last_name="Example", title="Owner", company="Example",
        company_id=company_id, email=email, email_status=email_status,
        status=status, score=score, phone=phone,
    ))


@pytest.mark.asyncio
async def test_prospect_export_appends_phone_and_location(state):
    assert CSV_COLUMNS[-2:] == ["phone", "location"]
    cid = await _company(state, "Clinica Alfa", "alfa.example", phone="+40 700 000 001",
                         location="Cluj-Napoca")
    await _prospect(state, cid, "Ana", "ana@alfa.example")
    await _prospect(state, cid, "Ion", "ion@alfa.example", phone="+40 700 000 002")
    rows = {r["first_name"]: r for r in await collect_prospects(state, config=None)}
    assert rows["Ana"]["phone"] == "+40 700 000 001"      # company's line
    assert rows["Ion"]["phone"] == "+40 700 000 002"      # the person's own
    assert rows["Ana"]["location"] == "Cluj-Napoca"


@pytest.mark.asyncio
async def test_company_export_keeps_companies_without_contacts(state, tmp_path):
    a = await _company(state, "Alfa Dental", "alfa.example", phone="+40 700 000 001",
                       location="Iasi")
    await _company(state, "Beta Lab", "beta.example", phone="+40 700 000 003")
    await _prospect(state, a, "Guess", "guess@alfa.example", email_status="guess", score=90)
    await _prospect(state, a, "Best", "best@alfa.example", email_status="verified", score=10)

    out = tmp_path / "companies.csv"
    count, text = await export_companies_csv(state, out_path=str(out))
    assert count == 2
    reader = csv.DictReader(io.StringIO(out.read_text(encoding="utf-8")))
    assert reader.fieldnames == COMPANY_CSV_COLUMNS
    rows = {r["name"]: r for r in reader}
    assert rows["Alfa Dental"]["email"] == "best@alfa.example"
    assert rows["Alfa Dental"]["contact_name"] == "Best Example"
    assert rows["Alfa Dental"]["location"] == "Iasi"
    assert rows["Beta Lab"]["email"] == ""
    assert rows["Beta Lab"]["phone"] == "+40 700 000 003"


@pytest.mark.asyncio
async def test_company_export_applies_suppression(state, monkeypatch):
    from harvey import export

    a = await _company(state, "Alfa", "alfa.example")
    b = await _company(state, "Beta", "beta.example")
    c = await _company(state, "Gamma", "gamma.example")
    await _prospect(state, a, "Optout", "optout@alfa.example", status="opted_out")
    await _prospect(state, b, "Blocked", "blocked@beta.example", score=90)
    await _prospect(state, b, "Other", "other@beta.example", email_status="risky")
    await _prospect(state, c, "Anyone", "anyone@gamma.example")
    monkeypatch.setattr(export, "_do_not_contact",
                        lambda config: ["blocked@beta.example", "gamma.example"])

    rows = {r["name"]: r for r in await collect_companies(state)}
    assert set(rows) == {"Alfa", "Beta"}                 # domain entry drops Gamma
    assert rows["Alfa"]["email"] == ""                   # opted out: no contact shown
    assert rows["Alfa"]["contact_name"] == ""
    assert rows["Beta"]["email"] == "other@beta.example"  # address entry skips one person


@pytest.mark.asyncio
async def test_company_export_never_shows_a_bounced_address(state):
    a = await _company(state, "Alfa", "alfa.example")
    b = await _company(state, "Beta", "beta.example")
    c = await _company(state, "Gamma", "gamma.example")
    await _prospect(state, a, "Bounced", "bounced@alfa.example", email_status="invalid",
                    score=99)
    await _prospect(state, a, "Good", "good@alfa.example", email_status="guess", score=1)
    await _prospect(state, b, "Bounced", "bounced@beta.example", email_status="invalid",
                    score=99)
    await _prospect(state, b, "Nomail", "", score=1)
    await _prospect(state, c, "Bounced", "bounced@gamma.example", email_status="invalid")

    rows = {r["name"]: r for r in await collect_companies(state)}
    assert rows["Alfa"]["email"] == "good@alfa.example"
    assert rows["Beta"]["contact_name"] == "Nomail Example"   # below no email at all
    assert rows["Beta"]["email"] == ""
    assert rows["Gamma"]["email"] == ""                       # only contact: no address
    assert rows["Gamma"]["email_status"] == ""


def test_cli_export_companies_defaults_to_companies_csv(tmp_path, monkeypatch):
    import asyncio
    import sys

    from harvey import cli, state as state_module

    async def _seed():
        s = StateManager(str(tmp_path / "cli.db"))
        await s.init_db()
        await _company(s, "Alfa", "alfa.example")
        return s

    seeded = asyncio.run(_seed())
    monkeypatch.setattr(state_module, "StateManager", lambda: seeded)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["harvey", "export", "--companies"])
    cli.main()
    assert "Alfa" in (tmp_path / "companies.csv").read_text(encoding="utf-8")
    assert not (tmp_path / "prospects.csv").exists()


def test_csv_export_neutralizes_formula_cells():
    from harvey.export import to_csv

    text = to_csv([{"company_name": "=HYPERLINK(\"http://evil.example\")",
                    "title": " @SUM(A1)", "phone": "+40 700 000 000",
                    "first_name": "Ana", "score": 0}])
    row = list(csv.DictReader(io.StringIO(text)))[0]
    assert row["company_name"] == "'=HYPERLINK(\"http://evil.example\")"
    assert row["title"] == "' @SUM(A1)"
    assert row["phone"] == "+40 700 000 000"
    assert row["first_name"] == "Ana"
    assert row["score"] == "0"
