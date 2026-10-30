"""DataForSEO's capabilities. The code they run is harvey/collectors/discover.py."""

from harvey.collectors.discover import provider_capabilities

CAPABILITIES = provider_capabilities("dataforseo", {
    "business_listings": "dataforseo_listings",
    "organic_rank": "dataforseo_serp",
    "maps_rank": "dataforseo_maps",
})
