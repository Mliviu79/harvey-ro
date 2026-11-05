"""The overlay's tables (harvey.db schema 36).

Integers for ids, times (unix seconds), enums and dictionary ids. A firm is
Harvey's own integer id; register identifiers live in ``firm_ident`` under
schemes the register modules declare ([[identifiers]] in their manifests),
so nothing here names a country, a register or its key.

Kept values (counts, current readings, last-search times) are maintained by
triggers.py on row-at-a-time writes and by recount.py after bulk writes.
Anything that depends on the clock is never kept: a timestamp plus an index,
counted by index range when read.

Versions. The upgrade (harvey/upgrade.py, overlay/migrate.py) writes schema
25 exactly as it was released: PARTS below is that schema and is never
edited. Every later change is a step in STEPS, run in place when a file is
opened (state._open_or_create), so a file the upgrade wrote, or one an
earlier release made, takes the steps it lacks in one transaction.
"""

from __future__ import annotations

import json
import sqlite3

# The schema the upgrade writes and verifies (frozen: PARTS), and the one
# this release reads, reached from it by STEPS.
UPGRADE_TARGET = 25
LATEST = 36
VERSION = LATEST
# The capability an observation is kept under when a search's hit gave it
# and the row names no capability (schema 24's collector "discover"): so
# a later site read can tell it from its own "not stated".
SEARCH_HIT = "search_hit"

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

# Do-not-contact entries (suppression.scope, .reason).
SUPPRESSION_SCOPE = {"address": 1, "domain": 2, "firm": 3}
SUPPRESSION_REASON = {"unsubscribe": 1, "opt_out": 2, "objection": 3, "by_hand": 4,
                      "bounce": 5, "complaint": 6, "migrated": 7}

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
           "firms_with_email", "firms_with_phone", "firms_with_mobile", "contacts",
           "contacts_email", "contacts_phone", "contacts_mobile", "contacts_do_not_contact",
           "people")

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
  best_trust INTEGER, n_people INTEGER NOT NULL DEFAULT 0,
  -- where it falls in the directory register's name and place orders
  -- (overlay/orders.py: refreshed per load of the register)
  name_order REAL, place_order REAL
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
CREATE INDEX firm_by_name      ON firm(name_order DESC, id DESC) WHERE live = 0;
CREATE INDEX firm_by_place     ON firm(place_order DESC, id DESC) WHERE live = 0;

-- key has no type affinity: an integer for a numeric scheme, text otherwise.
CREATE TABLE firm_ident (
  scheme INTEGER NOT NULL, key NOT NULL,
  firm INTEGER NOT NULL,                    -- the record that carries it (a member, after a merge)
  since INTEGER NOT NULL,
  PRIMARY KEY (scheme, key)
) WITHOUT ROWID;
CREATE INDEX firm_ident_firm ON firm_ident(firm, scheme);
-- (Which records gained an identifier since a register's kept sets were
-- built: register_set.)
CREATE INDEX firm_ident_since ON firm_ident(since);
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
  gone INTEGER,                             -- set when the register it came from no longer has it
  country TEXT                              -- as the place line named it (a recipient's time zone)
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
-- recounts them; at NULL means stale). n_values: how many values the firms
-- hold in all (each once per record: the people a register names).
CREATE TABLE register_tally (signal INTEGER NOT NULL, module INTEGER NOT NULL, mark TEXT NOT NULL,
  loaded INTEGER NOT NULL, n_true INTEGER, n_false INTEGER, at INTEGER, n_values INTEGER,
  PRIMARY KEY (signal, module));
-- The same job keeps which records each register signal holds for: part 0
-- the records the register has a value for, part 1 of those the ones whose
-- value reads yes, each a compressed bitmap over firm ids (bit n: record n);
-- part <year>, for a register that keeps each year's figures: the records
-- whose statement that year has the signal's figure.
-- What a cohort's register conditions are worked out from (overlay/bitmaps.py).
-- top: the highest firm id then; records: how many firm rows there were;
-- at: when the walk started (a record that gained an identifier since is
-- read again from the register).
CREATE TABLE register_set (signal INTEGER NOT NULL, module INTEGER NOT NULL, part INTEGER NOT NULL,
  mark TEXT NOT NULL, top INTEGER NOT NULL, records INTEGER NOT NULL, at INTEGER NOT NULL,
  bits BLOB NOT NULL, PRIMARY KEY (signal, module, part));
"""

# The tables kept from schema 24, unchanged but for integer firm ids.
OUTREACH = """
CREATE TABLE prospects (
  id TEXT PRIMARY KEY, company_id INTEGER, first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
  email TEXT DEFAULT '', email_verified INTEGER DEFAULT 0, phone TEXT DEFAULT '',
  phone_verified INTEGER DEFAULT 0, linkedin_url TEXT DEFAULT '', title TEXT DEFAULT '',
  seniority TEXT DEFAULT '', department TEXT DEFAULT '', source TEXT DEFAULT '',
  source_url TEXT DEFAULT '', status TEXT DEFAULT 'new', score INTEGER DEFAULT 0,
  personalization_notes TEXT DEFAULT '', company TEXT DEFAULT '', industry TEXT DEFAULT '',
  company_size TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, email_status TEXT DEFAULT '',
  timezone TEXT DEFAULT ''
);
CREATE INDEX idx_prospects_company_id ON prospects(company_id);
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

