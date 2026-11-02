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
          f"l.description, l.source_url, l.gone FROM {_J} CROSS JOIN local_firm l "
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
          f"SELECT p.firm, p.first_name, p.last_name, p.title, p.email, p.email_status, p.phone, "
          f"p.score, p.status, p.created_at FROM {_J} CROSS JOIN prospects p WHERE p.firm = j.value",
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
statement("overlay.read.next_free",
          "SELECT id FROM firm INDEXED BY firm_with_site WHERE live = 0 "
          "AND (website IS NOT NULL OR domain IS NOT NULL) AND n_contacts = 0 "
          "AND (site_read_deep IS NULL OR site_read_deep < ?) ORDER BY created DESC, id DESC "
          "LIMIT ?", params=(0, 50), bounded=True)
statement("overlay.read.next_shallow",
          "SELECT id FROM firm INDEXED BY firm_with_site WHERE live = 0 "
          "AND (website IS NOT NULL OR domain IS NOT NULL) AND n_contacts > 0 "
          "AND site_read_at IS NOT NULL AND site_read_deep IS NULL ORDER BY created DESC, id DESC "
          "LIMIT ?", params=(50,), bounded=True)
statement("overlay.read.next_paid",
          "SELECT id FROM firm INDEXED BY firm_no_contact WHERE live = 0 AND n_contacts = 0 "
          "AND website IS NULL AND domain IS NULL ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
          params=(50, 0), scans=_WALKED)
statement("overlay.read.searched_by",
          f"SELECT DISTINCT s.target FROM {_J} CROSS JOIN search s WHERE s.is_person = 0 "
          "AND s.target = j.value AND s.module = ? AND s.capability = ? AND s.at >= ? "
          "AND s.outcome != 0", params=("[1, 2]", 1, 1, 0), drives="j")
statement("overlay.read.prospect_emails",
          "SELECT p.firm, p.email, f.domain FROM prospects p LEFT JOIN firm f ON f.id = p.firm "
          "WHERE p.email != '' AND COALESCE(p.email_status, '') != 'invalid' "
          "AND p.status IS NOT 'opted_out'")
statement("overlay.read.prospect_phones",
          "SELECT DISTINCT firm FROM prospects WHERE phone != ''")
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
          "AND live != 0) g CROSS JOIN prospects p WHERE p.firm = g.id",
          params=(1, 1))
statement("overlay.read.prospects_by_id",
          f"SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, p.score, "
          f"p.personalization_notes, p.firm FROM {_J} CROSS JOIN prospects p WHERE p.id = j.value",
          params=('["p1"]',), drives="j")
statement("overlay.read.run_last_at",
          "SELECT MAX(o.at) FROM batch b CROSS JOIN obs o WHERE b.ref = ? AND o.run = b.id",
          params=("run-1",), drives="b")
statement("overlay.read.checked", "SELECT firm FROM firm_signal WHERE signal = ?", params=(1,))
statement("overlay.read.tally_signal",
          "SELECT n_true, n_checked FROM signal_tally WHERE signal = ?", params=(1,))
statement("overlay.read.register_tally_of",
          "SELECT mark, loaded, n_true, n_false, at FROM register_tally "
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
statement("overlay.read.prospected_emails",
          f"SELECT j.value FROM {_J} CROSS JOIN prospects p WHERE p.email = j.value",
          params=('["office@example.com"]',), drives="j")
statement("overlay.read.person_firms", "SELECT DISTINCT firm FROM person")
statement("overlay.read.unkeyed",
          "SELECT f.id FROM firm f WHERE f.live = 0 AND NOT EXISTS (SELECT 1 FROM firm_ident fi "
          "WHERE fi.firm IN (f.id, (SELECT m.id FROM firm m WHERE m.live = f.id AND m.live != 0)) "
          "AND fi.scheme = ?)", params=(1,),
          scans={"firm": "the firms with no key of a register's scheme (a few hundred): every "
                         "firm probed once per kept set"})
# Live firms with no domain of their own whose merged record has one (the
# domain is a key, kept on one record only): a few.
statement("overlay.read.domain_by_member",
          "SELECT COUNT(DISTINCT m.live) FROM firm m INDEXED BY firm_merged WHERE m.live != 0 "
          "AND m.domain IS NOT NULL AND (SELECT s.domain FROM firm s WHERE s.id = m.live) IS NULL")
statement("overlay.read.identity", "SELECT n FROM revision WHERE name = 'identity'")
statement("overlay.read.marks",
          "SELECT (SELECT MAX(id) FROM firm), (SELECT MAX(id) FROM search), "
          "(SELECT MAX(id) FROM obs), (SELECT MAX(id) FROM person), "
          "(SELECT MAX(id) FROM contact), (SELECT MAX(id) FROM merge)")
