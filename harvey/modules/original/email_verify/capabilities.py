"""The email verification module's capabilities. The code they run is
harvey/integrations/email_finder.py."""

from harvey.integrations import email_finder as ef
from harvey.modules.api import Capability, Contact, Draft


async def _find_email(inp, ctx) -> Draft:
    """One person's address at a company (``extra`` first_name, last_name;
    ``inp.domain``), honestly graded: the domain's pattern (what was learned
    before is ``facts["EMAIL_PATTERN"]``, the email_patterns row), then one
    candidate checked. ``values["email_pattern"]`` is what the lookup
    learned, kept even when verification fails."""
    x = inp.extra
    learned: dict = {}
    draft = Draft()
    try:
        result = await ef.find_email(
            x.get("first_name", ""), x.get("last_name", ""), inp.domain,
            verify=x.get("verify", True), env=ctx.env,
            known_emails=x.get("known_emails"),
            default_pattern=x.get("default_pattern", ef.DEFAULT_PATTERN),
            other_people=x.get("other_people"), cached=inp.facts.get("EMAIL_PATTERN"),
            budget=ctx.budget, enabled=ctx.enabled, learned=learned)
    except Exception as e:
        draft.failed = str(e)
        result = None
    if learned:
        draft.values["email_pattern"] = learned
    if result is not None:
        draft.contacts.append(Contact("email", result.email, status=result.status))
    return draft


async def _verify_address(inp, ctx) -> Draft:
    """An address the company publishes (``extra["email"]``), graded like
    any other."""
    email = inp.extra.get("email", "")
    status = await ef.verify_published_address(email, env=ctx.env, enabled=ctx.enabled)
    return Draft(contacts=[Contact("email", email, status=status)])


CAPABILITIES = {
    "find_email": Capability("email_verify", "find_email", _find_email),
    "verify_address": Capability("email_verify", "verify_address", _verify_address),
}
