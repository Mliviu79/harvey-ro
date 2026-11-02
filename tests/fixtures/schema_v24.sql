-- The empty schema 24 database Harvey's migrations made, before the slim
-- schema 25: the upgrade's tests build their old databases from it.
CREATE TABLE companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        description TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , tech_stack_json TEXT DEFAULT '[]', signals_json TEXT DEFAULT '[]', external_id TEXT DEFAULT '', phone TEXT DEFAULT '', prospects_checked_at TIMESTAMP DEFAULT NULL, profile_checked_at TIMESTAMP DEFAULT NULL, cui TEXT DEFAULT '', address TEXT DEFAULT '', merged_into TEXT DEFAULT '', website_trust TEXT DEFAULT '', website_source TEXT DEFAULT '');
CREATE TABLE prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        phone_verified INTEGER DEFAULT 0,
        linkedin_url TEXT DEFAULT '',
        title TEXT DEFAULT '',
        seniority TEXT DEFAULT '',
        department TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        company TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , email_status TEXT DEFAULT '', timezone TEXT DEFAULT '');
CREATE TABLE campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        instantly_campaign_id TEXT DEFAULT '',
        sequence_json TEXT DEFAULT '[]',
        prospect_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , codex_calls INTEGER DEFAULT 0);
CREATE TABLE processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE INDEX idx_companies_domain ON companies(domain);
CREATE INDEX idx_prospects_company_id ON prospects(company_id);
CREATE INDEX idx_prospects_status ON prospects(status);
CREATE INDEX idx_prospects_email ON prospects(email);
CREATE INDEX idx_campaigns_status ON campaigns(status);
CREATE INDEX idx_conversations_status ON conversations(status);
CREATE INDEX idx_feedback_entity ON feedback(entity_type, entity_id);
CREATE INDEX idx_usage_date ON usage_log(date);
CREATE UNIQUE INDEX uq_prospects_email
        ON prospects(email) WHERE email != '';
CREATE UNIQUE INDEX uq_prospects_linkedin
        ON prospects(linkedin_url) WHERE linkedin_url != '';
CREATE UNIQUE INDEX uq_companies_domain
        ON companies(domain) WHERE domain != '';
CREATE INDEX idx_conversations_campaign_id ON conversations(campaign_id);
CREATE INDEX idx_conversations_prospect_id ON conversations(prospect_id);
CREATE INDEX idx_conversations_intent ON conversations(intent);
CREATE INDEX idx_prospects_name_company
        ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
CREATE INDEX idx_prospects_status_updated ON prospects(status, updated_at);
CREATE INDEX idx_actions_created_at ON actions(created_at);
CREATE TABLE usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        request_key TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER DEFAULT 0,
        num_turns INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0,
        source TEXT DEFAULT 'result_json'
    , provider TEXT DEFAULT 'claude');
CREATE INDEX idx_usage_events_created ON usage_events(created_at);
CREATE INDEX idx_usage_events_agent ON usage_events(agent);
CREATE INDEX idx_usage_events_session ON usage_events(session_id);
CREATE UNIQUE INDEX uq_usage_events_request
        ON usage_events(request_key) WHERE request_key != '';
CREATE TABLE email_patterns (
        domain TEXT PRIMARY KEY,
        pattern TEXT DEFAULT '',
        source TEXT DEFAULT '',
        confidence REAL DEFAULT 0.0,
        mx_type TEXT DEFAULT '',
        is_catch_all INTEGER DEFAULT -1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE outbox (
        id TEXT PRIMARY KEY,
        campaign_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        step INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'sequence',
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        send_at TIMESTAMP,
        sent_at TIMESTAMP,
        provider TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        thread_ref TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , approved_by TEXT DEFAULT '');
CREATE UNIQUE INDEX uq_outbox_campaign_step
        ON outbox(campaign_id, prospect_id, step)
        WHERE campaign_id != '' AND kind = 'sequence';
CREATE INDEX idx_outbox_status_send_at ON outbox(status, send_at);
CREATE INDEX idx_outbox_prospect ON outbox(prospect_id);
CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        status TEXT DEFAULT 'proposed',
        confidence_floor REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , module TEXT DEFAULT '', subject TEXT DEFAULT 'company', unit TEXT DEFAULT '', cardinality TEXT DEFAULT 'one', keyed_by TEXT DEFAULT '', absence TEXT DEFAULT 'unknown', when_known TEXT DEFAULT 'enrich', available INTEGER DEFAULT 1);
