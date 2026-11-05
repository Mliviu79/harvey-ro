"""Nominatim's capabilities. The code they run is harvey/collectors/discover.py."""

from harvey.collectors.discover import nominatim_place
from harvey.modules import lanes
from harvey.modules.api import Capability, Draft


async def _geocode(inp, ctx) -> Draft:
    """``values["point"]``: "lat,lng"; "" when the place isn't known, None
    when Nominatim couldn't be asked. ``values["bbox"]``: the place's box
    [south, west, north, east], when Nominatim gave one."""
    # The usage policy's one request a second, across every caller (a run's
    # places, the agent's areas(), an estimate's): the lane's pace.
    await lanes.wait("nominatim")
    place = await nominatim_place(inp.locality or inp.region, ctx.http)
    if place is None:
        return Draft(values={"point": None})
    return Draft(values=dict(place))


CAPABILITIES = {"geocode": Capability("nominatim", "geocode", _geocode)}
