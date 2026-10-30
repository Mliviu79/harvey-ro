"""The phones module's capability. The code it runs is
harvey/collectors/phones.py."""

from harvey.browser import HTTP
from harvey.collectors.phones import MAX_PHONES, phone_region, phones_from_html
from harvey.modules.api import Capability, Contact, Draft, Fact


async def _site_phones(inp, ctx) -> Draft:
    """The phone numbers a company's own pages (``inp.pages``) publish, read
    in its country (its domain's, its location's, the ICP's), at most
    MAX_PHONES: each a phone contact and a PHONE_NUMBER fact, labelled
    ``settings["collector"]`` (with the browser that rendered the page, when
    one did)."""
    label = ctx.settings.get("collector", "phones")
    region = phone_region(inp.locality, ctx.config, inp.domain)
    draft, seen = Draft(), set()
    for page in inp.pages:
        for hit in phones_from_html(page.html, region):
            if hit.number in seen or len(seen) >= MAX_PHONES:
                continue
            seen.add(hit.number)
            draft.contacts.append(Contact("phone", hit.number, display=hit.display,
                                          confidence=hit.confidence, source_url=page.url))
            draft.facts.append(Fact(
                "PHONE_NUMBER", value_text=hit.number, confidence=hit.confidence,
                source_url=page.url,
                collector=label if page.method == HTTP else f"{label}:{page.method}"))
    return draft


CAPABILITIES = {"site_phones": Capability("phones", "site_phones", _site_phones)}
