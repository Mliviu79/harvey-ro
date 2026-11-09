"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import logging
import math
import os
import re
from datetime import date as _date, time as _time
from pathlib import Path

import yaml

from harvey.textio import read_user_text
from dotenv import load_dotenv
from pydantic import (
    BaseModel, ConfigDict, PrivateAttr, TypeAdapter, ValidationError, field_validator,
    model_validator,
)

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.config")


class ConfigError(Exception):
    """Raised when Harvey's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str = ""  # the sender's own profile; optional, nothing reads it
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}
    # Extra searches per industry for rank discovery, e.g.
    #   "roofing contractor": ["roofer", "roof repair"]
    # Each variant runs in every city alongside the industry itself, so the
    # estimate grows with it. Empty → each industry is its own one search.
    keyword_variants: dict[str, list[str]] = {}
    # Language code the DataForSEO SERP and Maps providers search in, e.g.
    # "ro" for Romanian results. Every other provider (DataForSEO Listings,
    # Serper, Scavio, TinyFish, OpenStreetMap) ignores it.
    search_language: str = "en"


class WarmupConfig(BaseModel):
    """Cold-email ramp for a new domain or mailbox.

    Harvey doesn't run a warmup network itself; that needs a pool of real
    inboxes (TrulyInbox, Lemwarm, Mailreach...). This is the other half: no
    cold email goes out for ``min_days`` after ``start_date``, then the cold
    cap climbs from ``start_sends`` by ``daily_increase`` a day until it
    reaches ``max_daily_sends``. Replies are never held. Empty
    ``start_date`` means no ramp.
    """

    start_date: str = ""      # YYYY-MM-DD the warmup service was switched on
    min_days: int = 14        # warmup-only days before any cold email
    start_sends: int = 5      # cold-email cap on the first sending day
    daily_increase: int = 3   # added to the cap each day after that
    dkim_selector: str = ""   # for `harvey warmup`, if not a common one

    @field_validator("start_date", "dkim_selector", mode="before")
    @classmethod
    def _blank_is_empty(cls, v, info):
        if v is None:
            return ""
        if info.field_name != "start_date":
            return v
        # YAML reads an unquoted 2026-10-01 as a date (with a time, a datetime).
        if isinstance(v, _date):
            return (v.date() if hasattr(v, "date") and callable(v.date) else v).isoformat()
        if isinstance(v, str) and len(v.strip()) > 10 and v.strip()[10] in " T":
            return v.strip()[:10]  # "2026-10-01 09:00": the day is what counts
        return "" if v is None else v

    @field_validator("min_days", "start_sends", "daily_increase", mode="before")
    @classmethod
    def _blank_is_default(cls, v, info):
        return cls.model_fields[info.field_name].default if v is None else v

    @field_validator("start_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        v = (v or "").strip()
        if v:
            try:
                _date.fromisoformat(v)
            except ValueError:
                raise ValueError(f"warmup.start_date '{v}' must be YYYY-MM-DD.")
        return v

    @field_validator("min_days", "start_sends", "daily_increase")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("warmup values must be >= 0")
        return v

    def days_in(self, today: _date | None = None) -> int | None:
        """Days since warmup started, or None when no ramp is configured."""
        if not self.start_date:
            return None
        return ((today or _date.today()) - _date.fromisoformat(self.start_date)).days

    def daily_cap(self, ceiling: int, today: _date | None = None) -> int:
        """Today's cold-email cap under the ramp (never above ``ceiling``)."""
        days = self.days_in(today)
        if days is None:
            return ceiling
        if days < self.min_days:
            return 0
        return min(ceiling, self.start_sends + (days - self.min_days) * self.daily_increase)


class SendWindowConfig(BaseModel):
    """When cold email may go out, in each recipient's local time.

    Off unless ``channels.email.send_window`` is set. A cold email goes out
    only between ``start`` and ``end`` (on weekdays, with ``weekdays_only``)
    in the recipient's own time zone, worked out from what Harvey knows
    about them (see harvey/send_window.py). Recipients whose zone can't be
    told get the window in ``usage.quiet_hours.timezone``; so does everyone
    with ``recipient_timezone: false``. Follow-ups keep their delay_days and
    move into the window. Replies are never held.
    """

    start: str = "08:00"
    end: str = "17:00"
    weekdays_only: bool = True     # Monday to Friday, in every country
    recipient_timezone: bool = True

    @field_validator("start", "end", mode="before")
    @classmethod
    def _valid_clock(cls, v, info):
        if v is None:
            return cls.model_fields[info.field_name].default
        # YAML 1.1 reads an unquoted 17:00 as the number 1020 (base 60).
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 24 * 60:
            v = f"{v // 60:02d}:{v % 60:02d}"
        v = str(v).strip()
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"send_window time '{v}' is not valid. Use 24h HH:MM, e.g. '08:00'."
            )
        return v

    @model_validator(mode="after")
    def _start_before_end(self):
        if _time.fromisoformat(self.start) >= _time.fromisoformat(self.end):
            raise ValueError(
                "send_window.start must be earlier than send_window.end "
                "(a send window can't run past midnight)."
            )
        return self


def send_window_of(config) -> "SendWindowConfig | None":
    """The configured send window, or None when there isn't one."""
    email = getattr(getattr(config, "channels", None), "email", None)
    window = getattr(email, "send_window", None)
    return window if isinstance(window, SendWindowConfig) else None


def operator_timezone(config) -> str:
    """usage.quiet_hours.timezone: the zone Harvey itself runs on."""
    quiet = getattr(getattr(config, "usage", None), "quiet_hours", None)
    return getattr(quiet, "timezone", "") or "UTC"


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "gmail" (Gmail/Workspace via API — recommended), "microsoft"
    # (Microsoft 365 / Outlook via Graph), or "smtp" (any SMTP+IMAP mailbox:
    # AgentMail, Fastmail, ...). Anything else (Instantly, which Harvey no
    # longer supports) sends and reads nothing: provider_notice says why.
    provider: str = "gmail"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Every email waits in the Outbox for approval. Set false explicitly for
    # autopilot once you trust the output.
    require_approval: bool = True
    # Your own domains besides the sending address's and the booking link's
    # (e.g. your main site when you send from a secondary domain). On
    # autopilot, an email linking anywhere else waits in the Outbox.
    link_domains: list[str] = []
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05
    # New domain? Ramp cold email up slowly (see WarmupConfig).
    warmup: WarmupConfig = WarmupConfig()
    # Send cold email only in office hours, in each recipient's own time
    # zone (see SendWindowConfig). None: no window, as before.
    send_window: SendWindowConfig | None = None

    @field_validator("warmup", mode="before")
    @classmethod
    def _blank_warmup(cls, v):
        return WarmupConfig() if v is None else v

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v

    def cold_daily_cap(self, today: _date | None = None) -> int:
        """Today's cap on cold (sequence) email: max_daily_sends, throttled
        by the warmup ramp. Replies count only toward max_daily_sends."""
        return self.warmup.daily_cap(self.max_daily_sends, today)


