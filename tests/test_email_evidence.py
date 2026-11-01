"""What a site's published addresses say about its email format, and a
pattern cache that no longer gets stuck on a default guess."""

import pytest

import harvey.integrations.email_finder as ef


# ── email patterns from published addresses ──

@pytest.mark.parametrize("local, pattern", [
    ("j.smith", "{fi}.{l}"),
    ("john.smith", "{f}.{l}"),
    ("john_smith", "{f}_{l}"),
])
def test_pattern_from_an_untied_address(local, pattern):
    assert ef.pattern_from_local(local)[0] == pattern


@pytest.mark.parametrize("local", [
    "info", "office", "servicesubmissions", "scholarships", "web2", "x",
    # A lone dotless address is ambiguous: kmorrow@ could be Kmorrow or K. Morrow,
    # sven@ a first name or S. Ven. Nobody on the site to tell, nothing learned.
    "corwin", "wren", "jsmith", "kmorrow", "tvoss", "bpell", "rquade", "sven", "drsmith",
])
def test_role_or_junk_addresses_teach_nothing(local):
    assert ef.pattern_from_local(local) is None


def test_an_address_tied_to_a_named_person_beats_guessing():
    known, pattern, conf = ef.site_email_evidence(
        ["nvale@acme.example", "info@acme.example", "dana@other.example"],
        [("Nora", "Vale")], "acme.example")
    assert known == [("nvale@acme.example", "Nora", "Vale")]
    assert pattern == "{fi}{l}" and conf >= 0.9


def test_untied_dotless_addresses_teach_nothing():
    known, pattern, conf = ef.site_email_evidence(
        ["cora@lanternagency.test", "wren@lanternagency.test", "kmorrow@lanternagency.test"],
        [("Nate", "Calloway")], "lanternagency.test")
    assert (known, pattern) == ([], "")


def test_a_named_surname_disambiguates_an_untied_address():
    # Nobody named Jake Smith, but Amy Smith works there: jsmith@ is an
    # initial plus a surname, not a first name.
    known, pattern, conf = ef.site_email_evidence(
        ["jsmith@acme.example"], [("Amy", "Smith"), ("Nora", "Vale")], "acme.example")
    assert known == [] and pattern == "{fi}{l}" and conf < 0.9


@pytest.mark.parametrize("address, pattern", [("jsmith@acme.example", "{fi}{l}"), ("smith@acme.example", "{l}")])
def test_an_address_two_named_people_could_own_is_format_evidence_only(address, pattern):
    known, learned, _ = ef.site_email_evidence(
        [address], [("John", "Smith"), ("Jane", "Smith")], "acme.example")
    assert known == [] and learned == pattern


@pytest.mark.asyncio
async def test_never_guess_an_address_another_named_person_could_own(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    async def verified(email, mx_type, env, enabled=None):
        raise AssertionError("a colliding address must never be sent to a verifier")

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", verified)
    st = PatternState({"pattern": "{fi}{l}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email("Jane", "Smith", "acme.example", cached=st.cached,
                                 other_people=[("John", "Smith")])
    assert result.email == "" and result.status != "verified"
    st = PatternState({"pattern": "{l}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email("Jane", "Smith", "acme.example", cached=st.cached,
                                 other_people=[("John", "Smith")])
    assert result.email == ""


@pytest.mark.asyncio
async def test_another_persons_published_address_is_never_this_persons_guess(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    st = PatternState({"pattern": "{f}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email(
        "Nora", "Quist", "acme.example", verify=False, cached=st.cached, other_people=[("Nora", "Vale")],
        known_emails=[("nora@acme.example", "Nora", "Vale")])
    assert result.email == ""


# ── pattern derivation no longer gets stuck on a default ──

class PatternState:
    def __init__(self, cached=None):
        self.cached = cached
        self.saved = []

    async def get_email_pattern(self, domain):
        return self.cached

    async def save_email_pattern(self, domain, pattern="", source="", confidence=0.0, **kw):
        self.saved.append((pattern, source, confidence))

    async def get_setting(self, key, default=None):
        return "0"

    async def settings_with_prefix(self, prefix):
        return {}

    async def increment_setting(self, key):
        pass


@pytest.mark.asyncio
async def test_a_cached_default_does_not_block_a_known_address():
    st = PatternState({"pattern": "{f}.{l}", "source": "default", "confidence": 0.3})
    pattern, source, _ = await ef.derive_pattern(
        "acme.example", known_emails=[("nvale@acme.example", "Nora", "Vale")], cached=st.cached)
    assert (pattern, source) == ("{fi}{l}", "known_email")


@pytest.mark.asyncio
async def test_a_cached_default_does_not_block_hunter(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return "{fi}{l}", 0.8

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")
    st = PatternState({"pattern": "{f}.{l}", "source": "default", "confidence": 0.3})
    assert (await ef.derive_pattern("acme.example", cached=st.cached))[0] == "{fi}{l}"
    assert calls == ["acme.example"]


@pytest.mark.asyncio
async def test_hunter_is_not_asked_twice_about_a_domain_it_had_no_pattern_for(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return None

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")
    first = await ef.derive_pattern("acme.example", cached=None)
    assert first[1] == "default_after_hunter" and calls == ["acme.example"]
    st = PatternState({"pattern": first[0], "source": first[1], "confidence": first[2]})
    await ef.derive_pattern("acme.example", cached=st.cached)
    assert calls == ["acme.example"]


@pytest.mark.asyncio
async def test_a_low_confidence_site_guess_still_gets_one_hunter_lookup(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return None

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")
    cached = {"pattern": "{fi}{l}", "source": "site_address_guess", "confidence": 0.6}
    pattern, source, conf = await ef.derive_pattern("acme.example", cached=cached)
    assert calls == ["acme.example"] and pattern == "{fi}{l}" and source.endswith("_after_hunter")
    await ef.derive_pattern("acme.example", cached=(
        {"pattern": pattern, "source": source, "confidence": conf}))
    assert calls == ["acme.example"]


@pytest.mark.asyncio
async def test_a_hunter_outage_is_not_remembered_as_no_pattern(monkeypatch):
    async def down(domain, key):
        raise ef.HunterUnavailable("429")

    monkeypatch.setattr(ef, "_hunter_domain_pattern", down)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "k")
    _, source, _ = await ef.derive_pattern("acme.example", cached=None)
    assert source == "default"      # asked again next time


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.asyncio
async def test_hunter_errors_are_transient(monkeypatch, status):
    class Resp:
        status_code = status

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
    with pytest.raises(ef.HunterUnavailable):
        await ef._hunter_domain_pattern("acme.example", "k")


@pytest.mark.asyncio
async def test_a_small_business_with_no_evidence_defaults_to_first_name(monkeypatch):
    monkeypatch.setattr(ef, "_hunter_api_key", lambda env: "")
    pattern, source, conf = await ef.derive_pattern(
        "acme.example", cached=None, default_pattern=ef.SMALL_BUSINESS_PATTERN)
    assert (pattern, source) == ("{f}", "default") and conf < 0.5


@pytest.mark.asyncio
async def test_a_persons_own_published_address_is_used_as_is(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    result = await ef.find_email(
        "Tess", "Marlowe", "marlowemarketingco.test", verify=False, cached=None,
        known_emails=[("tess@marlowemarketingco.test", "Tess", "Marlowe")])
    assert result.email == "tess@marlowemarketingco.test"
    assert result.status == "guess"   # unverified stays a guess: never sent
