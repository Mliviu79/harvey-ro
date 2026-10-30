"""ONRC — Romania's trade registry, as a source of companies and administrators.

The registry publishes every company it holds as open data on data.gov.ro
(CC-BY 4.0), refreshed every month or two as a new "firme-DD-MM-YYYY"
dataset of '^'-separated, BOM-prefixed UTF-8 files:

    OD_CAEN_AUTORIZAT   registration number -> authorised CAEN class + version
    OD_STARE_FIRMA      registration number -> status codes (1048 = operating)
    OD_FIRME            registration number -> name, CUI, registered office, web
    OD_REPREZENTANTI_LEGALI   registration number -> representative + role

Together they answer "every operating dental practice in Brașov, and who
runs it" for free, without a single search. The files are big (the firms
file alone is ~700 MB), so nothing here loads one into memory: each is
streamed, in the order that keeps the working set smallest. With an
activity filter: activity first (the classes you asked for, nationwide),
then status (only for those), then firms (only those, in your county).
Without one (every activity in a county or town): the firms file for the
area's registration numbers, status for those, the firms file again for
the ones kept. Then the activity file again for every class the kept
firms are authorised for (saved as company_activities, so they can be
searched by code or in words), then representatives (only for the firms
kept, and only when wanted). harvey/registry.py loads the same files once
per dataset into a local database instead, with every firm in it, and
searches that; this module's streaming import still works on its own.

What is kept is deliberately narrow. The representatives file also carries
birth dates and places and home localities; only a natural person's name
and role are read. A CUI that is really a personal numeric code (13
digits, as some sole traders' rows have) is dropped. Sole traders and
family businesses get no street address, since theirs is usually a home.
The downloaded files themselves are kept as published (data/onrc/<date>/):
prune_downloads() deletes the representatives file and older datasets, but
only when called.

Every fact lands as an observation with the dataset it came from, under
the same confirmation gate as discovery: companies (and what they are
authorised to do, which describes them like their industry) are saved
because you asked for them by activity and place, but ACTIVITY_CODE,
CONTACT_FOUND and DECISION_MAKER_TITLE rows are written only when you
confirmed those signals.
"""

import csv
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx

from harvey.models import Company

logger = logging.getLogger(__name__)

CKAN_API = "https://data.gov.ro/api/3/action"
DATASET_PAGE = "https://data.gov.ro/dataset/{name}"
SOURCE = "registry:onrc"
COLLECTOR = "registry"

# The files an import reads, by role. "people" is only fetched when its
# signals are confirmed: it's the second-largest file and useless otherwise.
FILES = {
    "caen": "OD_CAEN_AUTORIZAT.CSV",
    "status": "OD_STARE_FIRMA.CSV",
    "firms": "OD_FIRME.CSV",
    "people": "OD_REPREZENTANTI_LEGALI.CSV",
}
# Smallest believable size per file. The national files are hundreds of MB;
# anything far below that is an error page or a truncated download.
MIN_BYTES = {
    "caen": 50_000_000,
    "status": 10_000_000,
    "firms": 100_000_000,
    "people": 30_000_000,
}

ACTIVE_STATUS = "1048"      # "funcțiune": operating

# Forms that are a natural person trading (PF/PFA sole traders, II sole
# proprietorships, IF and AF family businesses). Their registered office
# is usually a home, so no street address is stored for them.
PERSONAL_FORMS = frozenset({"PF", "PFA", "II", "IF", "AF"})

# Roles that run the company day to day. Insolvency practitioners
# (lichidator, administrator judiciar/provizoriu/special/concordatar) are
# appointed by a court and are nobody's buyer.
_NOT_MANAGEMENT = ("lichidator", "judiciar", "provizoriu", "special",
                   "concordatar", "mandatar", "cenzor", "supraveghere")
_MANAGEMENT = ("administrator", "director", "presedinte", "reprezentant")
_DECISION_MAKERS = ("administrator", "director", "presedinte")
# A representative that is itself a company (often an insolvency firm).
_LEGAL_ENTITY = re.compile(
    r"\b(s\s?r\s?l|s\s?p\s?r\s?l|ipurl|s\s?a|s\s?c\s?a|s\s?n\s?c|srl\w*|"
    r"societate\w*|cabinet\w*|insolv\w*|consult\w*)\b")

# Two-letter county codes (as on number plates) -> the registry's spelling.
COUNTIES = {
    "AB": "Alba", "AR": "Arad", "AG": "Argeș", "BC": "Bacău", "BH": "Bihor",
    "BN": "Bistrița-Năsăud", "BT": "Botoșani", "BV": "Brașov", "BR": "Brăila",
    "B": "București", "BZ": "Buzău", "CS": "Caraș-Severin", "CL": "Călărași",
    "CJ": "Cluj", "CT": "Constanța", "CV": "Covasna", "DB": "Dâmbovița",
    "DJ": "Dolj", "GL": "Galați", "GR": "Giurgiu", "GJ": "Gorj", "HR": "Harghita",
    "HD": "Hunedoara", "IL": "Ialomița", "IS": "Iași", "IF": "Ilfov",
    "MM": "Maramureș", "MH": "Mehedinți", "MS": "Mureș", "NT": "Neamț",
    "OT": "Olt", "PH": "Prahova", "SM": "Satu Mare", "SJ": "Sălaj", "SB": "Sibiu",
    "SV": "Suceava", "TR": "Teleorman", "TM": "Timiș", "TL": "Tulcea",
    "VS": "Vaslui", "VL": "Vâlcea", "VN": "Vrancea",
}
_COUNTY_ALIASES = {"bucharest": "B", "bucuresti": "B", "buc": "B"}

