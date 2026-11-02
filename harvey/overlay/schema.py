"""The overlay's tables (harvey.db schema 25).

Integers for ids, times (unix seconds), enums and dictionary ids. A firm is
Harvey's own integer id; register identifiers live in ``firm_ident`` under
schemes the register modules declare ([[identifiers]] in their manifests),
so nothing here names a country, a register or its key.

Kept values (counts, current readings, last-search times) are maintained by
triggers.py on row-at-a-time writes and by recount.py after bulk writes.
Anything that depends on the clock is never kept: a timestamp plus an index,
counted by index range when read.
"""

from __future__ import annotations

import sqlite3

VERSION = 25

# ── Enums (stored as integers) ──

# Lower is surer, so MIN() over evidence is the best trust.
TRUST = {"confirmed": 0, "likely": 1, "unconfirmed": 2}
TRUST_NAMES = {v: k for k, v in TRUST.items()}
LINE_TYPE = {"unknown": 0, "mobile": 1, "fixed": 2, "voip": 3, "toll_free": 4}
# "valid": a phone number that parses as a real number of its country.
CONTACT_STATUS = {"unchecked": 0, "verified": 1, "risky": 2, "guess": 3, "invalid": 4, "valid": 5}
# A search that failed doesn't count as done (its outcome is ERROR).
OUTCOME = {"error": 0, "found": 1, "nothing": 2, "skipped": 3}
ERROR = OUTCOME["error"]

# The contact kinds every install has (contacts are core): fixed ids, so the
# triggers can count emails and phones without a lookup. Modules add more
# kinds (contact form, social profiles) with ids above these.
EMAIL, PHONE = 1, 2
MOBILE = LINE_TYPE["mobile"]
CORE_KINDS = ((EMAIL, "email", "Email", "email"), (PHONE, "phone", "Phone", "phone"))

# The kinds of key Harvey found for a firm (firm_key.kind). A name in its
# town ("name@town", resolver.name_key) is kept only for a firm no register
# describes: a register's firms are matched by name through its own index.
FIRM_KEY = {"email": 1, "domain": 2, "name": 3}

# Roles the kept values need to recognise (role table): the capability a
# website read is recorded under, the contact finder's, and the bit of
# ``search.looked_for`` that marks a deep site read (one that looked for
# the owner). Filled from the modules' capabilities when the dictionaries
# are synced; until then nothing counts as a site read.
ROLES = ("site_read", "finder", "deep_bit")

# What the overlay counts with no clock (tally rows). ``outbox:<status>``
# rows are added as statuses appear.
TALLIES = ("firms", "firms_with_contact", "firms_with_site", "firms_merged",
           "contacts", "contacts_email", "contacts_phone", "contacts_mobile",
           "contacts_do_not_contact", "people")

DICTIONARIES = """
CREATE TABLE module (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE source (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE capability (id INTEGER PRIMARY KEY, module INTEGER NOT NULL, name TEXT NOT NULL,
                         UNIQUE (module, name));
-- Declared by modules ([[identifiers]]). numeric: keys are stored as
-- integers. legal: two different keys are two different firms (never merged).
CREATE TABLE ident_scheme (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, module INTEGER NOT NULL,
  numeric INTEGER NOT NULL, legal INTEGER NOT NULL
);
-- The runs (job batches) searches and sources were made in, by their text id.
CREATE TABLE batch (id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE);
-- The bits of search.looked_for (bit is the value: 1, 2, 4, ...).
CREATE TABLE looked_for (bit INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE role (name TEXT PRIMARY KEY, id INTEGER NOT NULL) WITHOUT ROWID;
-- The signal vocabulary; status is the confirmation gate.
CREATE TABLE signal (
  id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, label TEXT, description TEXT, category TEXT,
  value_type TEXT, unit TEXT, cardinality TEXT, keyed_by TEXT, when_known TEXT, cost_note TEXT,
  status TEXT NOT NULL DEFAULT 'proposed', confidence_floor REAL DEFAULT 0
);
-- One row per (signal, module) that provides it. how: 0 stored in obs, 1
-- read from the module's register. precedence breaks a tie after "newest
-- dated wins" (lower first). absent, for reads: 0 unknown, 1 false (when
-- the register is loaded and has no row for the firm).
CREATE TABLE signal_source (
  signal INTEGER NOT NULL, module INTEGER NOT NULL, how INTEGER NOT NULL,
  precedence INTEGER NOT NULL, absent INTEGER NOT NULL,
  PRIMARY KEY (signal, module)
) WITHOUT ROWID;
"""