CREATE TABLE observations (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    , module TEXT DEFAULT '', capability TEXT DEFAULT '', origin_company_id TEXT DEFAULT '');
CREATE INDEX idx_obs_company ON observations(company_id);
CREATE INDEX idx_obs_signal ON observations(signal_code);
CREATE INDEX idx_obs_company_signal
        ON observations(company_id, signal_code, observed_at);
CREATE INDEX idx_obs_run ON observations(run_id);
CREATE TRIGGER trg_observations_signal_known
    BEFORE INSERT ON observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in signal_codes vocabulary');
    END;
CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );
CREATE INDEX idx_runs_stage_started ON runs(stage, started_at);
CREATE UNIQUE INDEX uq_companies_external
        ON companies(external_id) WHERE external_id != '';
CREATE INDEX idx_usage_events_provider ON usage_events(provider);
CREATE INDEX idx_actions_type_created ON actions(action_type, created_at);
CREATE UNIQUE INDEX uq_companies_cui
        ON companies(cui) WHERE cui != '';
CREATE TABLE caen_classes (
        code TEXT NOT NULL,
        version INTEGER NOT NULL,
        name_ro TEXT DEFAULT '',
        name_en TEXT DEFAULT '',
        keywords TEXT DEFAULT '',
        PRIMARY KEY (code, version)
    );
CREATE TABLE company_activities (
        company_id TEXT NOT NULL,
        code TEXT NOT NULL,
        version INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        confidence REAL DEFAULT 1.0,
        PRIMARY KEY (company_id, code, version, source)
    );
CREATE INDEX idx_company_activities_code
        ON company_activities(code, version);
CREATE TABLE company_search_ids (
        rid INTEGER PRIMARY KEY,
        company_id TEXT NOT NULL UNIQUE
    );
CREATE VIRTUAL TABLE company_search USING fts5(
        company_id UNINDEXED, name, activity, place,
        tokenize = 'unicode61 remove_diacritics 2'
    );
CREATE TRIGGER trg_company_search_insert
    AFTER INSERT ON companies
    BEGIN
        INSERT OR IGNORE INTO company_search_ids (company_id) VALUES (NEW.id);
        DELETE FROM company_search WHERE rowid =
            (SELECT rid FROM company_search_ids WHERE company_id = NEW.id);
        INSERT INTO company_search (rowid, company_id, name, activity, place)
            SELECT i.rid, s.company_id, s.name, s.activity, s.place
            FROM company_search_source s
            JOIN company_search_ids i ON i.company_id = s.company_id
            WHERE s.company_id = NEW.id;
    END;
CREATE TRIGGER trg_company_search_update
    AFTER UPDATE OF name, industry, location, address ON companies
    BEGIN
        INSERT OR IGNORE INTO company_search_ids (company_id) VALUES (NEW.id);
        DELETE FROM company_search WHERE rowid =
            (SELECT rid FROM company_search_ids WHERE company_id = NEW.id);
        INSERT INTO company_search (rowid, company_id, name, activity, place)
            SELECT i.rid, s.company_id, s.name, s.activity, s.place
            FROM company_search_source s
            JOIN company_search_ids i ON i.company_id = s.company_id
            WHERE s.company_id = NEW.id;
    END;
CREATE TRIGGER trg_company_search_delete
    AFTER DELETE ON companies
    BEGIN
        DELETE FROM company_search WHERE rowid =
            (SELECT rid FROM company_search_ids WHERE company_id = OLD.id);
        DELETE FROM company_search_ids WHERE company_id = OLD.id;
        DELETE FROM company_activities WHERE company_id = OLD.id;
    END;
CREATE TABLE company_search_dirty (
        company_id TEXT PRIMARY KEY
    );
CREATE TRIGGER trg_company_activities_insert
    AFTER INSERT ON company_activities
    BEGIN
        INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (NEW.company_id);
    END;