_DATASET_NAME = re.compile(r"^firme-(\d{2})-(\d{2})-(\d{4})$")


def _fold(text: str) -> str:
    """Lowercase, no diacritics (ș and ş alike), letters and digits only,
    single spaces: "Bistrița-Năsăud" and "bistrita nasaud" fold the same."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


# ── Which statuses mean a company has stopped (or is stopping) trading ──
#
# The registry has ~200 status codes and adds more over time, so the closed
# set is read off the official nomenclature's names rather than kept as a
# list of numbers: anything that says struck off (radiere/radiată), dissolved
# (dizolvare, incl. a dissolution request sent to court), in liquidation
# (lichidare/lichidator), bankrupt (faliment), insolvent (insolvență,
# insolvabilă, the insolvency laws 85/2006, 85/2014 and 64/1995, judicial
# reorganisation), suspended or paused (suspendare/întrerupere activitate),
# office struck off (sediu radiat), or a registration that was suspended,
# lapsed, refused or annulled. Not closed: statuses about a parent company
# ("societatea mamă ..."), a ban on *starting* dissolution (1140), and
# "denumire radiată" (1101: an old name struck off after a rename).
#
# "Sediu expirat" (2069) and "valabilitate sediu expirată" (1085) are NOT
# closed: they say the document proving the right to use the registered
# office has lapsed in the registry's records, which is common among
# companies that are plainly still trading and is a ground for a court to
# dissolve them later, not a dissolution. Once it gets that far the
# registration carries a dissolution status too, and that one excludes it.
_CLOSING = re.compile(
    r"\b(radier|dizolv|lichid|faliment|insolven|insolvab|intrerupere|nulitate)"
    r"|(?<!denumire )\bradiat"
    r"|\bsuspendare( interzicere)? activit"
    r"|\breorganizar\w* judiciar"
    r"|\bleg\w* nr (85 20|64 1995)"
    r"|\binmatriculare (suspendata|perimata|respinsa)")
_NOT_THIS_COMPANY = re.compile(r"\b(societatea|firma) mama\b|\binterdictia\b")


def status_names() -> dict[str, str]:
    """Status code -> Romanian name, from the nomenclature shipped in
    harvey/reference/onrc_status_ro.csv (ONRC open data, CC-BY 4.0). The
    'firme' datasets don't carry it, so a copy ships with Harvey."""
    text = resources.files("harvey").joinpath(
        "reference/onrc_status_ro.csv").read_text(encoding="utf-8")
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    return {r["code"]: r["name_ro"] for r in rows}


def closed_statuses(names: dict[str, str]) -> frozenset[str]:
    """The codes whose name says the company is closed or closing."""
    return frozenset(code for code, name in names.items()
                     if _CLOSING.search(_fold(name))
                     and not _NOT_THIS_COMPANY.search(_fold(name)))


# Codes that outrank 1048 when a registration carries both. Computed once,
# at import, from the nomenclature; tests/test_onrc.py pins the key ones.
CLOSED_STATUSES = closed_statuses(status_names())


def _comma_below(text: str) -> str:
    """Romanian's correct ș/ț for the cedilla ş/ţ older records use."""
    return (text or "").translate(str.maketrans("şţŞŢ", "șțȘȚ"))


def county_name(text: str) -> str | None:
    """The county a user means, as the registry spells it, or None.

    Takes the name with or without diacritics, a "Județul" prefix, or the
    two-letter code: "Brașov", "brasov", "BV" and "judetul Brasov" are all
    Brașov. București (also "Bucharest", "B", "Municipiul București")
    counts as a county here, as it does in the registry. A two-word county
    also answers to its first word, which no other county shares:
    "Bistrița", "Caraș", "Satu".
    """
    folded = _fold(text)
    folded = re.sub(r"^(judetul|judet|jud|municipiul|mun)\s+|\s+(county|judet)$", "",
                    folded).strip()
    if not folded:
        return None
    code = _COUNTY_ALIASES.get(folded) or (folded.upper() if folded.upper() in COUNTIES else "")
    if code:
        return COUNTIES[code]
    for name in COUNTIES.values():
        if _fold(name) == folded:
            return name
    by_first = [name for name in COUNTIES.values()
                if len(_fold(name).split()) > 1 and _fold(name).split()[0] == folded]
    return by_first[0] if len(by_first) == 1 else None


def clean_locality(text: str) -> str:
    """"Municipiul Braşov" -> "Brașov"; "Sat Cristian, Comuna Cristian" ->
    "Cristian"; "Loc. Râşnov, Oraş Râşnov" -> "Râșnov". The village or town
    itself, without its administrative label."""
    first = _comma_below((text or "").split(",")[0]).strip()
    return re.sub(r"^(municipiul|oraș|orașul|comuna|sat|satul|loc\.?)\s+", "",
                  first, flags=re.IGNORECASE).strip()