FIRMS = """
-- AUTOINCREMENT: a removed firm's id is never given to another (jobs,
-- actions and settings that name it must not reach a different firm).
CREATE TABLE firm (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  live INTEGER NOT NULL DEFAULT 0,          -- 0: itself; else the final survivor it is merged into
  created INTEGER NOT NULL,
  display_name TEXT,                        -- Harvey's own name for it; NULL: the register's
  website TEXT CHECK (website IS NULL OR website != ''),
  domain TEXT CHECK (domain IS NULL OR domain != ''),   -- a checked key only
  website_trust INTEGER,                    -- 0 confirmed, 1 likely, 2 unconfirmed, NULL declared
  website_source INTEGER,
  website_fill INTEGER,                     -- the merge that copied it here (undone with it)
  site_read_at INTEGER, site_read_deep INTEGER,
  profiled_at INTEGER, last_search_at INTEGER, finder_at INTEGER,
  n_contacts INTEGER NOT NULL DEFAULT 0, n_email INTEGER NOT NULL DEFAULT 0,
  n_phone INTEGER NOT NULL DEFAULT 0, n_mobile INTEGER NOT NULL DEFAULT 0,
  best_trust INTEGER, n_people INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX firm_domain ON firm(domain) WHERE domain IS NOT NULL;
CREATE INDEX firm_merged       ON firm(live) WHERE live != 0;
CREATE INDEX firm_new          ON firm(created DESC, id DESC) WHERE live = 0;
CREATE INDEX firm_with_contact ON firm(created DESC, id DESC) WHERE live = 0 AND n_contacts > 0;
CREATE INDEX firm_no_contact   ON firm(created DESC, id DESC) WHERE live = 0 AND n_contacts = 0;
CREATE INDEX firm_by_contacts  ON firm(n_contacts DESC, id DESC) WHERE live = 0;
CREATE INDEX firm_with_site    ON firm(site_read_deep, created DESC, id DESC)
                                  WHERE live = 0 AND (website IS NOT NULL OR domain IS NOT NULL);
CREATE INDEX firm_profiled     ON firm(profiled_at) WHERE live = 0;
CREATE INDEX firm_searched     ON firm(last_search_at DESC, id DESC)
                                  WHERE live = 0 AND last_search_at IS NOT NULL;

-- key has no type affinity: an integer for a numeric scheme, text otherwise.
CREATE TABLE firm_ident (
  scheme INTEGER NOT NULL, key NOT NULL,
  firm INTEGER NOT NULL,                    -- the record that carries it (a member, after a merge)
  since INTEGER NOT NULL,
  PRIMARY KEY (scheme, key)
) WITHOUT ROWID;
CREATE INDEX firm_ident_firm ON firm_ident(firm, scheme);
-- Counters that only grow (never recounted): ``identity`` moves whenever
-- which firm holds which identifier changes (a firm added, removed, merged
-- or unmerged, an identifier added or removed), so what was worked out
-- from the registers' keys for the firms in Harvey is known to be stale.
CREATE TABLE revision (name TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;

-- The description of a firm no register describes (a listing; a market with
-- no register module).
CREATE TABLE local_firm (
  firm INTEGER PRIMARY KEY, name TEXT, county TEXT, locality TEXT, address TEXT,
  phone TEXT, industry TEXT, description TEXT, source_url TEXT,
  gone INTEGER                              -- set when the register it came from no longer has it
);
CREATE INDEX local_firm_place ON local_firm(county, locality);
CREATE VIRTUAL TABLE local_fts USING fts5(name, industry, locality, county,
  content='local_firm', content_rowid='firm', tokenize='unicode61 remove_diacritics 2');
"""

CONTACTS = """
CREATE TABLE contact_kinds (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL UNIQUE, label TEXT DEFAULT '', channel TEXT DEFAULT '',
  module TEXT DEFAULT '', available INTEGER DEFAULT 1
);
CREATE TABLE contact (
  id INTEGER PRIMARY KEY, kind INTEGER NOT NULL, value TEXT NOT NULL, display TEXT,
  line_type INTEGER, status INTEGER, do_not_contact INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  n_firms INTEGER NOT NULL DEFAULT 0, best_trust INTEGER,   -- kept; n_firms counts live firms
  UNIQUE (kind, value)
);
CREATE INDEX contact_new   ON contact(first_seen DESC, id DESC);
CREATE INDEX contact_kind  ON contact(kind, line_type, first_seen DESC, id DESC);
CREATE INDEX contact_reach ON contact(n_firms DESC, id DESC);

-- One per (record found for, contact): kept from link_evidence.
CREATE TABLE link (
  firm INTEGER NOT NULL, contact INTEGER NOT NULL, trust INTEGER NOT NULL,
  person INTEGER NOT NULL DEFAULT 0, location INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (firm, contact)
) WITHOUT ROWID;
CREATE INDEX link_contact ON link(contact, firm);
CREATE INDEX link_person  ON link(person) WHERE person != 0;

-- location: the firm's place (locations row) the contact was found for, 0 none.
CREATE TABLE link_evidence (
  firm INTEGER NOT NULL, contact INTEGER NOT NULL, source INTEGER NOT NULL,
  person INTEGER NOT NULL DEFAULT 0, location INTEGER NOT NULL DEFAULT 0,
  capability INTEGER, trust INTEGER NOT NULL,
  confidence REAL, evidence_url TEXT, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY (firm, contact, source, person)
) WITHOUT ROWID;
CREATE INDEX link_evidence_source ON link_evidence(source, firm);

-- Only people Harvey found or acted on (a register's administrators are
-- read from the register).
CREATE TABLE person (
  id INTEGER PRIMARY KEY, firm INTEGER NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  role TEXT, source INTEGER, first_seen INTEGER, last_seen INTEGER, UNIQUE (firm, name_norm)
);
"""

