"""Which modules are on in this install, and turning one on or off.

Each install keeps ``modules.<id>.enabled`` ("on" or "off") in the settings
table. Unset means the manifest's ``default_enabled``. Core modules are
always on. A module whose ``requires`` aren't all on counts as off, so a
switch can never leave a module running without what it stands on.

Turning a module off deletes nothing: its observations stay, its signals
are hidden (``available = 0`` in signal_codes) and whatever it does is
refused with a sentence that names it, never skipped in silence.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from harvey.modules.loader import (DATA_DIR, REPO_DIR, Manifest, cross_check,
                                   load_manifests, manifest_paths, read_manifest,
                                   sort_manifests)
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

KEY = "modules.{}.enabled"
PREFIX = "modules."
ON, OFF = "on", "off"
CORE_SET = "core"
# The dashboard's Email switch before modules existed. Read once, when an
# install first sees modules, and moved to modules.email_outreach.enabled.
LEGACY_EMAIL_KEY = "ui.module.email"
EMAIL_MODULE = "email_outreach"
# Harvey's .env, where present_env_keys looks for key names.
ENV_FILE = PROJECT_ROOT / ".env"
# Kinds that call a service with a key of their own: on an install that
# predates modules, one with a key set stays on (upgrade_enabled).
KEYED_KINDS = ("search", "enricher")


class ModuleOff(ValueError):
    """Something was asked of a module that is turned off."""

    def __init__(self, module_id: str, what: str = ""):
        self.module_id = module_id
        name = _name(module_id)
        lead = f"{what} needs" if what else "This needs"
        super().__init__(
            f"{lead} the {name} module ({module_id}), which is off. Turn it on in "
            f"the dashboard (Surse) or with: harvey modules enable {module_id}")


class SwitchRefused(ValueError):
    """A module can't be turned on or off as asked; the message says why."""


@lru_cache(maxsize=1)
def shipped() -> tuple[Manifest, ...]:
    """The modules that ship with Harvey. A problem here is Harvey's own
    bug, so it raises."""
    return tuple(load_manifests(data_dir=None))


# Packs left out of manifests(), one line each saying why (shown on Surse).
_pack_problems: list[str] = []


@lru_cache(maxsize=1)
def manifests() -> tuple[Manifest, ...]:
    """The shipped manifests and the data packs that read cleanly, once per
    process. A broken pack is left out with a warning: a file a user
    dropped in data/modules must not stop the rest of Harvey."""
    mans = list(shipped())
    _pack_problems.clear()
    for path, data_only in manifest_paths(REPO_DIR, DATA_DIR):
        if not data_only:
            continue
        problems: list[str] = []
        m = read_manifest(path, data_only=True, problems=problems)
        if m is not None and not problems:
            problems = cross_check([*mans, m])
        if problems:
            _pack_problems.extend(problems)
            logger.warning("Module pack %s left out:\n%s", path.parent, "\n".join(problems))
            continue
        mans.append(m)
    return tuple(sort_manifests(mans))


def pack_problems() -> list[str]:
    """Why packs in data/modules were left out, one line each."""
    manifests()
    return list(_pack_problems)


def by_id() -> dict[str, Manifest]:
    return {m.id: m for m in manifests()}


def _name(module_id: str) -> str:
    m = by_id().get(module_id)
    return (m.name or m.id) if m else module_id


def resolve(stored: dict[str, str], mans=None) -> set[str]:
    """The ids that are on, from the stored switches (``{key: value}``)."""
    mans = manifests() if mans is None else mans
    on = {m.id for m in mans
          if m.set == CORE_SET
          or stored.get(KEY.format(m.id)) == ON
          or (stored.get(KEY.format(m.id)) != OFF and m.default_enabled)}
    # A module whose requirements are off is off too, all the way down.
    need = {m.id: m.requires for m in mans}
    changed = True
    while changed:
        dropped = {mid for mid in on if any(r not in on for r in need.get(mid, ()))}
        on -= dropped
        changed = bool(dropped)
    return on


async def enabled_ids(state) -> set[str]:
    return resolve(await state.settings_with_prefix(PREFIX))


async def is_enabled(state, module_id: str) -> bool:
    return module_id in await enabled_ids(state)


async def require(state, module_id: str, what: str = "") -> None:
    """Raise ModuleOff unless ``module_id`` is on."""
    if not await is_enabled(state, module_id):
        raise ModuleOff(module_id, what)


# A module's own settings ([settings] in its manifest), per install.
SETTING_KEY = "modules.{}.{}"


def setting_value(spec: dict, raw):
    """A stored (or given) value read as its manifest ``spec`` types it,
    clamped to ``min``/``max``; the default when it can't be read."""
    kind, default = spec.get("type"), spec.get("default")
    if raw is None or raw == "":
        return default
    try:
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "on", "yes"):
                return True
            if text in ("0", "false", "off", "no"):
                return False
            return default
        if kind in ("int", "float"):
            value = int(raw) if kind == "int" else float(raw)
            if spec.get("min") is not None:
                value = max(value, spec["min"])
            if spec.get("max") is not None:
                value = min(value, spec["max"])
            return value
        if kind == "list":
            return [v.strip() for v in str(raw).split(",") if v.strip()] \
                if not isinstance(raw, list) else raw
        return str(raw)
    except (TypeError, ValueError):
        return default


