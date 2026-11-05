"""Geofabrik's OpenStreetMap extracts, and fetching one.

Geofabrik (download.geofabrik.de) publishes a daily extract of
OpenStreetMap for every country and many regions, as
``<region>-latest.osm.pbf`` (Romania: europe/romania, about 320 MB) with an
``.md5`` beside it. The data is OpenStreetMap's, under the ODbL: whatever
shows it credits "© OpenStreetMap contributors".

A download goes to data/boundaries/<UTC date>/<region with _>-latest.osm.pbf,
streamed to a .part file (a stopped download carries on from where it was,
with an HTTP Range request) and renamed once whole and its MD5 matches.
Nothing there is deleted automatically. Only https://download.geofabrik.de
is asked, redirects included.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from harvey.paths import PROJECT_ROOT

HOST = "download.geofabrik.de"
BASE = f"https://{HOST}"
DOWNLOADS = PROJECT_ROOT / "data" / "boundaries"
MAX_REDIRECTS = 3
# Room a download needs beyond its own size (the database built from it is
# a few MB).
SPARE_BYTES = 200_000_000
# Smaller than this isn't an extract (an error page).
MIN_BYTES = 10_000
_REGION = re.compile(r"^[a-z][a-z0-9-]*(/[a-z][a-z0-9-]*)*$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_region(region: str) -> str:
    """The region path when it is one ("europe/romania"); ValueError."""
    region = (region or "").strip().strip("/")
    if not _REGION.match(region) or ".." in region:
        raise ValueError(f"not a Geofabrik region path: {region!r}")
    return region


def url(region: str) -> str:
    return f"{BASE}/{check_region(region)}-latest.osm.pbf"


def file_name(region: str) -> str:
    return check_region(region).replace("/", "_") + "-latest.osm.pbf"


def trusted(link: str) -> bool:
    parsed = urlparse(link or "")
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == HOST


@asynccontextmanager
async def _stream(client, link: str, headers: dict | None = None):
    """``client.stream("GET")`` with redirects followed by hand, each hop
    only to https://download.geofabrik.de."""
    for _ in range(MAX_REDIRECTS + 1):
        if not trusted(link):
            raise RuntimeError(f"refusing to fetch {urlparse(link or '').hostname or link!r}: "
                               f"only {BASE} is asked")
        async with client.stream("GET", link, headers=headers or {},
                                 follow_redirects=False) as resp:
            if not resp.is_redirect:
                yield resp
                return
            link = str(resp.url.join(resp.headers.get("location", "")))
    raise RuntimeError(f"too many redirects fetching {link}")


async def md5_of(client, region: str) -> str:
    """The extract's published MD5 ("" when it can't be read)."""
    async with _stream(client, url(region) + ".md5") as resp:
        if resp.status_code != 200:
            return ""
        text = (await resp.aread()).decode("ascii", "replace")
    m = re.match(r"\s*([0-9a-f]{32})\b", text)
    return m.group(1) if m else ""


def _md5(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def downloaded(region: str, root: Path | None = None) -> Path | None:
    """The newest complete download of ``region`` on disk (its sidecar says
    it is whole), or None."""
    root = Path(root or DOWNLOADS)
    name = file_name(region)
    days = sorted((d for d in root.glob("*") if d.is_dir() and _DAY.match(d.name)), reverse=True) \
        if root.is_dir() else []
    for day in days:
        path = day / name
        if path.is_file() and path.with_name(path.name + ".json").is_file():
            return path
    return None


def _check_space(folder: Path, size: int | None) -> None:
    if not size:
        return
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < size + SPARE_BYTES:
        raise RuntimeError(f"not enough free disk space for the extract: it needs about "
                           f"{(size + SPARE_BYTES) / 1e9:.1f} GB, {free / 1e9:.1f} GB is free")


async def fetch(client, region: str, root: Path | None = None, progress=None) -> Path:
    """Download ``region``'s extract into today's folder, carrying on a
    .part a stopped download left there. RuntimeError when it isn't whole
    or its MD5 doesn't match."""
    root = Path(root or DOWNLOADS)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = root / day / file_name(region)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.is_file() else 0
    expected_md5 = await md5_of(client, region)
    headers = {"Range": f"bytes={have}-"} if have else {}
    async with _stream(client, url(region), headers) as resp:
        if resp.status_code == 416 and have:
            pass            # the .part is already whole
        else:
            resp.raise_for_status()
            resumed = resp.status_code == 206 and have
            length = resp.headers.get("content-length")
            size = (int(length) if length and length.isdigit() else 0) + (have if resumed else 0)
            _check_space(target.parent, size - (have if resumed else 0))
            if progress:
                progress(f"downloading {file_name(region)}", size or None)
            with (open(part, "ab") if resumed else open(part, "wb")) as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
    written = part.stat().st_size if part.is_file() else 0
    if written < MIN_BYTES:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{file_name(region)}: {written} bytes isn't an extract")
    digest = _md5(part)
    if expected_md5 and digest != expected_md5:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{file_name(region)}: its MD5 doesn't match Geofabrik's; the "
                           "download was dropped, try again")
    part.replace(target)
    target.with_name(target.name + ".json").write_text(json.dumps({
        "region": region, "url": url(region), "bytes": written, "md5": digest,
        "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8")
    return target