SEARCHES = """
CREATE TABLE search (
  id INTEGER PRIMARY KEY, target INTEGER NOT NULL, is_person INTEGER NOT NULL DEFAULT 0,
  origin INTEGER,                           -- the record it was run for, when not the target
  module INTEGER NOT NULL, capability INTEGER NOT NULL, looked_for INTEGER NOT NULL DEFAULT 0,
  at INTEGER NOT NULL, outcome INTEGER NOT NULL, found INTEGER, cost REAL, batch INTEGER,
  detail TEXT
);
CREATE INDEX search_target ON search(is_person, target, module, capability, at);
CREATE INDEX search_at     ON search(at);
CREATE INDEX search_batch  ON search(batch) WHERE batch IS NOT NULL;

-- Every source that found a firm, register pulls included.
CREATE TABLE firm_source (
  firm INTEGER NOT NULL, source INTEGER NOT NULL, batch INTEGER NOT NULL DEFAULT 0,
  ref TEXT NOT NULL DEFAULT '', first_seen INTEGER, last_seen INTEGER,
  PRIMARY KEY (firm, source, batch, ref)
) WITHOUT ROWID;
CREATE INDEX firm_source_by ON firm_source(source, batch, firm);

CREATE TABLE merge (
  id INTEGER PRIMARY KEY, survivor INTEGER NOT NULL, merged INTEGER NOT NULL,
  score REAL, reasons TEXT, decided_by TEXT, grp INTEGER, at INTEGER, undone_at INTEGER,
  location INTEGER                          -- a branch merge's locations row, removed on undo
);
CREATE INDEX merge_survivor ON merge(survivor);
CREATE INDEX merge_merged   ON merge(merged);
CREATE INDEX merge_grp      ON merge(grp) WHERE grp IS NOT NULL;
CREATE TABLE merge_member (merge INTEGER NOT NULL, firm INTEGER NOT NULL,
                           PRIMARY KEY (merge, firm)) WITHOUT ROWID;
CREATE TABLE match_candidate (a INTEGER, b INTEGER, score REAL, reasons TEXT, status TEXT,
                              at INTEGER, decided_at INTEGER, PRIMARY KEY (a, b)) WITHOUT ROWID;
CREATE INDEX match_candidate_status ON match_candidate(status);
-- Harvey-found email and domain keys.
CREATE TABLE firm_key (
  kind INTEGER NOT NULL, value TEXT NOT NULL, firm INTEGER NOT NULL, source INTEGER,
  PRIMARY KEY (kind, value, firm)) WITHOUT ROWID;
CREATE INDEX firm_key_firm ON firm_key(firm);
-- The old text ids still named by job payloads, actions and settings.
CREATE TABLE legacy_id (uuid TEXT PRIMARY KEY, firm INTEGER NOT NULL) WITHOUT ROWID;
-- What an upgrade kept aside instead of deciding for the user: a name that
-- differs from the register's (a rename, or an older dataset: it becomes
-- the display name only when the user says so), a key no module declares.
CREATE TABLE migration_note (firm INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                             PRIMARY KEY (firm, kind, value)) WITHOUT ROWID;
"""