CREATE TRIGGER trg_company_activities_delete
    AFTER DELETE ON company_activities
    BEGIN
        INSERT OR IGNORE INTO company_search_dirty (company_id) VALUES (OLD.company_id);
    END;
CREATE TRIGGER trg_observations_activity
    AFTER INSERT ON observations
    WHEN NEW.signal_code = 'ACTIVITY_CODE' AND COALESCE(NEW.company_id, '') != ''
         AND instr(NEW.value_text, '/') > 1
    BEGIN
        INSERT OR IGNORE INTO company_activities
            (company_id, code, version, source, confidence)
        VALUES (NEW.company_id,
                substr(NEW.value_text, 1, instr(NEW.value_text, '/') - 1),
                CAST(substr(NEW.value_text, instr(NEW.value_text, '/') + 1) AS INTEGER),
                COALESCE(NULLIF(NEW.collector, ''), 'observation'),
                COALESCE(NEW.confidence, 1.0));
    END;
CREATE TABLE company_ids (
        company_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        source TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (kind, value, company_id)
    );
CREATE INDEX idx_company_ids_company ON company_ids(company_id);
CREATE TABLE match_candidates (
        company_a TEXT NOT NULL,
        company_b TEXT NOT NULL,
        score REAL DEFAULT 0,
        reasons_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_at TIMESTAMP DEFAULT NULL,
        PRIMARY KEY (company_a, company_b)
    );
CREATE INDEX idx_match_candidates_status ON match_candidates(status);
CREATE INDEX idx_companies_merged ON companies(merged_into)
        WHERE merged_into != '';
CREATE TABLE company_merges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        survivor TEXT NOT NULL,
        merged TEXT NOT NULL,
        score REAL,
        reasons_json TEXT DEFAULT '[]',
        decided_by TEXT DEFAULT '',
        filled_json TEXT DEFAULT '{}',
        moved_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        undone_at TIMESTAMP DEFAULT NULL
    , group_id INTEGER DEFAULT 0);
CREATE INDEX idx_company_merges_merged ON company_merges(merged);
CREATE TABLE company_merge_moves (
        merge_id INTEGER NOT NULL,
        tbl TEXT NOT NULL,
        key1 TEXT NOT NULL,
        key2 TEXT DEFAULT '',
        key3 TEXT DEFAULT ''
    );
CREATE INDEX idx_company_merge_moves ON company_merge_moves(merge_id, tbl, key1);
CREATE VIEW company_search_source AS
    SELECT c.id AS company_id,
           COALESCE(c.name, '') AS name,
           TRIM(COALESCE((
               SELECT group_concat(DISTINCT a.code || ' ' || COALESCE(k.name_ro, '')
                      || ' ' || COALESCE(k.name_en, '') || ' ' || COALESCE(k.keywords, ''))
               FROM company_activities a
               LEFT JOIN caen_classes k ON k.code = a.code AND k.version = a.version
               WHERE a.company_id = c.id), '') || ' ' || COALESCE(c.industry, '')) AS activity,
           TRIM(COALESCE(c.location, '') || ' ' || COALESCE(c.address, '')) AS place
    FROM companies c
    WHERE COALESCE(c.merged_into, '') = '';
CREATE TRIGGER trg_observations_phone
    AFTER INSERT ON observations
    WHEN NEW.signal_code = 'PHONE_NUMBER' AND COALESCE(NEW.company_id, '') != ''
         AND NEW.value_text LIKE '+%'
    BEGIN
        INSERT OR IGNORE INTO company_ids (company_id, kind, value, source, confidence)
        VALUES (NEW.company_id, 'phone', NEW.value_text,
                COALESCE(NEW.collector, ''), COALESCE(NEW.confidence, 1.0));
    END;
CREATE INDEX idx_obs_origin ON observations(origin_company_id)
        WHERE origin_company_id != '';
