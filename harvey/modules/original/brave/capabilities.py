"""Brave Search's capability: one web search for a company's name and town,
the results handed back in memory as website candidates. The contact finder
(harvey/finder.py) picks one; nothing here keeps, caches or logs a result."""

from harvey.collectors.discover import provider_capabilities
from harvey.modules.original.brave.provider import Brave

PROVIDERS = {"brave": Brave()}

CAPABILITIES = provider_capabilities("brave", {"website_search": "brave"})