SIGNALS = """
-- Web signals' history, written on change only.
CREATE TABLE obs (
  id INTEGER PRIMARY KEY, firm INTEGER NOT NULL, signal INTEGER NOT NULL, key TEXT NOT NULL DEFAULT '',
  num REAL, text TEXT, confidence REAL, module INTEGER, capability INTEGER, run INTEGER,
  evidence_url TEXT, at INTEGER NOT NULL
);
CREATE INDEX obs_firm ON obs(firm, signal, key, at);
CREATE INDEX obs_run  ON obs(run) WHERE run IS NOT NULL;

-- The current reading per (firm, signal, key): at is when the value last
-- changed, checked_at when it was last seen (a re-check moves only that).
CREATE TABLE reading (
  firm INTEGER NOT NULL, signal INTEGER NOT NULL, key TEXT NOT NULL,
  num REAL, text TEXT, confidence REAL, module INTEGER,
  at INTEGER NOT NULL, checked_at INTEGER NOT NULL,
  obs INTEGER NOT NULL, positive INTEGER NOT NULL,
  PRIMARY KEY (firm, signal, key)
) WITHOUT ROWID;
CREATE INDEX reading_signal ON reading(signal, positive, num, firm);

-- Per live firm and signal: how many readings its records have, and how
-- many are positive.
CREATE TABLE firm_signal (firm INTEGER NOT NULL, signal INTEGER NOT NULL, n_keys INTEGER NOT NULL,
                          n_positive INTEGER NOT NULL, PRIMARY KEY (signal, firm)) WITHOUT ROWID;
CREATE INDEX firm_signal_firm ON firm_signal(firm);
CREATE TABLE tally (name TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE signal_tally (signal INTEGER PRIMARY KEY, n_true INTEGER NOT NULL,
                           n_checked INTEGER NOT NULL);
-- Register signals counted over the firms in Harvey, per load mark (a job
-- recounts them; at NULL means stale).
CREATE TABLE register_tally (signal INTEGER NOT NULL, module INTEGER NOT NULL, mark TEXT NOT NULL,
  loaded INTEGER NOT NULL, n_true INTEGER, n_false INTEGER, at INTEGER,
  PRIMARY KEY (signal, module));
"""

