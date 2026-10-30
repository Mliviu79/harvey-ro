"""Modules: what Harvey can do beyond its core, each described by a manifest.

A module is a folder with a ``module.toml`` under ``harvey/modules/<set>/``
(``core``, ``original``, ``ro``), or a data-only pack under ``data/modules/``.
The manifest says what the module is, which code it is (``entry``), which
``.env`` keys it reads, what it costs, and which signals it declares.
``loader.load_manifests()`` reads and checks them all without importing any
module code.
"""
