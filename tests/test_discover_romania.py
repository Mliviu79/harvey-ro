"""DISCOVER for non-English markets — medical categories on OpenStreetMap
(English and Romanian terms) and the search language DataForSEO is asked for.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from harvey.collectors import discover as D
from harvey.config import ICPConfig


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


DENTIST = ["amenity=dentist"]
PHARMACY = ["amenity=pharmacy"]
CLINIC = ["amenity=clinic", "healthcare=clinic"]
DOCTOR = ["amenity=doctors", "healthcare=doctor"]
PHYSIO = ["healthcare=physiotherapist"]
LAB = ["healthcare=laboratory"]


@pytest.mark.parametrize("term,tags", [
    ("Pharmacy", PHARMACY),
    ("pharmacies", PHARMACY),
    ("Farmacie", PHARMACY),
    ("farmacii", PHARMACY),
    ("Cabinet stomatologic", DENTIST),
    ("Clinica stomatologica", DENTIST),
    ("dental clinic", DENTIST),
    ("Clinica", CLINIC),
    ("medical clinic", CLINIC),
    ("Clinica medicala", CLINIC),
    ("Doctors", DOCTOR),
    ("family doctor", DOCTOR),
    ("Cabinet medical", DOCTOR),
    ("medic de familie", DOCTOR),
    ("Physiotherapy", PHYSIO),
    ("physio clinic", PHYSIO),
    ("Kinetoterapie", PHYSIO),
    ("Cabinet fizioterapie", PHYSIO),
    ("Laborator analize medicale", LAB),
    ("medical lab", LAB),
    ("Laboratory", LAB),
    ("Clinică dentară", DENTIST),
    ("clinica dentara", DENTIST),
    ("Ortodonție", DENTIST),
    ("ortodontie", DENTIST),
    ("Clinică stomatologică", DENTIST),
    ("Recuperare medicală", PHYSIO),
    ("recuperare medicala", PHYSIO),
    ("Policlinică", CLINIC),
    ("policlinica", CLINIC),
    ("Clinică", CLINIC),
    ("Farmacie", PHARMACY),
    ("Cabinet medical de familie", DOCTOR),
])
def test_medical_terms_map_to_osm_tags(term, tags):
    assert D.OpenStreetMap()._tags(term) == tags


@pytest.mark.parametrize("term", [
    "medical spa", "Medical billing", "medical supply", "medical supplies",
    "medical marketing agency", "medicare insurance", "veterinary clinic",
    "Clinică veterinară", "clinica veterinara", "animal clinic",
    "clinical research", "dental lab", "Laborator de tehnică dentară",
    "laborator de tehnica dentara", "laborator dentar", "pharmaceutical",
    "Optică medicală", "optica medicala", "biomedical engineering",
])
def test_medical_lookalikes_have_no_osm_category(term):
    assert D.OpenStreetMap()._tags(term) == []


# Non-healthcare businesses that borrow a medical word. Main had no category
# for any of them, so they must still raise rather than pull in every doctor,
# clinic, pharmacy or lab in the area.
@pytest.mark.parametrize("term", [
    "lawn doctor", "drain doctor", "phone doctor", "computer doctor",
    "car doctor", "legal clinic", "tax clinic", "beauty clinic",
    "hair clinic", "pet clinic", "bike clinic", "pharmacy software",
    "laboratory equipment", "language school laboratory",
    "dental laboratory", "Laborator dentar privat", "medical spa",
    "medical billing", "veterinary clinic", "tehnică dentară",
    "doctorate", "clinical", "medicare", "pharmaceutical company",
    "medical design", "label printing",
])
def test_non_medical_terms_with_medical_words_have_no_osm_category(term):
    assert D.OpenStreetMap()._tags(term) == []


@pytest.mark.parametrize("term,tags", [
    ("doctor's office", DOCTOR),
    ("dental practice", DENTIST),
    ("general practice doctor", DOCTOR),
    ("Cabinet medical privat", DOCTOR),
    ("medic specialist", DOCTOR),
    ("medical laboratory", LAB),
    ("Clinica privata", CLINIC),
])
def test_medical_terms_built_from_practice_words_map(term, tags):
    assert D.OpenStreetMap()._tags(term) == tags


@pytest.mark.asyncio
async def test_medical_lookalike_raises_instead_of_fetching():
    def handler(request):
        raise AssertionError("Overpass must not be called")

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="no category"):
            await D.OpenStreetMap().fetch(client, {}, D.DiscoveryQuery(
                term="medical spa", coordinate="44.43,26.10,20"))


@pytest.mark.parametrize("term,tags", [
    ("Roofing contractors", ["craft=roofer", "shop=roofing"]),
    ("Emergency plumber", ["craft=plumber"]),
    ("HVAC repair", ["craft=hvac"]),
    ("Electrician", ["craft=electrician"]),
    ("cosmetic dentist", ["amenity=dentist"]),
    ("Divorce lawyer", ["office=lawyer"]),
    ("General contractor", ["craft=builder", "office=construction_company"]),
    ("Landscaping", ["craft=gardener", "shop=garden_centre"]),
    ("B2B SaaS", []),
])
def test_existing_trade_terms_are_unchanged(term, tags):
    assert D.OpenStreetMap()._tags(term) == tags


@pytest.mark.asyncio
async def test_medical_osm_rows_get_a_category():
    def handler(request):
        return httpx.Response(200, json={"elements": [
            {"type": "node", "id": 7, "tags": {
                "name": "Clinica Exemplu", "amenity": "clinic",
                "healthcare": "clinic", "website": "https://clinica.example.com"}},
            {"type": "node", "id": 8, "tags": {
                "name": "Laborator Exemplu", "healthcare": "laboratory"}},
        ]})

    async with _client(handler) as client:
        out = await D.OpenStreetMap().fetch(client, {}, D.DiscoveryQuery(
            term="clinica", coordinate="44.43,26.10,20"))
    assert [b.category for b in out] == ["clinic", "laboratory"]


def test_search_language_defaults_to_english_and_reaches_queries():
    icp = dict(industries=["stomatolog"], company_size="1-10", titles=["Owner"],
               geography=["Cluj-Napoca"])
    assert ICPConfig(**icp).search_language == "en"

    def queries(**extra):
        config = SimpleNamespace(icp=ICPConfig(**icp, **extra),
                                 product=SimpleNamespace(name="x"))
        return D.build_queries(config)

    assert [q.language for q in queries()] == ["en"]
    assert [q.language for q in queries(search_language="ro")] == ["ro"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [D.DataForSEOSerp, D.DataForSEOMaps])
@pytest.mark.parametrize("kwargs,expected", [({}, "en"), ({"language": "ro"}, "ro")])
async def test_dataforseo_sends_the_search_language(cls, kwargs, expected):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "status_code": 20000, "cost": 0.002,
            "tasks": [{"status_code": 20000, "result": [{"items": []}]}]})

    async with _client(handler) as client:
        await cls().fetch(client, {"dataforseo_login": "u", "dataforseo_password": "p"},
                          D.DiscoveryQuery(term="stomatolog", location="Iasi",
                                           depth=30, **kwargs))
    assert seen["body"][0]["language_code"] == expected