# The tables kept from schema 24, unchanged but for integer firm ids.
OUTREACH = """
CREATE TABLE prospects (
  id TEXT PRIMARY KEY, firm INTEGER, first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
  email TEXT DEFAULT '', email_verified INTEGER DEFAULT 0, phone TEXT DEFAULT '',
  phone_verified INTEGER DEFAULT 0, linkedin_url TEXT DEFAULT '', title TEXT DEFAULT '',
  seniority TEXT DEFAULT '', department TEXT DEFAULT '', source TEXT DEFAULT '',
  source_url TEXT DEFAULT '', status TEXT DEFAULT 'new', score INTEGER DEFAULT 0,
  personalization_notes TEXT DEFAULT '', company TEXT DEFAULT '', industry TEXT DEFAULT '',
  company_size TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, email_status TEXT DEFAULT '',
  timezone TEXT DEFAULT ''
);
CREATE INDEX idx_prospects_firm ON prospects(firm);
CREATE INDEX idx_prospects_status ON prospects(status);
CREATE UNIQUE INDEX uq_prospects_email ON prospects(email) WHERE email != '';
CREATE UNIQUE INDEX uq_prospects_linkedin ON prospects(linkedin_url) WHERE linkedin_url != '';
CREATE INDEX idx_prospects_name_company
  ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
CREATE INDEX idx_prospects_status_updated ON prospects(status, updated_at);
CREATE TABLE campaigns (
  id TEXT PRIMARY KEY, name TEXT DEFAULT '', channel TEXT DEFAULT 'email',
  instantly_campaign_id TEXT DEFAULT '', sequence_json TEXT DEFAULT '[]',
  prospect_ids_json TEXT DEFAULT '[]', status TEXT DEFAULT 'draft',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_campaigns_status ON campaigns(status);
CREATE TABLE outbox (
  id TEXT PRIMARY KEY, campaign_id TEXT DEFAULT '', prospect_id TEXT DEFAULT '',
  conversation_id TEXT DEFAULT '', step INTEGER DEFAULT 1, kind TEXT DEFAULT 'sequence',
  to_email TEXT DEFAULT '', subject TEXT DEFAULT '', body TEXT DEFAULT '',
  status TEXT DEFAULT 'pending_review', send_at TIMESTAMP, sent_at TIMESTAMP,
  provider TEXT DEFAULT '', message_id TEXT DEFAULT '', thread_ref TEXT DEFAULT '',
  in_reply_to TEXT DEFAULT '', error TEXT DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  approved_by TEXT DEFAULT ''
);
CREATE UNIQUE INDEX uq_outbox_campaign_step ON outbox(campaign_id, prospect_id, step)
  WHERE campaign_id != '' AND kind = 'sequence';
CREATE INDEX idx_outbox_status_send_at ON outbox(status, send_at);
CREATE INDEX idx_outbox_prospect ON outbox(prospect_id);
CREATE TABLE conversations (
  id TEXT PRIMARY KEY, prospect_id TEXT, campaign_id TEXT DEFAULT '', channel TEXT DEFAULT 'email',
  thread_json TEXT DEFAULT '[]', intent TEXT DEFAULT '', stage TEXT DEFAULT 'initial_outreach',
  status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_conversations_status ON conversations(status);
CREATE INDEX idx_conversations_campaign_id ON conversations(campaign_id);
CREATE INDEX idx_conversations_prospect_id ON conversations(prospect_id);
CREATE INDEX idx_conversations_intent ON conversations(intent);
CREATE TABLE processed_replies (reply_id TEXT PRIMARY KEY,
                                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE actions (id TEXT PRIMARY KEY, action_type TEXT, agent TEXT,
                      details_json TEXT DEFAULT '{}', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_actions_created_at ON actions(created_at);
CREATE INDEX idx_actions_type_created ON actions(action_type, created_at);
CREATE TABLE runs (
  id TEXT PRIMARY KEY, stage TEXT DEFAULT '', status TEXT DEFAULT 'running', provider TEXT DEFAULT '',
  records INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0.0, params_json TEXT DEFAULT '{}',
  error TEXT DEFAULT '', started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP
);
CREATE INDEX idx_runs_stage_started ON runs(stage, started_at);
CREATE TABLE usage_log (
  id TEXT PRIMARY KEY, date TEXT UNIQUE, claude_calls INTEGER DEFAULT 0,
  usage_percent REAL DEFAULT 0.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  codex_calls INTEGER DEFAULT 0
);
CREATE TABLE usage_events (
  id TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, agent TEXT DEFAULT '',
  task TEXT DEFAULT '', session_id TEXT DEFAULT '', request_key TEXT DEFAULT '',
  model TEXT DEFAULT '', input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_creation_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0.0, duration_ms INTEGER DEFAULT 0, num_turns INTEGER DEFAULT 0,
  is_error INTEGER DEFAULT 0, source TEXT DEFAULT 'result_json', provider TEXT DEFAULT 'claude'
);
CREATE INDEX idx_usage_events_created ON usage_events(created_at);
CREATE INDEX idx_usage_events_agent ON usage_events(agent);
CREATE INDEX idx_usage_events_session ON usage_events(session_id);
CREATE UNIQUE INDEX uq_usage_events_request ON usage_events(request_key) WHERE request_key != '';
CREATE INDEX idx_usage_events_provider ON usage_events(provider);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '',
                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE email_patterns (
  domain TEXT PRIMARY KEY, pattern TEXT DEFAULT '', source TEXT DEFAULT '',
  confidence REAL DEFAULT 0.0, mx_type TEXT DEFAULT '', is_catch_all INTEGER DEFAULT -1,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, lane TEXT NOT NULL, module TEXT DEFAULT '',
  capability TEXT DEFAULT '', label TEXT DEFAULT '', params_json TEXT DEFAULT '{}',
  dedupe_key TEXT, status TEXT NOT NULL DEFAULT 'queued', progress_json TEXT DEFAULT '{}',
  result_json TEXT DEFAULT '{}', est_cost REAL DEFAULT 0.0, reserved REAL DEFAULT 0.0,
  actual_cost REAL DEFAULT 0.0, attempts INTEGER DEFAULT 0, not_before TIMESTAMP,
  error TEXT DEFAULT '', paused_reason TEXT DEFAULT '', stop_requested TEXT DEFAULT '',
  batch_id TEXT DEFAULT '', parent_id TEXT DEFAULT '', worker TEXT DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_jobs_dedupe ON jobs(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'paused');
CREATE INDEX idx_jobs_lane ON jobs(lane, status, created_at);
CREATE INDEX idx_jobs_batch ON jobs(batch_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE job_batches (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT DEFAULT '', params_json TEXT DEFAULT '{}',
  max_spend REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE worker_lease (
  name TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER DEFAULT 0, host TEXT DEFAULT '',
  heartbeat_at REAL NOT NULL, started_at REAL NOT NULL
);
CREATE TABLE locations (
  id INTEGER PRIMARY KEY, firm INTEGER NOT NULL, name TEXT DEFAULT '', address TEXT DEFAULT '',
  locality TEXT DEFAULT '', county TEXT DEFAULT '', country TEXT DEFAULT '', point TEXT DEFAULT '',
  external_id TEXT DEFAULT '', phone TEXT DEFAULT '', source TEXT DEFAULT '',
  first_seen INTEGER, last_seen INTEGER
);
CREATE INDEX idx_locations_firm ON locations(firm);
CREATE UNIQUE INDEX uq_locations_listing ON locations(firm, external_id) WHERE external_id != '';
CREATE TABLE feedback (
  id TEXT PRIMARY KEY, entity_type TEXT DEFAULT '', entity_id TEXT DEFAULT '',
  comment TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_feedback_entity ON feedback(entity_type, entity_id);
"""