def cold_daily_cap(email_config, today: _date | None = None) -> int:
    """cold_daily_cap for any email config, including ones without a ramp."""
    warmup = getattr(email_config, "warmup", None)
    if isinstance(warmup, WarmupConfig):
        return warmup.daily_cap(email_config.max_daily_sends, today)
    return email_config.max_daily_sends


def provider_notice(config) -> str:
    """Why no email can go out or be read with the configured provider, or
    "" when it is one Harvey supports. Instantly was removed: a config still
    naming it keeps everything outside email working, and says what to pick."""
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS

    email = getattr(getattr(config, "channels", None), "email", None)
    if not getattr(email, "enabled", False):
        return ""
    provider = (getattr(email, "provider", "") or "").strip().lower()
    if provider in NATIVE_PROVIDERS:
        return ""
    if provider == "instantly":
        return (
            "Instantly nu mai e suportat: alege gmail, smtp sau microsoft la "
            "channels.email.provider (Setări → Trimitere, sau harvey.local.yaml). Până "
            "atunci nu se trimite și nu se citește niciun email; restul lui Harvey merge "
            "mai departe. Campaniile pornite deja în Instantly continuă acolo până le "
            "oprești în Instantly. / Instantly is no longer supported: set "
            "channels.email.provider to gmail, smtp or microsoft (Settings → Sending, or "
            "harvey.local.yaml). Until then no email is sent or read; the rest of Harvey "
            "keeps working. Campaigns already running in Instantly go on there until you "
            "stop them in Instantly."
        )
    return (
        f"Furnizorul de email „{provider}” nu e cunoscut: alege gmail, smtp sau microsoft "
        "la channels.email.provider. Până atunci nu se trimite și nu se citește niciun "
        f"email. / The email provider '{provider}' isn't one Harvey knows: set "
        "channels.email.provider to gmail, smtp or microsoft. Until then no email is "
        "sent or read."
    )


def sending_notice(config) -> str:
    """A plain-language warning when outbound sending is held by config, or ""."""
    return provider_notice(config)


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()


PRECLEARED_ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")


def normalize_prospect_country(v) -> str:
    """A two-letter country code ("us" -> "US", "UK" -> "GB"), or "" for none.

    Raises ValueError on anything else, including the booleans YAML makes of
    an unquoted NO (Norway) or ON, so a typo is never silently ignored.
    """
    if isinstance(v, bool):
        raise ValueError(
            f"prospect_country was read as {v}. YAML turns an unquoted NO or "
            'ON into true/false, so quote the code, e.g. prospect_country: "NO".'
        )
    v = str(v or "").strip().upper()
    v = {"UK": "GB", "USA": "US"}.get(v, v)
    if v and not (len(v) == 2 and v.isascii() and v.isalpha()):
        raise ValueError(
            f"'{v}' is not a valid prospect_country. Use a two-letter "
            "country code such as US or GB, or leave it empty."
        )
    return v


class PreclearedConfig(BaseModel):
    """Optional per-recipient cold-email check through Precleared.

    Off by default. When on, each new recipient is checked once, before the
    first cold email, against UK PECR, US CAN-SPAM, Canada's CASL, Germany's
    UWG and France's rules. Any verdict other than ok_to_email holds the
    email back with the reason. See harvey/integrations/precleared.py.
    """

    enabled: bool = False
    # Where your business is based: GB or US. Required when enabled.
    sender_country: str = ""
    # Where prospect addresses come from, in Precleared's terms. Harvey
    # builds most addresses from a learned pattern, so the honest default
    # is "unknown"; use "published" only if yours are copied from the
    # businesses' own websites.
    address_source: str = "unknown"
    # Optional: the country your prospects are in (two letters, e.g. "US"),
    # if they are all in one. Precleared uses it only when it can't work out
    # the recipient's country from the address, company records or the
    # company's website. Leave empty if your prospects are spread out.
    prospect_country: str = ""

    @field_validator("sender_country", mode="before")
    @classmethod
    def _upper_country(cls, v) -> str:
        return str(v or "").strip().upper()

    @field_validator("address_source", mode="before")
    @classmethod
    def _valid_source(cls, v) -> str:
        v = str(v or "unknown").strip().lower()
        if v not in PRECLEARED_ADDRESS_SOURCES:
            raise ValueError(
                f"'{v}' is not a valid address_source. "
                f"Use one of: {', '.join(PRECLEARED_ADDRESS_SOURCES)}."
            )
        return v

    @field_validator("prospect_country", mode="before")
    @classmethod
    def _valid_prospect_country(cls, v) -> str:
        return normalize_prospect_country(v)

    @model_validator(mode="after")
    def _country_when_enabled(self):
        if self.enabled and self.sender_country not in ("GB", "US"):
            raise ValueError(
                "compliance.precleared.sender_country must be GB or US "
                "when the check is enabled."
            )
        return self


# The opt-out line under every cold email: Romanian by default (Harvey RO
# writes to Romanian firms); the English one Harvey used before is still
# recognised as its own text in a quoted reply.
DEFAULT_OPT_OUT_LINE = 'Nu vă interesează? Răspundeți \u201estop" și nu vă mai scriu.'
ENGLISH_OPT_OUT_LINE = 'Not interested? Reply "no thanks" and I won\'t email again.'
# Under the first SMS or WhatsApp answer to a number (an answer only:
# Harvey never writes first there). Romanian by default; the English one
# for an install whose email opt-out line is the English one.
DEFAULT_MESSAGE_OPT_OUT_LINE = "Răspunde STOP ca să nu mai primești mesaje."
ENGLISH_MESSAGE_OPT_OUT_LINE = "Reply STOP to stop these messages."
# Under an answer autopilot sent with no one reading it first.
DEFAULT_AI_DISCLOSURE_LINE = "Acest răspuns a fost scris cu ajutorul unui asistent AI."
# The replies the Handler always honours as an opt-out on their own; an
# opt_out_line must ask for one of them ("Reply STOP", 'reply "no thanks"',
# 'Răspundeți „stop"').
OPT_OUT_WORDS = ("no thanks", "no thank you", "unsubscribe", "stop", "remove me",
                 "opt out", "dezabonare", "nu mai doresc", "nu, mulțumesc", "nu multumesc")
_ASKS_FOR_OPT_OUT_RE = re.compile(
    r"\b(?:reply|r[ăa]spunde(?:[țţt]i)?)(?:\s+(?:with|cu))?\s*"
    r"[\"'\u201c\u2018\u201e\u00ab]?\s*(?:"
    + "|".join(re.escape(w) for w in OPT_OUT_WORDS) + r")\b", re.IGNORECASE)


