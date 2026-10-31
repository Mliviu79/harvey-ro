"""The ONRC trade registry's capabilities. The code they run is
harvey/registry.py (the local registry database) and
harvey/collectors/onrc.py (the open-data files).

A read returns the firms as records with their facts, and in ``values``
what core needs to save and report them: the ``dataset``, the ``report``
(an onrc.ImportReport, which core's save adds its counts to), the signal
``codes`` the read collects, the ``collector`` label the firms'
activities carry, and the ``run_params`` the runs log keeps."""

import asyncio

from harvey import registry
from harvey.collectors import onrc
from harvey.modules.api import Capability, Draft

PEOPLE_CODES = ("CONTACT_FOUND", "DECISION_MAKER_TITLE")


def _codes(with_administrators: bool) -> tuple[str, ...]:
    return ("ACTIVITY_CODE", *PEOPLE_CODES) if with_administrators else ("ACTIVITY_CODE",)


async def _sync(inp, ctx) -> Draft:
    """Load a dataset's files (``extra["files"]``, ``extra["dataset"]``)
    into the registry database (``extra["path"]``), the module's own
    storage. ``values["registry"]`` is what was loaded."""
    # In a thread: a load takes minutes, and the dashboard's own requests
    # are served by the same event loop.
    meta = await asyncio.to_thread(registry.build, inp.extra["files"], inp.extra["dataset"],
                                   inp.extra["path"], progress=inp.extra.get("progress"))
    return Draft(values={"registry": meta})


async def _search(inp, ctx) -> Draft:
    """The firms a resolved query (``query``, a registry.Query) matches in
    the registry database (``extra["con"]``): the first ``extra["limit"]``,
    best first. ``area`` is how the query's area was asked for (a label)."""
    q, x = inp.query, inp.extra
    limit = x.get("limit")
    with_administrators = x.get("with_administrators", True)
    # A whole-country search reads millions of rows: in a thread, so the
    # dashboard (whose worker may run it) never freezes meanwhile.
    draft = await asyncio.to_thread(registry.search_draft, x["con"], q, limit=limit,
                                    with_administrators=with_administrators)
    dataset = draft.values["dataset"]
    report = onrc.ImportReport(dataset=dataset.name)
    report.classes = onrc.class_labels(q.classes)
    report.authorised = report.active = report.in_area = draft.counts.get("matched", 0)
    draft.values.update(
        report=report, codes=_codes(with_administrators), collector=onrc.COLLECTOR,
        run_params={"dataset": dataset.name, "source": "registry", "match": q.match[:500] or (
                        f"{len(q.firm_ids)} chosen firms" if q.firm_ids is not None else ""),
                    "area": inp.area, "classes": report.classes or ["any"], "limit": limit,
                    "administrators": with_administrators})
    return draft


async def _import(inp, ctx) -> Draft:
    """The firms the registry's files (``extra["files"]``, of
    ``extra["dataset"]``) hold for an area (``area`` counties,
    ``locality`` towns, comma-separated) and activity classes
    (``extra["classes"]``), operating ones only unless
    ``extra["active_only"]`` is False. The representatives file is read
    only for ``extra["with_administrators"]`` and a people signal that is
    wanted."""
    x = inp.extra
    classes = list(x.get("classes") or [])
    dataset = x["dataset"]
    active_only = x.get("active_only", True)
    with_administrators = x.get("with_administrators", True)
    with_people = with_administrators and any(ctx.wants(c) for c in PEOPLE_CODES)
    firms, codes, people, report = onrc.read_import(
        x["files"], dataset, county=inp.area, classes=classes, locality=inp.locality,
        active_only=active_only, with_people=with_people)
    draft = onrc.firms_draft(firms, dataset, codes=codes, people=people, classes=classes,
                             with_administrators=with_administrators)
    draft.values.update(
        dataset=dataset, report=report, codes=_codes(with_administrators),
        collector=onrc.COLLECTOR,
        run_params={"dataset": dataset.name,
                    "county": ", ".join(onrc.counties(inp.area) or []) or "Romania",
                    "locality": inp.locality,
                    "classes": report.classes or ["all activities"],
                    "active_only": active_only, "administrators": with_administrators})
    return draft


CAPABILITIES = {
    "registry_sync": Capability("onrc", "registry_sync", _sync),
    "registry_search": Capability("onrc", "registry_search", _search),
    "registry_import": Capability("onrc", "registry_import", _import),
}