# The external-content full-text index follows local_firm on every write
# (bulk writes too: listing firms are few). Not a kept value: never dropped.
LOCAL_FTS_SYNC = """
CREATE TRIGGER local_fts_ins AFTER INSERT ON local_firm BEGIN
  INSERT INTO local_fts (rowid, name, industry, locality, county)
  VALUES (NEW.firm, NEW.name, NEW.industry, NEW.locality, NEW.county);
END;
CREATE TRIGGER local_fts_del AFTER DELETE ON local_firm BEGIN
  INSERT INTO local_fts (local_fts, rowid, name, industry, locality, county)
  VALUES ('delete', OLD.firm, OLD.name, OLD.industry, OLD.locality, OLD.county);
END;
CREATE TRIGGER local_fts_upd AFTER UPDATE OF name, industry, locality, county ON local_firm BEGIN
  INSERT INTO local_fts (local_fts, rowid, name, industry, locality, county)
  VALUES ('delete', OLD.firm, OLD.name, OLD.industry, OLD.locality, OLD.county);
  INSERT INTO local_fts (rowid, name, industry, locality, county)
  VALUES (NEW.firm, NEW.name, NEW.industry, NEW.locality, NEW.county);
END;
"""

# Rules every write keeps, bulk writes too (never dropped: they keep no
# value, they refuse a write or follow one).
GUARDS = """
-- A numeric scheme's key is an integer: one firm_ident row per key, never
-- 1000004 and '1000004' side by side.
CREATE TRIGGER guard_ident_numeric BEFORE INSERT ON firm_ident
WHEN typeof(NEW.key) != 'integer'
     AND (SELECT numeric FROM ident_scheme WHERE id = NEW.scheme) = 1 BEGIN
  SELECT RAISE(ABORT, 'a numeric identifier scheme keeps its keys as integers');
END;
CREATE TRIGGER guard_ident_numeric_upd BEFORE UPDATE OF scheme, key ON firm_ident
WHEN typeof(NEW.key) != 'integer'
     AND (SELECT numeric FROM ident_scheme WHERE id = NEW.scheme) = 1 BEGIN
  SELECT RAISE(ABORT, 'a numeric identifier scheme keeps its keys as integers');
END;
-- A legal scheme's key is a firm's only one in that scheme: a record holding
-- a tax code never gains a second (two are two firms; merges refuse them).
CREATE TRIGGER guard_ident_legal BEFORE INSERT ON firm_ident
WHEN (SELECT legal FROM ident_scheme WHERE id = NEW.scheme) = 1
     AND EXISTS (SELECT 1 FROM firm_ident x WHERE x.firm = NEW.firm AND x.scheme = NEW.scheme
                 AND x.key IS NOT NEW.key) BEGIN
  SELECT RAISE(ABORT, 'a firm holds one key of a legal identifier scheme at most');
END;
CREATE TRIGGER guard_ident_legal_upd BEFORE UPDATE OF scheme, key, firm ON firm_ident
WHEN (SELECT legal FROM ident_scheme WHERE id = NEW.scheme) = 1
     AND EXISTS (SELECT 1 FROM firm_ident x WHERE x.firm = NEW.firm AND x.scheme = NEW.scheme
                 AND x.key IS NOT NEW.key AND x.key IS NOT OLD.key) BEGIN
  SELECT RAISE(ABORT, 'a firm holds one key of a legal identifier scheme at most');
END;
-- Which firm holds which identifier: revision ``identity`` follows every
-- change to it.
CREATE TRIGGER follow_identity_ident_ins AFTER INSERT ON firm_ident BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
CREATE TRIGGER follow_identity_ident_del AFTER DELETE ON firm_ident BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
CREATE TRIGGER follow_identity_ident_upd AFTER UPDATE OF scheme, key, firm ON firm_ident BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
CREATE TRIGGER follow_identity_firm_ins AFTER INSERT ON firm BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
CREATE TRIGGER follow_identity_firm_del AFTER DELETE ON firm BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
CREATE TRIGGER follow_identity_firm_live AFTER UPDATE OF live ON firm
WHEN NEW.live IS NOT OLD.live BEGIN
  UPDATE revision SET n = n + 1 WHERE name = 'identity';
END;
-- Contacts are never discarded: one a firm links stays (delete its
-- evidence first, and the link goes with it).
CREATE TRIGGER guard_contact_linked BEFORE DELETE ON contact
WHEN EXISTS (SELECT 1 FROM link WHERE contact = OLD.id) BEGIN
  SELECT RAISE(ABORT, 'a contact some firm links is never deleted');
END;
-- A firm other records are merged into stays while they point at it
-- (undo those merges first).
CREATE TRIGGER guard_firm_members BEFORE DELETE ON firm
WHEN EXISTS (SELECT 1 FROM firm m WHERE m.live = OLD.id AND m.live != 0) BEGIN
  SELECT RAISE(ABORT, 'records are merged into this firm: undo those merges first');
END;
-- A website written after a merge filled it is the firm's own: undoing
-- the merge leaves it.
CREATE TRIGGER follow_website_fill AFTER UPDATE OF website ON firm
WHEN OLD.website_fill IS NOT NULL AND NEW.website_fill IS OLD.website_fill
     AND NEW.website IS NOT OLD.website BEGIN
  UPDATE firm SET website_fill = NULL WHERE id = NEW.id;
END;
"""
GUARD_NAMES = ("guard_ident_numeric", "guard_ident_numeric_upd", "guard_ident_legal",
               "guard_ident_legal_upd", "follow_identity_ident_ins", "follow_identity_ident_del",
               "follow_identity_ident_upd", "follow_identity_firm_ins", "follow_identity_firm_del",
               "follow_identity_firm_live", "guard_contact_linked", "guard_firm_members",
               "follow_website_fill", "local_fts_ins", "local_fts_del", "local_fts_upd")