class ComplianceConfig(BaseModel):
    precleared: PreclearedConfig = PreclearedConfig()
    # Added under every cold (sequence) email at send time; never to replies.
    # CAN-SPAM requires a postal address you receive mail at (a street
    # address, PO box or virtual mailbox) and a working way to opt out.
    postal_address: str = ""
    # Must ask for wording Harvey recognises as an opt-out (OPT_OUT_WORDS).
    # "" leaves it off (and harvey warns).
    opt_out_line: str = DEFAULT_OPT_OUT_LINE
    # Companies this operator will never email: competitors, existing
    # customers, anyone under a legal hold, a partner you agreed not to
    # approach. An entry is an address ("jane@acme.example") or a domain
    # ("acme.example", which also covers its subdomains). Enforced by the gate at
    # send time, so it holds for an email a human already approved, and for
    # replies as well as cold mail — "never contact" means never.
    do_not_contact: list[str] = []
    # Your privacy page, added after the source line of a first email to a
    # personal address only (GDPR art. 14 asks for more than the source: who
    # you are, the purpose, the legal basis, the right to object). It is a
    # link, so it counts against the gate's one link. "" leaves it off.
    privacy_url: str = ""
    # The line under an answer that autopilot sent by itself (never under
    # cold email, or an answer you approved). "" leaves it off.
    ai_disclosure_line: str = DEFAULT_AI_DISCLOSURE_LINE
    # Under the first SMS or WhatsApp answer to a number (with the firm's
    # name before it). "" is the default for the install's language
    # (message_opt_out_line()); it can't be left off.
    message_opt_out_line: str = ""

    @field_validator("postal_address", "opt_out_line", "privacy_url", "ai_disclosure_line",
                     "message_opt_out_line", mode="before")
    @classmethod
    def _blank_is_empty(cls, v):
        return "" if v is None else v

    @field_validator("privacy_url")
    @classmethod
    def _privacy_link(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"https?://[^\s<>\"']+\.[^\s<>\"']+", v):
            raise ValueError("privacy_url must be a web address (https://...), or empty")
        return v

    @field_validator("ai_disclosure_line")
    @classmethod
    def _plain_disclosure(cls, v: str) -> str:
        v = " ".join(v.split())
        if any(bit in v.lower() for bit in ("http", "www.", "@")):
            raise ValueError("ai_disclosure_line can't contain a link or an email address")
        return v[:300]

    @field_validator("do_not_contact", mode="before")
    @classmethod
    def _clean_do_not_contact(cls, v):
        """One string is a list; blanks and duplicates are dropped.

        Normalising here rather than at the point of use keeps the gate's
        matching free of parsing, and means " @Acme.EXAMPLE " or a pasted
        "https://www.acme.example/" behaves the same as "acme.example". An
        entry that names no address or domain is an error, not a no-op.
        """
        from harvey.gate import do_not_contact_entries
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            raise ValueError("must be a list of email addresses and domains")
        seen: list[str] = []
        for item in v:
            if item is None:
                continue
            if not isinstance(item, str):
                raise ValueError(f"{item!r} is not an email address or a domain")
            for cleaned in do_not_contact_entries(item):
                if cleaned not in seen:
                    seen.append(cleaned)
        return seen

    @field_validator("message_opt_out_line")
    @classmethod
    def _recognisable_message_opt_out(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            return v
        if len(v) > 120:
            raise ValueError("message_opt_out_line is at most 120 characters")
        if any(bit in v.lower() for bit in ("http", "www.", "@", "{", "}")):
            raise ValueError("message_opt_out_line can't contain a link, an email address or "
                             "curly braces")
        if not _ASKS_FOR_OPT_OUT_RE.search(v):
            raise ValueError('message_opt_out_line must ask for a reply Harvey recognises as an '
                             'opt-out, e.g. "Reply STOP" / "Răspunde STOP"')
        return v

    @field_validator("opt_out_line")
    @classmethod
    def _recognisable_opt_out(cls, v: str) -> str:
        v = v.strip()
        lowered = v.lower()
        if not v:
            return v
        if any(bit in lowered for bit in ("http", "www.", "click", "@")):
            raise ValueError(
                "opt_out_line can't contain a link or an email address: cold email "
                "allows one link, and opting out must be as simple as replying."
            )
        if not _ASKS_FOR_OPT_OUT_RE.search(v):
            raise ValueError(
                "opt_out_line must ask for a reply Harvey recognises as an opt-out, "
                'e.g. Reply "no thanks" / Reply STOP. Recognised: '
                + ", ".join(f'"{w}"' for w in OPT_OUT_WORDS) + "."
            )
        return v


def message_opt_out_line(config) -> str:
    """The line under a first SMS or WhatsApp answer: the configured one,
    else the default in the install's language (English when its email
    opt-out line is the English one)."""
    compliance = compliance_of(config)
    own = (compliance.message_opt_out_line or "").strip()
    if own:
        return own
    return (ENGLISH_MESSAGE_OPT_OUT_LINE if compliance.opt_out_line.strip() == ENGLISH_OPT_OUT_LINE
            else DEFAULT_MESSAGE_OPT_OUT_LINE)


def compliance_of(config) -> ComplianceConfig:
    """The compliance section, or the defaults when a config has none."""
    found = getattr(config, "compliance", None)
    return found if isinstance(found, ComplianceConfig) else ComplianceConfig()


def compliance_footer_lines(config) -> list[str]:
    """The lines added under every cold email: address, then opt-out line."""
    compliance = compliance_of(config)
    return [line for line in (compliance.postal_address.strip(),
                              compliance.opt_out_line.strip()) if line]


def compliance_notice(config) -> str:
    """A plain-language warning when cold email goes out without a postal
    address, or "". Sending isn't held for it: this is the nudge."""
    email = getattr(getattr(config, "channels", None), "email", None)
    if not getattr(email, "enabled", False):
        return ""
    compliance = compliance_of(config)
    missing = []
    if not compliance.postal_address.strip():
        missing.append("a postal address (compliance.postal_address: a street "
                       "address, PO box or virtual mailbox)")
    if not compliance.opt_out_line.strip():
        missing.append("a way to opt out (compliance.opt_out_line)")
    if not missing:
        return ""
    return (
        "Cold emails are going out without " + " or ".join(missing) + ". US law "
        "(CAN-SPAM) requires both in every cold email. Set them in harvey.local.yaml "
        "(a copy of harvey.yaml, if you don't have one yet)."
    )


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


BRAIN_PROVIDERS = ("claude", "codex")
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# How the provider's CLI is logged in (harvey/brain_auth.py): ``terminal``,
# the login the user made outside Harvey (the CLI's own folder: what every
# install had before, and the default); ``account``, a login the CLI keeps
# in a folder of Harvey's own (data/auth/<provider>); ``api_key``, a key
# billed per token (held by brain.usd_per_day / usd_per_month,
# harvey/brain_spend.py). BRAIN_AUTH_READY are those this release can run.
BRAIN_AUTH_MODES = ("terminal", "account", "api_key")
BRAIN_AUTH_READY = ("terminal", "account", "api_key")

# Model names are passed to a CLI, so they must look like a model name and
# never like a flag ("-x") or anything a shell would treat specially.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")


class ProviderBrainConfig(BaseModel):
    """One provider's own settings (brain.claude, brain.codex): its login
    (``auth``, BRAIN_AUTH_MODES; absent: ``terminal``, the CLI's own login,
    as before), its model, Codex's reasoning effort, and, billed per token,
    its USD caps (``usd_per_day`` / ``usd_per_month``, UTC; None: no cap)
    and Codex's prices (``codex_usd_per_mtok_in`` / ``_out``, USD per
    million tokens: its CLI reports no cost, so with no price its calls
    don't run). With no cap set no API-key call is made at all
    (brain_spend.NO_CAP)."""

    model: str = ""  # empty = the provider's own default
    reasoning_effort: str = ""  # codex only; empty = model default
    auth: str = "terminal"  # BRAIN_AUTH_MODES
    usd_per_day: float | None = None
    usd_per_month: float | None = None
    codex_usd_per_mtok_in: float | None = None
    codex_usd_per_mtok_out: float | None = None

    @field_validator("model", mode="before")
    @classmethod
    def _valid_model(cls, v) -> str:
        v = str(v or "").strip()
        if v and not _MODEL_NAME_RE.match(v):
            raise ValueError(f"'{v}' is not a valid model name.")
        return v

    @field_validator("auth", mode="before")
    @classmethod
    def _valid_auth(cls, v) -> str:
        v = str(v or "terminal").strip().lower().replace("-", "_")
        if v not in BRAIN_AUTH_MODES:
            raise ValueError(f"'{v}' is not a way to log in. "
                             f"Use one of: {', '.join(BRAIN_AUTH_READY)}.")
        if v not in BRAIN_AUTH_READY:
            raise ValueError(f"brain.auth '{v}' isn't available in this version of Harvey. "
                             f"Use one of: {', '.join(BRAIN_AUTH_READY)}.")
        return v

    @field_validator("usd_per_day", "usd_per_month", "codex_usd_per_mtok_in",
                     "codex_usd_per_mtok_out", mode="before")
    @classmethod
    def _valid_usd(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, bool):
            raise ValueError("must be a number of dollars, 0 or more")
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("must be a number of dollars, 0 or more") from None
        if not (math.isfinite(v) and v >= 0):
            raise ValueError("must be a number of dollars, 0 or more")
        return v

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _valid_effort(cls, v) -> str:
        v = str(v or "").strip().lower()
        if v and v not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                f"'{v}' is not a valid reasoning effort. "
                f"Use one of: {', '.join(CODEX_REASONING_EFFORTS)}."
            )
        return v


