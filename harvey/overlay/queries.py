"""The overlay's named page statements (schema 25), checked by the plan
test. Pages page through partial indexes with LIMIT / OFFSET (page numbers
keep working); counts with no clock are tallies, counts with one an index
range read when asked."""

from harvey.queries import statement

_PAGE = (50, 0)

# ── Companies ──
statement("overlay.firms.new",
          "SELECT id FROM firm WHERE live = 0 ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
          params=_PAGE)
statement("overlay.firms.with_contact",
          "SELECT id FROM firm WHERE live = 0 AND n_contacts > 0 "
          "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE)
# The planner would rather sort the n_contacts = 0 rows of firm_by_contacts
# than walk this partial index in order: spelled out.
statement("overlay.firms.no_contact",
          "SELECT id FROM firm INDEXED BY firm_no_contact WHERE live = 0 AND n_contacts = 0 "
          "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.firms.by_contacts",
          "SELECT id FROM firm WHERE live = 0 ORDER BY n_contacts DESC, id DESC LIMIT ? OFFSET ?",
          params=_PAGE)
statement("overlay.firms.searched",
          "SELECT id FROM firm WHERE live = 0 AND last_search_at IS NOT NULL "
          "ORDER BY last_search_at DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.firms.rows",
          "SELECT f.* FROM json_each(?) j CROSS JOIN firm f WHERE f.id = j.value",
          params=("[1, 2]",), drives="j")
statement("overlay.firms.idents",
          "SELECT fi.firm, fi.scheme, fi.key FROM json_each(?) j CROSS JOIN firm_ident fi "
          "WHERE fi.firm = j.value", params=("[1, 2]",), drives="j")
statement("overlay.firms.members",
          "SELECT id FROM firm WHERE live = ? AND live != 0", params=(1,))
statement("overlay.firms.by_ident",
          "SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?", params=(1, 1))
statement("overlay.firms.local_place",
          "SELECT firm FROM local_firm WHERE county = ? AND locality = ?", params=("BV", "Brasov"))
statement("overlay.firms.local_search",
          "SELECT rowid FROM local_fts WHERE local_fts MATCH ?", params=("exemplu*",))
statement("overlay.firms.sources",
          "SELECT firm FROM firm_source WHERE source = ? AND batch = ?", params=(1, 1))

# ── Today, the backlog, the Signals page ──
statement("overlay.tally", "SELECT name, n FROM tally")
statement("overlay.signals",
          "SELECT s.code, s.status, t.n_true, t.n_checked FROM signal s "
          "LEFT JOIN signal_tally t ON t.signal = s.id")
statement("overlay.register_tally",
          "SELECT signal, module, mark, loaded, n_true, n_false, at FROM register_tally")
statement("overlay.backlog.site_to_read",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL) "
          "AND (site_read_deep IS NULL OR site_read_deep < ?)", params=(1_700_000_000,))
statement("overlay.backlog.needs_profile",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND (profiled_at IS NULL OR profiled_at < ?)",
          params=(1_700_000_000,),
          scans={"firm": "counted by its profile-time index, never kept (it depends on the "
                         "clock): a covering index read, about 0.05 s over 1.78M firms"})
statement("overlay.backlog.next_free",
          "SELECT id FROM firm WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL) "
          "AND site_read_deep IS NULL AND n_contacts = 0 ORDER BY created DESC, id DESC LIMIT ?",
          params=(50,))
statement("overlay.search.done",
          "SELECT 1 FROM search WHERE is_person = ? AND target = ? AND module = ? "
          "AND capability = ? AND at >= ? AND outcome != 0 LIMIT 1", params=(0, 1, 1, 1, 0))
statement("overlay.search.batch", "SELECT id FROM search WHERE batch = ?", params=(1,))

# ── Cohorts over stored signals ──
statement("overlay.cohort.positive",
          "SELECT firm FROM firm_signal WHERE signal = ? AND n_positive > 0", params=(1,))
statement("overlay.cohort.range",
          "SELECT firm FROM reading WHERE signal = ? AND positive = 1 AND num >= ?", params=(1, 1))

# ── Contacts ──
statement("overlay.contacts.new",
          "SELECT id FROM contact ORDER BY first_seen DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.contacts.by_reach",
          "SELECT id FROM contact ORDER BY n_firms DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.contacts.kind",
          "SELECT id FROM contact WHERE kind = ? AND line_type = ? "
          "ORDER BY first_seen DESC, id DESC LIMIT ? OFFSET ?", params=(2, 1, 50, 0))
statement("overlay.contacts.links",
          "SELECT l.contact, l.firm, l.trust FROM json_each(?) j CROSS JOIN link l "
          "WHERE l.contact = j.value", params=("[1, 2]",), drives="j")
statement("overlay.contacts.of_firm",
          "SELECT l.contact, l.trust, l.person FROM link l WHERE l.firm = ?", params=(1,))
statement("overlay.contacts.by_value",
          "SELECT id FROM contact WHERE kind = ? AND value = ?", params=(2, "+40700000000"))
statement("overlay.people.of_firm",
          "SELECT id, name, name_norm, role FROM person WHERE firm = ?", params=(1,))
statement("overlay.merges.of_firm",
          "SELECT id FROM merge WHERE survivor = ? OR merged = ?", params=(1, 1))
statement("overlay.legacy_id", "SELECT firm FROM legacy_id WHERE uuid = ?", params=("x",))
