"""Identifier schemes as a record brings them.

A firm's register identifiers live in ``firm_ident(scheme, key)``, under
schemes the modules declare ([[identifiers]]). A record a capability
returns still carries them the way sources have always written them: a
legal id by kind (``ids["cui"]``) and a listing's or a register's number as
an external id with the source's prefix (``"osm:node/1"``,
``"onrc:J01/2/2020"``). The manifest that declares a scheme says which kind
and prefix it arrives as (its ``legacy`` field: ``kind`` or
``kind:prefix``), so the core maps a record's ids to schemes without naming
any of them.

The resolver (harvey/resolver.py) compares records by kinds ("cui",
"reg", "external", ...); ``kinds_of`` gives a ``firm_ident`` row back as
those kinds, so one matching rule serves both schemas.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Scheme:
    id: int
    name: str
    numeric: bool
    legal: bool
    kind: str = ""          # the resolver's kind it arrives as ("cui", "external")
    prefix: str = ""        # an external id's prefix ("osm:")
    register: bool = False  # declared by a register module (a register describes the firm)


@dataclass
class Schemes:
    by_id: dict[int, Scheme] = field(default_factory=dict)

    @classmethod
    def load(cls, con: sqlite3.Connection, manifests=None) -> "Schemes":
        """The database's schemes, with how each arrives from the manifest
        that declares it (a scheme no manifest declares any more keeps its
        rows, and is never matched by an incoming record)."""
        from harvey.modules.loader import load_manifests

        out = cls()
        rows = con.execute("SELECT id, name, numeric, legal FROM ident_scheme").fetchall()
        declared = {}
        for m in (manifests if manifests is not None else load_manifests()):
            for ident in m.identifiers:
                declared[ident["name"]] = (ident.get("legacy") or "", bool(m.register))
        for sid, name, numeric, legal in rows:
            arrives, register = declared.get(name, ("", False))
            kind, _, prefix = arrives.partition(":")
            out.by_id[sid] = Scheme(sid, name, bool(numeric), bool(legal), kind, prefix, register)
        return out

    def named(self, name: str) -> Scheme | None:
        return next((s for s in self.by_id.values() if s.name == name), None)

    def key(self, kind: str, value: str) -> tuple[int, object] | None:
        """(scheme id, key) for an id a record brings ("cui", "123") or
        ("external", "osm:node/1"); None when no scheme takes it. A numeric
        scheme's key is an integer (one that isn't a number isn't its key)."""
        if kind == "reg":
            # The resolver's registration number: a register's own number,
            # which arrives as an external id.
            s = next((s for s in self.by_id.values() if s.register and s.kind == "external"),
                     None)
            return (s.id, value) if s is not None and value else None
        for s in self.by_id.values():
            if s.kind != kind:
                continue
            if s.prefix:
                if not value.startswith(s.prefix):
                    continue
                raw = value[len(s.prefix):]
            else:
                raw = value
            if not raw:
                continue
            if s.numeric:
                if not raw.isdigit() or raw.startswith("0") or len(raw) > 18:
                    continue
                return s.id, int(raw)
            return s.id, raw
        return None

    def kinds_of(self, scheme: int, key) -> list[tuple[str, str]]:
        """A ``firm_ident`` row as the resolver's (kind, value) pairs: a
        registration number is also its "reg" (resolver.identifiers gives
        both for a registry record)."""
        s = self.by_id.get(scheme)
        if s is None or not s.kind:
            return []
        value = f"{s.prefix}{key}"
        out = [(s.kind, value)]
        if s.kind == "external" and s.register:
            out.append(("reg", str(key)))
        return out

    def legal_kinds(self) -> tuple[str, ...]:
        """The resolver's kinds of the legal schemes: two different values
        are two different firms."""
        return tuple(sorted({s.kind for s in self.by_id.values() if s.legal and s.kind}))

    def legal_ids(self) -> set[int]:
        return {s.id for s in self.by_id.values() if s.legal}

    def register_ids(self) -> set[int]:
        """Schemes a register describes a firm by: a firm holding one needs
        no local description."""
        return {s.id for s in self.by_id.values() if s.register}
