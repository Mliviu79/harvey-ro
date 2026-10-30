"""Nominatim's capabilities. The code they run is harvey/collectors/discover.py."""

from harvey.collectors.discover import nominatim_point
from harvey.modules.api import Capability, Draft


async def _geocode(inp, ctx) -> Draft:
    """``values["point"]``: "lat,lng"; "" when the place isn't known, None
    when Nominatim couldn't be asked."""
    return Draft(values={"point": await nominatim_point(inp.locality or inp.region, ctx.http)})


CAPABILITIES = {"geocode": Capability("nominatim", "geocode", _geocode)}