# A provider's own keys (brain.<provider>.<key>); Codex's effort and token
# prices are Codex's only.
PROVIDER_KEYS = tuple(ProviderBrainConfig.model_fields)
CODEX_ONLY_KEYS = ("reasoning_effort", "codex_usd_per_mtok_in", "codex_usd_per_mtok_out")


def provider_keys(provider: str) -> tuple[str, ...]:
    return PROVIDER_KEYS if provider == "codex" else tuple(
        k for k in PROVIDER_KEYS if k not in CODEX_ONLY_KEYS)


def _flat_brain_keys(data):
    """A ``brain:`` mapping with the flat keys an older Harvey wrote moved
    into their provider's block (they win over the block's own, so a
    caller's ``BrainConfig(provider=p, model=m)`` is that provider's
    model): Codex's effort and prices to Codex, the rest to the default
    provider's. Anything else is returned as it is."""
    if not isinstance(data, dict):
        return data
    flat = {k: data[k] for k in PROVIDER_KEYS if k in data}
    if not flat:
        return data
    data = {k: v for k, v in data.items() if k not in PROVIDER_KEYS}
    target = str(data.get("provider") or "claude").strip().lower()
    for p in BRAIN_PROVIDERS:
        mine = {k: v for k, v in flat.items()
                if (p == "codex" and k in CODEX_ONLY_KEYS)
                or (p == target and k not in CODEX_ONLY_KEYS)}
        if not mine:
            continue
        block = data.get(p)
        if isinstance(block, ProviderBrainConfig):
            block = block.model_dump(exclude_unset=True)
        data[p] = {**(block if isinstance(block, dict) else {}), **mine}
    return data


class BrainConfig(BaseModel):
    """Which subscriptions Harvey thinks with: Claude (Claude Code headless,
    on your Claude plan) and Codex (on your ChatGPT plan), each set up on
    its own (``claude:``, ``codex:``: ProviderBrainConfig), side by side.

    ``provider`` is the default: what runs on its own (the Writer, the
    Handler, the Analyst, training) uses it, and a new chat starts on it; a
    chat can be on the other one (agent_chat.provider). There is never an
    automatic fallback between them, and an explicit ``model`` only affects
    Harvey: your own Claude Code / Codex settings are never read or changed.

    The settings an older Harvey wrote flat (``brain: {provider, model,
    auth, ...}``) load as the default provider's block (Codex's effort and
    prices as Codex's); the first save writes them in this shape
    (set_brain_settings).

    The flat attributes (``model``, ``auth``, ``usd_per_day``, ...) are the
    view every caller reads: those of ``provider``'s block. ``for_provider``
    gives the same view for the other provider.
    """

    provider: str = "claude"
    claude: ProviderBrainConfig = ProviderBrainConfig()
    codex: ProviderBrainConfig = ProviderBrainConfig()

    @model_validator(mode="before")
    @classmethod
    def _flat_keys(cls, data):
        return _flat_brain_keys(data)

    @field_validator("provider", mode="before")
    @classmethod
    def _valid_provider(cls, v) -> str:
        v = str(v or "claude").strip().lower()
        if v not in BRAIN_PROVIDERS:
            raise ValueError(
                f"'{v}' is not a supported brain provider. "
                f"Use one of: {', '.join(BRAIN_PROVIDERS)}."
            )
        return v

    @model_validator(mode="after")
    def _claude_has_no_codex_keys(self):
        set_here = [k for k in CODEX_ONLY_KEYS
                    if k in self.claude.model_fields_set and getattr(self.claude, k) not in ("", None)]
        if set_here:
            raise ValueError(f"brain.claude.{set_here[0]} is Codex's only: set it under brain.codex.")
        return self

    def block(self, provider: str | None = None) -> ProviderBrainConfig:
        """A provider's own settings (``provider``: the default)."""
        return self.codex if (provider or self.provider) == "codex" else self.claude

    def for_provider(self, provider: str | None) -> "BrainConfig":
        """The view of ``provider`` (None or "": the default): the same
        settings, its block's read as the flat attributes."""
        p = str(provider or self.provider).strip().lower()
        if p not in BRAIN_PROVIDERS:
            raise ValueError(f"'{p}' is not a supported brain provider.")
        return self if p == self.provider else self.model_copy(update={"provider": p}, deep=True)

    def model_copy(self, *, update=None, deep: bool = False):
        """A copy; flat keys in ``update`` (``model``, ``auth``, ...) set the
        copy's own provider's block (``provider`` first), as the flat view
        reads them."""
        flat = {k: v for k, v in (update or {}).items() if k in PROVIDER_KEYS}
        if not flat:
            return super().model_copy(update=update, deep=deep)
        rest = {k: v for k, v in update.items() if k not in PROVIDER_KEYS}
        out = super().model_copy(update=rest or None, deep=True)
        for k, v in flat.items():
            setattr(out, k, v)
        return out

    def chose_auth(self, provider: str) -> bool:
        """Whether the settings name a login for ``provider`` (written in
        the file, or set by a caller), rather than the default terminal."""
        return "auth" in self.block(provider).model_fields_set

    # The flat view: the provider's own block (Codex's effort and prices
    # are Codex's). Setting one sets it in that block, after validation.
    def _set(self, provider: str, key: str, value) -> None:
        block = self.block(provider)
        setattr(block, key, ProviderBrainConfig(**{key: value}).model_dump()[key])
        block.model_fields_set.add(key)

    @property
    def model(self) -> str:
        return self.block().model

    @model.setter
    def model(self, value) -> None:
        self._set(self.provider, "model", value)

    @property
    def reasoning_effort(self) -> str:
        return self.block().reasoning_effort if self.provider == "codex" else ""

    @reasoning_effort.setter
    def reasoning_effort(self, value) -> None:
        self._set("codex", "reasoning_effort", value)

    @property
    def auth(self) -> str:
        return self.block().auth

    @auth.setter
    def auth(self, value) -> None:
        self._set(self.provider, "auth", value)

    @property
    def usd_per_day(self) -> float | None:
        return self.block().usd_per_day

    @usd_per_day.setter
    def usd_per_day(self, value) -> None:
        self._set(self.provider, "usd_per_day", value)

    @property
    def usd_per_month(self) -> float | None:
        return self.block().usd_per_month

    @usd_per_month.setter
    def usd_per_month(self, value) -> None:
        self._set(self.provider, "usd_per_month", value)

    @property
    def codex_usd_per_mtok_in(self) -> float | None:
        return self.codex.codex_usd_per_mtok_in

    @codex_usd_per_mtok_in.setter
    def codex_usd_per_mtok_in(self, value) -> None:
        self._set("codex", "codex_usd_per_mtok_in", value)

    @property
    def codex_usd_per_mtok_out(self) -> float | None:
        return self.codex.codex_usd_per_mtok_out

    @codex_usd_per_mtok_out.setter
    def codex_usd_per_mtok_out(self, value) -> None:
        self._set("codex", "codex_usd_per_mtok_out", value)

    def flat(self) -> dict:
        """The view as the flat keys (status lines, the action log)."""
        return {k: getattr(self, k) for k in ("provider", *PROVIDER_KEYS)}


