"""The ONRC trade registry's capability: its load. The code it runs is
harvey/registry.py (the local registry database), built from the
open-data files harvey/collectors/onrc.py reads.

The registry is read in place (harvey/registry.py, the module's adapter
in queries.py): nothing here copies a firm into harvey.db."""

import asyncio

from harvey import registry
from harvey.modules.api import Capability, Draft


async def _sync(inp, ctx) -> Draft:
    """Load a dataset's files (``extra["files"]``, ``extra["dataset"]``)
    into the registry database (``extra["path"]``), the module's own
    storage. ``values["registry"]`` is what was loaded."""
    # In a thread: a load takes minutes, and the dashboard's own requests
    # are served by the same event loop.
    meta = await asyncio.to_thread(registry.build, inp.extra["files"], inp.extra["dataset"],
                                   inp.extra["path"], progress=inp.extra.get("progress"))
    return Draft(values={"registry": meta})


CAPABILITIES = {
    "registry_sync": Capability("onrc", "registry_sync", _sync),
}
