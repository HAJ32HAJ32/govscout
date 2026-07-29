CREATE TABLE fca_firms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frn TEXT NOT NULL UNIQUE CHECK (
        length(frn) BETWEEN 6 AND 8 AND frn NOT GLOB '*[^0-9]*'
    ),
    firm_name TEXT NOT NULL CHECK (length(trim(firm_name)) > 0),
    fca_status TEXT NOT NULL CHECK (length(trim(fca_status)) > 0),
    firm_type TEXT CHECK (firm_type IS NULL OR length(trim(firm_type)) > 0),
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    source_url TEXT NOT NULL CHECK (
        source_url = 'https://register.fca.org.uk/s/firm?id=' || frn
    ),
    website_url TEXT CHECK (
        website_url IS NULL OR (
            substr(website_url, 1, 8) = 'https://'
            AND length(website_url) > 8
            AND substr(website_url, 9, 1) NOT IN ('/', '?', '#')
            AND instr(website_url, '@') = 0
            AND instr(
                substr(
                    website_url,
                    9,
                    CASE instr(substr(website_url, 9), '/')
                        WHEN 0 THEN length(substr(website_url, 9))
                        ELSE instr(substr(website_url, 9), '/') - 1
                    END
                ),
                ':'
            ) = 0
            AND instr(website_url, '#') = 0
            AND instr(website_url, '?') = 0
            AND instr(website_url, char(9)) = 0
            AND instr(website_url, char(10)) = 0
            AND instr(website_url, char(13)) = 0
            AND instr(website_url, ' ') = 0
            AND instr(website_url, '\') = 0
        )
    ),
    source_location TEXT CHECK (
        source_location IS NULL OR length(trim(source_location)) > 0
    ),
    company_number TEXT CHECK (
        company_number IS NULL OR (
            length(company_number) = 8
            AND company_number = upper(company_number)
            AND company_number NOT GLOB '*[^A-Z0-9]*'
        )
    ),
    lead_id INTEGER UNIQUE REFERENCES leads(id),
    source_record_hash TEXT NOT NULL CHECK (
        length(source_record_hash) = 64
        AND source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    first_seen_at TEXT NOT NULL CHECK (substr(first_seen_at, -6) = '+00:00'),
    last_seen_at TEXT NOT NULL CHECK (substr(last_seen_at, -6) = '+00:00'),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (lead_id IS NULL OR (is_active = 1 AND company_number IS NOT NULL))
);

CREATE INDEX idx_fca_firms_active_id ON fca_firms(is_active, id);
CREATE INDEX idx_fca_firms_lead_id ON fca_firms(lead_id);

CREATE TABLE fca_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    observed_at TEXT NOT NULL CHECK (substr(observed_at, -6) = '+00:00'),
    source_record_hash TEXT NOT NULL CHECK (
        length(source_record_hash) = 64
        AND source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_record TEXT NOT NULL CHECK (
        length(canonical_record) BETWEEN 2 AND 32768
    ),
    UNIQUE (firm_id, source_record_hash)
);

CREATE INDEX idx_fca_observations_firm_time
ON fca_observations(firm_id, observed_at);

CREATE TABLE enrichment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    state TEXT NOT NULL CHECK (state IN ('running', 'complete', 'failed')),
    started_at TEXT NOT NULL CHECK (substr(started_at, -6) = '+00:00'),
    completed_at TEXT CHECK (completed_at IS NULL OR substr(completed_at, -6) = '+00:00'),
    website_url TEXT,
    final_url TEXT,
    page_hash TEXT CHECK (
        page_hash IS NULL OR (
            length(page_hash) = 64 AND page_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    score INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    temperature TEXT CHECK (temperature IS NULL OR temperature IN ('HOT', 'WARM', 'COOL')),
    failure_code TEXT,
    CHECK (
        (state = 'running' AND completed_at IS NULL AND score IS NULL
            AND temperature IS NULL AND failure_code IS NULL)
        OR (state = 'complete' AND completed_at IS NOT NULL AND score IS NOT NULL
            AND temperature IS NOT NULL AND failure_code IS NULL)
        OR (state = 'failed' AND completed_at IS NOT NULL AND score IS NULL
            AND temperature IS NULL AND length(trim(failure_code)) > 0)
    ),
    CHECK (completed_at IS NULL OR completed_at >= started_at)
);

CREATE INDEX idx_enrichment_runs_firm_state
ON enrichment_runs(firm_id, state, completed_at);

CREATE TABLE evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES enrichment_runs(id),
    signal_group TEXT NOT NULL CHECK (
        signal_group IN ('accountability', 'ai_exposure', 'governance_gap', 'site_health')
    ),
    code TEXT NOT NULL CHECK (
        length(code) BETWEEN 2 AND 80 AND code = upper(code)
    ),
    evidence_state TEXT NOT NULL CHECK (
        evidence_state IN ('present', 'absent', 'unknown')
    ),
    weight INTEGER NOT NULL CHECK (weight BETWEEN 0 AND 100),
    source_url TEXT,
    excerpt TEXT CHECK (excerpt IS NULL OR length(excerpt) BETWEEN 1 AND 500),
    observed_at TEXT NOT NULL CHECK (substr(observed_at, -6) = '+00:00'),
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE (run_id, code, source_url),
    CHECK (
        evidence_state != 'present'
        OR (source_url IS NOT NULL AND excerpt IS NOT NULL)
    )
);

CREATE INDEX idx_evidence_items_run_group ON evidence_items(run_id, signal_group);

CREATE TRIGGER evidence_items_running_run_only
BEFORE INSERT ON evidence_items
WHEN (SELECT state FROM enrichment_runs WHERE id = NEW.run_id) IS NOT 'running'
BEGIN
    SELECT RAISE(ABORT, 'evidence cannot be appended to a terminal enrichment run');
END;

CREATE TABLE qc_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    enrichment_run_id INTEGER REFERENCES enrichment_runs(id),
    state TEXT NOT NULL CHECK (state IN ('pass', 'fail')),
    reason_codes TEXT NOT NULL CHECK (length(reason_codes) BETWEEN 2 AND 4096),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    checked_at TEXT NOT NULL CHECK (substr(checked_at, -6) = '+00:00'),
    expires_at TEXT NOT NULL CHECK (substr(expires_at, -6) = '+00:00'),
    CHECK (expires_at > checked_at),
    CHECK (
        (state = 'pass' AND reason_codes = '[]' AND enrichment_run_id IS NOT NULL)
        OR (state = 'fail' AND reason_codes != '[]')
    )
);