# ── Schema 26: the do-not-contact list, the cold-email acknowledgement ──

SCHEMA_26 = """
-- One do-not-contact list. An entry is keyed on what it names, never on a
-- contact or firm row: an address (normalised), a domain (its subdomains
-- too), or a firm ("<legal scheme>:<key>" when it holds one, else
-- "firm:<record id>", matched through firm.live). Undoing one is soft.
CREATE TABLE suppression (
  id INTEGER PRIMARY KEY,
  scope INTEGER NOT NULL, value TEXT NOT NULL, reason INTEGER NOT NULL,
  campaign TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL, by TEXT NOT NULL DEFAULT '',
  undone_at INTEGER, undone_by TEXT NOT NULL DEFAULT '', undo_note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX suppression_live ON suppression(scope, value) WHERE undone_at IS NULL;
CREATE INDEX suppression_value ON suppression(value);
-- The operator's acknowledgement before cold email goes out: one row per
-- version of the text, with the text's hash.
CREATE TABLE acknowledgement (
  kind TEXT NOT NULL, version INTEGER NOT NULL, text_sha TEXT NOT NULL,
  at INTEGER NOT NULL, by TEXT NOT NULL,
  PRIMARY KEY (kind, version)
) WITHOUT ROWID;
-- The token a cold email's unsubscribe header carries, and the source
-- note a first email to a personal address was staged with.
ALTER TABLE outbox ADD COLUMN unsub_token TEXT DEFAULT '';
ALTER TABLE outbox ADD COLUMN source_note TEXT DEFAULT '';
CREATE INDEX idx_outbox_unsub ON outbox(unsub_token) WHERE unsub_token != '';
CREATE INDEX idx_outbox_message_id ON outbox(message_id) WHERE message_id != '';
"""

# contact.do_not_contact mirrors the live address entries; nothing else
# may clear it while one stands. Rules, not kept values: never dropped,
# bulk writes included (a contact a registry pull finds again comes back
# flagged).
SUPPRESSION_GUARDS = f"""
CREATE TRIGGER follow_suppression_ins AFTER INSERT ON suppression
WHEN NEW.scope = 1 AND NEW.undone_at IS NULL BEGIN
  UPDATE contact SET do_not_contact = 1
  WHERE kind = {EMAIL} AND value = NEW.value AND do_not_contact = 0;
END;
CREATE TRIGGER follow_suppression_undo AFTER UPDATE OF undone_at ON suppression
WHEN NEW.scope = 1 AND OLD.undone_at IS NULL AND NEW.undone_at IS NOT NULL BEGIN
  UPDATE contact SET do_not_contact = 0
  WHERE kind = {EMAIL} AND value = NEW.value AND do_not_contact != 0
    AND NOT EXISTS (SELECT 1 FROM suppression s WHERE s.scope = 1 AND s.value = NEW.value
                    AND s.undone_at IS NULL);
END;
CREATE TRIGGER follow_suppression_contact AFTER INSERT ON contact
WHEN NEW.kind = {EMAIL} AND NEW.do_not_contact = 0
     AND EXISTS (SELECT 1 FROM suppression s WHERE s.scope = 1 AND s.value = NEW.value
                 AND s.undone_at IS NULL) BEGIN
  UPDATE contact SET do_not_contact = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER guard_suppressed_contact BEFORE UPDATE OF do_not_contact ON contact
WHEN NEW.do_not_contact = 0 AND OLD.do_not_contact != 0 AND NEW.kind = {EMAIL}
     AND EXISTS (SELECT 1 FROM suppression s WHERE s.scope = 1 AND s.value = NEW.value
                 AND s.undone_at IS NULL) BEGIN
  SELECT RAISE(ABORT, 'this address is on the do-not-contact list: undo its entry instead');
END;
"""
SUPPRESSION_GUARD_NAMES = ("follow_suppression_ins", "follow_suppression_undo",
                           "follow_suppression_contact", "guard_suppressed_contact")