def _locality_matches(wanted: str, locality: str) -> bool:
    want = _fold(wanted).split()
    have = _fold(locality).split()
    return bool(want) and any(have[i:i + len(want)] == want
                              for i in range(len(have) - len(want) + 1))


def valid_cui(raw: str) -> str:
    """The tax code (CUI) as digits, or "" when there isn't a real one.

    Sole traders' rows can carry 0, or a 13-digit personal numeric code
    (CNP) where the CUI goes: that is a person's national id, and it is
    never stored. A CUI has at most 10 digits.
    """
    digits = re.sub(r"\D", "", (raw or "").upper().removeprefix("RO"))
    digits = digits.lstrip("0")
    return digits if 2 <= len(digits) <= 10 else ""


def split_person(raw: str) -> tuple[str, str]:
    """(first, last) from the registry's "SURNAME GIVEN NAMES" order.

    The registry writes surname first, in capitals. Title-cased per word
    and per hyphenated part. A compound surname written as two words
    can't be told apart from a second given name, so the first word is
    taken as the surname.
    """
    words = _comma_below(raw).split()
    if len(words) < 2:
        return "", ""

    def title(word: str) -> str:
        return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))

    return " ".join(title(w) for w in words[1:]), title(words[0])


def _role(raw: str) -> str:
    """The role as a title ("Administrator"), or "" when it isn't one that
    runs the company."""
    folded = _fold(raw)
    if not folded.startswith(_MANAGEMENT) or any(w in folded for w in _NOT_MANAGEMENT):
        return ""
    text = _comma_below(" ".join(raw.split()))
    return text[:1].upper() + text[1:]


def _address(row: dict) -> str:
    """Street and number of the registered office, and a current 6-digit
    postal code when filed. No block, staircase, floor or flat."""
    street = _comma_below(" ".join((row.get("ADR_DEN_STRADA") or "").split()))
    number = (row.get("ADR_NR_STRADA") or "").strip()
    postal = (row.get("ADR_COD_POSTAL") or "").strip()
    parts = [" ".join(p for p in (street, number) if p)]
    if re.fullmatch(r"\d{6}", postal):
        parts.append(postal)
    return ", ".join(p for p in parts if p)


def _website(raw: str) -> tuple[str, str]:
    """(website, domain) from the registry's free-text WEB field, which
    also holds phone numbers and typos: only a plausible host counts."""
    from harvey.collectors.discover import is_junk, normalize_domain

    try:
        domain = normalize_domain((raw or "").strip())
    except ValueError:          # "http://[..." and the like: not a URL at all
        return "", ""
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)*\.[a-z]{2,}", domain):
        return "", ""
    if is_junk(domain):
        return "", ""
    return f"https://{domain}", domain


# ── The dataset: find the current one, fetch it once ──

@dataclass
class Dataset:
    name: str                   # "firme-19-10-2026"
    date: str                   # "2026-10-19"
    files: dict[str, tuple[str, int | None]]   # role -> (url, size)

    @property
    def page(self) -> str:
        return DATASET_PAGE.format(name=self.name)


def _dataset_date(name: str) -> date | None:
    m = _DATASET_NAME.match(name or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _trusted_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "data.gov.ro" or host.endswith(".data.gov.ro"))


def _dataset_from_package(package: dict) -> Dataset | None:
    """The package as a Dataset, or None when it is missing a file an
    import reads or links one outside data.gov.ro."""
    when = _dataset_date(package.get("name", ""))
    if not when:
        return None
    by_name = {(r.get("name") or "").upper(): r for r in package.get("resources") or []}
    files = {}
    for role, filename in FILES.items():
        res = by_name.get(filename)
        if not res or not _trusted_url(res.get("url", "")):
            return None
        size = res.get("size")
        files[role] = (res["url"], int(size) if isinstance(size, int) or
                       (isinstance(size, str) and size.isdigit()) else None)
    return Dataset(package["name"], when.isoformat(), files)


async def latest_dataset(client: httpx.AsyncClient) -> Dataset:
    """The newest "firme-DD-MM-YYYY" dataset ONRC has published, by the
    date in its name, that has every file an import reads."""
    resp = await client.get(f"{CKAN_API}/package_search", params={
        "fq": "organization:onrc", "q": "name:firme*",
        "sort": "metadata_created desc", "rows": 20,
    })
    resp.raise_for_status()
    results = (resp.json().get("result") or {}).get("results") or []
    candidates = sorted(
        (p for p in results if _dataset_date(p.get("name", ""))),
        key=lambda p: _dataset_date(p["name"]), reverse=True)
    for package in candidates:
        dataset = _dataset_from_package(package)
        if dataset:
            return dataset
    raise RuntimeError("no complete ONRC 'firme' dataset found on data.gov.ro")


async def dataset_named(client: httpx.AsyncClient, name: str) -> Dataset:
    """One dataset by its name ("firme-19-10-2026")."""
    if not _dataset_date(name):
        raise ValueError(f"not an ONRC dataset name: {name!r} (firme-DD-MM-YYYY)")
    resp = await client.get(f"{CKAN_API}/package_show", params={"id": name})
    resp.raise_for_status()
    dataset = _dataset_from_package((resp.json() or {}).get("result") or {})
    if not dataset:
        raise RuntimeError(f"{name} is missing a registry file on data.gov.ro")
    return dataset


