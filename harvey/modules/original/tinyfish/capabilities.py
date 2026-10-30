"""TinyFish's capabilities. The code they run is harvey/collectors/discover.py."""

from harvey.collectors.discover import provider_capabilities

CAPABILITIES = provider_capabilities("tinyfish", {"web_search": "tinyfish"})