def _to_26(con: sqlite3.Connection) -> None:
    """Schema 26: the suppression list (what was do-not-contact before,
    a flagged email contact or an opted-out prospect, carried over), its
    triggers, the acknowledgement, the outbox's token and source note."""
    _script(con, SCHEMA_26)
    _script(con, SUPPRESSION_GUARDS)
    migrated, address = SUPPRESSION_REASON["migrated"], SUPPRESSION_SCOPE["address"]
    con.execute(f"""
        INSERT INTO suppression (scope, value, reason, at, by)
        SELECT {address}, value, {migrated}, CAST(strftime('%s', 'now') AS INTEGER), 'migration'
        FROM contact WHERE kind = {EMAIL} AND do_not_contact != 0 AND value LIKE '%_@_%'
        ON CONFLICT DO NOTHING""")
    con.execute(f"""
        INSERT INTO suppression (scope, value, reason, at, by)
        SELECT {address}, lower(trim(email)), {migrated},
               CAST(strftime('%s', 'now') AS INTEGER), 'migration'
        FROM prospects WHERE status = 'opted_out' AND email LIKE '%_@_%'
        ON CONFLICT DO NOTHING""")


# ── Schema 27: sending accounts, audiences, campaigns made by the user ──

SCHEMA_27 = """
-- The mailbox a campaign sends from, and the AI profile that writes for it
-- (one account per profile). Row 1 mirrors today's settings
-- (channels.email.provider, persona.email): caps, warmup, persona and the
-- reply reader still read those (harvey/campaigns.py: sync_account).
CREATE TABLE sending_account (
  id INTEGER PRIMARY KEY, label TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX sending_account_profile ON sending_account(profile_id) WHERE profile_id != '';
-- A saved Contacts filter: the page's query string, read by the same code
-- as /api/contacts (contacts.filter_from_query).
CREATE TABLE audience (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, filter_json TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE campaigns ADD COLUMN audience_id INTEGER;
ALTER TABLE campaigns ADD COLUMN audience_query TEXT DEFAULT '';
ALTER TABLE campaigns ADD COLUMN rolling INTEGER DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN rolled_at INTEGER;
ALTER TABLE campaigns ADD COLUMN account_id INTEGER;
ALTER TABLE campaigns ADD COLUMN profile_id TEXT DEFAULT '';
ALTER TABLE campaigns ADD COLUMN offer_json TEXT DEFAULT '{}';
ALTER TABLE campaigns ADD COLUMN li_note TEXT DEFAULT '';
ALTER TABLE campaigns ADD COLUMN review TEXT DEFAULT 'inherit';
ALTER TABLE campaigns ADD COLUMN origin TEXT DEFAULT 'auto';
ALTER TABLE campaigns ADD COLUMN started_at INTEGER;
ALTER TABLE campaigns ADD COLUMN ended_at INTEGER;
ALTER TABLE campaigns ADD COLUMN updated_at INTEGER;
-- Who a campaign writes to and how far each got: keyed on the address (a
-- contact found again has a new id; the member and its history stay).
-- firm is the record picked when added, read through firm.live.
CREATE TABLE campaign_member (
  campaign TEXT NOT NULL, email TEXT NOT NULL, contact INTEGER, firm INTEGER,
  person INTEGER NOT NULL DEFAULT 0, prospect_id TEXT NOT NULL DEFAULT '',
  status INTEGER NOT NULL, personal INTEGER NOT NULL DEFAULT 0,
  added_by INTEGER NOT NULL, added_at INTEGER NOT NULL,
  last_step INTEGER NOT NULL DEFAULT 0, last_sent_at INTEGER,
  ended_at INTEGER, end_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (campaign, email)
) WITHOUT ROWID;
CREATE INDEX campaign_member_status ON campaign_member(campaign, status);
CREATE INDEX campaign_member_email ON campaign_member(email, status);
CREATE INDEX campaign_member_prospect ON campaign_member(prospect_id);
ALTER TABLE outbox ADD COLUMN account_id INTEGER;
ALTER TABLE conversations ADD COLUMN account_id INTEGER;
"""

# campaign_member.status
MEMBER_STATUS = {"waiting": 0, "ready": 1, "staged": 2, "in_sequence": 3, "done": 4,
                 "replied": 5, "stopped": 6, "suppressed": 7, "bounced": 8, "skipped": 9,
                 "removed": 10}
# campaign_member.added_by
MEMBER_ADDED_BY = {"snapshot": 1, "rolling": 2, "by_hand": 3, "migrated": 4, "writer": 5}


def _to_27(con: sqlite3.Connection) -> None:
    """Schema 27: sending accounts (row 1, today's mailbox, filled from the
    settings when Harvey next reads them), audiences, the campaigns'
    new columns and their members. Every campaign on file was made by the
    Writer ('auto'): its prospects become members, as far as each got;
    nothing is staged or sent by this."""
    _script(con, SCHEMA_27)
    con.execute("INSERT INTO sending_account (id, label, created_at) "
                "VALUES (1, 'settings', CAST(strftime('%s', 'now') AS INTEGER))")
    con.execute("UPDATE campaigns SET account_id = 1, origin = 'auto', "
                "updated_at = CAST(strftime('%s', 'now') AS INTEGER)")
    con.execute("UPDATE outbox SET account_id = 1")
    for campaign, ids in con.execute("SELECT id, prospect_ids_json FROM campaigns").fetchall():
        try:
            prospect_ids = [str(p) for p in json.loads(ids or "[]")]
        except (ValueError, TypeError):
            prospect_ids = []
        members_from_prospects(con, campaign, prospect_ids, MEMBER_ADDED_BY["migrated"])


