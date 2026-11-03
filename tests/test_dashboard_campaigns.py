"""The Campaigns page's endpoints: saving a Contacts filter as an audience,
the new-campaign wizard (setup, preview, create, generate and save the
sequence, start), adding ticked contacts by hand, and the campaign's
actions. All data is fictional."""

import asyncio

import pytest

import harvey.dashboard as dash
from harvey import contacts
from harvey.models import Company
from harvey.models.campaign import EmailStep
from harvey.state import StateManager
from tests.test_campaigns import NOTE, SEQUENCE, config
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


def _seed(db, *emails):
    async def run():
        state = StateManager(str(db))
        await state.init_db()
        for email in emails:
            domain = email.rpartition("@")[2]
            firm = await state.add_company(Company(name=domain.split(".")[0].title(),
                                                   domain=domain, location="Brașov"))
            await state.add_contacts(firm, [contacts.Found(
                "email", email, status="verified", source="website", trust="confirmed",
                evidence_url=f"https://{domain}/contact")])
    asyncio.run(run())


@pytest.fixture
def gmail(monkeypatch):
    cfg = config()
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)
    return cfg


def test_the_wizard_saves_an_audience_previews_it_and_starts_a_campaign(client, gmail):
    _seed(client.db_path, "office@alfa.example.com", "office@beta.example.com")
    setup = client.get("/api/campaigns/setup").json()
    assert setup["native"] is True and setup["refusal"] == ""
    assert [a["id"] for a in setup["accounts"]] == [1]
    assert setup["accounts"][0]["address"] == "ana@trimite.example.com"
    assert setup["li_note_min"] == 80 and "Interes legitim" in setup["li_note_template"]

    saved = client.post("/api/audiences", json={"name": "Alfa", "query": "q=alfa&limit=50"})
    assert saved.status_code == 200
    audience = saved.json()["audience"]
    assert audience["query"] == "kind=email&q=alfa"
    assert [a["name"] for a in client.get("/api/audiences").json()["audiences"]] == ["Alfa"]
    preview = client.get(f"/api/audiences/{audience['id']}/preview").json()["counts"]
    assert (preview["total"], preview["ready"], preview["added"]) == (1, 1, 0)
    unsaved = client.post("/api/audiences/preview", json={"query": "kind=email"}).json()
    assert unsaved["counts"]["ready"] == 2

    made = client.post("/api/campaigns", json={
        "name": "Stomatologi", "audience_id": audience["id"], "li_note": NOTE,
        "offer": {"offer": "programări online", "goal": "răspuns"}}).json()
    cid = made["id"]
    assert made["campaign"]["status"] == "draft" and made["campaign"]["origin"] == "user"

    # No sequence yet: start says what is missing.
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.status_code == 409 and r.json()["need"] == "sequence"
    assert client.put(f"/api/campaigns/{cid}/templates",
                      json={"sequence": SEQUENCE}).status_code == 200
    started = client.post(f"/api/campaigns/{cid}/start").json()
    assert started["counts"]["added"] == 1 and started["campaign"]["status"] == "active"

    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["campaign"]["members"] == {"ready": 1}
    assert detail["campaign"]["query"] == "kind=email&q=alfa"
    assert [m["email"] for m in detail["members"]] == ["office@alfa.example.com"]
    # The list keeps its old shape and gains the new fields.
    listed = next(c for c in client.get("/api/campaigns").json() if c["id"] == cid)
    assert listed["recipients"][0]["email"] == "office@alfa.example.com"
    assert listed["member_total"] == 1 and listed["sequence"][0]["step"] == 1

    for action, status in (("pause", "paused"), ("resume", "active"), ("stop", "finished"),
                           ("archive", "archived")):
        assert client.post(f"/api/campaigns/{cid}/{action}").json()["campaign"]["status"] == status
    assert client.post(f"/api/campaigns/{cid}/resume").status_code == 400


