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
streamed once, in the order that keeps the working set smallest. Activity
first (the classes you asked for, nationwide), then status (only for
those), then firms (only those, in your county), then representatives
(only for the firms kept).

What is kept is deliberately narrow. The representatives file also carries
birth dates and places and home localities; only a natural person's name
and role are read. A CUI that is really a personal numeric code (13
digits, as some sole traders' rows have) is dropped. Sole traders and
family businesses get no street address, since theirs is usually a home.
Once an import has read it, the downloaded representatives file is deleted
(``harvey import-onrc --keep-download`` keeps it), and so is any older
dataset: only the current one stays on disk.

Every fact lands as an observation with the dataset it came from, under
the same confirmation gate as discovery: companies are saved because you
asked for them by name of activity and place, but ACTIVITY_CODE,
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

    domain = normalize_domain((raw or "").strip())
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
        by_name = {(r.get("name") or "").upper(): r for r in package.get("resources") or []}
        files = {}
        for role, filename in FILES.items():
            res = by_name.get(filename)
            if not res or not _trusted_url(res.get("url", "")):
                break
            size = res.get("size")
            files[role] = (res["url"], int(size) if isinstance(size, int) or
                           (isinstance(size, str) and size.isdigit()) else None)
        else:
            return Dataset(package["name"], _dataset_date(package["name"]).isoformat(), files)
    raise RuntimeError("no complete ONRC 'firme' dataset found on data.gov.ro")


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
    """Delete what an import no longer needs; returns what was deleted.

    The representatives file holds the birth date and place and the home
    locality of every company representative in Romania, of which an
    import keeps only names and roles: unless ``keep_people``, it goes as
    soon as an import has read it (the next import fetches it again).
    Older datasets go too, so only the current one stays cached. Only the
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


def matching_firms(path: Path, regs, county: list[str] | str | None = None,
                   locality: list[str] | str = "") -> list[Firm]:
    """The firms among these registrations whose registered office is in one
    of the counties (all of Romania when none) and, when given, one of the
    localities."""
    wanted = [county] if isinstance(county, str) else list(county or [])
    want_counties = {_fold(c) for c in wanted if c}
    places = _localities(locality) if isinstance(locality, str) else list(locality)
    out = []
    for row in read_rows(path, _FIRM_COLUMNS):
        rec = dict(zip(_FIRM_COLUMNS, row))
        reg = rec["COD_INMATRICULARE"].strip()
        if reg not in regs:
            continue
        if want_counties and _fold(rec["ADR_JUDET"]) not in want_counties:
            continue
        if places and not any(_locality_matches(p, rec["ADR_LOCALITATE"]) for p in places):
            continue
        form = rec["FORMA_JURIDICA"].strip().upper()
        website, domain = _website(rec["WEB"])
        out.append(Firm(
            reg=reg,
            name=_comma_below(" ".join(rec["DENUMIRE"].split())),
            cui=valid_cui(rec["CUI"]),
            form=form,
            county=_comma_below(rec["ADR_JUDET"].strip()),
            locality=clean_locality(rec["ADR_LOCALITATE"]),
            address="" if form in PERSONAL_FORMS else _address(rec),
            website=website, domain=domain,
            registered=rec["DATA_INMATRICULARE"].strip(),
        ))
    return out


def administrators(path: Path, regs) -> dict[str, list[tuple[str, str, str]]]:
    """Registration number -> [(first, last, role)] for the people who run
    the company. Only the name and role columns are read; companies acting
    as representatives, insolvency practitioners and any name holding
    digits (an id where a name should be) are left out."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for reg, name, role in read_rows(path, ("COD_INMATRICULARE",
                                            "PERSOANA_IMPUTERNICITA", "CALITATE")):
        reg = reg.strip()
        if reg not in regs:
            continue
        title = _role(role)
        if not title or re.search(r"\d", name) or _LEGAL_ENTITY.search(_fold(name)):
            continue
        first, last = split_person(name)
        if not first:
            continue
        people = out.setdefault(reg, [])
        if all((first, last) != (f, l) for f, l, _ in people):
            people.append((first, last, title))
    return out


# ── The import ──

@dataclass
class ImportReport:
    dataset: str = ""
    classes: list[str] = field(default_factory=list)
    authorised: int = 0          # registrations authorised for a wanted class
    active: int = 0              # ... of which operating
    in_area: int = 0             # ... of which in the county/locality
    catch_all_only: int = 0      # ... matched only on Rev.2's catch-all 8690
    shared_domain: int = 0       # website domain already held by another business
    excluded: int = 0            # on the do-not-contact list
    new_companies: int = 0
    known_companies: int = 0
    updated_companies: int = 0
    administrators: int = 0
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


def _location(firm: Firm) -> str:
    county = firm.county
    parts = [firm.locality] if firm.locality else []
    if county and not _fold(firm.locality).startswith(_fold(county)):
        parts.append(county)
    return ", ".join(parts + ["Romania"])