def members_from_prospects(con: sqlite3.Connection, campaign: str, prospect_ids,
                           added_by: int) -> int:
    """Members for a campaign's prospect cards (the Writer's campaigns, and
    the ones on file when schema 27 came): each where its card and the
    campaign's outbox say it got. Returns how many were added."""
    m = MEMBER_STATUS
    added = 0
    row = con.execute("SELECT sequence_json FROM campaigns WHERE id = ?", (campaign,)).fetchone()
    try:
        last = max([int(s.get("step") or 0) for s in json.loads((row and row[0]) or "[]")
                    if isinstance(s, dict)] or [0])
    except (ValueError, TypeError):
        last = 0
    for pid in dict.fromkeys(prospect_ids):
        row = con.execute("SELECT email, status, email_status, company_id FROM prospects "
                          "WHERE id = ?", (pid,)).fetchone()
        if row is None or "@" not in (row[0] or ""):
            continue
        email, pstatus, estatus, firm = (row[0] or "").strip().lower(), row[1] or "new", \
            row[2] or "", row[3]
        sent = con.execute("SELECT MAX(step), MAX(sent_at) FROM outbox WHERE campaign_id = ? "
                           "AND prospect_id = ? AND kind = 'sequence' AND status = 'sent'",
                           (campaign, pid)).fetchone()
        queued = con.execute("SELECT 1 FROM outbox WHERE campaign_id = ? AND prospect_id = ? "
                             "AND kind = 'sequence' AND status IN "
                             "('pending_review', 'approved', 'sending') LIMIT 1",
                             (campaign, pid)).fetchone()
        if pstatus == "opted_out":
            status = m["suppressed"]
        elif pstatus in ("replied", "meeting", "closed", "lost"):
            status = m["replied"]
        elif sent and sent[0] and last and int(sent[0]) >= last and not queued:
            status = m["done"]          # every step went out already
        elif (sent and sent[0]) or pstatus == "contacted":
            status = m["in_sequence"]
        elif queued:
            status = m["staged"]
        elif estatus in ("verified", "risky"):
            status = m["ready"]
        elif estatus in ("", "unchecked"):
            status = m["waiting"]
        else:
            status = m["skipped"]
        cur = con.execute(
            "INSERT OR IGNORE INTO campaign_member (campaign, email, firm, prospect_id, status, "
            "added_by, added_at, last_step) VALUES (?, ?, ?, ?, ?, ?, "
            "CAST(strftime('%s', 'now') AS INTEGER), ?)",
            (campaign, email, firm, pid, status, added_by, int(sent[0] or 0) if sent else 0))
        added += cur.rowcount
    return added


def _to_28(con: sqlite3.Connection) -> None:
    """Schema 28, data only. A conversation recorded before replies were
    attributed to their campaign gets the campaign of the email it answers:
    an outbox row of the conversation that names one, else the last cold
    email sent to that prospect before the conversation began (none: it
    stays without one). A campaign an earlier release deployed to
    Instantly goes on there: its members Harvey never wrote to stop, so
    Harvey's own mailbox never starts on them. An answer queued for review
    after you undid their opt-out gets the note that keeps it waiting for
    you (state.REVIEW_AFTER_UNDO): autopilot now approves answers at send
    time, and one with no note would go. A prospect the Writer's automatic
    campaigns couldn't write for ('write_failed', which nothing sets any
    more and nothing could reset) is 'new' again, so it can be added to a
    campaign or written to by hand."""
    sent = ("SELECT o.campaign_id FROM outbox o WHERE o.prospect_id = c.prospect_id "
            "AND o.kind = 'sequence' AND o.status = 'sent' AND COALESCE(o.campaign_id, '') != ''")
    rows = con.execute(f"""
        SELECT c.id, COALESCE(
          (SELECT o.campaign_id FROM outbox o WHERE o.conversation_id = c.id
             AND COALESCE(o.campaign_id, '') != '' ORDER BY o.created_at DESC LIMIT 1),
          ({sent} AND datetime(o.sent_at) <= datetime(c.created_at)
             ORDER BY datetime(o.sent_at) DESC LIMIT 1))
        FROM conversations c
        WHERE COALESCE(c.campaign_id, '') = '' AND COALESCE(c.prospect_id, '') != ''""").fetchall()
    con.executemany("UPDATE conversations SET campaign_id = ? WHERE id = ?",
                    [(campaign, cid) for cid, campaign in rows if campaign])
    m = MEMBER_STATUS
    con.execute(
        "UPDATE campaign_member SET status = ?, end_reason = 'deployed to Instantly', "
        "ended_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE status IN (?, ?) "
        "AND campaign IN (SELECT id FROM campaigns WHERE origin = 'auto' "
        "AND COALESCE(instantly_campaign_id, '') != '')",
        (m["stopped"], m["waiting"], m["ready"]))
    con.execute(
        "UPDATE outbox SET error = 'waiting for you: the first answer since you undid their "
        "opt-out' WHERE kind = 'reply' AND status = 'pending_review' "
        "AND COALESCE(error, '') = '' AND COALESCE(approved_by, '') = '' "
        "AND EXISTS (SELECT 1 FROM actions a WHERE a.action_type = 'opt_out_undone' "
        "AND json_extract(a.details_json, '$.prospect_id') = outbox.prospect_id "
        "AND datetime(a.created_at) <= datetime(outbox.created_at))")
    con.execute("UPDATE prospects SET status = 'new' WHERE status = 'write_failed'")


