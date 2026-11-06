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

# ── Today, the backlog, the signal listing ──
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

# ── The resolver (resolver.match_firm): every key probed by index ──
_GROUP = "(SELECT ? UNION ALL SELECT id FROM firm WHERE live = ? AND live != 0)"
statement("overlay.resolve.live",
          "SELECT CASE live WHEN 0 THEN id ELSE live END FROM firm WHERE id = ?", params=(1,))
statement("overlay.resolve.ident",
          "SELECT firm FROM firm_ident WHERE scheme = ? AND key = ?", params=(1, 1))
statement("overlay.resolve.domain", "SELECT id FROM firm WHERE domain = ?",
          params=("example.com",))
statement("overlay.resolve.key", "SELECT firm FROM firm_key WHERE kind = ? AND value = ?",
          params=(1, "office@example.com"))
statement("overlay.resolve.phone",
          "SELECT l.firm FROM contact c CROSS JOIN link l "
          "WHERE c.kind = ? AND c.value = ? AND l.contact = c.id",
          params=(2, "+40700000000"), drives="c")
# Every link of one address (email_rules: any link names a person).
statement("overlay.read.links_of_address",
          "SELECT l.firm, l.person, l.trust, c.id FROM contact c CROSS JOIN link l "
          "WHERE c.kind = ? AND c.value = ? AND l.contact = c.id",
          params=(1, "office@example.com"), drives="c")
statement("overlay.resolve.group_idents",
          f"SELECT scheme, key FROM firm_ident WHERE firm IN {_GROUP}", params=(1, 1))
statement("overlay.resolve.group_keys",
          f"SELECT kind, value FROM firm_key WHERE firm IN {_GROUP}", params=(1, 1))
statement("overlay.resolve.group_domains",
          f"SELECT domain FROM firm WHERE domain IS NOT NULL AND id IN {_GROUP}", params=(1, 1))

# ── Page reads (overlay/reads.py): a page's rows by key, its sets by index ──
_J = "json_each(?) j"
_IDS = ("[1, 2]",)
# The live firm each id stands for (a merged record's survivor).
_LIVE = "CASE f.live WHEN 0 THEN f.id ELSE f.live END"
statement("overlay.read.live_of",
          f"SELECT j.value, {_LIVE} FROM {_J} CROSS JOIN firm f WHERE f.id = j.value",
          params=_IDS, drives="j")
statement("overlay.read.live_of_keys",
          f"SELECT j.key, {_LIVE}, fi.firm FROM {_J} CROSS JOIN firm_ident fi CROSS JOIN firm f "
          "WHERE fi.scheme = ? AND fi.key = j.value AND f.id = fi.firm",
          params=("[1, 2]", 1), drives="j")
statement("overlay.read.members",
          f"SELECT f.live, f.id FROM {_J} CROSS JOIN firm f WHERE f.live = j.value AND f.live != 0",
          params=_IDS, drives="j")
statement("overlay.read.locals",
          f"SELECT l.firm, l.name, l.county, l.locality, l.address, l.phone, l.industry, "
          f"l.description, l.source_url, l.gone, l.country FROM {_J} CROSS JOIN local_firm l "
          "WHERE l.firm = j.value", params=_IDS, drives="j")
# Every firm no register describes (a few hundred listings): place filters
# match them in Python as schema 24's place line did.
statement("overlay.read.local_places", "SELECT firm, locality, county FROM local_firm")
statement("overlay.read.sources_of",
          f"SELECT fs.firm, s.name, fs.ref, fs.first_seen, fs.last_seen FROM {_J} "
          "CROSS JOIN firm_source fs CROSS JOIN source s WHERE fs.firm = j.value "
          "AND s.id = fs.source", params=_IDS, drives="j")
statement("overlay.read.links_of_firms",
          f"SELECT l.firm, l.contact, l.trust, l.person, l.location, l.first_seen FROM {_J} "
          "CROSS JOIN link l WHERE l.firm = j.value", params=_IDS, drives="j")
# The contacts the firms' links reach, of a kind (and line type) when
# given: a firm condition's contacts with no link row read into Python.
statement("overlay.read.contacts_of_firms",
          f"SELECT l.contact FROM {_J} CROSS JOIN link l CROSS JOIN contact c "
          "WHERE l.firm = j.value AND c.id = l.contact AND (? IS NULL OR c.kind = ?) "
          "AND (? IS NULL OR c.line_type = ?)", params=(*_IDS, 1, 1, None, None), drives="j")
statement("overlay.read.links_of_contacts",
          f"SELECT l.contact, l.firm, l.trust, l.person, l.location, l.first_seen FROM {_J} "
          "CROSS JOIN link l WHERE l.contact = j.value", params=_IDS, drives="j")
# Evidence by (firm, contact) pairs: [[firm, contact], ...].
statement("overlay.read.evidence",
          f"SELECT e.firm, e.contact, s.name, e.trust, e.evidence_url, e.first_seen, e.person, "
          f"e.location FROM {_J} CROSS JOIN link_evidence e CROSS JOIN source s "
          "WHERE e.firm = json_extract(j.value, '$[0]') AND e.contact = json_extract(j.value, '$[1]') "
          "AND s.id = e.source", params=("[[1, 2]]",), drives="j")
statement("overlay.read.contact_rows",
          f"SELECT c.id, k.kind, c.value, c.display, c.line_type, c.status, c.do_not_contact, "
          f"c.first_seen, c.last_seen, c.n_firms, c.best_trust, c.kind FROM {_J} "
          "CROSS JOIN contact c LEFT JOIN contact_kinds k ON k.id = c.kind WHERE c.id = j.value",
          params=_IDS, drives="j")
statement("overlay.read.people_of",
          f"SELECT p.firm, p.id, p.name, p.name_norm, p.role, s.name, p.first_seen FROM {_J} "
          "CROSS JOIN person p LEFT JOIN source s ON s.id = p.source WHERE p.firm = j.value",
          params=_IDS, drives="j")
statement("overlay.read.persons",
          f"SELECT p.id, p.firm, p.name, p.name_norm, p.role, s.name, p.first_seen FROM {_J} "
          "CROSS JOIN person p LEFT JOIN source s ON s.id = p.source WHERE p.id = j.value",
          params=_IDS, drives="j")
statement("overlay.read.prospects_of",
          f"SELECT p.company_id, p.first_name, p.last_name, p.title, p.email, p.email_status, p.phone, "
          f"p.score, p.status, p.created_at FROM {_J} CROSS JOIN prospects p WHERE p.company_id = j.value",
          params=_IDS, drives="j")
statement("overlay.read.searches_of",
          "SELECT s.id, s.is_person, s.target, m.name, c.name, s.looked_for, s.at, s.outcome, "
          "s.found, s.cost FROM search s JOIN module m ON m.id = s.module "
          "JOIN capability c ON c.id = s.capability WHERE s.is_person = ? AND s.target = ?",
          params=(0, 1))
statement("overlay.read.person_searched",
          f"SELECT s.target, MAX(s.at) FROM {_J} CROSS JOIN search s "
          "WHERE s.is_person = 1 AND s.target = j.value GROUP BY s.target",
          params=_IDS, drives="j")

# Companies: the index pages in both directions (the descending ones above).
statement("overlay.firms.new.asc",
          "SELECT id FROM firm WHERE live = 0 ORDER BY created ASC, id ASC LIMIT ? OFFSET ?",
          params=_PAGE)
statement("overlay.firms.with_contact.asc",
          "SELECT id FROM firm WHERE live = 0 AND n_contacts > 0 "
          "ORDER BY created ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.firms.no_contact.asc",
          "SELECT id FROM firm INDEXED BY firm_no_contact WHERE live = 0 AND n_contacts = 0 "
          "ORDER BY created ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.firms.by_contacts.asc",
          "SELECT id FROM firm WHERE live = 0 ORDER BY n_contacts ASC, id ASC LIMIT ? OFFSET ?",
          params=_PAGE)
statement("overlay.firms.searched.asc",
          "SELECT id FROM firm WHERE live = 0 AND last_search_at IS NOT NULL "
          "ORDER BY last_search_at ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.read.by_contacts.some.asc",
          "SELECT id FROM firm INDEXED BY firm_by_contacts WHERE live = 0 AND n_contacts > 0 "
          "ORDER BY n_contacts ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.read.walk_unsearched",
          "SELECT id FROM firm INDEXED BY firm_new WHERE live = 0 AND last_search_at IS NULL "
          "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?", params=_PAGE,
          scans={"firm": "the firms never searched, after the searched ones: nearly every "
                         "firm, so the walk stops soon after its page"})
