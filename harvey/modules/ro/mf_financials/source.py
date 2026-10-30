"""Where the Ministry of Finance publishes its annual financial statements,
and fetching them.

data.gov.ro (CKAN) holds one package per year, published about June of the
next year (2025's in June 2026), and a year later often a revised one with
late filers added (``situatii_financiare_2024_actualizat``). Package names
don't follow one pattern (2023's is ``situatii_financiare2023``), and some
packages also carry re-uploads of earlier years. So ``catalog()`` asks for
every package of the Ministry's organisation once, keeps the
``situatii_financiare`` ones, and picks per year and form (BL long balance
sheet, UU abridged, IR IFRS) the newest file by its publication date,
wherever it was published. The .csv beside each .txt is its column
specification, not data, and is skipped.

Downloads go to data/mf_financials/<year>/, named by publication date and
file name, so a revised file lands beside the first release instead of
over it. Nothing there is ever deleted automatically. data.gov.ro ignores
HTTP Range, so a download is always the whole file: it is streamed to a
.part file and renamed once whole.

What the listing says is not trusted with the disk or the network: a
publication date that isn't a date, or a file name with anything but
letters, digits, ``_``, ``.`` and ``-``, is never part of a path, a path
must stay inside the downloads folder, and every address asked, a
redirect's too, must be https on data.gov.ro.
"""

from __future__ import annotations

import json
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from harvey.paths import PROJECT_ROOT

CKAN_API = "https://data.gov.ro/api/3/action"
ORGANISATION = "mfp"
DOWNLOADS = PROJECT_ROOT / "data" / "mf_financials"
CATALOG_FILE = "catalog.json"
FORMS = ("BL", "UU", "IR")

_PACKAGE = re.compile(r"^situatii_financiare_?(\d{4})(_actualizat\w*)?$")
# web_bl_bs_sl_an2025.txt, webblbsslan2016.txt, web2008.txt (BL);
# web_uu_an2025.txt, webuu2011.txt (UU); web_ir_an2025.txt, webiran2012.txt (IR).
_FILE = re.compile(r"^web_?(?:(?P<form>bl_?bs_?sl|uu|ir)_?(?:an)?_?)?(?P<year>\d{4})\.txt$")
LICENCES = {"uk-ogl": "OGL-ROU-1.0", "cc-by-4.0": "CC-BY-4.0", "cc-by": "CC-BY-4.0"}
# Smallest believable file: the IFRS file is a few hundred companies (~10 KB).
MIN_BYTES = 1_000
MAX_REDIRECTS = 5
# Free space a download leaves for loading it: the database it is loaded
# into is about the size of the files.
SPARE_BYTES = 200_000_000

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass
class Resource:
    """One published data file."""
    year: int
    form: str
    url: str
    name: str
    package: str
    published: str          # the resource's creation time on data.gov.ro, ISO
    licence: str
    size: int | None = None

    @property
    def filename(self) -> str:
        return download_name(self.published, self.name)


def published_day(raw) -> str:
    """A listing's creation time when it is a date or a date and time
    (ISO), else "": an undated file sorts before every dated one."""
    text = str(raw or "").strip()
    return text if _DAY.fullmatch(text) else ""


def safe_name(name: str) -> bool:
    """Whether a published file name may be part of a path on disk."""
    return bool(_SAFE_NAME.match(name or "")) and ".." not in name


def download_name(published: str, name: str) -> str:
    """<publication day>-<file name>, from checked parts only."""
    if not safe_name(name):
        raise ValueError(f"refusing a download named {name!r}")
    day = published_day(published)[:10].replace("-", "")
    return f"{day or 'undated'}-{name}"


def inside(root: Path, path: Path) -> Path:
    """``path`` when it is inside ``root``; ValueError otherwise."""
    if not Path(path).resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"refusing a download outside {root}: {path}")
    return path


def trusted_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "data.gov.ro" or host.endswith(".data.gov.ro"))


def _refuse(url: str) -> None:
    if not trusted_url(url):
        raise RuntimeError(f"refusing to fetch {urlparse(url or '').hostname or url!r}: "
                           "only https://data.gov.ro is asked")


@asynccontextmanager
async def stream_trusted(client, url: str, params: dict | None = None):
    """``client.stream("GET", url)`` with redirects followed by hand, each
    hop only to https://data.gov.ro (the client may follow any)."""
    for _ in range(MAX_REDIRECTS + 1):
        _refuse(url)
        async with client.stream("GET", url, params=params, follow_redirects=False) as resp:
            if not resp.is_redirect:
                yield resp
                return
            url, params = str(resp.url.join(resp.headers.get("location", ""))), None
    raise RuntimeError(f"too many redirects fetching {url}")


async def get_trusted(client, url: str, params: dict | None = None):
    """A whole response, as stream_trusted asks for it."""
    async with stream_trusted(client, url, params) as resp:
        await resp.aread()
        return resp