CREATE INDEX idx_qc_runs_firm_time ON qc_runs(firm_id, checked_at);

CREATE TABLE firm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    qc_run_id INTEGER REFERENCES qc_runs(id),
    notes TEXT CHECK (notes IS NULL OR length(notes) <= 2000),
    rejection_reason TEXT CHECK (rejection_reason IS NULL OR length(trim(rejection_reason)) > 0),
    reviewed_at TEXT NOT NULL CHECK (substr(reviewed_at, -6) = '+00:00'),
    CHECK (
        (decision = 'approved' AND qc_run_id IS NOT NULL AND rejection_reason IS NULL)
        OR (decision = 'rejected' AND qc_run_id IS NULL AND rejection_reason IS NOT NULL)
    )
);

CREATE INDEX idx_firm_reviews_firm_id ON firm_reviews(firm_id, id);

CREATE TABLE retirement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_register TEXT NOT NULL,
    retired_count INTEGER NOT NULL CHECK (retired_count >= 0),
    leads_before INTEGER NOT NULL CHECK (leads_before >= 0),
    sends_before INTEGER NOT NULL CHECK (sends_before >= 0),
    backup_path TEXT NOT NULL CHECK (length(trim(backup_path)) > 0),
    backup_sha256 TEXT NOT NULL CHECK (
        length(backup_sha256) = 64 AND backup_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    retired_at TEXT NOT NULL CHECK (substr(retired_at, -6) = '+00:00'),
    note TEXT NOT NULL CHECK (length(trim(note)) > 0)
);

CREATE TRIGGER fca_observations_no_update
BEFORE UPDATE ON fca_observations
BEGIN
    SELECT RAISE(ABORT, 'FCA observations are immutable');
END;

CREATE TRIGGER fca_observations_no_delete
BEFORE DELETE ON fca_observations
BEGIN
    SELECT RAISE(ABORT, 'FCA observations cannot be deleted');
END;

CREATE TRIGGER evidence_items_no_update
BEFORE UPDATE ON evidence_items
BEGIN
    SELECT RAISE(ABORT, 'evidence items are immutable');
END;

CREATE TRIGGER evidence_items_no_delete
BEFORE DELETE ON evidence_items
BEGIN
    SELECT RAISE(ABORT, 'evidence items cannot be deleted');
END;

CREATE TRIGGER qc_runs_no_update
BEFORE UPDATE ON qc_runs
BEGIN
    SELECT RAISE(ABORT, 'QC runs are immutable');
END;

CREATE TRIGGER qc_runs_no_delete
BEFORE DELETE ON qc_runs
BEGIN
    SELECT RAISE(ABORT, 'QC runs cannot be deleted');
END;

CREATE TRIGGER firm_reviews_no_update
BEFORE UPDATE ON firm_reviews
BEGIN
    SELECT RAISE(ABORT, 'firm reviews are immutable');
END;

CREATE TRIGGER firm_reviews_no_delete
BEFORE DELETE ON firm_reviews
BEGIN
    SELECT RAISE(ABORT, 'firm reviews cannot be deleted');
END;

CREATE TRIGGER enrichment_runs_no_update
BEFORE UPDATE ON enrichment_runs
WHEN NOT (
    OLD.state = 'running'
    AND NEW.state IN ('complete', 'failed')
    AND NEW.id IS OLD.id
    AND NEW.firm_id IS OLD.firm_id
    AND NEW.started_at IS OLD.started_at
    AND NEW.website_url IS OLD.website_url
    AND NEW.input_hash IS OLD.input_hash
)
BEGIN
    SELECT RAISE(ABORT, 'enrichment runs are immutable');
END;

CREATE TRIGGER enrichment_runs_no_delete
BEFORE DELETE ON enrichment_runs
BEGIN
    SELECT RAISE(ABORT, 'enrichment runs cannot be deleted');
END;

CREATE TRIGGER retirement_events_no_update
BEFORE UPDATE ON retirement_events
BEGIN
    SELECT RAISE(ABORT, 'retirement events are immutable');
END;

CREATE TRIGGER retirement_events_no_delete
BEFORE DELETE ON retirement_events
BEGIN
    SELECT RAISE(ABORT, 'retirement events cannot be deleted');
END;

CREATE TRIGGER linked_fca_identity_no_update
BEFORE UPDATE ON fca_firms
WHEN OLD.lead_id IS NOT NULL AND (
    NEW.frn IS NOT OLD.frn
    OR NEW.firm_name IS NOT OLD.firm_name
    OR NEW.fca_status IS NOT OLD.fca_status
    OR NEW.is_active IS NOT OLD.is_active
    OR NEW.source_url IS NOT OLD.source_url
    OR NEW.company_number IS NOT OLD.company_number
    OR NEW.source_record_hash IS NOT OLD.source_record_hash
    OR NEW.lead_id IS NOT OLD.lead_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked FCA identity is immutable');
END;