# ── Schema 29: why a found website was taken, and a re-check's undo ──

SCHEMA_29 = """
-- Why a website a search found was taken (harvey/finder.py): a short JSON,
-- {"by": "cui"|"ro+cui"|"reg"|"email"|"phone"|"judge", "model", "at"}.
-- NULL: nothing checked it yet.
ALTER TABLE firm ADD COLUMN website_basis TEXT;
-- The found websites nothing checked yet (the re-check's selection).
CREATE INDEX firm_site_check ON firm(id)
  WHERE live = 0 AND website_source IS NOT NULL AND website_basis IS NULL
    AND website_trust > 0;
-- What a re-check took away when it found a website wasn't the firm's:
-- everything its read had left (evidence, contacts, people, keys, the
-- lookup's search), so ``harvey contacts rejudge --undo BATCH`` puts it back.
CREATE TABLE site_undo (
  firm INTEGER NOT NULL, batch TEXT NOT NULL, at INTEGER NOT NULL,
  payload TEXT NOT NULL, undone_at INTEGER,
  PRIMARY KEY (batch, firm)
) WITHOUT ROWID;
CREATE INDEX site_undo_firm ON site_undo(firm);
"""


def _to_29(con: sqlite3.Connection) -> None:
    """Schema 29: firm.website_basis, the index of found websites nothing
    checked yet, and site_undo. Nothing on file changes: the re-check runs
    only when the user starts it."""
    _script(con, SCHEMA_29)


# ── Schema 30: contacts verified by the classifier, and their undo ──

SCHEMA_30 = """
-- A link (firm, contact) the classifier was asked about (harvey/
-- contact_check.py): its verdict (CHECK_VERDICT), the profile and model
-- that gave it, and for a no the evidence's trust it lowered (payload),
-- so ``harvey contacts verify --undo BATCH`` puts it back. Nothing is
-- deleted.
CREATE TABLE contact_check (
  firm INTEGER NOT NULL, contact INTEGER NOT NULL, batch TEXT NOT NULL, at INTEGER NOT NULL,
  verdict INTEGER NOT NULL, profile TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  p REAL, note TEXT, payload TEXT, undone_at INTEGER,
  PRIMARY KEY (batch, firm, contact)
) WITHOUT ROWID;
CREATE INDEX contact_check_link ON contact_check(contact, firm, at);
CREATE INDEX contact_check_open ON contact_check(verdict, at) WHERE undone_at IS NULL;
-- A campaign's members by contact: a no never lowers a member's link.
CREATE INDEX campaign_member_contact ON campaign_member(contact) WHERE contact IS NOT NULL;
-- A finished run's report (harvey/modules/jobs.py batch_report), with what
-- it was made from; NULL until the run finishes, and again when a job of
-- it is retried or resumed.
ALTER TABLE job_batches ADD COLUMN report_json TEXT;
"""
# contact_check.verdict
CHECK_VERDICT = {"no": 0, "yes": 1, "unsure": 2}


def _to_30(con: sqlite3.Connection) -> None:
    """Schema 30: contact_check, campaign members by contact, and runs'
    stored reports. Nothing on file changes: a verification runs only when
    the user starts it, and a run's report is stored the next time it is
    read."""
    _script(con, SCHEMA_30)


# ── Schema 31: the chat with Harvey's agent, and the tools it called ──

SCHEMA_31 = """
-- The chat on the dashboard's Chat tab (harvey/agent/chat.py): a person's
-- message and the agent's answer share a turn. status: a user message is
-- queued, running, held (the plan's quota), answered or failed; an
-- answer is ok, or error with what went wrong as its text.
CREATE TABLE agent_message (
  id INTEGER PRIMARY KEY, turn TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user', 'agent')),
  text TEXT NOT NULL, status TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL, updated_at INTEGER
);
CREATE INDEX agent_message_turn ON agent_message(turn);
-- Every call the agent made to Harvey's tools (harvey/agent/tools.py): its
-- arguments, a summary of what it returned, how long it took, and how it
-- ended (ok, refused, error).
CREATE TABLE agent_tool_call (
  id INTEGER PRIMARY KEY, turn TEXT NOT NULL, tool TEXT NOT NULL,
  arguments TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '',
  ms INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, at INTEGER NOT NULL
);
CREATE INDEX agent_tool_call_turn ON agent_tool_call(turn, id);
"""


