"""Which providers have an account linked in Harvey (data/auth).

Config reads this to tell which login a provider naming none uses, so it
imports nothing of Harvey's beyond the paths: the code that links, renews
and checks those logins (brain_auth, codex_link, claude_link) reaches the
database, and config must not. Nothing here is logged or kept.
"""

from __future__ import annotations

from harvey.paths import PROJECT_ROOT

AUTH_DIR = PROJECT_ROOT / "data" / "auth"


def has_account(provider: str) -> bool:
    """Whether Harvey's folder holds a login for ``provider``: Codex's file,
    or Harvey's own note of the Claude account it linked. Only that the file
    is there: a credential is never opened here, and whether it works is the
    status's to say. (An API key saved there comes with its login named.)"""
    name = ("codex", "auth.json") if provider == "codex" else ("claude-link.json",)
    return AUTH_DIR.joinpath(*name).exists()