statement("overlay.firms.searched.count",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND last_search_at IS NOT NULL")
# Harvey's own columns as conditions on the newest-first walk (a site: yes
# 1 / no 0 / either NULL; a contact: the same; an email, a phone, a mobile:
# 1 to require). Most firms pass most of them, so the walk stops soon after
# its page; a rare one (a website: 1 in 160) is read from its own index
# instead (overlay.read.with_site).
_WALK = ("live = 0 AND (? IS NULL OR (website IS NOT NULL OR domain IS NOT NULL) = ?) "
         "AND (? IS NULL OR (n_contacts > 0) = ?) AND (? = 0 OR n_email > 0) "
         "AND (? = 0 OR n_phone > 0) AND (? = 0 OR n_mobile > 0)")
_WALK_PARAMS = (None, None, 1, 1, 0, 1, 0)
_WALKED = {"firm": "a page walked newest first: most firms pass Harvey's own conditions, so "
                   "the walk stops soon after its page (a rare condition reads its own index)"}
statement("overlay.read.walk",
          f"SELECT id FROM firm INDEXED BY firm_new WHERE {_WALK} "
          "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
          params=(*_WALK_PARAMS, 50, 0), scans=_WALKED)
statement("overlay.read.walk.asc",
          f"SELECT id FROM firm INDEXED BY firm_new WHERE {_WALK} "
          "ORDER BY created ASC, id ASC LIMIT ? OFFSET ?",
          params=(*_WALK_PARAMS, 50, 0), scans=_WALKED)
# The firms of a list that pass Harvey's own columns (the same conditions),
# in no order: a big set filtered in SQL, only the passing ids returned.
statement("overlay.read.passing",
          f"SELECT f.id FROM {_J} CROSS JOIN firm f WHERE f.id = j.value AND {_WALK}",
          params=(*_IDS, *_WALK_PARAMS), drives="j")
# A Find-contacts run's firms: when each was added and whether it has a
# site, those with no contact only when asked (the second param: 1).
# (Each firm's time, whether it has a site, and whether a search found
# that site: website_source and a trust, as finder_company reads it, and
# that trust.)
statement("overlay.read.finder_rows",
          "SELECT f.id, f.created, (f.website IS NOT NULL OR f.domain IS NOT NULL), "
          "(f.website IS NOT NULL AND f.website_source IS NOT NULL "
          f"AND f.website_trust IS NOT NULL), f.website_trust FROM {_J} "
          "CROSS JOIN firm f WHERE f.id = j.value AND (? = 0 OR f.n_contacts = 0)",
          params=(*_IDS, 1), drives="j")
statement("overlay.read.walk.count",
          f"SELECT COUNT(*) FROM firm INDEXED BY firm_new WHERE {_WALK}",
          params=_WALK_PARAMS,
          scans={"firm": "the total of a list Harvey's own columns narrow and no tally "
                         "counts: one read of the newest-first index (about 0.3 s over 1.8M)"})
# Every live firm, newest first: a list whose order needs every firm (a
# sort by name, a figure's firms without one) or a set given as "all but".
statement("overlay.read.all_live",
          "SELECT id FROM firm WHERE live = 0 ORDER BY created DESC, id DESC",
          scans={"firm": "every firm in Harvey, once per kept ordering (Snapshots): an "
                         "index-only read, about 1 s over 1.8M"})
statement("overlay.read.with_site",
          "SELECT id FROM firm WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL)")
# The firms a module's contact kind reaches (forms, profiles: a few
# thousand each; email, phone and mobile read the firm's own counts).
statement("overlay.read.kind_firms",
          "SELECT l.firm FROM contact c CROSS JOIN link l WHERE c.kind = ? AND l.contact = c.id",
          params=(3,), drives="c",
          scans={"contact": "a module's contact kind: a few thousand rows (email and phone "
                            "filters read the firm's own counts)"})
statement("overlay.read.source_firms",
          "SELECT firm FROM firm_source WHERE source = ?", params=(1,),
          scans={"firm_source": "the firms one source found: its index range (every pulled "
                                "firm for a register's pull)"})
# The distinct values of an index, one seek each (a DISTINCT would walk it).
statement("overlay.read.firm_source_names",
          "WITH RECURSIVE s(v) AS (SELECT MIN(source) FROM firm_source UNION ALL "
          "SELECT (SELECT MIN(source) FROM firm_source WHERE source > s.v) FROM s "
          "WHERE s.v IS NOT NULL) SELECT n.name FROM s JOIN source n ON n.id = s.v "
          "ORDER BY n.name", bounded=True)
statement("overlay.read.evidence_source_names",
          "WITH RECURSIVE s(v) AS (SELECT MIN(source) FROM link_evidence UNION ALL "
          "SELECT (SELECT MIN(source) FROM link_evidence WHERE source > s.v) FROM s "
          "WHERE s.v IS NOT NULL) SELECT n.name FROM s JOIN source n ON n.id = s.v "
          "ORDER BY n.name", bounded=True)
statement("overlay.read.person_source_names",
          "SELECT DISTINCT n.name FROM person p JOIN source n ON n.id = p.source ORDER BY n.name")
statement("overlay.read.contact_kinds",
          "SELECT k.kind, k.label, k.channel FROM contact_kinds k WHERE k.available = 1 "
          "OR EXISTS (SELECT 1 FROM contact c WHERE c.kind = k.id) ORDER BY k.id",
          scans={"contact": "whether a kind has any contact: the search stops at its first"})
# The figure a sort or a range reads, page by page in its index's order:
# the records in Harvey whose register key has a value (overlay/reads.py
# hands a register's own statement its scheme).

# Contacts: the index pages in both directions.
statement("overlay.contacts.new.asc",
          "SELECT id FROM contact ORDER BY first_seen ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
statement("overlay.contacts.by_reach.asc",
          "SELECT id FROM contact ORDER BY n_firms ASC, id ASC LIMIT ? OFFSET ?", params=_PAGE)
# One kind (and line type) newest first: most contacts are phones, so the
# walk stops soon after its page; a rare kind is read from its own index.
_KIND_WALK = {"contact": "a page walked newest first: most contacts are phones, so the walk "
                         "stops soon after its page (a rare kind reads its own index)"}
statement("overlay.read.contacts.walk",
          "SELECT id FROM contact INDEXED BY contact_new WHERE kind = ? "
          "AND (? IS NULL OR line_type = ?) ORDER BY first_seen DESC, id DESC LIMIT ? OFFSET ?",
          params=(2, 1, 1, 50, 0), scans=_KIND_WALK)
statement("overlay.read.contacts.walk.asc",
          "SELECT id FROM contact INDEXED BY contact_new WHERE kind = ? "
          "AND (? IS NULL OR line_type = ?) ORDER BY first_seen ASC, id ASC LIMIT ? OFFSET ?",
          params=(2, 1, 1, 50, 0), scans=_KIND_WALK)
statement("overlay.read.contacts.of_kind",
          "SELECT id FROM contact WHERE kind = ?", params=(1,),
          scans={"contact": "a rare kind's contacts (emails, forms, profiles: thousands); "
                            "phones are walked a page at a time instead"})
# A text search over contact values (the Contacts page's box): every
# contact, as schema 24 read it; the box is typed rarely and capped.
statement("overlay.read.contacts.value_like",
          "SELECT id FROM contact WHERE value LIKE ? ESCAPE '\\'", params=("%07%",),
          scans={"contact": "the search box's words in a contact's value: read once per "
                            "search, as schema 24 did"})
# The same for a few contacts by id (a rolling campaign's changed ones).
statement("overlay.read.contacts.value_like_of",
          f"SELECT c.id FROM {_J} CROSS JOIN contact c WHERE c.id = j.value "
          "AND c.value LIKE ? ESCAPE '\\'", params=(*_IDS, "%07%"), drives="j")
statement("overlay.read.persons_like",
          "SELECT id, firm FROM person WHERE name_norm LIKE ? ESCAPE '\\'", params=("%ana%",))
statement("overlay.read.linked_persons",
          "SELECT DISTINCT person FROM link WHERE person != 0")
statement("overlay.read.persons_all",
          "SELECT p.id, p.firm, p.name, p.name_norm, p.role, s.name, p.first_seen FROM person p "
          "LEFT JOIN source s ON s.id = p.source")

# Today, the backlog.
statement("overlay.read.with_domain", "SELECT COUNT(*) FROM firm WHERE live = 0 AND domain IS NOT NULL")
statement("overlay.read.needs_profile",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND domain IS NOT NULL "
          "AND (profiled_at IS NULL OR profiled_at < ?)", params=(1_700_000_000,))
statement("overlay.read.needing_profile",
          "SELECT f.id, f.domain FROM firm f WHERE f.live = 0 AND f.domain IS NOT NULL "
          "AND (f.profiled_at IS NULL OR f.profiled_at < ?) "
          "ORDER BY f.profiled_at IS NOT NULL, f.profiled_at LIMIT ?",
          params=(1_700_000_000, 100), bounded=True)
statement("overlay.read.profiled",
          "SELECT COUNT(*), SUM(finder_at IS NOT NULL) FROM firm WHERE live = 0 "
          "AND domain IS NOT NULL AND profiled_at IS NOT NULL")
statement("overlay.read.profiled_since",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND domain IS NOT NULL AND profiled_at >= ?",
          params=(1_700_000_000,))
statement("overlay.read.searches_since",
          "SELECT COUNT(*), COALESCE(SUM(found), 0), COALESCE(SUM(cost), 0) FROM search "
          "WHERE at >= ? AND is_person = 0 AND module = ? AND capability = ?",
          params=(1_700_000_000, 1, 1))
statement("overlay.read.lookups_since",
          "SELECT m.name, COUNT(*), COALESCE(SUM(s.found), 0) FROM search s "
          "JOIN module m ON m.id = s.module WHERE s.at >= ? AND s.is_person = 0 "
          "AND (s.looked_for & ?) != 0 GROUP BY m.name", params=(1_700_000_000, 64))
statement("overlay.read.last_search",
          "SELECT MAX(at) FROM (SELECT at, module, capability, is_person FROM search "
          "ORDER BY id DESC LIMIT 500) WHERE is_person = 0 AND module = ? AND capability = ?",
          params=(1, 1))
# The finder's backlog: firms with no contact, by what is left to do. The
# site part walks the firms with a site (their index); the searched part
# the firms the finder touched (its index range).
statement("overlay.read.backlog.sites",
          "SELECT COUNT(*), SUM(site_read_deep IS NULL OR site_read_deep < ?) FROM firm "
          "INDEXED BY firm_with_site WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL) "
          "AND n_contacts = 0",
          params=(0,))
statement("overlay.read.backlog.searched",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND n_contacts = 0 AND finder_at IS NOT NULL "
          "AND finder_at >= ?", params=(0,),
          scans={"firm": "the firms with no contact, counted once per BACKLOG_CACHE_SECONDS: "
                         "the no-contact index read whole (about 0.3 s over 700k)"})
# (A website a search found that waits for a check is never read meanwhile:
# finder.find skips it, so the walk leaves it out.)
_NOT_WAITING = "AND (website_trust IS NOT 2 OR domain IS NOT NULL) "
statement("overlay.read.next_free",
          "SELECT id FROM firm INDEXED BY firm_with_site WHERE live = 0 "
          "AND (website IS NOT NULL OR domain IS NOT NULL) AND n_contacts = 0 "
          "AND (site_read_deep IS NULL OR site_read_deep < ?) " + _NOT_WAITING +
          "ORDER BY created DESC, id DESC LIMIT ?", params=(0, 50), bounded=True)
statement("overlay.read.next_shallow",
          "SELECT id FROM firm INDEXED BY firm_with_site WHERE live = 0 "
          "AND (website IS NOT NULL OR domain IS NOT NULL) AND n_contacts > 0 "
          "AND site_read_at IS NOT NULL AND site_read_deep IS NULL " + _NOT_WAITING +
          "ORDER BY created DESC, id DESC LIMIT ?", params=(50,), bounded=True)
# A page of them below a key and above another, newest first (a keyset
# walk: (created, id) is the index's order).
statement("overlay.read.next_paid",
          "SELECT id, created FROM firm INDEXED BY firm_no_contact WHERE live = 0 "
          "AND n_contacts = 0 AND website IS NULL AND domain IS NULL "
          "AND (created, id) < (?, ?) AND (created, id) > (?, ?) "
          "ORDER BY created DESC, id DESC LIMIT ?",
          params=(2**62, 2**62, -1, -1, 50), scans=_WALKED)
# The walk forgotten when a firm it passed becomes searchable again (a
# found website taken back, a merge undone): the next one starts over.
statement("overlay.finder.paid_walk_reset",
          "DELETE FROM settings WHERE key = ?", params=("finder_paid_walk",))
statement("overlay.read.searched_by",
          f"SELECT DISTINCT s.target FROM {_J} CROSS JOIN search s WHERE s.is_person = 0 "
          "AND s.target = j.value AND s.module = ? AND s.capability = ? AND s.at >= ? "
          "AND s.outcome != 0", params=("[1, 2]", 1, 1, 0), drives="j")
statement("overlay.read.prospect_emails",
          "SELECT p.company_id, p.email, f.domain FROM prospects p "
          "LEFT JOIN firm f ON f.id = p.company_id "
          "WHERE p.email != '' AND COALESCE(p.email_status, '') != 'invalid' "
          "AND p.status IS NOT 'opted_out'")
statement("overlay.read.prospect_phones",
          "SELECT DISTINCT company_id FROM prospects WHERE phone != ''")
statement("overlay.read.local_phones",
          "SELECT firm FROM local_firm WHERE phone IS NOT NULL AND phone != ''")
statement("overlay.read.with_phone",
          "SELECT COUNT(*) FROM firm WHERE live = 0 AND n_phone > 0",
          scans={"firm": "the landing page's firms with a phone: one read of the firm table "
                         "(about 0.3 s over 1.8M); n_phone is kept, but not indexed"})

# Signals and cohorts over stored signals.
statement("overlay.read.vocabulary", "SELECT * FROM signal ORDER BY id")
statement("overlay.read.signal_sources",
          "SELECT s.code, m.name, ss.how, ss.precedence, ss.absent FROM signal_source ss "
          "JOIN signal s ON s.id = ss.signal JOIN module m ON m.id = ss.module")
statement("overlay.read.readings",
          "SELECT firm, key, num, text, confidence, positive, at FROM reading WHERE signal = ?",
          params=(1,))
statement("overlay.read.readings_of",
          "SELECT r.firm, s.code, r.key, r.num, r.text, r.confidence, m.name, r.at "
          f"FROM {_J} CROSS JOIN reading r JOIN signal s ON s.id = r.signal "
          "LEFT JOIN module m ON m.id = r.module WHERE r.firm = j.value",
          params=_IDS, drives="j")
# A company's web-signal history (StateManager.get_observations).
statement("overlay.read.obs_of",
          "SELECT o.id, s.code, o.num, o.text, o.confidence, m.name, c.name, o.evidence_url, "
          "o.at, b.ref "
          f"FROM {_J} CROSS JOIN obs o JOIN signal s ON s.id = o.signal "
          "LEFT JOIN module m ON m.id = o.module LEFT JOIN capability c ON c.id = o.capability "
          "LEFT JOIN batch b ON b.id = o.run WHERE o.firm = j.value",
          params=_IDS, drives="j")
statement("overlay.read.obs_of_signal",
          "SELECT o.id, s.code, o.num, o.text, o.confidence, m.name, c.name, o.evidence_url, "
          "o.at, b.ref "
          f"FROM {_J} CROSS JOIN obs o CROSS JOIN signal s "
          "LEFT JOIN module m ON m.id = o.module LEFT JOIN capability c ON c.id = o.capability "
          "LEFT JOIN batch b ON b.id = o.run WHERE o.firm = j.value AND o.signal = ? "
          "AND s.id = o.signal",
          params=(*_IDS, 1), drives="j")
# Outreach on the overlay: a company's prospects, prospects by id, and
# when a run last recorded a reading.
statement("overlay.read.company_prospects",
          "SELECT p.* FROM (SELECT ? AS id UNION ALL SELECT id FROM firm WHERE live = ? "
          "AND live != 0) g CROSS JOIN prospects p WHERE p.company_id = g.id",
          params=(1, 1))
statement("overlay.read.prospects_by_id",
          f"SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, p.score, "
          f"p.personalization_notes, p.company_id AS firm FROM {_J} CROSS JOIN prospects p "
          "WHERE p.id = j.value",
          params=('["p1"]',), drives="j")
statement("overlay.read.run_last_at",
          "SELECT MAX(o.at) FROM batch b CROSS JOIN obs o WHERE b.ref = ? AND o.run = b.id",
          params=("run-1",), drives="b")
statement("overlay.read.ranked",
          "SELECT r.firm FROM signal s CROSS JOIN reading r WHERE s.code = ? AND r.signal = s.id "
          "AND r.positive = 1 AND r.key = ?",
          params=("SERP_RANK", "dentist"), drives="s",
          scans={"reading": "a rank signal's positive readings: one per firm and phrase ranked"})
statement("overlay.read.checked", "SELECT firm FROM firm_signal WHERE signal = ?", params=(1,))
statement("overlay.read.tally_signal",
          "SELECT n_true, n_checked FROM signal_tally WHERE signal = ?", params=(1,))
statement("overlay.read.register_tally_of",
          "SELECT mark, loaded, n_true, n_false, at, n_values FROM register_tally "
          "WHERE signal = ? AND module = ?", params=(1, 1))
statement("overlay.read.firm_rows",
          f"SELECT f.id, f.live, f.created, f.display_name, f.website, f.domain, "
          f"f.website_trust, f.n_contacts, f.n_email, f.n_phone, f.n_mobile, f.best_trust, "
          f"f.n_people, f.last_search_at, f.profiled_at, f.site_read_at, f.site_read_deep, "
          f"f.finder_at FROM {_J} CROSS JOIN firm f WHERE f.id = j.value",
          params=_IDS, drives="j")
statement("overlay.read.display_names",
          f"SELECT f.id, f.display_name FROM {_J} CROSS JOIN firm f WHERE f.id = j.value "
          "AND f.display_name IS NOT NULL", params=_IDS, drives="j")
# Every firm's key of one scheme (a sort of every firm by its register
# value): the scheme's index range, read once per kept ordering.
statement("overlay.read.idents_of_scheme", "SELECT firm, key FROM firm_ident WHERE scheme = ?",
          params=(1,), scans={"firm_ident": "every key of a scheme, once per kept ordering of "
                                            "every firm (an index range)"})
statement("overlay.read.all_display_names",
          "SELECT id, display_name FROM firm WHERE display_name IS NOT NULL",
          scans={"firm": "Harvey's own names (a few), once per kept ordering of every firm"})
# Contacts by a value with no index of its own: the table read once,
# returning the few that match (a status, a best trust); by source and by
# person through their indexes.
statement("overlay.read.contacts.by_status", "SELECT id FROM contact WHERE status = ?",
          params=(1,), scans={"contact": "a status other than a phone's: the table read once, "
                                         "the few that match returned"})
statement("overlay.read.contacts.by_trust", "SELECT id FROM contact WHERE best_trust = ?",
          params=(1,), scans={"contact": "a trust below confirmed: the table read once, the "
                                         "few that match returned"})
statement("overlay.read.contacts.by_source",
          "SELECT DISTINCT contact FROM link_evidence WHERE source = ?", params=(1,),
          scans={"link_evidence": "one source's evidence: its index range"})
statement("overlay.read.contacts.with_person",
          "SELECT DISTINCT contact FROM link INDEXED BY link_person WHERE person != 0")
# A Contacts page's summaries, per contact (counts and the best link done
# in SQLite: a shared phone can reach thousands of firms).
statement("overlay.read.contact_summary",
          f"SELECT l.contact, COUNT(DISTINCT NULLIF(l.person, 0)), group_concat(DISTINCT s.name) "
          f"FROM {_J} CROSS JOIN link l CROSS JOIN link_evidence e CROSS JOIN source s "
          "WHERE l.contact = j.value AND e.firm = l.firm AND e.contact = l.contact "
          "AND s.id = e.source GROUP BY l.contact", params=_IDS, drives="j", bounded=True)
statement("overlay.read.contact_first",
          "SELECT l.firm, l.person FROM link l WHERE l.contact = ? "
          "ORDER BY l.trust, l.first_seen, l.firm LIMIT 1", params=(1,), bounded=True)
statement("overlay.read.person_firms", "SELECT DISTINCT firm FROM person")
statement("overlay.read.unkeyed",
          "SELECT f.id FROM firm f WHERE f.live = 0 AND NOT EXISTS (SELECT 1 FROM firm_ident fi "
          "WHERE fi.firm = f.id AND fi.scheme = ?)", params=(1,),
          scans={"firm": "the firms with no key of a register's scheme on their own record (a "
                         "few hundred): every firm probed once per kept set"})
# Live firms with no domain of their own whose merged record has one (the
# domain is a key, kept on one record only): a few.
statement("overlay.read.domain_by_member",
          "SELECT COUNT(DISTINCT m.live) FROM firm m INDEXED BY firm_merged WHERE m.live != 0 "
          "AND m.domain IS NOT NULL AND (SELECT s.domain FROM firm s WHERE s.id = m.live) IS NULL")
statement("overlay.read.identity", "SELECT n FROM revision WHERE name = 'identity'")
# The register signals' kept sets (overlay/bitmaps.py): a signal's two parts;
# where a walk starts (the highest firm id, how many firm rows there are);
# whether a firm was removed since one (the rows there are now, less those
# added after it, must be what it counted: a removal takes one off it); the
# records given an identifier since it started, and how many identifiers
# (up to a cap); and one row of a module's (its load mark, where and when
# its walk started).
statement("overlay.read.kept",
          "SELECT part, mark, top, records, at, bits FROM register_set "
          "WHERE signal = ? AND module = ?", params=(1, 1))
statement("overlay.read.kept_start",
          "SELECT (SELECT MAX(id) FROM firm), "
          "(SELECT COALESCE(SUM(n), 0) FROM tally WHERE name IN ('firms', 'firms_merged'))")
statement("overlay.read.kept_records",
          "SELECT (SELECT COALESCE(SUM(n), 0) FROM tally WHERE name IN ('firms', 'firms_merged')) "
          "- (SELECT COUNT(*) FROM firm WHERE id > ?)", params=(1,))
statement("overlay.read.idents_since",
          "SELECT DISTINCT firm FROM firm_ident INDEXED BY firm_ident_since WHERE since >= ?",
          params=(1_700_000_000,))
statement("overlay.read.idents_since_count",
          "SELECT COUNT(*) FROM (SELECT 1 FROM firm_ident INDEXED BY firm_ident_since "
          "WHERE since >= ? LIMIT ?)", params=(1_700_000_000, 10_001))
statement("overlay.read.kept_of_module",
          "SELECT mark, top, records, at FROM register_set WHERE module = ? LIMIT 1", params=(1,),
          scans={"register_set": "a row per register signal and part (a few dozen)"})
statement("overlay.read.marks",
          "SELECT (SELECT MAX(id) FROM firm), (SELECT MAX(id) FROM search), "
          "(SELECT MAX(id) FROM obs), (SELECT MAX(id) FROM person), "
          "(SELECT MAX(id) FROM contact), (SELECT MAX(id) FROM merge)")

# The contact finder's reads of one company: which of these records (or
# persons) a module's capability searched since a cutoff (a search that
# looked for a word: its bit in looked_for), and each link's evidence with
# its capability.
statement("overlay.read.searched_targets",
          f"SELECT DISTINCT s.target FROM {_J} CROSS JOIN search s WHERE s.is_person = ? "
          "AND s.target = j.value AND s.module = ? AND s.capability = ? AND s.at >= ? "
          "AND s.outcome != 0 AND (s.looked_for & ?) = ?",
          params=("[1, 2]", 0, 1, 1, 0, 0, 0), drives="j")
statement("overlay.read.evidence_caps",
          f"SELECT e.contact, s.name, c.name FROM {_J} CROSS JOIN link_evidence e "
          "CROSS JOIN source s LEFT JOIN capability c ON c.id = e.capability "
          "WHERE e.firm = json_extract(j.value, '$[0]') AND e.contact = json_extract(j.value, '$[1]') "
          "AND s.id = e.source", params=("[[1, 2]]",), drives="j")

# Duplicates: the review list (a few hundred pairs at most), the merges in
# force, and what a side shows (the keys Harvey found, its places).
statement("overlay.read.candidates",
          "SELECT a, b, score, reasons, status, at, decided_at FROM match_candidate "
          "WHERE status = ? ORDER BY score DESC, at DESC LIMIT ?",
          params=("pending", 100), bounded=True)
statement("overlay.read.candidates_open",
          "SELECT a, b, score, reasons, status FROM match_candidate "
          "WHERE status IN ('pending', 'separate')")
statement("overlay.read.candidates_all", "SELECT a, b, status FROM match_candidate")
statement("overlay.read.merges",
          "SELECT id, survivor, merged, score, reasons, decided_by, at, grp FROM merge "
          "WHERE undone_at IS NULL ORDER BY id DESC LIMIT ?", params=(50,))
statement("overlay.read.undone_between",
          "SELECT 1 FROM merge WHERE undone_at IS NOT NULL AND ((survivor = ?1 AND merged = ?2) "
          "OR (survivor = ?2 AND merged = ?1)) LIMIT 1", params=(1, 2))
statement("overlay.read.keys_of",
          f"SELECT k.firm, k.kind, k.value FROM {_J} CROSS JOIN firm_key k WHERE k.firm = j.value",
          params=_IDS, drives="j")
statement("overlay.read.places_count",
          f"SELECT l.firm, COUNT(*) FROM {_J} CROSS JOIN locations l WHERE l.firm = j.value "
          "GROUP BY l.firm", params=_IDS, drives="j", bounded=True)
# Firms with a domain of their own (a few thousand: the site index's part
# with a domain), and when a module's capability last searched each record.
statement("overlay.read.with_domain_ids",
          "SELECT id FROM firm INDEXED BY firm_with_site WHERE live = 0 "
          "AND (website IS NOT NULL OR domain IS NOT NULL) AND domain IS NOT NULL",
          scans={"firm": "the site index's firms (website or domain): ~11k of 1.8M"})
statement("overlay.read.last_searched",
          f"SELECT s.target, MAX(s.at) FROM {_J} CROSS JOIN search s WHERE s.is_person = 0 "
          "AND s.target = j.value AND s.module = ? AND s.capability = ? GROUP BY s.target",
          params=("[1, 2]", 1, 1), drives="j", bounded=True)
# Every firm by its kept place in the directory register's name and place
# orders (overlay/orders.py), a page at a time or walked whole (a set too
# big to sort, filtered as it is read), and those positions by firm.
for _col, _name in (("name_order", "name"), ("place_order", "place")):
    statement(f"overlay.firms.by_{_name}",
              f"SELECT id FROM firm WHERE live = 0 ORDER BY {_col} DESC, id DESC "
              "LIMIT ? OFFSET ?", params=_PAGE)
    statement(f"overlay.firms.by_{_name}.asc",
              f"SELECT id FROM firm WHERE live = 0 ORDER BY {_col} ASC, id ASC "
              "LIMIT ? OFFSET ?", params=_PAGE)
    statement(f"overlay.read.walk_by_{_name}",
              f"SELECT id FROM firm WHERE live = 0 ORDER BY {_col} DESC, id DESC",
              scans={"firm": "a set of most firms in this order, read in it and filtered "
                             "(no sort of a million rows)"})
    statement(f"overlay.read.walk_by_{_name}.asc",
              f"SELECT id FROM firm WHERE live = 0 ORDER BY {_col} ASC, id ASC",
              scans={"firm": "a set of most firms in this order, read in it and filtered "
                             "until the page is full"})
statement("overlay.read.orders",
          f"SELECT f.id, f.name_order, f.place_order FROM {_J} CROSS JOIN firm f "
          "WHERE f.id = j.value", params=_IDS, drives="j")
# (The Email module's prospects: thousands at most, every one, sorted.)
statement("overlay.read.prospect_rows",
          "SELECT * FROM prospects ORDER BY created_at DESC, rowid DESC", bounded=True,
          scans={"prospects": "the Email module's prospects (thousands at most), every one"})

# ── The contact finder's website check (harvey/finder.py, site_match.py) ──
# Found websites nothing checked yet: a small partial index (schema 29).
_SITE_CHECK = ("live = 0 AND website_source IS NOT NULL AND website_basis IS NULL "
               "AND website_trust > 0")
statement("overlay.site_check.ids",
          f"SELECT id FROM firm INDEXED BY firm_site_check WHERE {_SITE_CHECK} "
          "ORDER BY id LIMIT ?", params=(50,))
statement("overlay.site_check.count",
          f"SELECT website_trust, COUNT(*) FROM firm INDEXED BY firm_site_check "
          f"WHERE {_SITE_CHECK} GROUP BY website_trust")
statement("overlay.site_check.rows",
          f"SELECT f.id, f.website, f.website_trust FROM {_J} CROSS JOIN firm f "
          "WHERE f.id = j.value", params=_IDS, drives="j")
# The classifier's calls since midnight (its day cap), by its usage rows.
statement("overlay.classify.calls_today",
          "SELECT COUNT(*) FROM usage_events WHERE created_at >= ? AND task LIKE ?",
          params=("2026-01-01 00:00:00", "classify.%"))
# What its calls cost since a time (its USD caps): the classifier's own rows
# only (Harvey's own model records its usage under its own source).
statement("overlay.classify.spend_since",
          "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE created_at >= ? "
          "AND task LIKE 'classify.%' AND source LIKE 'classify%'",
          params=("2026-01-01 00:00:00",))
# Its calls, errors, tokens, spend and unpriced calls per task since a time.
statement("overlay.classify.by_task",
          "SELECT task, COUNT(*), SUM(is_error), SUM(input_tokens), SUM(output_tokens), "
          "SUM(cost_usd), SUM(source = 'classify:unpriced' AND is_error = 0) "
          "FROM usage_events WHERE created_at >= ? AND task LIKE 'classify.%' GROUP BY task",
          params=("2026-01-01 00:00:00",), bounded=True)
# The size of its answered calls per task and model since a time (an
# estimate's typical and high request).
statement("overlay.classify.sizes",
          "SELECT task, model, COUNT(*), AVG(input_tokens), MAX(input_tokens), "
          "AVG(output_tokens), MAX(output_tokens) FROM usage_events WHERE created_at >= ? "
          "AND task LIKE 'classify.%' AND is_error = 0 AND input_tokens > 0 GROUP BY task, model",
          params=("2026-01-01 00:00:00",), bounded=True)
# How the last answered call of a model was priced (a reply carrying its cost).
statement("overlay.classify.last_source",
          "SELECT source FROM usage_events WHERE created_at >= ? AND task LIKE 'classify.%' "
          "AND model = ? AND is_error = 0 AND input_tokens > 0 "
          "ORDER BY created_at DESC LIMIT 1",
          params=("2026-01-01 00:00:00", "m"))

# ── Contacts the classifier verified (harvey/contact_check.py, schema 30) ──
# One contact's verdicts, newest first (the contact's panel).
statement("overlay.contact_check.of_contact",
          "SELECT firm, verdict, at, profile, model, p, note, batch, undone_at "
          "FROM contact_check WHERE contact = ? ORDER BY at DESC LIMIT 50", params=(1,))
# Verdicts still standing of one kind, newest first (the unsure ones wait
# for a person's review).
statement("overlay.contact_check.open",
          "SELECT firm, contact, verdict, at, profile, model, p, note, batch "
          "FROM contact_check INDEXED BY contact_check_open WHERE verdict = ? "
          "AND undone_at IS NULL ORDER BY at DESC LIMIT ?", params=(2, 50))
# A verification run's verdicts, counted.
statement("overlay.contact_check.batch",
          "SELECT verdict, COUNT(*), SUM(undone_at IS NOT NULL) FROM contact_check "
          "WHERE batch = ? GROUP BY verdict", params=("b",))
# Whether a contact is in a campaign, by its id or its address (a no never
# lowers a campaign member's link).
statement("overlay.contact_check.in_campaign",
          "SELECT EXISTS (SELECT 1 FROM campaign_member WHERE contact = ?) "
          "OR EXISTS (SELECT 1 FROM campaign_member WHERE email = ?)", params=(1, "a@example.com"))
# The contacts a verification run asked about at one firm already (a job
# resumed asks only the rest).
statement("overlay.contact_check.done",
          "SELECT contact FROM contact_check WHERE batch = ? AND firm = ?", params=("b", 1))

# ── The agent's views (harvey/agent/views.py) and its chat (schemas 31, 32) ──
# Counts only: the Email module's prospects, campaign members, the outbox
# (no subject or body) and conversations (no thread) by status.
statement("overlay.agent.prospects", "SELECT status, COUNT(*) FROM prospects GROUP BY status",
          bounded=True)
statement("overlay.agent.members",
          "SELECT campaign, status, COUNT(*) FROM campaign_member GROUP BY campaign, status",
          bounded=True,
          scans={"campaign_member": "every campaign's members counted by status: its "
                                    "status index read once, when the agent asks"})
statement("overlay.agent.outbox",
          "SELECT campaign_id, kind, status, COUNT(*) FROM outbox "
          "GROUP BY campaign_id, kind, status", bounded=True)
statement("overlay.agent.conversations",
          "SELECT status, stage, COUNT(*) FROM conversations GROUP BY status, stage",
          bounded=True)
statement("overlay.agent.campaigns",
          "SELECT id, name, status, origin, rolling, audience_id, started_at, ended_at, "
          "updated_at FROM campaigns ORDER BY rowid DESC LIMIT ?", params=(50,))
# Contacts by kind, line, trust and status (with how many are on the
# do-not-contact list).
statement("overlay.agent.contacts",
          "SELECT k.kind, c.line_type, c.best_trust, c.status, COUNT(*), "
          "SUM(c.do_not_contact != 0) FROM contact c CROSS JOIN contact_kinds k "
          "WHERE k.id = c.kind GROUP BY k.kind, c.line_type, c.best_trust, c.status",
          drives="c", bounded=True,
          scans={"contact": "every contact counted by kind, trust and status, when the "
                            "agent asks: the table read once"})
# A set of firms counted (the counts view, coverage): websites, contacts by
# kind, sites read, searched, people named. Reader.FIRM_COUNTS names them.
statement("overlay.agent.firm_counts",
          "SELECT COUNT(*), SUM(f.website IS NOT NULL OR f.domain IS NOT NULL), "
          "SUM(f.n_contacts = 0), SUM(f.n_email > 0), SUM(f.n_phone > 0), SUM(f.n_mobile > 0), "
          "SUM(f.n_email > 0 OR f.n_mobile > 0), SUM(f.site_read_at IS NOT NULL), "
          "SUM(f.finder_at IS NOT NULL), SUM(f.last_search_at IS NOT NULL), "
          "SUM(f.n_people > 0) "
          f"FROM {_J} CROSS JOIN firm f WHERE f.id = j.value", params=_IDS, drives="j")
# Every firm with an email or a mobile (no tally keeps the two together):
# the firms with a contact, by their index.
statement("overlay.agent.firms_reached",
          "SELECT COUNT(*) FROM firm INDEXED BY firm_with_contact WHERE live = 0 "
          "AND n_contacts > 0 AND (n_email > 0 OR n_mobile > 0)",
          scans={"firm": "the firms with a contact (its partial index), counted when the "
                         "agent asks"})
# The searches made since a time, by module, capability and outcome.
statement("overlay.agent.searches",
          "SELECT m.name, c.name, s.outcome, COUNT(*), COALESCE(SUM(s.cost), 0), MAX(s.at) "
          "FROM search s INDEXED BY search_at CROSS JOIN module m CROSS JOIN capability c "
          "WHERE s.at >= ? AND m.id = s.module AND c.id = s.capability "
          "GROUP BY m.name, c.name, s.outcome", params=(1_700_000_000,), drives="s",
          bounded=True,
          scans={"search": "the searches of the last days, by their time index"})
# A discovery lane's searches since a time (phrase_yield: per phrase, what
# each found and cost), by the lane's index; job errors are never read.
statement("overlay.agent.discover_jobs",
          "SELECT status, params_json, result_json, progress_json, actual_cost FROM jobs "
          "WHERE lane = ? AND status IN ('done', 'failed', 'cancelled', 'paused') "
          "AND created_at >= ? AND kind = 'discover'", params=("serper", "2026-01-01 00:00:00"))
# Harvey's own model calls since a time, by agent and task.
statement("overlay.agent.usage",
          "SELECT agent, task, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
          "SUM(num_turns) FROM usage_events WHERE created_at >= ? GROUP BY agent, task",
          params=("2026-01-01 00:00:00",), bounded=True)
# What Harvey's own API-key calls cost since a time (brain.usd_per_day /
# usd_per_month, harvey/brain_spend.py): their own rows only, never the
# classifier's or a subscription's notional cost.
# A cost counted at a turn's bound (api_key_estimate) is spend too; an
# unpriced Codex call's tokens (api_key_unpriced) are priced by the reader.
statement("overlay.brain.spend_since",
          "SELECT COALESCE(SUM(CASE WHEN source != 'api_key_unpriced' THEN cost_usd END), 0), "
          "COALESCE(SUM(CASE WHEN source = 'api_key_unpriced' THEN input_tokens "
          "+ cache_read_tokens + cache_creation_tokens END), 0), "
          "COALESCE(SUM(CASE WHEN source = 'api_key_unpriced' THEN output_tokens END), 0) "
          "FROM usage_events WHERE created_at >= ? "
          "AND source IN ('api_key', 'api_key_estimate', 'api_key_unpriced')",
          params=("2026-01-01 00:00:00",))
# The most an agent's API-key turn cost since a time (a wake's or a task's
# worst case).
statement("overlay.brain.agent_turn_cost",
          "SELECT MAX(cost_usd / num_turns) FROM usage_events WHERE created_at >= ? "
          "AND source = 'api_key' AND agent = 'agent' AND provider = ? AND num_turns > 0",
          params=("2026-01-01 00:00:00", "claude"))
# The chats' messages: the newest (of a chat, or of every chat), a turn's,
# and the tool calls of some turns.
_MESSAGE = ("SELECT id, turn, role, text, status, chat, created_at, updated_at, tokens, context, "
            "wake FROM agent_message ")
statement("overlay.agent.messages", _MESSAGE + "ORDER BY id DESC LIMIT ?", params=(50,))
statement("overlay.agent.messages_before", _MESSAGE + "WHERE id < ? ORDER BY id DESC LIMIT ?",
          params=(100, 50))
statement("overlay.agent.chat_messages",
          _MESSAGE + "INDEXED BY agent_message_chat WHERE chat = ? ORDER BY id DESC LIMIT ?",
          params=("c", 50))
statement("overlay.agent.chat_messages_before",
          _MESSAGE + "INDEXED BY agent_message_chat WHERE chat = ? AND id < ? "
          "ORDER BY id DESC LIMIT ?", params=("c", 100, 50))
statement("overlay.agent.turn", _MESSAGE + "WHERE turn = ? ORDER BY id", params=("t",))
# How long the chat is now: the context its newest answer with a result
# was given.
statement("overlay.agent.chat_context",
          "SELECT context FROM agent_message INDEXED BY agent_message_chat "
          "WHERE chat = ? AND role = 'agent' AND context > 0 ORDER BY id DESC LIMIT 1",
          params=("c",))
statement("overlay.agent.set_status",
          "UPDATE agent_message SET status = ?, updated_at = ? WHERE turn = ? AND role = 'user'",
          params=("running", 1, "t"))
# (Only into a chat on file: an answer that ends after its chat was
# deleted leaves nothing behind.)
statement("overlay.agent.add",
          "INSERT INTO agent_message (turn, role, text, status, chat, created_at, updated_at, "
          "tokens, context) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? "
          "WHERE EXISTS (SELECT 1 FROM agent_chat WHERE id = ?)",
          params=("t", "user", "", "running", "c", 1, 1, 0, 0, "c"))
statement("overlay.agent.calls",
          "SELECT c.id, c.turn, c.tool, c.arguments, c.result, c.ms, c.outcome, c.at "
          f"FROM {_J} CROSS JOIN agent_tool_call c WHERE c.turn = j.value ORDER BY c.id",
          params=('["t"]',), drives="j", bounded=True)
# A turn's tool calls after the last one seen (the stream of an answer).
statement("overlay.agent.calls_after",
          "SELECT id, tool, arguments, result, ms, outcome FROM agent_tool_call "
          "WHERE turn = ? AND id > ? ORDER BY id", params=("t", 0))
# The chats (schema 32): newest activity first (all, or those whose title
# holds some words), one, a new one, its title, the message it answers
# now, and its removal with its messages and their tool calls (its
# usage_events rows stay: they are the plan's accounting).
_CHAT = "SELECT id, title, created_at, last_at, turn, unread FROM agent_chat "
statement("overlay.agent.chats",
          _CHAT + "INDEXED BY agent_chat_recent ORDER BY last_at DESC LIMIT ?", params=(50,))
statement("overlay.agent.chats_like",
          _CHAT + "INDEXED BY agent_chat_recent WHERE title LIKE ? ESCAPE '\\' "
          "ORDER BY last_at DESC LIMIT ?", params=("%x%", 50),
          scans={"agent_chat": "the chats walked newest first for a title search: one row a "
                               "conversation the user keeps"})
statement("overlay.agent.chat", _CHAT + "WHERE id = ?", params=("c",))
statement("overlay.agent.new_chat",
          "INSERT INTO agent_chat (id, title, created_at, last_at) VALUES (?, ?, ?, ?)",
          params=("c", "t", 1, 1))
statement("overlay.agent.rename_chat", "UPDATE agent_chat SET title = ? WHERE id = ?",
          params=("t", "c"))
statement("overlay.agent.chat_turn", "SELECT turn FROM agent_chat WHERE id = ?", params=("c",))
statement("overlay.agent.set_chat_turn",
          "UPDATE agent_chat SET turn = ?, last_at = ? WHERE id = ?", params=("t", 1, "c"))
statement("overlay.agent.delete_chat_calls",
          "DELETE FROM agent_tool_call INDEXED BY agent_tool_call_turn WHERE turn IN "
          "(SELECT turn FROM agent_message INDEXED BY agent_message_chat WHERE chat = ?)",
          params=("c",))
statement("overlay.agent.delete_chat_messages",
          "DELETE FROM agent_message WHERE chat = ?", params=("c",))
# A deleted chat's sets keep their row (an action's record names it), not
# their ids: no action takes a set of a chat that is gone.
statement("overlay.agent.delete_chat_set_members",
          "DELETE FROM agent_set_member WHERE set_id IN "
          "(SELECT id FROM agent_set INDEXED BY agent_set_chat WHERE chat = ?)",
          params=("c",))
statement("overlay.agent.delete_chat", "DELETE FROM agent_chat WHERE id = ?", params=("c",))
# The chats some Harvey process has a live process for (their leases).
statement("overlay.agent.chat_leases",
          "SELECT name, owner FROM worker_lease WHERE name LIKE 'agent_chat:%' "
          "AND heartbeat_at >= ?", params=(1.0,))
statement("overlay.agent.add_call",
          "INSERT INTO agent_tool_call (turn, tool, arguments, result, ms, outcome, at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?)", params=("t", "schema", "{}", "", 0, "ok", 1))

# ── The agent's plans, sets and actions (schema 33, harvey/agent/plans.py) ──
_PLAN = ("SELECT id, chat, turn, goal, steps, budget_usd, expiry_hours, status, created_at, "
         "decided_at, expires_at, closed_at, decided_by, closed_by FROM agent_plan ")
statement("overlay.agent.plan_add",
          "INSERT INTO agent_plan (chat, turn, goal, steps, budget_usd, expiry_hours, status, "
          "created_at) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)",
          params=("c", "t", "goal", "[]", 1.0, 72, 1))
statement("overlay.agent.plan", _PLAN + "WHERE id = ?", params=(1,))
statement("overlay.agent.plans_chat",
          _PLAN + "INDEXED BY agent_plan_chat WHERE chat = ? ORDER BY id DESC LIMIT ?",
          params=("c", 50))
statement("overlay.agent.plans_chat_status",
          _PLAN + "INDEXED BY agent_plan_chat WHERE chat = ? AND status = ? "
          "ORDER BY id DESC LIMIT ?", params=("c", "approved", 50))
statement("overlay.agent.plans", _PLAN + "ORDER BY id DESC LIMIT ?", params=(50,))
statement("overlay.agent.plans_status",
          _PLAN + "INDEXED BY agent_plan_status WHERE status = ? ORDER BY id DESC LIMIT ?",
          params=("approved", 50))
# An approved plan past its time is expired, checked whenever a plan is read
# or acted under (nothing runs on a clock).
statement("overlay.agent.plan_expire",
          "UPDATE agent_plan SET status = 'expired', closed_at = expires_at, "
          "closed_by = 'expiry' WHERE status = 'approved' AND expires_at <= ?", params=(1,))
statement("overlay.agent.plan_decide",
          "UPDATE agent_plan SET status = ?, decided_at = ?, decided_by = ?, expires_at = ? "
          "WHERE id = ? AND status = 'proposed'", params=("approved", 1, "dashboard", 2, 1))
# A deleted chat's open plans: nothing can be done under them any more (no
# chat answers there), so a proposed one is rejected and an approved one
# done, its runs going on to their end under their caps.
statement("overlay.agent.plans_chat_deleted",
          "UPDATE agent_plan INDEXED BY agent_plan_chat "
          "SET decided_at = CASE status WHEN 'proposed' THEN ? ELSE decided_at END, "
          "decided_by = CASE status WHEN 'proposed' THEN 'chat deleted' ELSE decided_by END, "
          "closed_at = ?, closed_by = 'chat deleted', "
          "status = CASE status WHEN 'proposed' THEN 'rejected' ELSE 'done' END "
          "WHERE chat = ? AND status IN ('proposed', 'approved')", params=(1, 1, "c"))
statement("overlay.agent.plan_close",
          "UPDATE agent_plan SET status = ?, closed_at = ?, closed_by = ? "
          "WHERE id = ? AND status = 'approved'", params=("done", 1, "dashboard", 1))
# A plan's runs: each action that holds or held money, with its run's cap
# now, whether any of its jobs is still to finish, and what they spent.
statement("overlay.agent.plan_runs",
          "SELECT a.id, a.tool, a.batch, a.cap_usd, a.classify_cap_usd, a.outcome, a.at, "
          "b.id IS NOT NULL, b.max_spend, json_extract(b.params_json, '$.classify_max_spend'), "
          "b.label, "
          "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id = a.batch "
          "AND j.status IN ('queued', 'running', 'paused')), "
          "(SELECT COALESCE(SUM(j.actual_cost), 0) FROM jobs j WHERE j.batch_id = a.batch) "
          "FROM agent_action a INDEXED BY agent_action_plan "
          "LEFT JOIN job_batches b ON b.id = a.batch "
          "WHERE a.plan = ? AND a.outcome IN ('reserved', 'ok') ORDER BY a.id", params=(1,),
          drives="a")
# A plan's own model runs (its wakes, by their dedupe key, and its tasks'
# jobs): what each spent and holds, for the plan's money in API-key mode
# (harvey/brain_spend.py), and a run's hold of the plan's budget.
statement("overlay.agent.plan_model_jobs",
          "SELECT j.id, j.status, j.actual_cost, j.reserved FROM jobs j INDEXED BY idx_jobs_lane "
          "WHERE j.lane = 'agent' AND j.dedupe_key = ? "
          "UNION ALL SELECT j.id, j.status, j.actual_cost, j.reserved "
          "FROM agent_task t INDEXED BY agent_task_plan JOIN jobs j ON j.id = t.job "
          "WHERE t.plan = ?", params=("agent_wake:1", 1))
statement("overlay.agent.run_hold", "UPDATE jobs SET reserved = ? WHERE id = ?",
          params=(0.0, "j"))
statement("overlay.agent.action_add",
          "INSERT INTO agent_action (plan, chat, turn, tool, arguments, batch, cap_usd, "
          "classify_cap_usd, outcome, why, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          params=(1, "c", "t", "find_contacts", "{}", None, 0.0, 0.0, "reserved", "", 1))
statement("overlay.agent.action_settle",
          "UPDATE agent_action SET batch = ?, outcome = ?, why = ? WHERE id = ?",
          params=("b", "ok", "", 1))
statement("overlay.agent.actions_plan",
          "SELECT id, tool, arguments, batch, cap_usd, classify_cap_usd, outcome, why, at "
          "FROM agent_action INDEXED BY agent_action_plan WHERE plan = ? "
          "ORDER BY id DESC LIMIT ?", params=(1, 50))
statement("overlay.agent.set_add",
          "INSERT INTO agent_set (chat, name, kind, of, definition, n, created_at, frozen_at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          params=("c", "n", "filter", "firms", "{}", 0, 1, 1))
statement("overlay.agent.set",
          "SELECT id, chat, name, kind, of, definition, n, created_at, frozen_at, refreshed_at "
          "FROM agent_set WHERE id = ?", params=(1,))
statement("overlay.agent.sets_chat",
          "SELECT id, name, kind, of, n, created_at, frozen_at, refreshed_at "
          "FROM agent_set INDEXED BY agent_set_chat "
          "WHERE chat = ? ORDER BY id DESC LIMIT ?", params=("c", 50))
# A set's ids (schema 39): written once when it is saved or refreshed (the
# old ones cleared first, in the same transaction), read by its primary key
# by an action; its count and when, kept on the set.
statement("overlay.agent.set_members",
          "SELECT member FROM agent_set_member WHERE set_id = ?", params=(1,))
statement("overlay.agent.set_member_add",
          "INSERT OR IGNORE INTO agent_set_member (set_id, member) VALUES (?, ?)", params=(1, 1))
statement("overlay.agent.set_members_clear",
          "DELETE FROM agent_set_member WHERE set_id = ?", params=(1,))
# (frozen_at stays the first freeze's; a refresh sets refreshed_at.)
statement("overlay.agent.set_frozen",
          "UPDATE agent_set SET n = ?, frozen_at = COALESCE(frozen_at, ?), refreshed_at = ? "
          "WHERE id = ?", params=(1, 1, None, 1))
# The contact ids of a piece of a set still on file (a removed one is
# skipped); a firm id's live firm is overlay.read.live_of.
statement("overlay.agent.contacts_on_file",
          f"SELECT c.id FROM {_J} CROSS JOIN contact c WHERE c.id = j.value",
          params=_IDS, drives="j")

# ── What wakes the agent, and its memory (schema 34, harvey/agent/wakes.py,
# memory.py) ──
_EVENT = ("SELECT id, plan, kind, ref, data, due_at, created_at, seen_at, wake, cancelled_at "
          "FROM agent_event ")
# (Once per plan, kind and ref: a run that finishes is one event however
# often it is seen finished.)
statement("overlay.agent.event_add",
          "INSERT OR IGNORE INTO agent_event (plan, kind, ref, data, due_at, created_at) "
          "VALUES (?, ?, ?, ?, ?, ?)", params=(1, "run_done", "b@t", "{}", 1, 1))
statement("overlay.agent.event", _EVENT + "WHERE id = ?", params=(1,))
# A plan's events not seen yet that are due (what a wake reads), and those
# still to come or to read (the plan card, plan_status).
statement("overlay.agent.events_due",
          _EVENT + "INDEXED BY agent_event_ref WHERE plan = ? AND seen_at IS NULL "
          "AND cancelled_at IS NULL AND due_at <= ? ORDER BY id LIMIT ?", params=(1, 1, 50),
          bounded=True)
statement("overlay.agent.events_open",
          _EVENT + "INDEXED BY agent_event_ref WHERE plan = ? AND seen_at IS NULL "
          "AND cancelled_at IS NULL ORDER BY due_at, id LIMIT ?", params=(1, 50), bounded=True)
statement("overlay.agent.due_plans",
          "SELECT DISTINCT plan FROM agent_event INDEXED BY agent_event_open "
          "WHERE seen_at IS NULL AND cancelled_at IS NULL AND due_at <= ?", params=(1,),
          bounded=True)
statement("overlay.agent.events_seen",
          "UPDATE agent_event SET seen_at = ?, wake = ? "
          "WHERE id IN (SELECT value FROM json_each(?)) AND seen_at IS NULL",
          params=(1, "t", "[1, 2]"))
statement("overlay.agent.event_cancel",
          "UPDATE agent_event SET cancelled_at = ? WHERE id = ? AND seen_at IS NULL "
          "AND cancelled_at IS NULL", params=(1, 1))
statement("overlay.agent.events_cancel_plan",
          "UPDATE agent_event INDEXED BY agent_event_ref SET cancelled_at = ? "
          "WHERE plan = ? AND seen_at IS NULL AND cancelled_at IS NULL", params=(1, 1))
statement("overlay.agent.scheduled_open",
          "SELECT COUNT(*) FROM agent_event INDEXED BY agent_event_ref WHERE plan = ? "
          "AND kind = 'scheduled' AND seen_at IS NULL AND cancelled_at IS NULL", params=(1,))
# The plans a run belongs to, and whether all its jobs have finished.
statement("overlay.agent.batch_plans",
          "SELECT DISTINCT plan FROM agent_action INDEXED BY agent_action_batch "
          "WHERE batch = ? AND outcome = 'ok' AND plan IS NOT NULL", params=("b",))
statement("overlay.agent.batch_done",
          "SELECT COUNT(*), COALESCE(SUM(status IN ('queued', 'running', 'paused')), 0), "
          "MAX(finished_at) FROM jobs WHERE batch_id = ?", params=("b",))
statement("overlay.agent.batch_statuses",
          "SELECT status, COUNT(*) FROM jobs WHERE batch_id = ? GROUP BY status", params=("b",))
statement("overlay.agent.plan_batches",
          "SELECT a.batch, a.tool, b.label FROM agent_action a INDEXED BY agent_action_plan "
          "LEFT JOIN job_batches b ON b.id = a.batch "
          "WHERE a.plan = ? AND a.outcome = 'ok' AND a.batch IS NOT NULL ORDER BY a.id",
          params=(1,), drives="a")
# A plan's wake job still to finish, and how many of its wakes ran today.
statement("overlay.agent.wake_job",
          "SELECT id, status, not_before FROM jobs WHERE dedupe_key = ? "
          "AND status IN ('queued', 'running', 'paused')", params=("agent_wake:1",))
statement("overlay.agent.wakes_ran",
          "SELECT COUNT(*) FROM jobs INDEXED BY idx_jobs_lane WHERE lane = ? "
          "AND status IN ('done', 'failed') AND created_at >= datetime('now', '-1 day') "
          "AND dedupe_key = ?", params=("agent", "agent_wake:1"))
# The newest of a plan's wake jobs cancelled (by the user on the Queue page,
# or with its plan), and the events it would have read, set aside with it.
statement("overlay.agent.wake_cancelled_last",
          "SELECT MAX(finished_at) FROM jobs INDEXED BY idx_jobs_lane WHERE lane = ? "
          "AND status = 'cancelled' AND dedupe_key = ?", params=("agent", "agent_wake:1"))
statement("overlay.agent.events_dismiss",
          "UPDATE agent_event INDEXED BY agent_event_ref SET cancelled_at = ? "
          "WHERE plan = ? AND seen_at IS NULL AND cancelled_at IS NULL AND created_at <= ? "
          "AND due_at <= ?", params=(1, 1, 1, 1))
statement("overlay.agent.wake_failed_since",
          "SELECT COUNT(*) FROM jobs INDEXED BY idx_jobs_lane WHERE lane = ? AND status = 'failed' "
          "AND dedupe_key = ? AND finished_at >= datetime('now', ?)",
          params=("agent", "agent_wake:1", "-900 seconds"))
# A wake's answer in its plan's chat (only into a chat on file), the chat's
# wake answers the user hasn't opened, and the message the chat answers.
statement("overlay.agent.add_wake",
          "INSERT INTO agent_message (turn, role, text, status, chat, created_at, updated_at, "
          "tokens, context, wake) SELECT ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ? "
          "WHERE EXISTS (SELECT 1 FROM agent_chat WHERE id = ?)",
          params=("t", "", "ok", "c", 1, 1, 0, 0, "{}", "c"))
statement("overlay.agent.chat_unread_add",
          "UPDATE agent_chat SET unread = unread + 1, last_at = ? WHERE id = ?", params=(1, "c"))
statement("overlay.agent.chat_read",
          "UPDATE agent_chat SET unread = 0 WHERE id = ? AND unread != 0", params=("c",))
statement("overlay.agent.chat_take_turn",
          "UPDATE agent_chat SET turn = ? WHERE id = ?", params=("t", "c"))
statement("overlay.agent.chat_turn_if",
          "UPDATE agent_chat SET turn = ? WHERE id = ? AND turn = ?", params=("", "c", "t"))
statement("overlay.agent.chat_last_id",
          "SELECT id FROM agent_message INDEXED BY agent_message_chat WHERE chat = ? "
          "ORDER BY id DESC LIMIT 1", params=("c",))
statement("overlay.agent.chat_wakes_after",
          _MESSAGE + "INDEXED BY agent_message_chat WHERE chat = ? AND id > ? AND wake != '' "
          "ORDER BY id LIMIT ?", params=("c", 1, 20))
# The notes and the playbooks.
_NOTE = "SELECT id, text, author, created_at, updated_at FROM agent_note "
statement("overlay.agent.notes", _NOTE + "ORDER BY id")
statement("overlay.agent.note", _NOTE + "WHERE id = ?", params=(1,))
statement("overlay.agent.note_add",
          "INSERT INTO agent_note (text, author, created_at, updated_at) VALUES (?, ?, ?, ?)",
          params=("n", "agent", 1, 1))
statement("overlay.agent.note_set",
          "UPDATE agent_note SET text = ?, author = ?, updated_at = ? WHERE id = ?",
          params=("n", "agent", 1, 1))
statement("overlay.agent.note_delete", "DELETE FROM agent_note WHERE id = ?", params=(1,))
_PLAYBOOK = "SELECT id, name, body, author, created_at, updated_at FROM agent_playbook "
statement("overlay.agent.playbooks", _PLAYBOOK + "ORDER BY name")
statement("overlay.agent.playbook", _PLAYBOOK + "WHERE name = ?", params=("n",))
statement("overlay.agent.playbook_id", _PLAYBOOK + "WHERE id = ?", params=(1,))
statement("overlay.agent.playbook_save",
          "INSERT INTO agent_playbook (name, body, author, created_at, updated_at) "
          "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET body = excluded.body, "
          "author = excluded.author, updated_at = excluded.updated_at",
          params=("n", "b", "agent", 1, 1))
statement("overlay.agent.playbook_set",
          "UPDATE agent_playbook SET name = ?, body = ?, author = ?, updated_at = ? WHERE id = ?",
          params=("n", "b", "agent", 1, 1))
statement("overlay.agent.playbook_delete", "DELETE FROM agent_playbook WHERE id = ?",
          params=(1,))

# ── The campaigns the agent made under a plan (schema 35,
# harvey/agent/outreach.py) ──
_LINK = ("SELECT campaign, plan, chat, created_at, start_requested_at, start_request "
         "FROM agent_campaign ")
statement("overlay.agent.campaign_link_add",
          "INSERT INTO agent_campaign (campaign, plan, chat, created_at) VALUES (?, ?, ?, ?)",
          params=("c", 1, "c", 1))
statement("overlay.agent.campaign_link", _LINK + "WHERE campaign = ?", params=("c",))
statement("overlay.agent.plan_campaigns",
          _LINK + "INDEXED BY agent_campaign_plan WHERE plan = ? ORDER BY created_at",
          params=(1,), bounded=True)
statement("overlay.agent.campaign_start_request",
          "UPDATE agent_campaign SET start_requested_at = ?, start_request = ? WHERE campaign = ?",
          params=(1, "{}", "c"))
# The open approved plan a campaign belongs to (what its events wake),
# read inside a campaign's own transaction too.
statement("overlay.agent.campaign_open_plan",
          "SELECT l.plan FROM agent_campaign l JOIN agent_plan p ON p.id = l.plan "
          "WHERE l.campaign = ? AND p.status = 'approved' AND p.expires_at > ?",
          params=("c", 1))
# The active campaigns of open plans (a sending rail reached: each one's
# plan is told).
statement("overlay.agent.campaigns_open_active",
          "SELECT l.campaign, l.plan FROM agent_campaign l "
          "CROSS JOIN agent_plan p CROSS JOIN campaigns c "
          "WHERE p.id = l.plan AND c.id = l.campaign "
          "AND p.status = 'approved' AND p.expires_at > ? AND c.status = 'active'",
          params=(1,), drives="l",
          scans={"agent_campaign": "the campaigns the agent made: a handful, read when a "
                                   "sending rail is reached"})
# A campaign in counts: its members by status, its cold email by status,
# its conversations by intent, stage and status, their answers by status,
# and its newest conversations' structured fields (never their text).
statement("overlay.agent.campaign_members",
          "SELECT status, COUNT(*) FROM campaign_member INDEXED BY campaign_member_status "
          "WHERE campaign = ? GROUP BY status", params=("c",))
statement("overlay.agent.campaign_sequence",
          "SELECT status, COUNT(*) FROM outbox INDEXED BY uq_outbox_campaign_step "
          "WHERE campaign_id = ? AND campaign_id != '' AND kind = 'sequence' GROUP BY status",
          params=("c",))
statement("overlay.agent.campaign_replies",
          "SELECT intent, stage, status, COUNT(*) FROM conversations "
          "INDEXED BY idx_conversations_campaign_id WHERE campaign_id = ? "
          "GROUP BY intent, stage, status", params=("c",))
statement("overlay.agent.campaign_answers",
          "SELECT o.status, COUNT(*) FROM conversations c INDEXED BY idx_conversations_campaign_id "
          "CROSS JOIN outbox o INDEXED BY idx_outbox_prospect "
          "WHERE c.campaign_id = ? AND o.prospect_id = c.prospect_id AND o.conversation_id = c.id "
          "AND o.kind = 'reply' GROUP BY o.status", params=("c",), drives="c")
statement("overlay.agent.campaign_conversations",
          "SELECT c.id, c.intent, c.stage, c.status, c.updated_at, "
          "(SELECT COUNT(*) FROM outbox o INDEXED BY idx_outbox_prospect "
          "WHERE o.prospect_id = c.prospect_id AND o.conversation_id = c.id AND o.kind = 'reply' "
          "AND o.status IN ('pending_review', 'approved')), "
          "CASE WHEN json_valid(c.thread_json) THEN json_extract(c.thread_json, '$[#-1].sender') "
          "END "
          "FROM conversations c INDEXED BY idx_conversations_campaign_id WHERE c.campaign_id = ? "
          "ORDER BY c.updated_at DESC LIMIT ?", params=("c", 50), bounded=True)
# One conversation's structured fields: its campaign, intent, stage,
# status, how many messages its thread holds (never the thread), whether
# an answer to it waits, who wrote its newest message (prospect or harvey,
# never what).
statement("overlay.agent.conversation",
          "SELECT c.id, c.prospect_id, c.campaign_id, c.intent, c.stage, c.status, "
          "json_array_length(CASE WHEN json_valid(c.thread_json) THEN c.thread_json "
          "ELSE '[]' END), "
          "(SELECT COUNT(*) FROM outbox o INDEXED BY idx_outbox_prospect "
          "WHERE o.prospect_id = c.prospect_id AND o.conversation_id = c.id AND o.kind = 'reply' "
          "AND o.status IN ('pending_review', 'approved')), "
          "CASE WHEN json_valid(c.thread_json) THEN json_extract(c.thread_json, '$[#-1].sender') "
          "END "
          "FROM conversations c WHERE c.id = ?", params=("c",))
# The thread an answer goes into: the newest email sent in it (an answer,
# else the campaign's cold email to them).
statement("overlay.agent.reply_thread",
          "SELECT subject, thread_ref, message_id FROM outbox INDEXED BY idx_outbox_prospect "
          "WHERE prospect_id = ? AND status = 'sent' AND ((kind = 'reply' AND conversation_id = ?) "
          "OR (kind = 'sequence' AND campaign_id = ?)) ORDER BY sent_at DESC LIMIT 1",
          params=("p", "c", "c"), bounded=True)
# Why a run's contact-finder jobs found nothing, by class (finder.Found.why).
statement("overlay.agent.batch_why",
          "SELECT json_extract(result_json, '$.why'), COUNT(*) FROM jobs INDEXED BY idx_jobs_batch "
          "WHERE batch_id = ? AND kind = 'find_contacts' AND json_valid(result_json) "
          "AND json_extract(result_json, '$.why') IS NOT NULL GROUP BY 1", params=("b",))
# An email contact's address, for a do-not-contact entry the agent asks for
# (never returned to it).
statement("overlay.agent.contact_value",
          "SELECT k.kind, c.value FROM contact c JOIN contact_kinds k ON k.id = c.kind "
          "WHERE c.id = ?", params=(1,))
# Every campaign's conversations by intent, and the plan each campaign the
# agent made belongs to (the campaigns() view).
statement("overlay.agent.campaign_intents",
          "SELECT campaign_id, intent, COUNT(*) FROM conversations WHERE campaign_id != '' "
          "GROUP BY campaign_id, intent", bounded=True,
          scans={"conversations": "every campaign's replies counted by intent, when the "
                                  "agent asks: one row a conversation"})
statement("overlay.agent.campaign_plans", "SELECT campaign, plan FROM agent_campaign",
          scans={"agent_campaign": "the campaigns the agent made: a handful"})

# ── The tasks the agent hands to a child under a plan (schema 36,
# harvey/agent/tasks.py) ──
_TASK = ("SELECT id, plan, chat, parent_turn, turn, profile, campaign, prompt, output, status, "
         "attempts, result_json, problem, created_at, started_at, finished_at, tokens, job "
         "FROM agent_task ")
statement("overlay.agent.task_add",
          "INSERT INTO agent_task (plan, chat, parent_turn, turn, profile, campaign, prompt, "
          "output, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
          params=(1, "c", "t", "u", "researcher", "", "p", "researcher", 1))
statement("overlay.agent.task", _TASK + "WHERE id = ?", params=(1,))
statement("overlay.agent.tasks_plan",
          _TASK + "INDEXED BY agent_task_plan WHERE plan = ? ORDER BY id DESC LIMIT ?",
          params=(1, 20))
statement("overlay.agent.tasks", _TASK + "ORDER BY id DESC LIMIT ?", params=(20,))
# A plan's tasks still to finish, and those assigned since a time (the
# caps assign_task holds).
statement("overlay.agent.tasks_open",
          "SELECT COUNT(*) FROM agent_task INDEXED BY agent_task_plan WHERE plan = ? "
          "AND status IN ('queued', 'running')", params=(1,))
statement("overlay.agent.tasks_since",
          "SELECT COUNT(*) FROM agent_task INDEXED BY agent_task_plan WHERE plan = ? "
          "AND created_at >= ?", params=(1, 1))
statement("overlay.agent.task_job", "UPDATE agent_task SET job = ? WHERE id = ?",
          params=("j", 1))
statement("overlay.agent.task_start",
          "UPDATE agent_task SET status = 'running', attempts = attempts + 1, started_at = ? "
          "WHERE id = ? AND status IN ('queued', 'running')", params=(1, 1))
# How a task ended (only one still to finish: a cancelled one stays so).
statement("overlay.agent.task_finish",
          "UPDATE agent_task SET status = ?, result_json = ?, problem = ?, finished_at = ?, "
          "tokens = tokens + ? WHERE id = ? AND status IN ('queued', 'running')",
          params=("done", "{}", "", 1, 0, 1))
# How a task ended with a result (done, invalid, failed): only while its
# plan is approved and not past its time; a task whose plan closed keeps
# nothing of its answer.
statement("overlay.agent.task_finish_open",
          "UPDATE agent_task SET status = ?, result_json = ?, problem = ?, finished_at = ?, "
          "tokens = tokens + ? WHERE id = ? AND status IN ('queued', 'running') AND EXISTS "
          "(SELECT 1 FROM agent_plan WHERE agent_plan.id = agent_task.plan "
          "AND agent_plan.status = 'approved' AND agent_plan.expires_at > ?)",
          params=("done", "{}", "", 1, 0, 1, 1))
statement("overlay.agent.task_tokens",
          "UPDATE agent_task SET tokens = tokens + ? WHERE id = ?", params=(0, 1))
statement("overlay.agent.task_cancel",
          "UPDATE agent_task SET status = 'cancelled', problem = ?, finished_at = ? "
          "WHERE id = ? AND status IN ('queued', 'running')", params=("", 1, 1))
statement("overlay.agent.tasks_open_plan",
          "SELECT id, job FROM agent_task INDEXED BY agent_task_plan WHERE plan = ? "
          "AND status IN ('queued', 'running')", params=(1,))
# A deleted chat's tasks' tool calls (the chat's own go by its messages).
statement("overlay.agent.delete_chat_task_calls",
          "DELETE FROM agent_tool_call INDEXED BY agent_tool_call_turn WHERE turn IN "
          "(SELECT turn FROM agent_task INDEXED BY agent_task_chat WHERE chat = ?)",
          params=("c",))
statement("overlay.agent.task_requeue",
          "UPDATE agent_task SET status = 'queued' WHERE id = ? AND status = 'running'",
          params=(1,))

# ── Audiences, campaigns and their members (harvey/campaigns.py, schema 27;
# the outbox by address, schema 37) ──
# A list a statement takes is bound as one JSON array (json_each), so its
# text is the same whatever its length: campaigns.OPEN_MEMBER, prospect
# ids, do-not-contact reasons, campaign statuses.
_LIST = "(SELECT value FROM json_each(?))"
_OPEN = "[0, 1, 2, 3]"
statement("overlay.campaigns.account_add",
          "INSERT OR IGNORE INTO sending_account (id, label, created_at) "
          "VALUES (1, 'settings', ?)", params=(1,))
statement("overlay.campaigns.account_sync",
          "UPDATE sending_account SET provider = ?, address = ? WHERE id = 1 "
          "AND (provider != ? OR address != ?)", params=("gmail", "a@example.com", "", ""))
statement("overlay.campaigns.accounts",
          "SELECT id, label, provider, address, profile_id, enabled, created_at "
          "FROM sending_account ORDER BY id")
statement("overlay.campaigns.audience_add",
          "INSERT INTO audience (name, filter_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
          params=("n", "{}", 1, 1))
_AUDIENCE = "SELECT id, name, filter_json, created_at, updated_at, archived FROM audience "
# (archived: 1 lists the archived ones too.)
statement("overlay.campaigns.audiences",
          _AUDIENCE + "WHERE archived = 0 OR ? ORDER BY id DESC", params=(0,),
          scans={"audience": "the saved audiences, a handful, listed whole"})
statement("overlay.campaigns.audience", _AUDIENCE + "WHERE id = ?", params=(1,))
statement("overlay.campaigns.audience_use",
          "SELECT audience_id, COUNT(*) FROM campaigns WHERE audience_id IS NOT NULL "
          "GROUP BY audience_id",
          scans={"campaigns": "the campaigns, a handful, counted per audience"})
# A rename or an (un)archive: NULL leaves a field as it is.
statement("overlay.campaigns.audience_update",
          "UPDATE audience SET name = COALESCE(?, name), archived = COALESCE(?, archived), "
          "updated_at = ? WHERE id = ?", params=("n", None, 1, 1))
_CAMPAIGN = ("SELECT id, name, status, sequence_json, created_at, audience_id, audience_query, "
             "rolling, rolled_at, account_id, profile_id, offer_json, li_note, review, origin, "
             "started_at, ended_at, updated_at, send_to_risky, candidate_seq, checked_all_at "
             "FROM campaigns ")
statement("overlay.campaigns.get", _CAMPAIGN + "WHERE id = ?", params=("c",))
# (statuses: NULL lists every campaign.)
statement("overlay.campaigns.list",
          _CAMPAIGN + f"WHERE ? IS NULL OR status IN {_LIST} ORDER BY created_at DESC",
          params=(None, None),
          scans={"campaigns": "the campaigns, a handful, listed whole"})
statement("overlay.campaigns.member_counts",
          "SELECT campaign, status, COUNT(*) FROM campaign_member INDEXED BY "
          "campaign_member_status GROUP BY campaign, status",
          scans={"campaign_member": "every campaign's members counted by status for the "
                                    "Campaigns page: its (campaign, status) index read whole"})
statement("overlay.campaigns.sent_counts",
          "SELECT campaign_id, COUNT(*) FROM outbox INDEXED BY uq_outbox_campaign_step "
          "WHERE campaign_id != '' AND kind = 'sequence' AND status = 'sent' "
          "GROUP BY campaign_id",
          scans={"outbox": "every campaign's cold email sent, counted for the Campaigns "
                           "page: the campaigns' sequence index read whole"})
statement("overlay.campaigns.reply_counts",
          "SELECT campaign_id, COUNT(*) FROM conversations INDEXED BY "
          "idx_conversations_campaign_id WHERE campaign_id != '' GROUP BY campaign_id",
          scans={"conversations": "every campaign's conversations counted for the Campaigns "
                                  "page: its index read whole"})
statement("overlay.campaigns.add",
          "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, status, "
          "created_at, audience_id, audience_query, rolling, account_id, profile_id, offer_json, "
          "li_note, review, origin, updated_at, send_to_risky) VALUES (?, ?, 'email', ?, '[]', "
          "'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, ?)",
          params=("c", "n", "[]", "2026-01-01", None, "", 0, 1, None, "{}", "", "inherit", 1, 0))
# Every field _touch may change, written back whole (read in the same
# transaction): a field set to NULL is set to NULL.
statement("overlay.campaigns.save",
          "UPDATE campaigns SET name = ?, status = ?, sequence_json = ?, audience_id = ?, "
          "audience_query = ?, rolling = ?, rolled_at = ?, offer_json = ?, li_note = ?, "
          "review = ?, started_at = ?, ended_at = ?, updated_at = ?, send_to_risky = ?, "
          "candidate_seq = ?, checked_all_at = ? WHERE id = ?",
          params=("n", "draft", "[]", None, "", 0, None, "{}", "", "inherit", None, None, 1, 0,
                  0, None, "c"))
statement("overlay.campaigns.review",
          "SELECT review FROM campaigns WHERE id = ?", params=("c",))
# The Outbox's note on a paused campaign's approved emails, and taking it off.
statement("overlay.campaigns.note_paused",
          "UPDATE outbox INDEXED BY uq_outbox_campaign_step SET error = ? "
          "WHERE campaign_id = ? AND campaign_id != '' AND kind = 'sequence' "
          "AND status = 'approved' AND COALESCE(error, '') = ''", params=("p", "c"))
statement("overlay.campaigns.note_resumed",
          "UPDATE outbox INDEXED BY uq_outbox_campaign_step SET error = '' "
          "WHERE campaign_id = ? AND campaign_id != '' AND kind = 'sequence' "
          "AND status = 'approved' AND error = ?", params=("c", "p"))
statement("overlay.campaigns.cancel_queued",
          "UPDATE outbox INDEXED BY uq_outbox_campaign_step SET status = 'cancelled', "
          "error = ?, updated_at = ? WHERE campaign_id = ? AND campaign_id != '' "
          "AND kind = 'sequence' AND status IN ('pending_review', 'approved')",
          params=("e", "2026-01-01", "c"))
statement("overlay.campaigns.end_members",
          "UPDATE campaign_member SET status = ?, ended_at = ?, end_reason = ? "
          f"WHERE campaign = ? AND status IN {_LIST}", params=(6, 1, "r", "c", _OPEN))
statement("overlay.campaigns.has_open",
          f"SELECT 1 FROM campaign_member WHERE campaign = ? AND status IN {_LIST} LIMIT 1",
          params=("c", _OPEN))
_MEMBER = ("SELECT campaign, email, contact, firm, person, prospect_id, status, personal, "
           "added_by, added_at, last_step, last_sent_at, ended_at, end_reason "
           "FROM campaign_member ")
statement("overlay.campaigns.member", _MEMBER + "WHERE campaign = ? AND email = ?",
          params=("c", "a@example.com"))
# A campaign's members in the order they joined (a page; the record takes
# them all): its members sorted, a campaign's tens of thousands at most.
statement("overlay.campaigns.members",
          _MEMBER + "WHERE campaign = ? ORDER BY added_at, email LIMIT ? OFFSET ?",
          params=("c", 500, 0), bounded=True)
statement("overlay.campaigns.members_status",
          _MEMBER + "WHERE campaign = ? AND status = ? ORDER BY added_at, email LIMIT ? OFFSET ?",
          params=("c", 1, 500, 0), bounded=True)
# A member moved on: NULL keeps last_step and last_sent_at; a final status
# (ended 1) records when and why it ended.
statement("overlay.campaigns.set_member",
          "UPDATE campaign_member SET status = ?, last_step = COALESCE(?, last_step), "
          "last_sent_at = COALESCE(?, last_sent_at), "
          "ended_at = CASE WHEN ? THEN ? ELSE ended_at END, "
          "end_reason = CASE WHEN ? THEN ? ELSE end_reason END WHERE campaign = ? AND email = ?",
          params=(1, None, None, 0, 1, 0, "", "c", "a@example.com"))
# One address's queued emails in a campaign: those after a step, or all.
statement("overlay.campaigns.cancel_after",
          "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
          "WHERE to_email = ? AND campaign_id = ? AND kind = 'sequence' AND step > ? "
          "AND status IN ('pending_review', 'approved')",
          params=("e", "2026-01-01", "a@example.com", "c", 1))
statement("overlay.campaigns.cancel_address",
          "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
          "WHERE to_email = ? AND campaign_id = ? AND kind = 'sequence' "
          "AND status IN ('pending_review', 'approved')",
          params=("e", "2026-01-01", "a@example.com", "c"))
statement("overlay.campaigns.end_address",
          "UPDATE campaign_member INDEXED BY campaign_member_email "
          "SET status = ?, ended_at = ?, end_reason = ? "
          f"WHERE email = ? AND status IN {_LIST}", params=(5, 1, "r", "a@example.com", _OPEN))
statement("overlay.campaigns.end_prospects",
          "UPDATE campaign_member INDEXED BY campaign_member_prospect "
          "SET status = ?, ended_at = ?, end_reason = ? "
          f"WHERE prospect_id IN {_LIST} AND status IN {_LIST}",
          params=(6, 1, "r", '["p"]', _OPEN))
# Why another campaign holds an address (campaigns.overlap): open in an
# active or paused campaign, cold email sent to it lately, or contacted
# with no cold email on file.
statement("overlay.campaigns.held_by",
          "SELECT c.name FROM campaign_member m INDEXED BY campaign_member_email "
          "CROSS JOIN campaigns c ON c.id = m.campaign "
          f"WHERE m.email = ? AND m.campaign != ? AND m.status IN {_LIST} "
          "AND c.status IN ('active', 'paused') LIMIT 1",
          params=("a@example.com", "c", _OPEN), drives="m")
statement("overlay.campaigns.sent_lately",
          "SELECT MAX(sent_at) FROM outbox INDEXED BY idx_outbox_to_email WHERE to_email = ? "
          "AND kind = 'sequence' AND status = 'sent' AND sent_at >= ? AND campaign_id != ?",
          params=("a@example.com", "2026-01-01", "c"))
statement("overlay.campaigns.contacted_elsewhere",
          "SELECT 1 FROM prospects p INDEXED BY uq_prospects_email WHERE p.email = ? "
          "AND p.email != '' AND p.status = 'contacted' AND NOT EXISTS "
          "(SELECT 1 FROM outbox o INDEXED BY idx_outbox_prospect WHERE o.prospect_id = p.id "
          "AND o.kind = 'sequence' AND o.status = 'sent') LIMIT 1", params=("a@example.com",))
statement("overlay.campaigns.first_sent",
          "SELECT 1 FROM outbox INDEXED BY idx_outbox_to_email WHERE to_email = ? "
          "AND kind = 'sequence' AND status = 'sent' AND campaign_id = ? AND step = 1 "
          "AND id != ? LIMIT 1", params=("a@example.com", "c", ""))
statement("overlay.campaigns.prospect_status",
          "SELECT status FROM prospects INDEXED BY uq_prospects_email WHERE email = ? "
          "AND email != ''", params=("a@example.com",))
statement("overlay.campaigns.member_add",
          "INSERT OR IGNORE INTO campaign_member (campaign, email, contact, firm, person, "
          "prospect_id, status, personal, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
          "?, ?)", params=("c", "a@example.com", 1, 1, 0, "p", 1, 0, 1, 1))
# The live firms where someone answered (state.stop_company's marks).
statement("overlay.campaigns.answered_firms",
          "SELECT DISTINCT (SELECT CASE f.live WHEN 0 THEN f.id ELSE f.live END FROM firm f "
          "WHERE f.id = p.company_id) FROM settings s CROSS JOIN prospects p "
          "WHERE s.key >= ? AND s.key < ? AND p.id = substr(s.key, ?) AND p.company_id != ''",
          params=("company_stop:", "company_stop;", 14), drives="s")
# The record: the acknowledgements, the cold email and answers sent, the
# opt-outs, and the members' firms.
statement("overlay.campaigns.acknowledgements",
          "SELECT version, text_sha, at, by FROM acknowledgement WHERE kind = ? ORDER BY at",
          params=("cold_email",))
statement("overlay.campaigns.record_sends",
          "SELECT to_email, step, subject, sent_at, source_note, message_id, kind FROM outbox "
          "INDEXED BY uq_outbox_campaign_step WHERE campaign_id = ? AND campaign_id != '' "
          "AND kind = 'sequence' AND status = 'sent' "
          "UNION ALL SELECT o.to_email, o.step, o.subject, o.sent_at, o.source_note, "
          "o.message_id, o.kind FROM conversations c INDEXED BY idx_conversations_campaign_id "
          "CROSS JOIN outbox o INDEXED BY idx_outbox_prospect WHERE c.campaign_id = ? "
          "AND o.prospect_id = c.prospect_id AND o.conversation_id = c.id AND o.kind = 'reply' "
          "AND o.status = 'sent' ORDER BY 4, 2", params=("c", "c"), bounded=True)
statement("overlay.campaigns.opt_outs",
          "SELECT scope, value, reason, campaign, at, by, undone_at, undo_note FROM suppression "
          f"WHERE reason IN {_LIST} ORDER BY at, id", params=('[1, 2]',),
          scans={"suppression": "the do-not-contact list's opt-outs, read whole for one "
                                "campaign's record (an export)"})
statement("overlay.campaigns.member_firms",
          "SELECT email, firm FROM campaign_member WHERE campaign = ?", params=("c",))

# ── Each campaign's candidates and the contact change log (schema 38) ──
# An address decided: a member, or left out with why and when to decide it
# again (campaigns.add_members). A recheck_at a hook set while it was being
# decided (from the decision's start to now) stays: the decision may not
# have seen what the hook saw.
statement("overlay.campaigns.candidate_put",
          "INSERT INTO campaign_candidate (campaign, email, contact, state, why, recheck_at, "
          "checked_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (campaign, email) DO UPDATE SET "
          "contact = excluded.contact, state = excluded.state, why = excluded.why, "
          "recheck_at = CASE WHEN excluded.state = 0 AND campaign_candidate.recheck_at "
          "BETWEEN ? AND excluded.checked_at THEN campaign_candidate.recheck_at "
          "ELSE excluded.recheck_at END, checked_at = excluded.checked_at",
          params=("c", "a@example.com", 1, 0, 5, None, 1, 1))
# A campaign's candidates counted by state and why (its page, harvey
# campaigns show): its primary key's prefix.
statement("overlay.campaigns.candidate_counts",
          "SELECT state, why, COUNT(*) FROM campaign_candidate WHERE campaign = ? "
          "GROUP BY state, why", params=("c",),
          scans={"campaign_candidate": "one campaign's candidates counted for its page "
                                       "(its audience's size): its primary key's prefix"})
# The rows whose recheck_at has come, and those of them a roll didn't
# decide again (they no longer match its filter): nothing more to wait for.
statement("overlay.campaigns.candidates_due",
          "SELECT email FROM campaign_candidate INDEXED BY campaign_candidate_recheck "
          "WHERE campaign = ? AND recheck_at IS NOT NULL AND recheck_at <= ?", params=("c", 1))
# (Strictly before the roll's time: a hook that set one due as the roll
# ran set it to that time or later, and it is decided at the next roll.)
statement("overlay.campaigns.candidates_settle",
          "UPDATE campaign_candidate SET recheck_at = NULL, checked_at = ? "
          f"WHERE campaign = ? AND email IN {_LIST} AND recheck_at IS NOT NULL "
          "AND recheck_at < ?", params=(1, "c", '["a@example.com"]', 1))
# Email contacts by address (the due rows'; a contact found again has a new id).
statement("overlay.campaigns.contact_ids",
          f"SELECT c.id FROM json_each(?) j CROSS JOIN contact c "
          "WHERE c.kind = ? AND c.value = j.value", params=('["a@example.com"]', 1), drives="j")
# The log: the last seq given (pruned or not) and the lowest still kept;
# the contacts changed in a range of it; pruning below the slowest cursor.
statement("overlay.campaigns.change_marks",
          "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'contact_change'), 0), "
          "(SELECT MIN(seq) FROM contact_change)",
          scans={"sqlite_sequence": "one row per table with AUTOINCREMENT: a few"})
statement("overlay.campaigns.changed",
          "SELECT DISTINCT contact FROM contact_change WHERE seq > ? AND seq <= ?",
          params=(0, 10))
statement("overlay.campaigns.slowest_cursor",
          "SELECT MIN(candidate_seq) FROM campaigns WHERE rolling = 1 AND status = 'active'",
          scans={"campaigns": "the campaigns, a handful"})
statement("overlay.campaigns.prune_changes",
          "DELETE FROM contact_change WHERE seq <= ?", params=(10,))
# A campaign made before schema 38 takes the setting's send_to_risky.
statement("overlay.campaigns.fill_risky",
          "UPDATE campaigns SET send_to_risky = ? WHERE send_to_risky IS NULL", params=(0,),
          scans={"campaigns": "the campaigns, a handful"})