def local_dataset(root: Path, name: str, roles=FILES) -> tuple[Dataset, dict[str, Path]] | None:
    """A dataset already downloaded under root/<date>/, without asking
    data.gov.ro: (Dataset, role -> file) when every one of ``roles`` is
    there at a believable size, else None."""
    when = _dataset_date(name)
    if not when:
        raise ValueError(f"not an ONRC dataset name: {name!r} (firme-DD-MM-YYYY)")
    folder = Path(root) / when.isoformat()
    out = {}
    for role in roles:
        path = folder / FILES[role]
        if not path.is_file() or not _size_ok(role, path.stat().st_size, None):
            return None
        out[role] = path
    return Dataset(name, when.isoformat(), {}), out


async def fetch_files(client: httpx.AsyncClient, dataset: Dataset, root: Path,
                      roles=FILES, progress=None) -> dict[str, Path]:
    """Each file on disk under root/<dataset date>/, downloaded only when
    there isn't already a complete copy. Streamed to a .part file and
    renamed once it is whole, so an interrupted download is never mistaken
    for a cached one."""
    folder = Path(root) / dataset.date
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for role in roles:
        url, expected = dataset.files[role]
        target = folder / FILES[role]
        if target.exists() and _size_ok(role, target.stat().st_size, expected):
            out[role] = target
            continue
        if progress:
            progress(role, FILES[role], expected)
        part = target.with_name(target.name + ".part")
        written = 0
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            announced = resp.headers.get("content-length")
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
                    written += len(chunk)
        if announced and announced.isdigit() and int(announced) != written:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{FILES[role]}: got {written} of {announced} bytes")
        if not _size_ok(role, written, expected):
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{FILES[role]}: {written} bytes doesn't look like the "
                               f"registry file (expected {expected or 'more'})")
        part.replace(target)
        out[role] = target
    return out


def _size_ok(role: str, size: int, expected: int | None) -> bool:
    if expected:
        return size == expected
    return size >= MIN_BYTES.get(role, 1)


_DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def prune_downloads(root: Path, dataset: Dataset, keep_people: bool = False) -> list[Path]:
    """Delete downloads no longer needed; returns what was deleted. Never
    called automatically: the files are kept as published unless someone
    asks for this.

    The representatives file holds the birth date and place and the home
    locality of every company representative in Romania, of which the
    registry keeps only names and roles: unless ``keep_people``, it goes
    (the next load fetches it again). Older datasets go too, so only the
    current one stays on disk. Only the
    registry's own files (and interrupted .part downloads) in date-named
    folders are touched; a folder is removed once nothing else is in it.
    """
    root = Path(root)
    ours = {name for name in FILES.values()} | {name + ".part" for name in FILES.values()}
    deleted: list[Path] = []
    if not root.is_dir():
        return deleted
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not _DATE_FOLDER.match(folder.name):
            continue
        current = folder.name == dataset.date
        for path in sorted(folder.iterdir()):
            if path.name not in ours or not path.is_file():
                continue
            if current and (keep_people or path.name != FILES["people"]):
                continue
            path.unlink()
            deleted.append(path)
        if not current and not any(folder.iterdir()):
            folder.rmdir()
    return deleted


# ── Reading the files ──