def with_brain_provider(config, provider: str | None):
    """``config`` (a HarveyConfig, or anything with a ``brain``) whose
    brain is ``provider``'s view (BrainConfig.for_provider): what a chat,
    its wakes and tasks run with. The same config when it already is."""
    brain = getattr(config, "brain", None)
    if config is None or brain is None:
        import types
        return types.SimpleNamespace(brain=BrainConfig().for_provider(provider))
    if not hasattr(brain, "for_provider"):
        if not provider or getattr(brain, "provider", "claude") == provider:
            return config
        brain = BrainConfig(provider=getattr(brain, "provider", "claude"))
    view = brain.for_provider(provider)
    if view is brain:
        return config
    if hasattr(config, "model_copy"):
        return config.model_copy(update={"brain": view})
    import copy
    out = copy.copy(config)
    out.brain = view
    return out


class WriterConfig(BaseModel):
    # Run every draft through one extra editing-only brain call using the
    # Humanizer skill's instructions (~/.claude/skills/humanizer/SKILL.md,
    # or HARVEY_HUMANIZER_SKILL). Off by default: installing the skill for
    # your own Claude Code use never changes what Harvey sends on its own.
    humanizer: bool = False


class SpendConfig(BaseModel):
    """Limits on what paid searches spend across every run, above each
    run's own cap: per UTC day and per UTC month, in dollars. None (the
    default) is no limit beyond the runs' caps."""
    per_day_usd: float | None = None
    per_month_usd: float | None = None

    @field_validator("per_day_usd", "per_month_usd")
    @classmethod
    def _valid_limit(cls, v: float | None) -> float | None:
        if v is not None and not (math.isfinite(v) and v >= 0):
            raise ValueError("a spend limit must be a number of dollars, 0 or more")
        return v


class HarveyConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    brain: BrainConfig = BrainConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    writer: WriterConfig = WriterConfig()
    spend: SpendConfig = SpendConfig()
    # The file it was loaded from, so a running Harvey can re-read the
    # sending mode from that same file (refresh_sending_mode).
    _source_path: str | None = PrivateAttr(default=None)


class EnvConfig(BaseModel):
    # A module's own keys beyond the ones below (its manifest's ``secrets``)
    # come in as extra fields, lower case: a new module needs no edit here.
    model_config = ConfigDict(extra="allow")

    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    hunter_api_key: str = ""
    serper_api_key: str = ""
    scavio_api_key: str = ""
    tinyfish_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Microsoft 365 (channels.email.provider: microsoft)
    m365_client_id: str = ""
    m365_tenant_id: str = ""
    # Optional: only used when compliance.precleared.enabled is true.
    precleared_api_key: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        data = yaml.safe_load(read_user_text(config_path))
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Run 'harvey setup' to make one."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'harvey setup' to configure Harvey.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        config = HarveyConfig(**data)
        config._source_path = str(config_path)
        return config
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'harvey setup'."
            + _partial_local_hint(config_path, e)
        )
        raise


def _partial_local_hint(config_path, error: ValidationError) -> str:
    """harvey.local.yaml is read instead of harvey.yaml, not on top of it:
    one written by hand with only the keys someone meant to change is
    missing everything else. Say so, rather than leave a list of fields."""
    path = Path(config_path)
    if path.name != LOCAL_CONFIG or not path.with_name(TEMPLATE_CONFIG).exists():
        return ""
    if not any(err.get("type") == "missing" for err in error.errors()):
        return ""
    return (f"\n{LOCAL_CONFIG} is read instead of {TEMPLATE_CONFIG}, not on top of it, so it "
            f"needs every setting. Copy your settings from {TEMPLATE_CONFIG} into it "
            f"(or move {LOCAL_CONFIG} away and change any setting from the dashboard or "
            f"'harvey setup': Harvey then makes it as a full copy).")