def _to_31(con: sqlite3.Connection) -> None:
    """Schema 31: agent_message and agent_tool_call. Nothing on file
    changes: the chat starts empty."""
    _script(con, SCHEMA_31)


# ── Schema 32: the chats the user keeps ──

SCHEMA_32 = """
-- A conversation with Harvey's agent (harvey/agent/chat.py), kept until the
-- user deletes it: titled from its first message (or renamed), listed by
-- its last activity. turn: the message being answered now ('' none), which
-- Harvey's tools record their calls under.
CREATE TABLE agent_chat (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
  last_at INTEGER NOT NULL, turn TEXT NOT NULL DEFAULT ''
);
CREATE INDEX agent_chat_recent ON agent_chat(last_at);
-- The chat a message belongs to; an answer's tokens (its part of the
-- process's usage) and the context it was answered with (its last model
-- call's input, cache read and cache creation tokens: how long the chat is).
ALTER TABLE agent_message ADD COLUMN chat TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_message ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_message ADD COLUMN context INTEGER NOT NULL DEFAULT 0;
CREATE INDEX agent_message_chat ON agent_message(chat, id);
"""


def _to_32(con: sqlite3.Connection) -> None:
    """Schema 32: agent_chat, and the chat of each message. The messages on
    file become one chat, titled from its first message. The chat no longer
    runs as jobs: the agent_turn jobs left waiting are cancelled."""
    _script(con, SCHEMA_32)
    con.execute("UPDATE jobs SET status = 'cancelled', error = 'the chat no longer runs as a job', "
                "finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE kind = 'agent_turn' AND status IN ('queued', 'running', 'paused')")
    first = con.execute("SELECT MIN(created_at), MAX(COALESCE(updated_at, created_at)) "
                        "FROM agent_message").fetchone()
    if first[0] is None:
        return
    row = con.execute("SELECT text FROM agent_message WHERE role = 'user' "
                      "ORDER BY id LIMIT 1").fetchone()
    title = " ".join((row[0] if row else "").split())
    if len(title) > 60:
        title = title[:59].rstrip() + "\u2026"
    con.execute("INSERT INTO agent_chat (id, title, created_at, last_at) VALUES (?, ?, ?, ?)",
                ("earlier", title, first[0], first[1]))
    con.execute("UPDATE agent_message SET chat = 'earlier'")


# ── Schema 33: the agent's plans, its saved sets and what it queued ──

SCHEMA_33 = """
-- A plan Harvey's agent proposed in a chat (harvey/agent/plans.py): its goal,
-- its steps (a JSON list of short texts), the budget it asks for (USD, 0:
-- free work only) and how long it stays open once approved. Only the user
-- approves, rejects, cancels or closes it (the dashboard, the CLI); the
-- agent may close it too, and acts only under an approved one of its own
-- chat. status: proposed, approved, rejected, cancelled, expired, done.
CREATE TABLE agent_plan (
  id INTEGER PRIMARY KEY, chat TEXT NOT NULL, turn TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL, steps TEXT NOT NULL DEFAULT '[]',
  budget_usd REAL NOT NULL CHECK (budget_usd >= 0),
  expiry_hours INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'approved', 'rejected', 'cancelled', 'expired', 'done')),
  created_at INTEGER NOT NULL, decided_at INTEGER, expires_at INTEGER, closed_at INTEGER,
  decided_by TEXT NOT NULL DEFAULT '', closed_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX agent_plan_chat ON agent_plan(chat, id);
CREATE INDEX agent_plan_status ON agent_plan(status, id);
-- A set of firms or contacts the agent saved by a filter (the Contacts and
-- Companies pages' query string) or by one read-only SELECT of ids
-- (of: firms, contacts, registry): resolved again each time an action
-- takes it, never returned to the model. n: how many it held when saved.
CREATE TABLE agent_set (
  id INTEGER PRIMARY KEY, chat TEXT NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('filter', 'select')), of TEXT NOT NULL,
  definition TEXT NOT NULL, n INTEGER, created_at INTEGER NOT NULL
);
CREATE INDEX agent_set_chat ON agent_set(chat, id);
-- Every action the agent asked for under a plan (harvey/agent/actions.py),
-- refused ones too: its arguments, the run it queued (batch, a job_batches
-- id) and the caps it took from the plan's budget. outcome: reserved (its
-- caps held while it is queued), ok, nothing (nothing to do), refused or
-- error, with why.
CREATE TABLE agent_action (
  id INTEGER PRIMARY KEY, plan INTEGER, chat TEXT NOT NULL, turn TEXT NOT NULL DEFAULT '',
  tool TEXT NOT NULL, arguments TEXT NOT NULL DEFAULT '{}', batch TEXT,
  cap_usd REAL NOT NULL DEFAULT 0, classify_cap_usd REAL NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', at INTEGER NOT NULL
);
CREATE INDEX agent_action_plan ON agent_action(plan, id);
CREATE INDEX agent_action_chat ON agent_action(chat, id);
"""