def read_rows(path: Path, columns: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """The named columns of each row, streamed. Rows with a different
    number of fields than the header (a stray '^' in a name) are skipped
    rather than read shifted into the wrong columns."""
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        header = fh.readline().rstrip("\r\n").split("^")
        try:
            idx = [header.index(c) for c in columns]
        except ValueError as e:
            raise RuntimeError(f"{Path(path).name}: unexpected header ({e})") from None
        width = len(header)
        for line in fh:
            parts = line.rstrip("\r\n").split("^")
            if len(parts) != width:
                continue
            yield tuple(parts[i] for i in idx)


def _version(raw: str) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def matching_registrations(path: Path, classes: set[tuple[str, int | None]]
                           ) -> dict[str, list[tuple[str, int]]]:
    """Registration number -> the wanted classes it is authorised for.
    A class with version None matches the code in Rev.2 and Rev.3 alike;
    the pre-2008 nomenclatures never match (their numbers mean other
    things)."""
    wanted = {(c, v) for c, v in classes if v is not None}
    any_current = {c for c, v in classes if v is None}
    out: dict[str, list[tuple[str, int]]] = {}
    for reg, code, ver in read_rows(path, ("COD_INMATRICULARE", "COD_CAEN_AUTORIZAT",
                                           "VER_CAEN_AUTORIZAT")):
        code, version = code.strip().zfill(4), _version(ver)
        if (code, version) in wanted or (code in any_current and version in (2, 3)):
            hits = out.setdefault(reg.strip(), [])
            if (code, version) not in hits:
                hits.append((code, version))
    return out


def authorised_codes(path: Path, regs) -> dict[str, list[tuple[str, int]]]:
    """Registration number -> every class it is authorised for, any
    revision (the pre-2008 ones too: they describe old registrations), in
    file order. Only for ``regs``: the national file is streamed, and only
    the codes of these registrations are held."""
    out: dict[str, list[tuple[str, int]]] = {}
    for reg, code, ver in read_rows(path, ("COD_INMATRICULARE", "COD_CAEN_AUTORIZAT",
                                           "VER_CAEN_AUTORIZAT")):
        reg = reg.strip()
        if reg not in regs:
            continue
        code, version = code.strip().zfill(4), _version(ver)
        if version is None or not code.isdigit():
            continue
        have = out.setdefault(reg, [])
        if (code, version) not in have:
            have.append((code, version))
    return out


def active_registrations(path: Path, regs) -> set[str]:
    """Which of these registrations are operating: status 1048 and none of
    the closing statuses. One with no status row at all isn't counted."""
    codes: dict[str, set[str]] = {}
    for reg, code in read_rows(path, ("COD_INMATRICULARE", "COD")):
        reg = reg.strip()
        if reg in regs:
            codes.setdefault(reg, set()).add(code.strip())
    return {reg for reg, have in codes.items()
            if ACTIVE_STATUS in have and not have & CLOSED_STATUSES}


@dataclass
class Firm:
    reg: str
    name: str
    cui: str
    form: str
    county: str
    locality: str
    address: str
    website: str
    domain: str
    registered: str


_FIRM_COLUMNS = ("DENUMIRE", "CUI", "COD_INMATRICULARE", "DATA_INMATRICULARE",
                 "FORMA_JURIDICA", "ADR_JUDET", "ADR_LOCALITATE", "ADR_DEN_STRADA",
                 "ADR_NR_STRADA", "ADR_COD_POSTAL", "WEB")


def counties(spec: str) -> list[str] | None:
    """The registry's spelling of each comma-separated county in ``spec``,
    or None for "" (the whole country). An unknown one raises ValueError."""
    names = []
    for part in re.split(r"[,;]", spec or ""):
        if not part.strip():
            continue
        name = county_name(part)
        if name is None:
            raise ValueError(f"unknown county {part.strip()!r}: use its name or code (BV, CJ, B)")
        if name not in names:
            names.append(name)
    return names or None


def _localities(spec: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", spec or "") if p.strip()]


def _in_area(county: list[str] | str | None, locality: list[str] | str):
    """A test for (county, locality) as the firms file spells them."""
    wanted = [county] if isinstance(county, str) else list(county or [])
    want_counties = {_fold(c) for c in wanted if c}
    places = _localities(locality) if isinstance(locality, str) else list(locality)

    def test(row_county: str, row_locality: str) -> bool:
        if want_counties and _fold(row_county) not in want_counties:
            return False
        return not places or any(_locality_matches(p, row_locality) for p in places)
    return test


def registrations_in_area(path: Path, county: list[str] | str | None = None,
                          locality: list[str] | str = "") -> set[str]:
    """Every registration number whose registered office is in the area,
    whatever it does: the first pass of an import with no activity filter.
    Only the numbers are held; the firms are read again for those kept."""
    inside = _in_area(county, locality)
    return {reg.strip() for reg, row_county, row_locality in read_rows(
        path, ("COD_INMATRICULARE", "ADR_JUDET", "ADR_LOCALITATE"))
        if inside(row_county, row_locality)}


def parse_firm(rec: dict) -> Firm:
    """One row of the firms file (column name -> raw value) as a Firm:
    names with Romanian's comma-below letters, a real CUI or none, no street
    address for a sole trader or family business, a website only when it
    looks like one."""
    form = rec["FORMA_JURIDICA"].strip().upper()
    website, domain = _website(rec["WEB"]) if rec["WEB"].strip() else ("", "")
    return Firm(
        reg=rec["COD_INMATRICULARE"].strip(),
        name=_comma_below(" ".join(rec["DENUMIRE"].split())),
        cui=valid_cui(rec["CUI"]),
        form=form,
        county=_comma_below(rec["ADR_JUDET"].strip()),
        locality=clean_locality(rec["ADR_LOCALITATE"]),
        address="" if form in PERSONAL_FORMS else _address(rec),
        website=website, domain=domain,
        registered=rec["DATA_INMATRICULARE"].strip(),
    )


def matching_firms(path: Path, regs, county: list[str] | str | None = None,
                   locality: list[str] | str = "") -> list[Firm]:
    """The firms among these registrations whose registered office is in one
    of the counties (all of Romania when none) and, when given, one of the
    localities."""
    inside = _in_area(county, locality)
    out = []
    for row in read_rows(path, _FIRM_COLUMNS):
        rec = dict(zip(_FIRM_COLUMNS, row))
        if rec["COD_INMATRICULARE"].strip() not in regs:
            continue
        if not inside(rec["ADR_JUDET"], rec["ADR_LOCALITATE"]):
            continue
        out.append(parse_firm(rec))
    return out


PEOPLE_COLUMNS = ("COD_INMATRICULARE", "PERSOANA_IMPUTERNICITA", "CALITATE")


def parse_person(name: str, role: str) -> tuple[str, str, str] | None:
    """(first, last, role) for a representative who runs the company, or
    None: companies acting as representatives, insolvency practitioners and
    any name holding digits (an id where a name should be) are left out."""
    title = _role(role)
    if not title or re.search(r"\d", name) or _LEGAL_ENTITY.search(_fold(name)):
        return None
    first, last = split_person(name)
    return (first, last, title) if first else None


def administrators(path: Path, regs) -> dict[str, list[tuple[str, str, str]]]:
    """Registration number -> [(first, last, role)] for the people who run
    the company. Only the name and role columns are read; companies acting
    as representatives, insolvency practitioners and any name holding
    digits (an id where a name should be) are left out."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for reg, name, role in read_rows(path, PEOPLE_COLUMNS):
        reg = reg.strip()
        if reg not in regs:
            continue
        person = parse_person(name, role)
        if not person:
            continue
        people = out.setdefault(reg, [])
        if all(person[:2] != (f, l) for f, l, _ in people):
            people.append(person)
    return out


# ── The import ──

PROGRESS_EVERY = 500


@dataclass
class ImportReport:
    dataset: str = ""
    classes: list[str] = field(default_factory=list)
    # With an activity filter: registrations authorised for a wanted class,
    # of which operating, of which in the county/locality. Without one:
    # registrations in the area, of which operating, and in_area == active.
    authorised: int = 0
    active: int = 0
    in_area: int = 0
    catch_all_only: int = 0      # ... matched only on Rev.2's catch-all 8690
    shared_domain: int = 0       # website domain already held by another business
    excluded: int = 0            # on the do-not-contact list
    new_companies: int = 0
    known_companies: int = 0
    updated_companies: int = 0
    administrators: int = 0
    activities: int = 0          # company activities (CAEN classes) added
    observations: int = 0
    unconfirmed: list[str] = field(default_factory=list)
    run_id: str = ""
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# Rev.2's "other human health activities": Rev.3 split it into labs (8691),
# psychologists (8693), physiotherapy (8695) and more. A search for one of
# those also asks for 8690 Rev.2, since older registrations still carry it,
# but such a company may be any of the others: its ACTIVITY_CODE is
# recorded at CATCH_ALL_CONFIDENCE and counted apart in the report.
CATCH_ALL = ("8690", 2)
CATCH_ALL_CONFIDENCE = 0.5


def _catch_all_is_vague(classes) -> bool:
    """True when 8690 Rev.2 is only in the query standing in for a
    narrower Rev.3 class, not asked for in its own right (--codes 8690)."""
    return CATCH_ALL in classes and any(
        v == 3 and c.startswith("869") for c, v in classes)


def class_labels(classes) -> list[str]:
    """One label per code, with the revisions asked for, in query order:
    [("8623", 3), ("8623", 2), ("8690", 2)] -> ["8623 Rev.2/3", "8690 Rev.2"]."""
    from harvey.caen import revision_label

    versions: dict[str, set] = {}
    for code, version in classes:
        versions.setdefault(code, set()).update((2, 3) if version is None else (version,))
    out = []
    for code, have in versions.items():
        if have == {2, 3}:
            out.append(f"{code} Rev.2/3")
        else:
            out.append(f"{code} " + "/".join(revision_label(v) for v in sorted(have)))
    return out


def activity_value(code: str, version: int) -> str:
    """The ACTIVITY_CODE value: the class and its CAEN revision, "8623/3".
    The revision matters: the same number can mean different activities
    in different nomenclatures. caen.describe() reads it back."""
    return f"{code}/{version}"


def _industry_class(codes: list[tuple[str, int]]) -> tuple[str, int] | None:
    """The class that names a company's industry when nothing was asked
    for: its first Rev.3 class, else its first Rev.2 one, else its first."""
    for version in (3, 2):
        for pair in codes:
            if pair[1] == version:
                return pair
    return codes[0] if codes else None


def _location(firm: Firm) -> str:
    county = firm.county
    parts = [firm.locality] if firm.locality else []
    if county and not _fold(firm.locality).startswith(_fold(county)):
        parts.append(county)
    return ", ".join(parts + ["Romania"])


# Companies saved per transaction. Big enough that committing is noise,
# small enough that an interrupted import keeps most of what it saved.
SAVE_BATCH = 500
# The ImportReport counts save_firms adds to as it saves.
_REPORT_COUNTS = ("catch_all_only", "shared_domain", "excluded", "new_companies",
                  "known_companies", "updated_companies", "administrators",
                  "activities", "observations")


def _class_matcher(classes):
    """A test for whether (code, version) is one of ``classes``; a class
    with version None stands for Rev.2 and Rev.3 alike."""
    exact = {(c, v) for c, v in classes if v is not None}
    either = {c for c, v in classes if v is None}
    return lambda pair: pair in exact or (pair[0] in either and pair[1] in (2, 3))


async def save_firms(
    state,
    firms: list[Firm],
    dataset: Dataset,
    report: ImportReport,
    *,
    codes: dict[str, list[tuple[str, int]]],
    people: dict[str, list[tuple[str, str, str]]] | None = None,
    classes: list[tuple[str, int | None]] | None = None,
    run_params: dict | None = None,
    with_administrators: bool = True,
    dry_run: bool = False,
    do_not_contact: list[str] | None = None,
    progress=None,
) -> ImportReport:
    """Save registry firms into the working database, SAVE_BATCH per
    transaction. Shared by the streaming import and harvey registry pull.

    ``codes`` holds every class each firm (by registration number) is
    authorised for; ``classes`` the ones the request named, in its order:
    those a firm holds are its ACTIVITY_CODE observations, and the first
    names its industry (otherwise its first Rev.3 class does). ``people``
    are its administrators. Companies are matched to what is on file by
    CUI, then by registration number, never by website domain alone (a
    domain another business holds is counted in ``shared_domain`` and left
    off); a known one has its gaps filled, never its existing values
    replaced, except the registered-office address, which is always
    refreshed: the registry is the authority on it, and a re-import is how
    a moved office reaches the record. ``dry_run`` reads everything and
    writes nothing. Observations are written once per dataset: re-importing
    the same dataset adds nothing, a newer one adds its readings beside the
    old. ``progress(done, total)`` is called every PROGRESS_EVERY companies.
    """
    from harvey.caen import describe
    from harvey.gate import do_not_contact_domain_match
    from harvey.modules import switches
    from harvey.signals import sync_vocabulary

    await switches.require(state, "onrc", "Saving firms from the ONRC registry")
    classes = list(classes or [])
    people = people or {}
    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    wanted_codes = ("ACTIVITY_CODE", "CONTACT_FOUND", "DECISION_MAKER_TITLE") \
        if with_administrators else ("ACTIVITY_CODE",)
    report.unconfirmed = [c for c in wanted_codes if c not in confirmed]
    if not with_administrators:
        people = {}

    named = _class_matcher(classes)
    order = {pair: i for i, pair in enumerate(classes)}
    vague = CATCH_ALL if _catch_all_is_vague(classes) else None

    def rank(pair):
        return (pair == vague,
                min(order.get(pair, len(order)), order.get((pair[0], None), len(order))))

    if not dry_run:
        report.run_id = await state.start_run("import", provider="onrc",
                                              params=run_params or {})
    evidence = dataset.page

    async def save(batch, firm: Firm):
        if firm.domain and do_not_contact_domain_match(firm.domain, do_not_contact or []):
            report.excluded += 1
            return
        firm_codes = list(codes.get(firm.reg, []))
        matched = sorted({pair for pair in firm_codes if named(pair)}, key=rank) \
            if classes else []
        if matched and matched[0] == vague:
            report.catch_all_only += 1
        # Nothing asked for (or matched by name only): ACTIVITY_CODE records
        # only classes someone asked for (a whole county would be ~600k
        # observations), and the company's first current class names it.
        first = matched[0] if matched else _industry_class(firm_codes)
        industry = (describe(*first) or "") if first else ""
        external = f"onrc:{firm.reg}"
        draft = Company(
            name=firm.name, cui=firm.cui, external_id=external,
            website=firm.website, domain=firm.domain, industry=industry,
            location=_location(firm), address=firm.address,
            source=SOURCE, source_url=evidence,
        )
        # The resolver matches the legal identity (CUI, registration
        # number) as before, and now also a company found elsewhere by the
        # same name in the same town. The registry's website field is
        # self-declared: a domain another business already holds may be a
        # shared group site or simply wrong, so on its own it is a possible
        # match for review, never a reason to hang these administrators and
        # this address on that business.
        match = await batch.match_company(draft, declared_domain=True)
        existing = match.company_id if match.attach else None
        domain = firm.domain
        holder = await batch.get_company_by_domain(domain) if domain else None
        if holder and (existing is None or holder.id != existing):
            report.shared_domain += 1
            domain = draft.domain = ""
        elif holder and not match.same_record:
            # Joining the holder by a merge: the merge brings the domain.
            draft.domain = ""
        if existing:
            report.known_companies += 1
        else:
            report.new_companies += 1
        admins = people.get(firm.reg, [])
        report.administrators += len(admins)
        if dry_run:
            return

        if existing and match.same_record:
            company_id = existing
            changed = await batch.fill_company_fields(
                company_id, cui=firm.cui, name=firm.name, website=firm.website,
                domain=domain, industry=industry, location=_location(firm),
                address=firm.address)
            await batch.register_ids(company_id, draft, match)
            if changed:
                report.updated_companies += 1
        else:
            # New, or the same company by other evidence (saved, then merged
            # into it, which can be undone), or a possible one for review.
            company_id = (await batch.save_company(draft, match)).company_id

        # What the company does, all of it: describes the record (like
        # its industry), so it isn't held behind the signal gate.
        report.activities += await batch.set_company_activities(
            company_id, firm_codes, source=COLLECTOR)

        rows: dict[tuple[str, str], float] = {}     # (signal, value) -> confidence
        if "ACTIVITY_CODE" in confirmed:
            for pair in matched:
                rows[("ACTIVITY_CODE", activity_value(*pair))] = \
                    CATCH_ALL_CONFIDENCE if pair == vague else 1.0
        for first_name, last, title in admins:
            text = f"{first_name} {last} — {title}"
            if "CONTACT_FOUND" in confirmed:
                rows.setdefault(("CONTACT_FOUND", text), 1.0)
            if "DECISION_MAKER_TITLE" in confirmed and \
                    _fold(title).startswith(_DECISION_MAKERS):
                rows.setdefault(("DECISION_MAKER_TITLE", text), 1.0)
        if rows:
            seen = await batch.observed(company_id, evidence)
            fresh = [{"company_id": company_id, "signal_code": code_,
                      "collector": COLLECTOR, "value_text": text,
                      "confidence": confidence, "evidence_url": evidence,
                      "run_id": report.run_id}
                     for (code_, text), confidence in rows.items()
                     if (code_, text) not in seen]
            if fresh:
                report.observations += await batch.add_observations(
                    fresh, run_id=report.run_id)

    try:
        done = 0
        for start in range(0, len(firms), SAVE_BATCH):
            # A batch that fails is rolled back, so are its counts.
            counts = {k: getattr(report, k) for k in _REPORT_COUNTS}
            try:
                async with state.batch() as batch:
                    for firm in firms[start:start + SAVE_BATCH]:
                        done += 1
                        if progress and done % PROGRESS_EVERY == 0:
                            progress(done, len(firms))
                        await save(batch, firm)
            except Exception:
                for k, v in counts.items():
                    setattr(report, k, v)
                raise
        if not dry_run:
            await state.finish_run(report.run_id, status="completed",
                                   records=report.new_companies + report.known_companies)
    except Exception as e:
        logger.exception("onrc import failed")
        report.errors.append(str(e))
        if not dry_run:
            await state.finish_run(report.run_id, status="failed",
                                   records=report.new_companies, error=str(e))
    return report


async def import_registry(
    state,
    files: dict[str, Path],
    dataset: Dataset,
    *,
    county: str = "",
    classes: list[tuple[str, int | None]] | None = None,
    locality: str = "",
    active_only: bool = True,
    dry_run: bool = False,
    do_not_contact: list[str] | None = None,
    with_administrators: bool = True,
    progress=None,
) -> ImportReport:
    """Read the registry files and save what matches (save_firms).

    ``county`` and ``locality`` take comma-separated lists; no county is the
    whole country. ``classes`` are (code, version) pairs, version None for "Rev.2 or
    Rev.3"; their order picks which class names the company's industry
    (Rev.2's catch-all 8690 last, when it stands in for a narrower class).
    No classes imports every activity in the area, which then needs a
    county or a locality (the whole country would be millions of rows).
    Either way every class a kept company is authorised for is saved as
    its activities, so it can be searched by any of them later
    (``harvey search``). ``with_administrators`` False leaves the
    representatives file unread.
    """
    from harvey.signals import sync_vocabulary

    classes = list(classes or [])
    report = ImportReport(dataset=dataset.name, dry_run=dry_run)
    report.classes = class_labels(classes)
    names = counties(county)
    if not classes and not names and not _localities(locality):
        raise ValueError("every activity needs an area: give a county or a locality "
                         "(or words or CAEN codes to search the whole country)")

    await sync_vocabulary(state)
    confirmed = await state.confirmed_signal_codes()
    people_codes = {"CONTACT_FOUND", "DECISION_MAKER_TITLE"} & confirmed \
        if with_administrators else set()

    # Streamed in the order that keeps the working set smallest. With an
    # activity filter: the wanted classes nationwide, their status, the
    # firms in the area. Without one: the area's registrations (numbers
    # only), their status, the firms kept. Then every class of the kept
    # firms, and their representatives.
    if classes:
        authorised = matching_registrations(files["caen"], set(classes))
        report.authorised = len(authorised)
        candidates = set(authorised)
    else:
        candidates = registrations_in_area(files["firms"], names, locality)
        report.authorised = len(candidates)
    keep = active_registrations(files["status"], candidates) if active_only \
        else candidates
    report.active = len(keep)
    firms = matching_firms(files["firms"], keep, names, locality)
    report.in_area = len(firms)
    kept_regs = {f.reg for f in firms}
    all_codes = authorised_codes(files["caen"], kept_regs)
    if classes:
        # A class asked for that the full read skipped (no version) still
        # counts, as it did when it was matched.
        for reg, hits in authorised.items():
            if reg in kept_regs:
                have = all_codes.setdefault(reg, [])
                have += [pair for pair in hits if pair not in have]
    people = administrators(files["people"], kept_regs) \
        if people_codes and "people" in files else {}

    return await save_firms(
        state, firms, dataset, report, codes=all_codes, people=people,
        classes=classes, with_administrators=with_administrators,
        dry_run=dry_run, do_not_contact=do_not_contact, progress=progress,
        run_params={
            "dataset": dataset.name, "county": ", ".join(names or []) or "Romania",
            "locality": locality,
            "classes": report.classes or ["all activities"], "active_only": active_only,
            "administrators": with_administrators})


def parse_classes(words: str | list[str] = "", codes: str = "") -> list[tuple[str, int | None]]:
    """What to import, as (code, version) pairs: explicit codes first (in
    both current revisions), then what the words mean via harvey.caen.

    Each activity is resolved on its own: a list item, or a comma- or
    semicolon-separated part of a string. Read as one phrase, "dentist
    clinica" would let the dental word swallow the generic "clinica"."""
    from harvey.caen import classes_for

    out: list[tuple[str, int | None]] = []
    for raw in re.split(r"[,\s;]+", codes or ""):
        digits = re.sub(r"\D", "", raw)
        if digits and len(digits) <= 4 and (digits.zfill(4), None) not in out:
            out.append((digits.zfill(4), None))
    items = [words] if isinstance(words, str) else list(words or [])
    for item in items:
        for phrase in re.split(r"[,;]", item or ""):
            if phrase.strip():
                out += [pair for pair in classes_for(phrase) if pair not in out]
    return out