def load_brain_config(config_path: str | None = None) -> BrainConfig:
    """Read just the ``brain:`` section, without validating the rest.

    Training runs before a product is configured (the template harvey.yaml
    still has placeholder values), but it must still think with the
    provider the user chose. A missing file or section means the Claude
    default; an invalid section raises rather than silently switching
    providers.
    """
    if config_path is None:
        try:
            config_path = _find_config_file()
        except ConfigFileNotFoundError:
            return BrainConfig()
    try:
        data = yaml.safe_load(read_user_text(config_path))
    except FileNotFoundError:
        return BrainConfig()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read the brain settings in {config_path}: {e}")
    section = data.get("brain") if isinstance(data, dict) else None
    if section is None:
        return BrainConfig()
    if not isinstance(section, dict):
        raise ConfigError(f"'brain' in {config_path} must be a mapping.")
    try:
        return BrainConfig(**section)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid brain settings in {config_path}:\n{_format_validation_error(e)}"
        )


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        scavio_api_key=os.getenv("SCAVIO_API_KEY", "").strip(),
        tinyfish_api_key=os.getenv("TINYFISH_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        m365_client_id=os.getenv("M365_CLIENT_ID", "").strip(),
        m365_tenant_id=os.getenv("M365_TENANT_ID", "").strip(),
        precleared_api_key=os.getenv("PRECLEARED_API_KEY", "").strip(),
        **_module_keys(),
    )
    return env


def _module_keys() -> dict[str, str]:
    """Every .env key a module's manifest names that has no field above,
    as ``{key in lower case: value}``."""
    try:
        from harvey.modules.switches import manifests
        names = {k for m in manifests() for k in m.secrets}
    except Exception as e:
        logger.debug("module keys unavailable: %s", e)
        return {}
    known = set(EnvConfig.model_fields)
    return {k.lower(): os.getenv(k, "").strip() for k in sorted(names)
            if k.lower() not in known}


# ── Approval or autopilot ─────────────────────────────────────────────────
#
# channels.email.require_approval: true (the default) means every email
# waits in the Outbox for you; false is autopilot. The dashboard can switch
# it, so it is edited in place: one line changes and the comments stay.

_APPROVAL_LINE_RE = re.compile(
    r"^(?P<lead>[ \t]*require_approval[ \t]*:[ \t]*)(?P<val>[^\s#]+)", re.MULTILINE)
_EMAIL_KEY_RE = re.compile(r"^(?P<ind>[ \t]+)email[ \t]*:[ \t]*(?:#.*)?$", re.MULTILINE)


def _email_section(data):
    channels = data.get("channels") if isinstance(data, dict) else None
    email = channels.get("email") if isinstance(channels, dict) else None
    return email if isinstance(email, dict) else None