def test_contacts_ticked_on_the_page_are_added_through_the_rules(client, gmail):
    _seed(client.db_path, "office@alfa.example.com", "office@beta.example.com")
    cid = client.post("/api/campaigns", json={"name": "De mână", "li_note": NOTE,
                                              "sequence": SEQUENCE}).json()["id"]
    r = client.post(f"/api/campaigns/{cid}/members",
                    json={"emails": ["office@alfa.example.com", "nimeni@x.example.com"]})
    assert r.json()["counts"]["added"] == 1 and r.json()["counts"]["not_found"] == 1
    # Already in it: not added twice.
    again = client.post(f"/api/campaigns/{cid}/members",
                        json={"emails": ["office@alfa.example.com"]}).json()["counts"]
    assert (again["added"], again["already"]) == (0, 1)
    assert client.post(f"/api/campaigns/{cid}/members", json={"emails": "x"}).status_code == 400
    gone = client.request("DELETE", f"/api/campaigns/{cid}/members",
                          json={"emails": ["office@alfa.example.com"]}).json()
    assert gone["removed"] == 1
    members = client.get(f"/api/campaigns/{cid}/members?status=removed").json()["members"]
    assert [m["email"] for m in members] == ["office@alfa.example.com"]


def test_a_new_campaign_is_refused_on_instantly(client, monkeypatch):
    cfg = config(provider="instantly")
    monkeypatch.setattr(dash, "_load_config_quietly", lambda: cfg)
    setup = client.get("/api/campaigns/setup").json()
    assert setup["native"] is False and "Gmail, SMTP sau Microsoft" in setup["refusal"]
    r = client.post("/api/campaigns", json={"name": "X", "li_note": NOTE})
    assert r.status_code == 409 and r.json()["need"] == "native_provider"


@pytest.mark.no_acknowledgement
def test_start_asks_for_the_acknowledgement_first(client, gmail):
    cid = client.post("/api/campaigns", json={"name": "X", "li_note": NOTE,
                                              "sequence": SEQUENCE}).json()["id"]
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.status_code == 409 and r.json()["need"] == "acknowledgement"
    assert client.post("/api/acknowledgement", json={"typed": "accept"}).status_code == 200
    assert client.post(f"/api/campaigns/{cid}/start").json()["campaign"]["status"] == "active"


def test_generating_the_sequence_returns_it_for_editing_without_saving(client, gmail,
                                                                      monkeypatch):
    _seed(client.db_path, "office@alfa.example.com")
    seen = {}

    class Writer:
        async def write_templates(self, campaign, prospects):
            seen.update(offer=campaign.offer_json, sample=[p.email for p in prospects])
            return [EmailStep(step=1, subject="o întrebare", body="Bună ziua?", delay_days=0)]

    monkeypatch.setattr(dash, "_campaign_writer", lambda state, cfg: Writer())
    cid = client.post("/api/campaigns", json={"name": "X", "query": "kind=email",
                                              "offer": {"offer": "programări"}}).json()["id"]
    out = client.post(f"/api/campaigns/{cid}/templates").json()
    assert out["sequence"][0]["subject"] == "o întrebare"
    assert seen["sample"] == ["office@alfa.example.com"] and "programări" in seen["offer"]
    assert client.get(f"/api/campaigns/{cid}").json()["campaign"]["sequence"] == []


def test_bad_input_is_refused(client, gmail):
    assert client.post("/api/audiences", json={"name": "", "query": "kind=email"}).status_code == 400
    assert client.post("/api/audiences", json={"name": "X", "query": "caen=zzzqqq"}).status_code == 400
    cid = client.post("/api/campaigns", json={"name": "X"}).json()["id"]
    bad = client.patch(f"/api/campaigns/{cid}", json={"sequence": [{"subject": "", "body": ""}]})
    assert bad.status_code == 400
    assert client.get("/api/campaigns/nimic").status_code == 404
    assert client.post(f"/api/campaigns/{cid}/explode").status_code == 404