def _to_33(con: sqlite3.Connection) -> None:
    """Schema 33: agent_plan, agent_set and agent_action. Nothing on file
    changes: the agent proposes its first plan when asked to act."""
    _script(con, SCHEMA_33)


# ── Schema 34: what wakes the agent, and what it remembers ──

SCHEMA_34 = """
-- What wakes Harvey's agent under an approved plan (harvey/agent/wakes.py):
-- a run of the plan finished (kind run_done, ref its batch and its last
-- finish), a wake the agent scheduled (scheduled, due_at ahead, its note in
-- data), or what another module adds (any kind: a reply, new members). One
-- row per (plan, kind, ref). A wake reads the plan's due events not yet
-- seen; seen_at and wake (the turn it answered) once it has finished;
-- cancelled_at when the user cancelled it, or the plan or chat went.
CREATE TABLE agent_event (
  id INTEGER PRIMARY KEY, plan INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}', due_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
  seen_at INTEGER, wake TEXT NOT NULL DEFAULT '', cancelled_at INTEGER
);
CREATE UNIQUE INDEX agent_event_ref ON agent_event(plan, kind, ref);
CREATE INDEX agent_event_open ON agent_event(due_at)
  WHERE seen_at IS NULL AND cancelled_at IS NULL;
-- The plans whose run a batch is (a run finishing is looked up by it).
CREATE INDEX agent_action_batch ON agent_action(batch) WHERE batch IS NOT NULL;
-- The agent's memory, install-wide (harvey/agent/memory.py): short notes
-- given to every new conversation, and named procedures it reads when it
-- needs one (only their names are given). author: agent, or the user's
-- dashboard or terminal.
CREATE TABLE agent_note (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'agent',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE agent_playbook (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'agent', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
-- An answer Harvey wrote on a wake, not to a message: why it woke (JSON);
-- the chat's wake answers the user hasn't opened yet.
ALTER TABLE agent_message ADD COLUMN wake TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_chat ADD COLUMN unread INTEGER NOT NULL DEFAULT 0;
"""


def _to_34(con: sqlite3.Connection) -> None:
    """Schema 34: agent_event, agent_note and agent_playbook, and an
    answer's wake. Nothing on file changes: the first wake comes with the
    next run of an approved plan to finish."""
    _script(con, SCHEMA_34)


# ── Schema 35: the campaigns Harvey's agent made under a plan ──

SCHEMA_35 = """
-- A campaign Harvey's agent made under a plan (harvey/agent/outreach.py):
-- the plan and chat it belongs to, so what happens to it (members a roll
-- took, a reply, the campaign finishing) wakes that plan; and when the
-- agent asked the user to start it, with what it counted then (JSON: the
-- preview's counts, what is still to do before it can start). The chat
-- shows it as a card; the start itself stays the user's own, on the
-- Campaigns page.
CREATE TABLE agent_campaign (
  campaign TEXT PRIMARY KEY, plan INTEGER NOT NULL, chat TEXT NOT NULL,
  created_at INTEGER NOT NULL, start_requested_at INTEGER,
  start_request TEXT NOT NULL DEFAULT ''
);
CREATE INDEX agent_campaign_plan ON agent_campaign(plan);
"""


def _to_35(con: sqlite3.Connection) -> None:
    """Schema 35: agent_campaign. Nothing on file changes: a campaign the
    user made belongs to no plan."""
    _script(con, SCHEMA_35)


# ── Schema 36: the tasks Harvey's agent hands to a child under a plan ──

SCHEMA_36 = """
-- A task the agent assigned under a plan (harvey/agent/tasks.py): a child
-- run with one profile's tools only (researcher, campaign_driver), no chat
-- history, its prompt masked, its answer one JSON object checked against
-- the profile's output (output: its name). parent_turn: the message the
-- agent was answering when it assigned it; turn: the child's own, its tool
-- calls' (agent_tool_call.turn). campaign: the one a campaign_driver works
-- on. status queued, running, done (result_json valid), invalid (the
-- answer kept, masked, with the problem), failed, cancelled; job: its
-- agent_task job.
CREATE TABLE agent_task (
  id INTEGER PRIMARY KEY, plan INTEGER NOT NULL, chat TEXT NOT NULL,
  parent_turn TEXT NOT NULL DEFAULT '', turn TEXT NOT NULL, profile TEXT NOT NULL,
  campaign TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL, output TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued', 'running', 'done', 'failed', 'invalid', 'cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL DEFAULT '',
  problem TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, started_at INTEGER,
  finished_at INTEGER, tokens INTEGER NOT NULL DEFAULT 0, job TEXT NOT NULL DEFAULT ''
);
CREATE INDEX agent_task_plan ON agent_task(plan, id);
CREATE INDEX agent_task_chat ON agent_task(chat, id);
"""


