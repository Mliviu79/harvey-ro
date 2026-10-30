"""Words as search compares them: folded, and the joining words left out.

Shared by the working database's company search (harvey/state.py) and the
trade registry's (harvey/registry.py), so a query reads the same in both.
"""

import re
import unicodedata

# Joining words that would otherwise have to appear in the company's text.
SEARCH_STOPWORDS = frozenset({"de", "si", "la", "in", "pe", "cu", "din", "a",
                              "of", "and", "the", "for"})


def fold_words(text: str) -> list[str]:
    """Lowercase words without accents, punctuation dropped."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", plain)