PARTS = (DICTIONARIES, FIRMS, CONTACTS, SEARCHES, SIGNALS, OUTREACH, LOCAL_FTS_SYNC, GUARDS)


def create(con: sqlite3.Connection, *, triggers: bool = True) -> None:
    """Create the schema in an empty database: tables, indexes, the core
    contact kinds and roles, zeroed tallies, and the kept-value triggers
    (``triggers=False`` leaves them out, for a bulk load that recounts
    and installs them at the end)."""
    if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]:
        raise ValueError("schema 25 is created in an empty database only")
    with con:
        for part in PARTS:
            _script(con, part)
        con.executemany("INSERT INTO contact_kinds (id, kind, label, channel) VALUES (?, ?, ?, ?)",
                        CORE_KINDS)
        con.executemany("INSERT INTO tally (name, n) VALUES (?, 0)", [(t,) for t in TALLIES])
        con.execute("INSERT INTO revision (name, n) VALUES ('identity', 0)")
        con.execute(f"PRAGMA user_version = {VERSION}")
    if triggers:
        from harvey.overlay import triggers as kept

        kept.install(con)


def install_guards(con: sqlite3.Connection) -> None:
    """Create the guards and the full-text sync triggers that are missing."""
    have = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type = 'trigger'")}
    buf = ""
    for line in (LOCAL_FTS_SYNC + GUARDS).splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            name = buf.split("CREATE TRIGGER", 1)[-1].split()[0] if "CREATE TRIGGER" in buf else ""
            if name and name not in have:
                con.execute(buf)
            buf = ""


def _script(con: sqlite3.Connection, script: str) -> None:
    """Run a script's statements inside the caller's transaction
    (executescript would commit first)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                con.execute(buf)
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete statement: {buf[:80]!r}")


def version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


# ── Dictionaries ──

def dict_id(con: sqlite3.Connection, table: str, name: str) -> int:
    """The id of ``name`` in a name dictionary (module, source), added when new."""
    if table not in ("module", "source"):
        raise ValueError(f"not a name dictionary: {table}")
    row = con.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid


def capability_id(con: sqlite3.Connection, module: str, name: str) -> int:
    mod = dict_id(con, "module", module)
    row = con.execute("SELECT id FROM capability WHERE module = ? AND name = ?",
                      (mod, name)).fetchone()
    if row:
        return row[0]
    return con.execute("INSERT INTO capability (module, name) VALUES (?, ?)",
                       (mod, name)).lastrowid


def batch_id(con: sqlite3.Connection, ref: str) -> int | None:
    """The integer id of a run's text id, added when new (None for none)."""
    if not ref:
        return None
    row = con.execute("SELECT id FROM batch WHERE ref = ?", (ref,)).fetchone()
    if row:
        return row[0]
    return con.execute("INSERT INTO batch (ref) VALUES (?)", (ref,)).lastrowid