def _with_approval_line(raw: str, word: str) -> list[str]:
    """Ways to say require_approval: <word> in ``raw``: change the line that
    is there, or add one under an ``email:`` key."""
    out = [raw[:m.start("val")] + word + raw[m.end("val"):] for m in _APPROVAL_LINE_RE.finditer(raw)]
    for m in _EMAIL_KEY_RE.finditer(raw):
        child = m.group("ind") + "  "
        for line in raw[m.end():].split("\n")[1:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            if len(indent) > len(m.group("ind")):
                child = indent
            break
        out.append(raw[:m.end()] + f"\n{child}require_approval: {word}" + raw[m.end():])
    return out


def set_require_approval(value: bool, path=None) -> Path:
    """Write channels.email.require_approval into harvey.local.yaml (made
    from the config Harvey reads when there is none yet, see
    save_config_change). Only that value changes: the result must parse
    to the same settings apart from it, or nothing is written and
    ConfigError says to set it by hand. Returns the file written."""
    import copy
    word = "true" if value else "false"

    def edit(data_bytes: bytes, name: str) -> bytes:
        bom = data_bytes.startswith(b"\xef\xbb\xbf")
        try:
            raw = data_bytes.decode("utf-8-sig").replace("\r\n", "\n")
        except UnicodeDecodeError:
            raise ConfigError(f"{name} isn't UTF-8, so it isn't rewritten. Set "
                              f"channels.email.require_approval there by hand.")
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {name}: {e}")
        if _email_section(data) is None:
            raise ConfigError(f"{name} has no channels.email section. Set "
                              f"channels.email.require_approval there by hand.")
        expected = copy.deepcopy(data)
        _email_section(expected)["require_approval"] = bool(value)
        for new in _with_approval_line(raw, word):
            try:
                if yaml.safe_load(new) != expected:
                    continue
            except yaml.YAMLError:
                continue
            HarveyConfig(**expected)          # still a valid config
            if b"\r\n" in data_bytes:
                new = new.replace("\n", "\r\n")
            return (b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")
        raise ConfigError(f"Couldn't change require_approval in {name} safely. "
                          f"Set channels.email.require_approval: {word} there by hand.")

    written = save_config_change(edit, path)
    _mode_cache.pop(str(written.resolve()), None)
    return written


BRAIN_KEYS = ("provider", *PROVIDER_KEYS)


def brain_section(brain: BrainConfig, keep: dict | None = None) -> dict:
    """The ``brain:`` block to write for ``brain``: the default provider and
    each provider's keys that differ from the defaults, or that ``keep``
    (the section on file, in either shape) names for it, so a value written
    on purpose stays written."""
    keep = _flat_brain_keys(dict(keep)) if isinstance(keep, dict) else {}
    defaults = ProviderBrainConfig().model_dump()
    out: dict = {}
    if brain.provider != "claude" or "provider" in keep:
        out["provider"] = brain.provider
    for p in BRAIN_PROVIDERS:
        mine = keep.get(p) if isinstance(keep.get(p), dict) else {}
        block = brain.block(p).model_dump()
        values = {k: block[k] for k in provider_keys(p) if block[k] != defaults[k] or k in mine}
        if values:
            out[p] = values
    return out


def set_brain_settings(values: dict, path=None, *, provider: str | None = None) -> Path:
    """Write brain settings into harvey.local.yaml (made from the config
    Harvey reads when there is none yet, see save_config_change):
    ``values["provider"]``, the default provider, and with ``provider``
    that provider's own keys (provider_keys(provider)) in its block. Linking,
    unlinking or saving a key for one provider never touches the other's.

    Only those keys change, each line rewritten in place, and the section
    must still be a valid BrainConfig, or nothing is written and ConfigError
    says why. A key absent from the file and set to its default stays absent
    (no ``auth: terminal`` written for an install that never chose). A
    section an older Harvey wrote flat is rewritten whole in the new shape
    (brain_section; the comments inside it go, the rest of the file's stay).
    Returns the file written."""
    import copy
    if provider is not None:
        provider = str(provider).strip().lower()
        if provider not in BRAIN_PROVIDERS:
            raise ConfigError(f"'{provider}' is not a supported brain provider.")
    allowed = {"provider", *(provider_keys(provider) if provider else ())}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"Not a brain setting{' of ' + provider if provider else ''}: "
                          f"{', '.join(unknown)}.")
    own = {k: v for k, v in values.items() if k != "provider"}
    try:
        default = BrainConfig(provider=values["provider"]).provider if "provider" in values \
            else None
        wanted = ProviderBrainConfig(**own).model_dump(include=set(own))
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e))

    def edit(data_bytes: bytes, name: str) -> bytes:
        bom = data_bytes.startswith(b"\xef\xbb\xbf")
        try:
            raw = data_bytes.decode("utf-8-sig").replace("\r\n", "\n")
        except UnicodeDecodeError:
            raise ConfigError(f"{name} isn't UTF-8, so it isn't rewritten. Set brain: there by hand.")
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {name}: {e}")
        if not isinstance(data, dict):
            raise ConfigError(f"{name} isn't a YAML mapping. Set brain: there by hand.")
        section = data.get("brain")
        if section is not None and not isinstance(section, dict):
            raise ConfigError(f"'brain' in {name} must be a mapping.")
        section = section or {}
        try:
            current = BrainConfig(**section)
        except ValidationError as e:
            raise ConfigError(f"Invalid brain settings in {name}:\n{_format_validation_error(e)}")
        changes = []
        if default is not None and current.provider != default:
            changes.append((("brain", "provider"), default))
        if provider:
            have = current.block(provider).model_dump()
            changes += [(("brain", provider, k), v) for k, v in wanted.items() if have[k] != v]
        if not changes:
            return data_bytes
        flat = any(k in section for k in PROVIDER_KEYS)
        new_brain = _flat_brain_keys(copy.deepcopy(section))
        for (_, *keys), v in changes:
            node = new_brain
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = v
        try:
            result = BrainConfig(**new_brain)
        except ValidationError as e:
            raise ConfigError(f"Invalid brain settings:\n{_format_validation_error(e)}")
        expected = copy.deepcopy(data)
        if flat:
            # (Written whole in the new shape: brain_section.)
            expected["brain"] = brain_section(result, keep=new_brain)
            edits = [(("brain",), expected["brain"])]
        else:
            # (A changed key differs from its default where the file never
            # had it, so nothing is written that wasn't chosen.)
            expected["brain"] = new_brain
            edits = changes
        try:
            new = set_yaml_values(raw, edits)
            ok = yaml.safe_load(new) == expected
        except yaml.YAMLError:
            ok = False
        if not ok:
            raise ConfigError(f"Couldn't change brain: in {name} safely. Set it there by hand.")
        if b"\r\n" in data_bytes:
            new = new.replace("\n", "\r\n")
        return (b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")

    return save_config_change(edit, path)


_mode_cache: dict[str, tuple[tuple, object]] = {}


def refresh_sending_mode(config, path=None) -> bool:
    """Bring a running config's require_approval in line with the file it was
    loaded from, so a switch made while Harvey runs counts at once: the
    sender, writer and handler call this before they queue or send. Cheap:
    the file is only read again when it changed. True when the value
    changed. A config not loaded from a file, or a file that can't be read,
    leaves the running value alone."""
    path = path or getattr(config, "_source_path", None)
    if not path:
        return False
    try:
        target = live_config_file(path).resolve()
        st = target.stat()
        # Not the mtime alone: a restore or `cp -p` keeps it while the file changes.
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        cached = _mode_cache.get(str(target))
        if cached and cached[0] == stamp:
            value = cached[1]
        else:
            email = _email_section(yaml.safe_load(read_user_text(target)))
            value = email.get("require_approval", True) if email is not None else None
            try:
                # Read the way load_config reads it: "true", 'no', on...
                value = TypeAdapter(bool).validate_python(value)
            except ValidationError:
                value = None
            _mode_cache[str(target)] = (stamp, value)
    except Exception:
        return False
    if not isinstance(value, bool) or value == config.channels.email.require_approval:
        return False
    config.channels.email.require_approval = value
    return True


_dnc_cache: dict[str, tuple[tuple, list[str] | None]] = {}


def live_do_not_contact(config) -> list[str]:
    """The do-not-contact list as the config file says it is now.

    Someone who sees an approved email to a company they must not contact
    adds it to the list while Harvey runs; waiting for a restart would let
    that email go. Re-read only when the file changed (as refresh_sending_mode
    does). A file that can't be read, or a list with a mistake in it, keeps
    the last good list: never an empty one.
    """
    compliance = compliance_of(config)
    path = getattr(config, "_source_path", None)
    if not path:
        return compliance.do_not_contact
    try:
        target = live_config_file(path).resolve()
        st = target.stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        cached = _dnc_cache.get(str(target))
        if cached and cached[0] == stamp:
            value = cached[1]
        else:
            value = None
            try:
                value = do_not_contact_from_file(target)
            except Exception as e:
                logger.warning(f"compliance.do_not_contact in {target.name} couldn't be read "
                               f"({e}); still using the last good list.")
            _dnc_cache[str(target)] = (stamp, value)
    except OSError:
        return compliance.do_not_contact
    if value is None:
        return compliance.do_not_contact
    if value != compliance.do_not_contact and isinstance(
            getattr(config, "compliance", None), ComplianceConfig):
        config.compliance.do_not_contact = value
    return value


def do_not_contact_from_file(path=None) -> list[str]:
    """Just ``compliance.do_not_contact`` from a config file, validated.
    Raises on a file that can't be read or a list with a mistake in it."""
    data = yaml.safe_load(read_user_text(path or _find_config_file()))
    section = data.get("compliance") if isinstance(data, dict) else None
    raw = section.get("do_not_contact") if isinstance(section, dict) else None
    return ComplianceConfig(do_not_contact=raw).do_not_contact


LOCAL_CONFIG = "harvey.local.yaml"
TEMPLATE_CONFIG = "harvey.yaml"


def local_config_file(path=None) -> Path:
    """Where a change to the config at ``path`` (default: the one Harvey
    loads) is saved: the harvey.local.yaml next to a harvey.yaml, any other
    file itself. Nothing is created; writable_config_file does that."""
    path = Path(path or _find_config_file())
    return path.with_name(LOCAL_CONFIG) if path.name == TEMPLATE_CONFIG else path


# ── Editing settings in place ─────────────────────────────────────────────
#
# A change saved by setup rewrites only the keys it sets, so the comments in
# harvey.local.yaml (copied from the template) stay. Callers check that the
# result parses to what they meant and fall back to a whole rewrite if not.

_PLAIN_KEY_RE = re.compile(r"^(?P<ind>[ ]*)(?P<key>[A-Za-z_][\w-]*)[ \t]*:(?:[ \t]|$)")
_INLINE_COMMENT_RE = re.compile(r"[ \t]+#[^'\"\n]*$")   # a note with no quotes in it


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key_block(lines: list[str], lo: int, hi: int, key: str):
    """The ``key:`` among lines[lo:hi] at their mapping's indent:
    (key line, end of its block, indent), or (None, hi, indent) when it is
    not there (indent None for an empty range). Trailing comments and blank
    lines are left outside the block: they introduce what follows."""
    body = None
    for i in range(lo, hi):
        if not _significant(lines[i]):
            continue
        n = _indent_of(lines[i])
        if body is None:
            body = n
        if n != body:
            continue
        m = _PLAIN_KEY_RE.match(lines[i])
        if not (m and m.group("key") == key):
            continue
        last = i
        for j in range(i + 1, hi):
            if not _significant(lines[j]):
                continue
            nj = _indent_of(lines[j])
            if nj < body or (nj == body and not lines[j].lstrip().startswith("- ")):
                break
            last = j
        return i, last + 1, body
    last = lo - 1
    for i in range(lo, hi):
        if _significant(lines[i]):
            last = i
    return None, max(last + 1, lo), body


def _dump_lines(key: str, value, indent: int) -> list[str]:
    text = yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False,
                          allow_unicode=True, width=1000)
    return [" " * indent + line for line in text.rstrip("\n").split("\n")]