def check_space(root: Path, size: int | None, what: str) -> None:
    """RuntimeError when the disk holding ``root`` hasn't room for a
    download of ``size`` bytes and the database loaded from it."""
    if not size:
        return
    probe = Path(root)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    need = 2 * size + SPARE_BYTES
    if free < need:
        raise RuntimeError(f"{what}: not enough free disk space ({free / 1e9:.1f} GB free, "
                           f"about {need / 1e9:.1f} GB needed to download and load it). "
                           f"Old downloads in {root} can be deleted by hand.")


def classify(filename: str) -> tuple[int, str] | None:
    """(year, form) of a data file name, or None for anything else."""
    m = _FILE.match((filename or "").lower())
    if not m:
        return None
    form = (m.group("form") or "").replace("_", "") or None
    form = {"blbssl": "BL", "uu": "UU", "ir": "IR"}.get(form, "BL" if form is None else "")
    return (int(m.group("year")), form) if form else None


def _size(raw) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def resources(packages: list[dict]) -> dict[tuple[int, str], Resource]:
    """The newest data file per (year, form) across the statement packages."""
    best: dict[tuple[int, str], Resource] = {}
    for package in packages:
        name = package.get("name") or ""
        if not _PACKAGE.match(name):
            continue
        licence = LICENCES.get((package.get("license_id") or "").lower(),
                               package.get("license_id") or "")
        for res in package.get("resources") or []:
            url = res.get("url") or ""
            found = classify(url.rsplit("/", 1)[-1])
            filename = url.rsplit("/", 1)[-1].lower()
            if not found or not trusted_url(url) or not safe_name(filename):
                continue
            year, form = found
            item = Resource(year=year, form=form, url=url, name=filename,
                            package=name, published=published_day(res.get("created")),
                            licence=licence, size=_size(res.get("size")))
            old = best.get(found)
            if old is None or item.published > old.published:
                best[found] = item
    return best


async def catalog(client) -> dict[tuple[int, str], Resource]:
    """Every year's newest files, from one search of the Ministry's packages."""
    resp = await get_trusted(client, f"{CKAN_API}/package_search", params={
        "q": "situatii_financiare", "fq": f"organization:{ORGANISATION}", "rows": 1000})
    resp.raise_for_status()
    packages = (resp.json().get("result") or {}).get("results") or []
    found = resources(packages)
    if not found:
        raise RuntimeError("no financial statements found on data.gov.ro")
    return found


def published_years(found: dict[tuple[int, str], Resource]) -> list[int]:
    """Years with at least the long and abridged forms published, newest
    first (before 2011 there is only the long one)."""
    forms: dict[int, set[str]] = {}
    for year, form in found:
        forms.setdefault(year, set()).add(form)
    return sorted((y for y, f in forms.items() if "BL" in f and ("UU" in f or y < 2011)),
                  reverse=True)


def save_catalog(found: dict[tuple[int, str], Resource], root: Path | None = None) -> None:
    """What data.gov.ro listed, and when it was asked: what status compares
    the loaded files with, offline."""
    root = Path(root or DOWNLOADS)
    root.mkdir(parents=True, exist_ok=True)
    data = {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "files": [asdict(r) for r in sorted(found.values(),
                                                key=lambda r: (-r.year, r.form))]}
    part = root / (CATALOG_FILE + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    part.replace(root / CATALOG_FILE)


def load_catalog(root: Path | None = None) -> tuple[str, dict[tuple[int, str], Resource]]:
    """(checked_at, files) from the last check, or ("", {})."""
    path = Path(root or DOWNLOADS) / CATALOG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", {}
    out = {}
    for item in data.get("files") or []:
        try:
            r = Resource(**item)
        except TypeError:
            continue
        out[(r.year, r.form)] = r
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
        progress(f"downloading {res.form} {res.year}", res.size)
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
        raise RuntimeError(f"{res.name}: {written} bytes doesn't look like the "
                           f"statements file (expected {res.size or 'more'})")
    with open(part, "rb") as fh:
        head = fh.read(64).lstrip(b"\xef\xbb\xbf").upper()
    if not head.startswith(b"CUI,CAEN"):
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{res.name}: not a statements file (no CUI,CAEN header)")
    part.replace(target)
    sidecar.write_text(json.dumps(asdict(res), ensure_ascii=False), encoding="utf-8")
    return target


def downloaded(root: Path | None = None) -> dict[tuple[int, str], tuple[Resource, Path]]:
    """The newest complete download per (year, form) already on disk, with
    its listing: what a load uses when data.gov.ro can't be reached."""
    out: dict[tuple[int, str], tuple[Resource, Path]] = {}
    root = Path(root or DOWNLOADS)
    if not root.is_dir():
        return out
    for sidecar in root.glob("[0-9][0-9][0-9][0-9]/*.txt.json"):
        try:
            res = Resource(**json.loads(sidecar.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        path = sidecar.with_name(sidecar.name[:-len(".json")])
        if not _complete(path, res.size):
            continue
        key = (res.year, res.form)
        if key not in out or res.published > out[key][0].published:
            out[key] = (res, path)
    return out