def looked_for_bit(con: sqlite3.Connection, name: str) -> int:
    """The bit ``name`` has in search.looked_for, assigned when new."""
    row = con.execute("SELECT bit FROM looked_for WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    top = con.execute("SELECT COALESCE(MAX(bit), 0) FROM looked_for").fetchone()[0]
    bit = top * 2 if top else 1
    con.execute("INSERT INTO looked_for (bit, name) VALUES (?, ?)", (bit, name))
    return bit


def looked_for_mask(con: sqlite3.Connection, names) -> int:
    mask = 0
    for name in names:
        if name:
            mask |= looked_for_bit(con, name)
    return mask


def set_role(con: sqlite3.Connection, name: str, value: int) -> None:
    """Set a role; when it changes, the search times it decides are counted
    again for every firm that has searches (a role set after the searches
    were recorded, or moved to another capability)."""
    if name not in ROLES:
        raise ValueError(f"unknown role {name!r}")
    old = con.execute("SELECT id FROM role WHERE name = ?", (name,)).fetchone()
    con.execute("INSERT INTO role (name, id) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET id = excluded.id", (name, value))
    if (old[0] if old else None) != value:
        from harvey.overlay import recount

        recount.search_times(con)


def ident_scheme(con: sqlite3.Connection, name: str, module: str, *, numeric: bool,
                 legal: bool) -> int:
    """A declared identifier scheme's id, added (or updated) from its manifest."""
    mod = dict_id(con, "module", module)
    con.execute("INSERT INTO ident_scheme (name, module, numeric, legal) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET module = excluded.module, "
                "numeric = excluded.numeric, legal = excluded.legal",
                (name, mod, int(numeric), int(legal)))
    return con.execute("SELECT id FROM ident_scheme WHERE name = ?", (name,)).fetchone()[0]


def sync_identifiers(con: sqlite3.Connection, manifests=None) -> dict[str, int]:
    """Every scheme the modules declare ([[identifiers]]), by name -> id."""
    from harvey.modules.loader import load_manifests

    out = {}
    for m in manifests if manifests is not None else load_manifests():
        for ident in m.identifiers:
            out[ident["name"]] = ident_scheme(con, ident["name"], m.id,
                                              numeric=ident["numeric"], legal=ident["legal"])
    return out


# ── The vocabulary ──

SIGNAL_COLUMNS = ("code", "label", "description", "category", "value_type", "unit", "cardinality",
                  "keyed_by", "when_known", "cost_note", "status", "confidence_floor")


def sync_signals(con: sqlite3.Connection, rows=(), manifests=None) -> dict[str, int]:
    """The signal vocabulary and who provides each signal: ``rows`` are
    signal rows to keep as they are (the confirmation gate, ``status``,
    is the user's: an upgrade brings the old vocabulary's rows); a signal
    a manifest declares that isn't among them is added as proposed. Then
    one ``signal_source`` row per (signal, module): stored (how 0) for a
    module that declares the signal, or whose capabilities return it as a
    fact (``outputs.facts``: a search module recording a rank, a listing's
    "no website"), read (how 1) for a register module that reads it, with
    its precedence and what no row means. A stored source's precedence is
    0 for the declaring module, 1 for one that only returns the fact.
    Returns code -> id."""
    from harvey.modules.loader import load_manifests

    manifests = list(manifests if manifests is not None else load_manifests())
    cols = ", ".join(SIGNAL_COLUMNS)
    marks = ", ".join("?" * len(SIGNAL_COLUMNS))
    for row in rows:
        values = [row.get(c) for c in SIGNAL_COLUMNS]
        values[SIGNAL_COLUMNS.index("status")] = row.get("status") or "proposed"
        con.execute(f"INSERT INTO signal ({cols}) VALUES ({marks}) ON CONFLICT (code) DO UPDATE "
                    f"SET {', '.join(f'{c} = excluded.{c}' for c in SIGNAL_COLUMNS[1:])}", values)
    for m in manifests:
        for sig in m.signals:
            values = [sig.get(c) for c in SIGNAL_COLUMNS]
            values[SIGNAL_COLUMNS.index("status")] = "proposed"
            con.execute(f"INSERT OR IGNORE INTO signal ({cols}) VALUES ({marks})", values)
    ids = dict(con.execute("SELECT code, id FROM signal").fetchall())
    for m in manifests:
        mod = dict_id(con, "module", m.id)
        read = {r["code"]: r for r in (m.register or {}).get("reads", ())}
        for sig in m.signals:
            if sig["code"] not in read and sig["code"] in ids:
                con.execute("INSERT OR IGNORE INTO signal_source (signal, module, how, precedence, "
                            "absent) VALUES (?, ?, 0, 0, 0)", (ids[sig["code"]], mod))
        if not m.register:
            # (A register module's facts are read from its register.)
            for cap in m.capabilities:
                for code in (cap.get("outputs") or {}).get("facts") or ():
                    if code in ids:
                        con.execute("INSERT OR IGNORE INTO signal_source (signal, module, how, "
                                    "precedence, absent) VALUES (?, ?, 0, 1, 0)",
                                    (ids[code], mod))
        for code, r in read.items():
            if code in ids:
                con.execute("INSERT INTO signal_source (signal, module, how, precedence, absent) "
                            "VALUES (?, ?, 1, ?, ?) ON CONFLICT (signal, module) DO UPDATE SET "
                            "how = 1, precedence = excluded.precedence, absent = excluded.absent",
                            (ids[code], mod, r["precedence"], int(r["absent"] == "false")))
    return ids