def set_yaml_values(raw: str, changes) -> str:
    """``raw`` with each ``(keys, value)`` in ``changes`` set: the block of
    the key path ``keys`` (a tuple) is replaced by ``value``, or added at
    the end of its parent, and every other line, comments included, stays.
    Plain block-style YAML only; callers check the result parses as meant."""
    lines = raw.split("\n")
    for keys, value in changes:
        lo, hi, parent = 0, len(lines), -2
        for depth, key in enumerate(keys):
            line, end, body = _key_block(lines, lo, hi, key)
            if line is None:
                nested = value
                for k in reversed(keys[depth + 1:]):
                    nested = {k: nested}
                indent = body if body is not None else parent + 2
                new = _dump_lines(key, nested, indent)
                if depth == 0:
                    while lines and not lines[-1].strip():
                        lines.pop()
                    lines[len(lines):] = [""] + new + [""]
                else:
                    lines[end:end] = new
                break
            if depth == len(keys) - 1:
                new = _dump_lines(key, value, body)
                comment = _INLINE_COMMENT_RE.search(lines[line])
                if end == line + 1 and len(new) == 1 and comment:
                    new[0] += comment.group(0)   # a scalar keeps its note
                lines[line:end] = new
                break
            lo, hi, parent = line + 1, end, body
    return "\n".join(lines)


def config_to_edit() -> str:
    """How a message names the file to change a setting in by hand:
    harvey.local.yaml, plus how to make it when there is none yet (it is
    read instead of harvey.yaml, so a file with one key would not load)."""
    try:
        if Path(_find_config_file()).name == LOCAL_CONFIG:
            return LOCAL_CONFIG
    except ConfigError:
        pass
    return f"{LOCAL_CONFIG} (copy {TEMPLATE_CONFIG} to it first)"


def live_config_file(path) -> Path:
    """The file a config loaded from ``path`` reads now: a harvey.local.yaml
    made next to it since (the first change saved from the dashboard or
    setup) wins over the harvey.yaml it was loaded from."""
    path = Path(path)
    local = local_config_file(path)
    return local if local != path and local.exists() else path


def writable_config_file(path=None) -> Path:
    """The file every config change is written to: harvey.local.yaml.

    harvey.yaml is the tracked template; writing it would make ``git pull``
    refuse to update. When there is no harvey.local.yaml next to ``path``
    (default: the config Harvey loads) yet, it is made first as a byte copy
    of ``path``, so the settings and comments in use carry over. A file
    under any other name is written in place. To change settings, use
    save_config_change: it makes the copy only once the change is good.
    """
    path = Path(path or _find_config_file())
    local = local_config_file(path)
    _refuse_dangling(local)
    if local != path and not os.path.lexists(local) and path.exists():
        _create_new(local, path.resolve().read_bytes(), mode_from=path.resolve())
    return local


def save_config_change(edit, path=None) -> Path:
    """Save a change to the config at ``path`` (default: the one Harvey
    loads) into harvey.local.yaml, never the tracked harvey.yaml.

    ``edit(old_bytes, file_name)`` returns the new file's bytes; it reads
    harvey.local.yaml when there is one, else the file at ``path``, so a
    first change builds on the settings and comments in use. Whatever it
    raises propagates, and nothing is written: a failed change leaves no
    copy behind. The file is swapped in whole, so a crash never leaves half
    a config, and a harvey.local.yaml someone else makes meanwhile is
    edited, not replaced. Returns the file written.
    """
    path = Path(path or _find_config_file())
    local = local_config_file(path)
    for _ in range(5):
        _refuse_dangling(local)
        fresh = local != path and not os.path.lexists(local)
        source = (path if fresh else local).resolve()   # a symlinked config stays a symlink
        try:
            old = source.read_bytes()
        except FileNotFoundError:
            raise ConfigError(f"{path.name} not found. Run 'harvey setup' to configure Harvey.")
        new = edit(old, local.name)
        if not fresh:
            _replace_file(source, new)
            return local
        if _create_new(local, new, mode_from=source):
            return local
        # Someone made harvey.local.yaml meanwhile: apply the change to it.
    raise ConfigError(f"{local.name} kept changing while Harvey saved to it. Try again.")


def _refuse_dangling(local: Path) -> None:
    """A harvey.local.yaml link to a missing file: reads skip it (they use
    harvey.yaml), so a write through it would be lost, or fail half-way."""
    if local.is_symlink() and not local.exists():
        raise ConfigError(f"{local} is a link to a file that doesn't exist. "
                          f"Fix the link or remove it, then try again.")


def _temp_beside(target: Path) -> Path:
    """A new, empty temp file next to ``target``, named like it, so the
    .gitignore entry for harvey.local.yaml.* covers a leftover."""
    import tempfile
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    os.close(fd)
    return Path(name)


def _replace_file(target: Path, data: bytes) -> None:
    """Swap ``target``'s content for ``data`` all at once, keeping its mode."""
    import shutil
    tmp = _temp_beside(target)
    try:
        tmp.write_bytes(data)
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _create_new(target: Path, data: bytes, mode_from: Path) -> bool:
    """Make ``target`` with ``data`` and ``mode_from``'s permissions, all at
    once: a crash never leaves half a file, and one made by someone else
    meanwhile is kept. True when this call made it."""
    import shutil
    tmp = _temp_beside(target)
    try:
        tmp.write_bytes(data)
        shutil.copymode(mode_from, tmp)
        try:
            os.link(tmp, target)             # refuses to replace anything
            return True
        except FileExistsError:
            return False
        except OSError:
            # Filesystems without hard links: exclusive create instead.
            try:
                with open(target, "xb") as f:
                    f.write(data)
            except FileExistsError:
                return False
            shutil.copymode(mode_from, target)
            return True
    finally:
        tmp.unlink(missing_ok=True)


def _find_config_file() -> str:
    """Search for Harvey's config.

    ``harvey.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``harvey.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    Harvey only ever writes harvey.local.yaml (writable_config_file).
    """
    candidates = [
        Path.cwd() / LOCAL_CONFIG,
        PROJECT_ROOT / LOCAL_CONFIG,
        Path.cwd() / TEMPLATE_CONFIG,
        Path.cwd().parent / LOCAL_CONFIG,
        Path.cwd().parent / TEMPLATE_CONFIG,
        PROJECT_ROOT / TEMPLATE_CONFIG,
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "harvey.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Run 'harvey setup' to make one."
    )
