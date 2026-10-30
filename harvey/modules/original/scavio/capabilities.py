"""Scavio's capabilities. The code they run is harvey/collectors/discover.py."""

from harvey.collectors.discover import provider_capabilities

CAPABILITIES = provider_capabilities("scavio", {
    "maps_listings": "scavio_maps",
    "google_search": "scavio_search",
})
