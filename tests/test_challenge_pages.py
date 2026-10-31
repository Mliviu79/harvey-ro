"""Bot challenges and block pages, told apart from real pages that load
reCAPTCHA or say "access denied" in passing."""

from types import SimpleNamespace

import pytest

from harvey.collectors.profile import ProfileCollector, looks_like_challenge


# ── blocked or challenged, not unreachable ──

def test_a_recaptcha_script_is_not_a_block():
    page = ('<html><head><script src="https://www.google.com/recaptcha/api.js"></script></head>'
            '<body>' + "<p>Real roofing content.</p>" * 50 + '<div class="g-recaptcha"></div></body></html>')
    assert not looks_like_challenge(page)


@pytest.mark.parametrize("page", [
    "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>",
    '<html><head><title>Access to this page has been denied</title><meta name="description" content="px-captcha"></head></html>',
    "<html><body>Our systems have detected unusual traffic from your computer network.</body></html>",
])
def test_real_challenge_pages_are_blocks(page):
    assert looks_like_challenge(page)


SG_CHALLENGE = ('<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
                'content="0;/.well-known/sgcaptcha/?r=%2F&y=ipr:1.2.3.4:1.1"></meta></head></html>')


@pytest.mark.asyncio
async def test_profile_collector_reports_a_challenge_without_trying_every_variant():
    calls = []

    class Client:
        async def get(self, url):
            calls.append(url)
            return SimpleNamespace(status_code=202, text=SG_CHALLENGE, headers={"content-type": "text/html"})

    collector = ProfileCollector()
    collector._client = Client()
    assert await collector._resolve_site("challengedlaw.test") == ("", "")
    assert calls == ["https://challengedlaw.test"]
    assert "challengedlaw.test" in collector.challenged


@pytest.mark.parametrize("page", [
    "<html><title>Contact</title><body><form></form><p>This site is protected by reCAPTCHA and the Google Privacy Policy.</p></body></html>",
    "<html><title>Our Policies</title><body>Unauthorized access denied to staff area.</body></html>",
])
def test_ordinary_page_text_is_not_a_challenge(page):
    assert not looks_like_challenge(page)