def _to_36(con: sqlite3.Connection) -> None:
    """Schema 36: agent_task. Nothing on file changes: the agent assigns
    its first task when a plan's work is worth handing on."""
    _script(con, SCHEMA_36)


# (target version, step), in order. Append only: a released step is never
# edited, and PARTS stays schema 25.
STEPS = ((26, _to_26), (27, _to_27), (28, _to_28), (29, _to_29), (30, _to_30), (31, _to_31),
         (32, _to_32), (33, _to_33), (34, _to_34), (35, _to_35), (36, _to_36))


def create(con: sqlite3.Connection, *, triggers: bool = True, version: int | None = None) -> None:
    """Create the schema in an empty database: tables, indexes, the core
    contact kinds and roles, zeroed tallies, and the kept-value triggers
    (``triggers=False`` leaves them out, for a bulk load that recounts
    and installs them at the end). ``version``: LATEST by default; the
    upgrade asks for UPGRADE_TARGET (schema 25 as released). All of it in
    one write transaction, the caller's when one is open: a creation that
    fails leaves the file empty (its DDL would otherwise commit statement
    by statement)."""
    version = LATEST if version is None else version
    if not UPGRADE_TARGET <= version <= LATEST:
        raise ValueError(f"schema {version} isn't one this release creates")
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]:
            raise ValueError(f"schema {version} is created in an empty database only")
        for part in PARTS:
            _script(con, part)
        con.executemany("INSERT INTO contact_kinds (id, kind, label, channel) VALUES (?, ?, ?, ?)",
                        CORE_KINDS)
        con.executemany("INSERT INTO tally (name, n) VALUES (?, 0)", [(t,) for t in TALLIES])
        con.execute("INSERT INTO revision (name, n) VALUES ('identity', 0)")
        for target, step in STEPS:
            if target <= version:
                step(con)
        if triggers:
            from harvey.overlay import triggers as kept

            kept.install(con)
        con.execute(f"PRAGMA user_version = {version}")
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own and con.in_transaction:
            con.execute("ROLLBACK")
        raise


def step_up(con: sqlite3.Connection) -> int:
    """Take a file of schema UPGRADE_TARGET or later to LATEST in place
    (an autocommit connection): every step it lacks, in one write
    transaction, its version read again inside it (another process may
    have just done it). Returns the version it is at; any other version
    (older: the upgrade's; newer: a newer Harvey's) is left as it is."""
    con.execute("BEGIN IMMEDIATE")
    try:
        found = version(con)
        if UPGRADE_TARGET <= found < LATEST:
            for target, step in STEPS:
                if found < target:
                    step(con)
                    found = target
            con.execute(f"PRAGMA user_version = {found}")
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return found


_SHAPE: dict[str, set[str]] | None = None


def missing(con: sqlite3.Connection) -> list[str]:
    """What this release's schema has that the open database lacks
    (tables, ``table.column``): a file made by an earlier build of the
    same schema version, which no version number tells apart."""
    global _SHAPE
    if _SHAPE is None:
        mem = sqlite3.connect(":memory:")
        try:
            create(mem, triggers=False)
            _SHAPE = _columns(mem)
        finally:
            mem.close()
    have = _columns(con)
    out = []
    for table, cols in sorted(_SHAPE.items()):
        if table not in have:
            out.append(table)
        else:
            out += [f"{table}.{c}" for c in sorted(cols - have[table])]
    return out


def _columns(con: sqlite3.Connection) -> dict[str, set[str]]:
    rows = con.execute("SELECT name, sql FROM main.sqlite_master WHERE type = 'table' "
                       "AND name NOT LIKE 'sqlite!_%' ESCAPE '!'").fetchall()
    virtual = [n for n, sql in rows if (sql or "").upper().startswith("CREATE VIRTUAL")]
    # (Virtual tables and their shadow tables are the module's business.)
    tables = [n for n, _ in rows if n not in virtual
              and not any(n.startswith(v + "_") for v in virtual)]
    return {t: {r[1] for r in con.execute(f"PRAGMA main.table_info(\"{t}\")")} for t in tables}


def guard_names(con: sqlite3.Connection) -> tuple[str, ...]:
    """The guards this file should have: schema 26's with its tables."""
    if con.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' "
                   "AND name = 'suppression'").fetchone():
        return GUARD_NAMES + SUPPRESSION_GUARD_NAMES
    return GUARD_NAMES


def install_guards(con: sqlite3.Connection) -> None:
    """Create the guards and the full-text sync triggers that are missing."""
    have = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type = 'trigger'")}
    script = LOCAL_FTS_SYNC + GUARDS
    if len(guard_names(con)) > len(GUARD_NAMES):
        script += SUPPRESSION_GUARDS
    buf = ""
    for line in script.splitlines(keepends=True):
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