CREATE TABLE job_batches (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        label TEXT DEFAULT '',
        params_json TEXT DEFAULT '{}',
        max_spend REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        lane TEXT NOT NULL,
        module TEXT DEFAULT '',
        capability TEXT DEFAULT '',
        label TEXT DEFAULT '',
        params_json TEXT DEFAULT '{}',
        dedupe_key TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        progress_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}',
        est_cost REAL DEFAULT 0.0,
        reserved REAL DEFAULT 0.0,
        actual_cost REAL DEFAULT 0.0,
        attempts INTEGER DEFAULT 0,
        not_before TIMESTAMP,
        error TEXT DEFAULT '',
        paused_reason TEXT DEFAULT '',
        stop_requested TEXT DEFAULT '',
        batch_id TEXT DEFAULT '',
        parent_id TEXT DEFAULT '',
        worker TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE UNIQUE INDEX idx_jobs_dedupe ON jobs(dedupe_key)
        WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'paused');
CREATE INDEX idx_jobs_lane ON jobs(lane, status, created_at);
CREATE INDEX idx_jobs_batch ON jobs(batch_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE worker_lease (
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        pid INTEGER DEFAULT 0,
        host TEXT DEFAULT '',
        heartbeat_at REAL NOT NULL,
        started_at REAL NOT NULL
    );
CREATE TABLE contact_kinds (
        kind TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        channel TEXT DEFAULT '',
        module TEXT DEFAULT '',
        available INTEGER DEFAULT 1
    );
CREATE TABLE contacts (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        display TEXT DEFAULT '',
        line_type TEXT DEFAULT '',
        status TEXT DEFAULT '',
        do_not_contact INTEGER DEFAULT 0,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (kind, value)
    );
CREATE INDEX idx_contacts_kind ON contacts(kind, line_type, status);
CREATE INDEX idx_contacts_first_seen ON contacts(first_seen);
CREATE TRIGGER trg_contacts_kind_known
    BEFORE INSERT ON contacts
    FOR EACH ROW
    WHEN NEW.kind NOT IN (SELECT kind FROM contact_kinds)
    BEGIN
        SELECT RAISE(ABORT, 'unknown contact kind: not in contact_kinds');
    END;
CREATE TABLE people (
        id INTEGER PRIMARY KEY,
        company_id TEXT NOT NULL,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        role TEXT DEFAULT '',
        source TEXT DEFAULT '',
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        origin_company_id TEXT DEFAULT '',
        UNIQUE (company_id, name_norm)
    );
CREATE INDEX idx_people_first_seen ON people(first_seen);
CREATE TABLE locations (
        id INTEGER PRIMARY KEY,
        company_id TEXT NOT NULL,
        name TEXT DEFAULT '',
        address TEXT DEFAULT '',
        locality TEXT DEFAULT '',
        county TEXT DEFAULT '',
        country TEXT DEFAULT '',
        point TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        source TEXT DEFAULT '',
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        origin_company_id TEXT DEFAULT ''
    );
CREATE INDEX idx_locations_company ON locations(company_id);
CREATE UNIQUE INDEX uq_locations_listing
        ON locations(company_id, external_id) WHERE external_id != '';
CREATE TABLE company_contacts (
        id INTEGER PRIMARY KEY,
        company_id TEXT NOT NULL,
        contact_id INTEGER NOT NULL,
        person_id INTEGER NOT NULL DEFAULT 0,
        location_id INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '',
        capability TEXT DEFAULT '',
        evidence_url TEXT DEFAULT '',
        trust TEXT DEFAULT 'unconfirmed',
        confidence REAL DEFAULT 1.0,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        origin_company_id TEXT DEFAULT '',
        UNIQUE (company_id, contact_id, person_id, source)
    );
CREATE INDEX idx_company_contacts_contact ON company_contacts(contact_id);
CREATE INDEX idx_company_contacts_person ON company_contacts(person_id)
        WHERE person_id != 0;
CREATE INDEX idx_company_contacts_source ON company_contacts(source);
CREATE TABLE company_sources (
        company_id TEXT NOT NULL,
        source TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '',
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        origin_company_id TEXT DEFAULT '',
        PRIMARY KEY (company_id, source, ref)
    );
CREATE INDEX idx_company_sources_source ON company_sources(source);
CREATE TABLE searches (
        id INTEGER PRIMARY KEY,
        target TEXT NOT NULL,
        target_id TEXT NOT NULL,
        module TEXT NOT NULL,
        capability TEXT NOT NULL DEFAULT '',
        looked_for TEXT DEFAULT '',
        searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        outcome TEXT DEFAULT '',
        found INTEGER DEFAULT 0,
        cost REAL DEFAULT 0,
        batch_id TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        origin_company_id TEXT DEFAULT ''
    );
CREATE INDEX idx_searches_target
        ON searches(target, target_id, module, capability, searched_at);
CREATE TRIGGER trg_prospects_contact_insert
    AFTER INSERT ON prospects
    WHEN NEW.email != '' AND instr(NEW.email, '@') > 1
    BEGIN
        INSERT INTO contacts (kind, value, display, status, do_not_contact)
        VALUES ('email', NEW.email, NEW.email, CASE WHEN NEW.email_status IN ('verified', 'risky', 'guess', 'invalid') THEN NEW.email_status ELSE 'unchecked' END,
                NEW.status = 'opted_out')
        ON CONFLICT(kind, value) DO UPDATE SET
            status = CASE WHEN excluded.status != 'unchecked' THEN excluded.status
                          ELSE contacts.status END,
            do_not_contact = MAX(contacts.do_not_contact, excluded.do_not_contact),
            last_seen = CURRENT_TIMESTAMP;
        INSERT INTO people (company_id, name, name_norm, role, source, origin_company_id)
        SELECT NEW.company_id, TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, '')), LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')),
               COALESCE(NEW.title, ''), COALESCE(NEW.source, ''), NEW.company_id
        WHERE COALESCE(NEW.company_id, '') != '' AND LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')) != ''
          AND NOT EXISTS (SELECT 1 FROM people pr WHERE pr.company_id = NEW.company_id
                          AND pr.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.last_name, '') || ' ' || COALESCE(NEW.first_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')))
        ON CONFLICT(company_id, name_norm) DO UPDATE SET last_seen = CURRENT_TIMESTAMP;
        INSERT INTO company_contacts (company_id, contact_id, person_id, source, evidence_url,
                                      trust, origin_company_id)
        SELECT NEW.company_id, ct.id,
               COALESCE((SELECT pe.id FROM people pe WHERE pe.company_id = NEW.company_id
                         AND pe.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't'))
                         AND pe.name_norm != ''),
                        (SELECT pe.id FROM people pe WHERE pe.company_id = NEW.company_id
                         AND pe.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.last_name, '') || ' ' || COALESCE(NEW.first_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't'))
                         AND pe.name_norm != ''), 0),
               COALESCE(NEW.source, ''), COALESCE(NEW.source_url, ''),
               CASE WHEN substr(NEW.email, instr(NEW.email, '@') + 1) =
                         (SELECT c.domain FROM companies c WHERE c.id = NEW.company_id
                          AND c.domain != '') THEN 'confirmed' ELSE 'likely' END,
               NEW.company_id
        FROM contacts ct
        WHERE ct.kind = 'email' AND ct.value = NEW.email AND COALESCE(NEW.company_id, '') != ''
        ON CONFLICT(company_id, contact_id, person_id, source) DO UPDATE SET last_seen = CURRENT_TIMESTAMP;
    END;