async def module_settings(state, module_id: str) -> dict:
    """Every setting a module's manifest declares, as stored for this
    install or else its default."""
    m = by_id().get(module_id)
    if m is None:
        return {}
    stored = await state.settings_with_prefix(f"{PREFIX}{module_id}.")
    return {key: setting_value(spec, stored.get(SETTING_KEY.format(module_id, key)))
            for key, spec in m.settings.items() if isinstance(spec, dict)}


async def set_module_setting(state, module_id: str, key: str, value):
    """Store one setting (typed and clamped as the manifest says); returns
    what was stored. ValueError for a setting the manifest doesn't declare."""
    m = by_id().get(module_id)
    spec = (m.settings if m else {}).get(key)
    if not isinstance(spec, dict) or key == "enabled":
        raise ValueError(f"{module_id} has no setting {key!r}")
    typed = setting_value(spec, value)
    stored = ("on" if typed else "off") if spec.get("type") == "bool" else (
        ",".join(typed) if isinstance(typed, list) else str(typed))
    await state.set_setting(SETTING_KEY.format(module_id, key), stored)
    return typed


def capability_owner(field: str, key: str) -> str | None:
    """The module whose capability names ``field = key`` (a discovery
    ``provider`` or ``preset`` key), or None."""
    for m in manifests():
        if any(cap.get(field) == key for cap in m.capabilities):
            return m.id
    return None


def discovery_problem(enabled: set[str], provider: str, preset: str = "") -> ModuleOff | None:
    """Why a discovery run can't go ahead with these modules on, or None."""
    for field, key, what in (("provider", provider, f"The {provider} discovery source"),
                             ("preset", preset, f"The {preset} preset")):
        owner = capability_owner(field, key) if key else None
        if owner and owner not in enabled:
            return ModuleOff(owner, what)
    return None


async def require_discovery(state, provider: str, preset: str = "") -> None:
    problem = discovery_problem(await enabled_ids(state), provider, preset)
    if problem:
        raise problem


def env_keys(enabled: set[str]) -> list[str]:
    """The .env keys the enabled modules read, in manifest order."""
    keys: list[str] = []
    for m in manifests():
        if m.id in enabled:
            keys += [k for k in m.secrets if k not in keys]
    return keys


def env_key_owners(key: str) -> list[str]:
    return [m.id for m in manifests() if key in m.secrets]


def present_env_keys() -> set[str]:
    """Names of the keys that hold a value in Harvey's .env or in the
    process environment. Only names come back: no value is kept, shown or
    parsed, and the environment is left as it was (this runs while the
    database opens)."""
    from dotenv import dotenv_values

    present = {k for k, v in os.environ.items() if v.strip()}
    try:
        present |= {k for k, v in dotenv_values(ENV_FILE).items() if (v or "").strip()}
    except OSError:
        pass
    return present


def upgrade_enabled(mans, present: set[str], confirmed: set[str],
                    used: set[str] = frozenset()) -> list[str]:
    """Modules that stay on when an install that predates modules first sees
    them, though their default is off: a search or enricher module with one
    of its .env keys set (its provider was set up), a module that declares
    a signal the user confirmed (it was being collected), and a module the
    install has data from (``used``: it was running). Nobody's working
    setup turns itself off on upgrade."""
    out = []
    for m in mans:
        if m.default_enabled or m.set == CORE_SET:
            continue
        keyed = m.kind in KEYED_KINDS and any(k in present for k in m.secrets)
        collecting = any(sig["code"] in confirmed for sig in m.signals)
        if keyed or collecting or m.id in used:
            out.append(m.id)
    return out


async def set_enabled(state, module_id: str, on: bool) -> set[str]:
    """Turn a module on or off and return what is on afterwards.

    Refused (SwitchRefused) for an unknown module, a core one, turning on a
    module whose requirements are off (enabling one never enables another),
    and turning off one that an enabled module requires. The vocabulary is
    synced right after, so the module's signals show or hide at once.
    """
    mans = by_id()
    m = mans.get(module_id)
    if m is None:
        raise SwitchRefused(f"There is no module {module_id!r}. "
                            f"See: harvey modules list")
    if m.set == CORE_SET and not on:
        raise SwitchRefused(f"{m.name or m.id} is part of Harvey's core and can't be "
                            f"turned off.")
    enabled = await enabled_ids(state)
    if on:
        missing = [r for r in m.requires if r not in enabled]
        if missing:
            names = ", ".join(f"{_name(r)} ({r})" for r in missing)
            raise SwitchRefused(f"{m.name or m.id} needs {names}, which "
                                f"{'is' if len(missing) == 1 else 'are'} off. "
                                f"Turn {'it' if len(missing) == 1 else 'them'} on first.")
    else:
        users = [o.id for o in mans.values() if o.id in enabled and module_id in o.requires]
        if users:
            names = ", ".join(f"{_name(u)} ({u})" for u in users)
            raise SwitchRefused(f"{names} {'needs' if len(users) == 1 else 'need'} "
                                f"{m.name or m.id}. Turn "
                                f"{'it' if len(users) == 1 else 'them'} off first.")
    await state.set_setting(KEY.format(module_id), ON if on else OFF)
    from harvey.signals import sync_vocabulary

    await sync_vocabulary(state)
    return await enabled_ids(state)