async def import_registry(
    state,
    files: dict[str, Path],
    dataset: Dataset,
    *,
    county: str = "",
    classes: list[tuple[str, int | None]],
    locality: str = "",
    active_only: bool = True,
    dry_run: bool = False,
    do_not_contact: list[str] | None = None,
) -> ImportReport:
    """Read the registry files and save what matches.

    ``county`` and ``locality`` take comma-separated lists; no county is the
    whole country. ``classes`` are (code, version) pairs, version None for "Rev.2 or
    Rev.3"; their order picks which class names the company's industry
    (Rev.2's catch-all 8690 last, when it stands in for a narrower class).
    Companies are matched to what is on file by CUI, then by registration
    number, never by website domain alone (a domain another business holds
    is counted in ``shared_domain`` and left off); a known one has its gaps filled, never
    its existing values replaced, except the registered-office address,
    which is always refreshed: the registry is the authority on it, and a
    re-import is how a moved office reaches the record. ``dry_run`` reads everything and writes
    nothing. Observations are written once per dataset: re-importing the
    same dataset adds nothing, a newer one adds its readings beside the old.
    """
    from harvey.caen import describe
    from harvey.gate import do_not_contact_domain_match
    from harvey.signals import seed_signal_catalog

    report = ImportReport(dataset=dataset.name, dry_run=dry_run)
    report.classes = class_labels(classes)
    names = counties(county)
    if not classes:
        raise ValueError("no activity: give words or CAEN codes")

    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes()
    people_codes = {"CONTACT_FOUND", "DECISION_MAKER_TITLE"} & confirmed
    report.unconfirmed = [c for c in ("ACTIVITY_CODE", "CONTACT_FOUND", "DECISION_MAKER_TITLE")
                          if c not in confirmed]

    authorised = matching_registrations(files["caen"], set(classes))
    report.authorised = len(authorised)
    keep = active_registrations(files["status"], authorised.keys()) if active_only \
        else set(authorised)
    report.active = len(keep)
    firms = matching_firms(files["firms"], keep, names, locality)
    report.in_area = len(firms)
    people = administrators(files["people"], {f.reg for f in firms}) \
        if people_codes and "people" in files else {}

    if not dry_run:
        report.run_id = await state.start_run("import", provider="onrc", params={
            "dataset": dataset.name, "county": ", ".join(names or []) or "Romania",
            "locality": locality,
            "classes": report.classes, "active_only": active_only})

    order = {pair: i for i, pair in enumerate(classes)}
    vague = CATCH_ALL if _catch_all_is_vague(classes) else None

    def rank(pair):
        return (pair == vague,
                min(order.get(pair, len(order)), order.get((pair[0], None), len(order))))

    evidence = dataset.page
    try:
        for firm in firms:
            if firm.domain and do_not_contact_domain_match(firm.domain, do_not_contact or []):
                report.excluded += 1
                continue
            matched = sorted(authorised[firm.reg], key=rank)
            code, version = matched[0]
            if (code, version) == vague:
                report.catch_all_only += 1
            industry = describe(code, version) or ""
            external = f"onrc:{firm.reg}"
            # Only the legal identity merges. The registry's website field is
            # self-declared: a domain another business already holds may be
            # a shared group site or simply wrong, so it is a possible match
            # for review, never a reason to hang these administrators and
            # this address on that business.
            existing = (await state.get_company_by_cui(firm.cui)
                        or await state.get_company_by_external_id(external))
            domain, website = firm.domain, firm.website
            holder = await state.get_company_by_domain(domain) if domain else None
            if holder and (existing is None or holder.id != existing.id):
                report.shared_domain += 1
                domain = ""
            if existing:
                report.known_companies += 1
            else:
                report.new_companies += 1
            admins = people.get(firm.reg, [])
            report.administrators += len(admins)
            if dry_run:
                continue

            if existing:
                company_id = existing.id
                changed = await state.fill_company_fields(
                    company_id, cui=firm.cui, name=firm.name, website=website,
                    domain=domain, industry=industry, location=_location(firm),
                    address=firm.address)
                if changed:
                    report.updated_companies += 1
            else:
                company_id = await state.add_company(Company(
                    name=firm.name, cui=firm.cui, external_id=external,
                    website=website, domain=domain, industry=industry,
                    location=_location(firm), address=firm.address,
                    source=SOURCE, source_url=evidence,
                ))

            rows: dict[tuple[str, str], float] = {}     # (signal, value) -> confidence
            if "ACTIVITY_CODE" in confirmed:
                for pair in matched:
                    rows[("ACTIVITY_CODE", activity_value(*pair))] = \
                        CATCH_ALL_CONFIDENCE if pair == vague else 1.0
            for first, last, title in admins:
                text = f"{first} {last} — {title}"
                if "CONTACT_FOUND" in confirmed:
                    rows.setdefault(("CONTACT_FOUND", text), 1.0)
                if "DECISION_MAKER_TITLE" in confirmed and \
                        _fold(title).startswith(_DECISION_MAKERS):
                    rows.setdefault(("DECISION_MAKER_TITLE", text), 1.0)
            if rows:
                seen = {(o["signal_code"], o["value_text"])
                        for o in await state.get_observations(company_id=company_id, limit=2000)
                        if o.get("evidence_url") == evidence}
                fresh = [{"company_id": company_id, "signal_code": code_,
                          "collector": COLLECTOR, "value_text": text,
                          "confidence": confidence, "evidence_url": evidence,
                          "run_id": report.run_id}
                         for (code_, text), confidence in rows.items()
                         if (code_, text) not in seen]
                if fresh:
                    report.observations += await state.add_observations(
                        fresh, run_id=report.run_id)
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
