"""Where the Ministry of Finance publishes its taxpayer identification file,
and fetching it.

data.gov.ro (CKAN) holds one package a year, published in June (with a
corrected version in July): ``date_de_identificare_platitori_actualizate_
iunie_2026``, ``date_identificare_062025``, ... Names don't follow one
pattern, so ``catalog()`` asks for the Ministry's packages once, keeps the
``identificare`` ones, takes the newest year, and in it per part (2026 is
split in two, ``_a`` and ``_b``) the newest data file by its publication
date: a July correction (``_actualizat_iulie``) over the June file, a .txt
or .csv alike. Archives and tiny test files are skipped.

Downloads go to data/mf_taxpayers/<year>/, named by publication date and
file name, so a corrected file lands beside the first instead of over it.
Nothing there is deleted automatically. data.gov.ro ignores HTTP Range, so
a download is always the whole file: streamed to a .part file and renamed
once whole.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from harvey.modules.ro.mf_financials.source import (LICENCES, check_space, download_name,
                                                    get_trusted, inside, published_day,
                                                    safe_name, stream_trusted, trusted_url)
from harvey.paths import PROJECT_ROOT

CKAN_API = "https://data.gov.ro/api/3/action"
ORGANISATION = "mfp"
DOWNLOADS = PROJECT_ROOT / "data" / "mf_taxpayers"
CATALOG_FILE = "catalog.json"
# Smaller than this is a test upload, not a year's file (~400 MB a part).
MIN_BYTES = 1_000_000

_PACKAGE = re.compile(r"identificare")
_YEAR = re.compile(r"(20\d{2})(?!\d)")
_DATA = re.compile(r"\.(txt|csv)$")
_REVISED = re.compile(r"_+actualizat\w*$")


@dataclass
class Resource:
    """One published data file (a part of a year's file)."""
    year: int
    part: str
    url: str
    name: str
    package: str
    published: str          # the resource's creation time on data.gov.ro, ISO
    licence: str
    size: int | None = None

    @property
    def filename(self) -> str:
        return download_name(self.published, self.name)


def package_year(name: str) -> int | None:
    years = _YEAR.findall(name or "")
    return int(years[-1]) if years else None


def part_of(filename: str) -> str:
    """The part a file is of: its name without the extension and the
    correction suffix (date_..._2026_a_actualizat_iulie.txt is
    date_..._2026_a)."""
    stem = _DATA.sub("", (filename or "").lower())
    return _REVISED.sub("", stem)


def _size(raw) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def resources(packages: list[dict]) -> list[Resource]:
    """The newest year's files (of the years with a file Harvey can read),
    the newest one per part."""
    years: dict[int, dict[str, Resource]] = {}
    for package in packages:
        name = package.get("name") or ""
        year = package_year(name)
        if not _PACKAGE.search(name) or year is None:
            continue
        chosen = years.setdefault(year, {})
        licence = LICENCES.get((package.get("license_id") or "").lower(),
                               package.get("license_id") or "")
        for res in package.get("resources") or []:
            url = res.get("url") or ""
            filename = url.rsplit("/", 1)[-1].lower()
            size = _size(res.get("size"))
            if not _DATA.search(filename) or not trusted_url(url) or not safe_name(filename):
                continue
            if size is not None and size < MIN_BYTES:
                continue
            item = Resource(year=year, part=part_of(filename), url=url, name=filename,
                            package=name, published=published_day(res.get("created")),
                            licence=licence, size=size)
            old = chosen.get(item.part)
            if old is None or item.published > old.published:
                chosen[item.part] = item
    usable = [y for y, chosen in years.items() if chosen]
    if not usable:
        return []
    return sorted(years[max(usable)].values(), key=lambda r: r.part)


async def catalog(client) -> list[Resource]:
    """The newest year's files, from one search of the Ministry's packages."""
    resp = await get_trusted(client, f"{CKAN_API}/package_search", params={
        "q": "identificare", "fq": f"organization:{ORGANISATION}", "rows": 100})
    resp.raise_for_status()
    packages = (resp.json().get("result") or {}).get("results") or []
    found = resources(packages)
    if not found:
        raise RuntimeError("no taxpayer identification file found on data.gov.ro")
    return found


def save_catalog(found: list[Resource], root: Path | None = None) -> None:
    """What data.gov.ro listed, and when it was asked."""
    root = Path(root or DOWNLOADS)
    root.mkdir(parents=True, exist_ok=True)
    data = {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "files": [asdict(r) for r in found]}
    part = root / (CATALOG_FILE + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    part.replace(root / CATALOG_FILE)


def load_catalog(root: Path | None = None) -> tuple[str, list[Resource]]:
    """(checked_at, files) from the last check, or ("", [])."""
    try:
        data = json.loads((Path(root or DOWNLOADS) / CATALOG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []
    out = []
    for item in data.get("files") or []:
        try:
            out.append(Resource(**item))
        except TypeError:
            continue
    return str(data.get("checked_at") or ""), out


def local_file(res: Resource, root: Path | None = None) -> Path:
    root = Path(root or DOWNLOADS)
    return inside(root, root / str(int(res.year)) / res.filename)


def _complete(path: Path, expected: int | None) -> bool:
    if not path.is_file():
        return False
    size = path.stat().st_size
    return size == expected if expected else size >= MIN_BYTES


async def fetch(client, res: Resource, root: Path | None = None, progress=None) -> Path:
    """The file on disk, downloaded only when there isn't a complete copy.
    Its listing is kept beside it (<file>.json), so it can be loaded again
    offline with its origin."""
    target = local_file(res, root)
    sidecar = target.with_name(target.name + ".json")
    if _complete(target, res.size):
        if not sidecar.exists():
            sidecar.write_text(json.dumps(asdict(res), ensure_ascii=False), encoding="utf-8")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    check_space(target.parent, res.size, res.name)
    if progress:
        progress(f"downloading {res.name}", res.size)
    part = target.with_name(target.name + ".part")
    written = 0
    async with stream_trusted(client, res.url) as resp:
        resp.raise_for_status()
        announced = resp.headers.get("content-length")
        with open(part, "wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 20):
                fh.write(chunk)
                written += len(chunk)
    if announced and announced.isdigit() and int(announced) != written:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{res.name}: got {written} of {announced} bytes")
    if (res.size and written != res.size) or written < MIN_BYTES:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{res.name}: {written} bytes doesn't look like the taxpayer "
                           f"file (expected {res.size or 'more'})")
    with open(part, "rb") as fh:
        head = fh.read(256).lstrip(b"\xef\xbb\xbf").upper()
    if b"COD_FISCAL" not in head and not head.startswith(b"CUI"):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{res.name}: not a taxpayer file (no COD_FISCAL header)")
    part.replace(target)
    sidecar.write_text(json.dumps(asdict(res), ensure_ascii=False), encoding="utf-8")
    return target


def downloaded(root: Path | None = None) -> list[tuple[Resource, Path]]:
    """The newest year's complete downloads already on disk, the newest per
    part, with their listing: what a load uses when data.gov.ro can't be
    reached."""
    root = Path(root or DOWNLOADS)
    if not root.is_dir():
        return []
    found: dict[tuple[int, str], tuple[Resource, Path]] = {}
    for sidecar in root.glob("[0-9][0-9][0-9][0-9]/*.json"):
        try:
            res = Resource(**json.loads(sidecar.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        path = sidecar.with_name(sidecar.name[:-len(".json")])
        if not _complete(path, res.size):
            continue
        key = (res.year, res.part)
        if key not in found or res.published > found[key][0].published:
            found[key] = (res, path)
    if not found:
        return []
    year = max(y for y, _ in found)
    return [found[k] for k in sorted(found) if k[0] == year]