CREATE TRIGGER trg_prospects_contact_update
    AFTER UPDATE OF email, email_status, status ON prospects
    WHEN NEW.email != '' AND instr(NEW.email, '@') > 1
    BEGIN
        INSERT INTO contacts (kind, value, display, status, do_not_contact)
        VALUES ('email', NEW.email, NEW.email, CASE WHEN NEW.email_status IN ('verified', 'risky', 'guess', 'invalid') THEN NEW.email_status ELSE 'unchecked' END,
                NEW.status = 'opted_out')
        ON CONFLICT(kind, value) DO UPDATE SET
            status = CASE WHEN excluded.status != 'unchecked' THEN excluded.status
                          ELSE contacts.status END,
            do_not_contact = CASE WHEN NEW.status = 'opted_out' THEN 1
                                  WHEN OLD.status = 'opted_out' THEN 0
                                  ELSE contacts.do_not_contact END,
            last_seen = CURRENT_TIMESTAMP;
        INSERT INTO people (company_id, name, name_norm, role, source, origin_company_id)
        SELECT NEW.company_id, TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, '')), LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')),
               COALESCE(NEW.title, ''), COALESCE(NEW.source, ''), NEW.company_id
        WHERE COALESCE(NEW.company_id, '') != '' AND LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')) != ''
          AND NOT EXISTS (SELECT 1 FROM people pr WHERE pr.company_id = NEW.company_id
                          AND pr.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.last_name, '') || ' ' || COALESCE(NEW.first_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't')))
        ON CONFLICT(company_id, name_norm) DO UPDATE SET last_seen = CURRENT_TIMESTAMP;
        INSERT INTO company_contacts (company_id, contact_id, person_id, source, evidence_url,
                                      trust, origin_company_id)
        SELECT NEW.company_id, ct.id,
               COALESCE((SELECT pe.id FROM people pe WHERE pe.company_id = NEW.company_id
                         AND pe.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.first_name, '') || ' ' || COALESCE(NEW.last_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't'))
                         AND pe.name_norm != ''),
                        (SELECT pe.id FROM people pe WHERE pe.company_id = NEW.company_id
                         AND pe.name_norm = LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(TRIM(COALESCE(NEW.last_name, '') || ' ' || COALESCE(NEW.first_name, ''))), 'ă', 'a'), 'â', 'a'), 'î', 'i'), 'ș', 's'), 'ş', 's'), 'ț', 't'), 'ţ', 't'), 'Ă', 'a'), 'Â', 'a'), 'Î', 'i'), 'Ș', 's'), 'Ş', 's'), 'Ț', 't'), 'Ţ', 't'))
                         AND pe.name_norm != ''), 0),
               COALESCE(NEW.source, ''), COALESCE(NEW.source_url, ''),
               CASE WHEN substr(NEW.email, instr(NEW.email, '@') + 1) =
                         (SELECT c.domain FROM companies c WHERE c.id = NEW.company_id
                          AND c.domain != '') THEN 'confirmed' ELSE 'likely' END,
               NEW.company_id
        FROM contacts ct
        WHERE ct.kind = 'email' AND ct.value = NEW.email AND COALESCE(NEW.company_id, '') != ''
        ON CONFLICT(company_id, contact_id, person_id, source) DO UPDATE SET last_seen = CURRENT_TIMESTAMP;
    END;
CREATE INDEX idx_companies_website ON companies(website)
        WHERE website != '';
CREATE INDEX idx_companies_created ON companies(created_at);
CREATE INDEX idx_searches_batch ON searches(batch_id) WHERE batch_id != '';
CREATE INDEX idx_companies_live_created ON companies(created_at)
        WHERE COALESCE(merged_into, '') = '';
CREATE INDEX idx_companies_live_name ON companies(name)
        WHERE COALESCE(merged_into, '') = '';
CREATE INDEX idx_companies_live_place ON companies(location)
        WHERE COALESCE(merged_into, '') = '';
CREATE INDEX idx_company_merges_group ON company_merges(group_id)
        WHERE group_id != 0;
CREATE INDEX idx_company_merges_survivor ON company_merges(survivor);
CREATE TRIGGER trg_company_contacts_email
    AFTER INSERT ON company_contacts
    WHEN COALESCE(NEW.company_id, '') != ''
         AND (SELECT kind FROM contacts WHERE id = NEW.contact_id) = 'email'
    BEGIN
        INSERT OR IGNORE INTO company_ids (company_id, kind, value, source, confidence)
        SELECT NEW.company_id, 'email', value, COALESCE(NEW.source, ''),
               COALESCE(NEW.confidence, 1.0)
        FROM contacts WHERE id = NEW.contact_id;
    END;
INSERT INTO "contact_kinds" (kind, label, channel, module, available) VALUES ('email', 'Email', 'email', 'website', 1);
INSERT INTO "contact_kinds" (kind, label, channel, module, available) VALUES ('phone', 'Phone', 'phone', 'phones', 1);
INSERT INTO "contact_kinds" (kind, label, channel, module, available) VALUES ('contact_form', 'Contact form', 'web', 'website', 1);
INSERT INTO "contact_kinds" (kind, label, channel, module, available) VALUES ('linkedin', 'LinkedIn', 'social', 'website', 1);
PRAGMA user_version = 24;
