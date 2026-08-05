CREATE TABLE firm_website_evidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    action TEXT NOT NULL CHECK (action IN ('assert', 'withdraw')),
    website_url TEXT NOT NULL CHECK (
        length(website_url) BETWEEN 9 AND 2048
        AND website_url = trim(website_url)
    ),
    evidence_url TEXT NOT NULL CHECK (
        length(evidence_url) BETWEEN 9 AND 2048
        AND evidence_url = trim(evidence_url)
    ),
    justification TEXT NOT NULL CHECK (
        length(trim(justification)) BETWEEN 10 AND 1000
        AND justification = trim(justification)
        AND instr(justification, char(0)) = 0
        AND justification NOT GLOB '*[^ -~]*'
    ),
    actor TEXT NOT NULL CHECK (
        length(trim(actor)) BETWEEN 1 AND 100
        AND actor = trim(actor)
        AND instr(actor, char(0)) = 0
        AND actor NOT GLOB '*[^ -~]*'
    ),
    fca_source_record_hash TEXT NOT NULL CHECK (
        length(fca_source_record_hash) = 64
        AND fca_source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    collector_import_id TEXT NOT NULL REFERENCES collector_imports(import_id),
    expected_previous_event_id INTEGER
        REFERENCES firm_website_evidence_events(id),
    occurred_at TEXT NOT NULL CHECK (
        instr(occurred_at, char(0)) = 0
        AND julianday(occurred_at) IS NOT NULL
        AND substr(occurred_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(occurred_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(occurred_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', occurred_at) = substr(occurred_at, 1, 19)
        AND (
            occurred_at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR occurred_at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    )
);

CREATE INDEX idx_firm_website_evidence_events_latest
ON firm_website_evidence_events(firm_id, id DESC);

CREATE TRIGGER firm_website_evidence_events_url_guard
BEFORE INSERT ON firm_website_evidence_events
WHEN NOT (
    substr(NEW.website_url, 1, 8) = 'https://'
    AND instr(substr(NEW.website_url, 9), '/') > 1
    AND substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ))
    AND substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND length(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    )) <= 253
    AND substr(substr(NEW.website_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.website_url, 9),
        instr(substr(NEW.website_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.website_url, 9), 1,
        instr(substr(NEW.website_url, 9), '/') - 1
    ), '-.') = 0
    AND NOT EXISTS (
        WITH RECURSIVE labels(label, remainder) AS (
            SELECT
                substr(host || '.', 1, instr(host || '.', '.') - 1),
                substr(host || '.', instr(host || '.', '.') + 1)
            FROM (
                SELECT substr(
                    substr(NEW.website_url, 9), 1,
                    instr(substr(NEW.website_url, 9), '/') - 1
                ) AS host
            )
            UNION ALL
            SELECT
                substr(remainder, 1, instr(remainder || '.', '.') - 1),
                substr(remainder, instr(remainder || '.', '.') + 1)
            FROM labels WHERE remainder != ''
        )
        SELECT 1 FROM labels WHERE length(label) NOT BETWEEN 1 AND 63
    )
    AND instr(substr(NEW.website_url, 9), ':') = 0
    AND instr(substr(NEW.website_url, 9), '@') = 0
    AND instr(NEW.website_url, '?') = 0
    AND instr(NEW.website_url, '#') = 0
    AND instr(NEW.website_url, '%') = 0
    AND instr(NEW.website_url, '\') = 0
    AND instr(NEW.website_url, char(0)) = 0
    AND length(CAST(NEW.website_url AS BLOB)) = length(NEW.website_url)
    AND NEW.website_url NOT GLOB
        ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
    AND instr(
        substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')),
        '//'
    ) = 0
    AND instr(
        substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')),
        '/./'
    ) = 0
    AND instr(
        substr(NEW.website_url, 9 + instr(substr(NEW.website_url, 9), '/')),
        '/../'
    ) = 0
    AND substr(NEW.website_url, -2) != '/.'
    AND substr(NEW.website_url, -3) != '/..'
    AND substr(NEW.evidence_url, 1, 8) = 'https://'
    AND instr(substr(NEW.evidence_url, 9), '/') > 1
    AND substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ) = lower(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ))
    AND length(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    )) <= 253
    AND substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ) NOT GLOB '*[^a-z0-9.-]*'
    AND substr(substr(NEW.evidence_url, 9), 1, 1) NOT IN ('.', '-')
    AND substr(
        substr(NEW.evidence_url, 9),
        instr(substr(NEW.evidence_url, 9), '/') - 1, 1
    ) NOT IN ('.', '-')
    AND instr(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ), '..') = 0
    AND instr(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ), '.-') = 0
    AND instr(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ), '-.') = 0
    AND NOT EXISTS (
        WITH RECURSIVE labels(label, remainder) AS (
            SELECT
                substr(host || '.', 1, instr(host || '.', '.') - 1),
                substr(host || '.', instr(host || '.', '.') + 1)
            FROM (
                SELECT substr(
                    substr(NEW.evidence_url, 9), 1,
                    instr(substr(NEW.evidence_url, 9), '/') - 1
                ) AS host
            )
            UNION ALL
            SELECT
                substr(remainder, 1, instr(remainder || '.', '.') - 1),
                substr(remainder, instr(remainder || '.', '.') + 1)
            FROM labels WHERE remainder != ''
        )
        SELECT 1 FROM labels WHERE length(label) NOT BETWEEN 1 AND 63
    )
    AND instr(substr(NEW.evidence_url, 9), ':') = 0
    AND instr(substr(
        substr(NEW.evidence_url, 9), 1,
        instr(substr(NEW.evidence_url, 9), '/') - 1
    ), '@') = 0
    AND instr(NEW.evidence_url, '#') = 0
    AND instr(NEW.evidence_url, '%') = 0
    AND instr(NEW.evidence_url, '\') = 0
    AND instr(NEW.evidence_url, char(0)) = 0
    AND length(CAST(NEW.evidence_url AS BLOB)) = length(NEW.evidence_url)
    AND NEW.evidence_url NOT GLOB
        ('*[' || char(1) || '-' || char(32) || char(127) || ']*')
)
BEGIN
    SELECT RAISE(ABORT, 'website evidence URLs must be canonical HTTPS');
END;

CREATE TRIGGER firm_website_evidence_events_legal_insert
BEFORE INSERT ON firm_website_evidence_events
BEGIN
    SELECT CASE WHEN NEW.fca_source_record_hash IS NOT (
        SELECT source_record_hash FROM fca_firms WHERE id = NEW.firm_id
    ) THEN RAISE(ABORT, 'website evidence FCA identity mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM fca_processing_jobs AS job
        JOIN collector_imports AS imported ON imported.import_id = job.import_id
        WHERE job.firm_id = NEW.firm_id
          AND job.source_record_hash = NEW.fca_source_record_hash
          AND job.import_id = NEW.collector_import_id
          AND imported.state = 'accepted'
    ) THEN RAISE(ABORT, 'website evidence Collector import mismatch') END;
    SELECT CASE WHEN NEW.expected_previous_event_id IS NOT (
        SELECT id FROM firm_website_evidence_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ) THEN RAISE(ABORT, 'stale website evidence event') END;
    SELECT CASE WHEN NEW.action = 'withdraw' AND COALESCE((
        SELECT action FROM firm_website_evidence_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ), 'withdraw') != 'assert'
    THEN RAISE(ABORT, 'website evidence is already absent') END;
    SELECT CASE WHEN NEW.action = 'withdraw' AND NEW.website_url IS NOT (
        SELECT website_url FROM firm_website_evidence_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ) THEN RAISE(ABORT, 'withdrawal website does not match assertion') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM firm_archive_events AS archive
        WHERE archive.id = (
            SELECT id FROM firm_archive_events
            WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
        ) AND archive.action = 'archive'
    ) THEN RAISE(ABORT, 'archived firm cannot receive website evidence') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM firm_website_evidence_events
        WHERE firm_id = NEW.firm_id
          AND occurred_at > NEW.occurred_at
    ) THEN RAISE(ABORT, 'website evidence time moved backwards') END;
END;

CREATE TRIGGER firm_website_evidence_events_no_update
BEFORE UPDATE ON firm_website_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'website evidence history is immutable');
END;

CREATE TRIGGER firm_website_evidence_events_no_delete
BEFORE DELETE ON firm_website_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'website evidence history cannot be deleted');
END;

CREATE TABLE fca_reprocessing_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    source_job_id INTEGER NOT NULL REFERENCES fca_processing_jobs(id),
    source_record_hash TEXT NOT NULL CHECK (
        length(source_record_hash) = 64
        AND source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    website_evidence_event_id INTEGER NOT NULL
        REFERENCES firm_website_evidence_events(id),
    company_verification_attempt_id INTEGER NOT NULL
        REFERENCES company_verification_attempts(id),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64
        AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    requested_by TEXT NOT NULL CHECK (
        length(trim(requested_by)) BETWEEN 1 AND 100
        AND requested_by = trim(requested_by)
        AND instr(requested_by, char(0)) = 0
        AND requested_by NOT GLOB '*[^ -~]*'
    ),
    request_reason TEXT NOT NULL CHECK (
        length(trim(request_reason)) BETWEEN 10 AND 500
        AND request_reason = trim(request_reason)
        AND instr(request_reason, char(0)) = 0
        AND request_reason NOT GLOB '*[^ -~]*'
    ),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    available_at TEXT NOT NULL CHECK (
        instr(available_at, char(0)) = 0
        AND julianday(available_at) IS NOT NULL
        AND substr(available_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(available_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(available_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', available_at) = substr(available_at, 1, 19)
        AND (
            available_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR available_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    ),
    claimed_at TEXT CHECK (
        claimed_at IS NULL OR (
            instr(claimed_at, char(0)) = 0
            AND julianday(claimed_at) IS NOT NULL
            AND substr(claimed_at, 12, 2) BETWEEN '00' AND '23'
            AND substr(claimed_at, 15, 2) BETWEEN '00' AND '59'
            AND substr(claimed_at, 18, 2) BETWEEN '00' AND '59'
            AND strftime('%Y-%m-%dT%H:%M:%S', claimed_at) = substr(claimed_at, 1, 19)
            AND (
                claimed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
                OR claimed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
            )
        )
    ),
    claim_token TEXT CHECK (
        claim_token IS NULL OR (
            length(claim_token) = 32
            AND claim_token NOT GLOB '*[^0-9a-f]*'
        )
    ),
    completed_at TEXT CHECK (
        completed_at IS NULL OR (
            instr(completed_at, char(0)) = 0
            AND julianday(completed_at) IS NOT NULL
            AND substr(completed_at, 12, 2) BETWEEN '00' AND '23'
            AND substr(completed_at, 15, 2) BETWEEN '00' AND '59'
            AND substr(completed_at, 18, 2) BETWEEN '00' AND '59'
            AND strftime('%Y-%m-%dT%H:%M:%S', completed_at) = substr(completed_at, 1, 19)
            AND (
                completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
                OR completed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
            )
        )
    ),
    outcome_code TEXT CHECK (
        outcome_code IS NULL OR (
            length(outcome_code) BETWEEN 2 AND 80
            AND outcome_code = upper(outcome_code)
            AND outcome_code NOT GLOB '*[^A-Z0-9_]*'
        )
    ),
    created_at TEXT NOT NULL CHECK (
        instr(created_at, char(0)) = 0
        AND julianday(created_at) IS NOT NULL
        AND substr(created_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(created_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(created_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND (
            created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    ),
    updated_at TEXT NOT NULL CHECK (
        instr(updated_at, char(0)) = 0
        AND julianday(updated_at) IS NOT NULL
        AND substr(updated_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(updated_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(updated_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', updated_at) = substr(updated_at, 1, 19)
        AND (
            updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    ),
    UNIQUE (firm_id, input_hash),
    CHECK (
        (state = 'pending' AND attempt_count < 3 AND claimed_at IS NULL
            AND claim_token IS NULL AND completed_at IS NULL
            AND (
                (attempt_count = 0 AND outcome_code IS NULL)
                OR (attempt_count BETWEEN 1 AND 2 AND outcome_code IS NOT NULL)
            ))
        OR (state = 'running' AND attempt_count BETWEEN 1 AND 3
            AND claimed_at IS NOT NULL AND claim_token IS NOT NULL
            AND completed_at IS NULL AND outcome_code IS NULL)
        OR (state = 'succeeded' AND claimed_at IS NOT NULL
            AND claim_token IS NOT NULL AND completed_at IS NOT NULL
            AND outcome_code IN ('QC_PASS', 'QC_FAIL'))
        OR (state = 'failed' AND claimed_at IS NOT NULL
            AND claim_token IS NOT NULL AND completed_at IS NOT NULL
            AND outcome_code IS NOT NULL
            AND outcome_code NOT IN ('QC_PASS', 'QC_FAIL'))
    )
);

CREATE INDEX idx_fca_reprocessing_jobs_due
ON fca_reprocessing_jobs(state, available_at, id);

CREATE TRIGGER fca_reprocessing_jobs_insert_guard
BEFORE INSERT ON fca_reprocessing_jobs
WHEN NOT (
    EXISTS (
        SELECT 1
        FROM fca_processing_jobs AS source
        JOIN firm_website_evidence_events AS evidence
          ON evidence.id = NEW.website_evidence_event_id
         AND source.import_id = evidence.collector_import_id
        JOIN collector_imports AS imported
          ON imported.import_id = source.import_id
        WHERE source.id = NEW.source_job_id
          AND source.firm_id = NEW.firm_id
          AND source.source_record_hash = NEW.source_record_hash
          AND imported.state = 'accepted'
    )
    AND EXISTS (
        SELECT 1 FROM firm_website_evidence_events AS evidence
        WHERE evidence.id = NEW.website_evidence_event_id
          AND evidence.firm_id = NEW.firm_id
          AND evidence.action = 'assert'
          AND evidence.fca_source_record_hash = NEW.source_record_hash
          AND evidence.id = (
              SELECT id FROM firm_website_evidence_events
              WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
          )
    )
    AND EXISTS (
        SELECT 1 FROM company_verification_attempts AS attempt
        JOIN fca_firms AS firm ON firm.id = NEW.firm_id
        WHERE attempt.id = NEW.company_verification_attempt_id
          AND attempt.firm_id = NEW.firm_id
          AND attempt.id = (
              SELECT id FROM company_verification_attempts
              WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
          )
          AND attempt.state = 'verified'
          AND attempt.company_number = firm.company_number
          AND attempt.fca_source_record_hash = NEW.source_record_hash
          AND attempt.company_status = 'active'
          AND attempt.legal_form IN ('ltd', 'plc', 'llp', 'cic', 'charitable_company')
          AND attempt.profile_hash IS NOT NULL
    )
    AND NOT EXISTS (
        SELECT 1 FROM firm_archive_events AS archive
        WHERE archive.id = (
            SELECT id FROM firm_archive_events
            WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
        ) AND archive.action = 'archive'
    )
    AND NEW.state = 'pending'
    AND NEW.attempt_count = 0
    AND NEW.claimed_at IS NULL
    AND NEW.claim_token IS NULL
    AND NEW.completed_at IS NULL
    AND NEW.outcome_code IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'reprocessing input is not current');
END;

CREATE TABLE fca_reprocessing_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES fca_reprocessing_jobs(id),
    from_state TEXT CHECK (
        from_state IS NULL OR from_state IN ('pending', 'running', 'succeeded', 'failed')
    ),
    to_state TEXT NOT NULL CHECK (
        to_state IN ('pending', 'running', 'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 3),
    outcome_code TEXT CHECK (
        outcome_code IS NULL OR (
            length(outcome_code) BETWEEN 2 AND 80
            AND outcome_code = upper(outcome_code)
            AND outcome_code NOT GLOB '*[^A-Z0-9_]*'
        )
    ),
    occurred_at TEXT NOT NULL CHECK (
        instr(occurred_at, char(0)) = 0
        AND julianday(occurred_at) IS NOT NULL
        AND substr(occurred_at, 12, 2) BETWEEN '00' AND '23'
        AND substr(occurred_at, 15, 2) BETWEEN '00' AND '59'
        AND substr(occurred_at, 18, 2) BETWEEN '00' AND '59'
        AND strftime('%Y-%m-%dT%H:%M:%S', occurred_at) = substr(occurred_at, 1, 19)
        AND (
            occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
            OR occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        )
    )
);

CREATE INDEX idx_fca_reprocessing_job_events_job
ON fca_reprocessing_job_events(job_id, id);

CREATE TRIGGER fca_reprocessing_job_events_insert_guard
BEFORE INSERT ON fca_reprocessing_job_events
WHEN NOT (
    EXISTS (
        SELECT 1 FROM fca_reprocessing_jobs AS job
        WHERE job.id = NEW.job_id
          AND job.state = NEW.to_state
          AND job.attempt_count = NEW.attempt_count
          AND job.outcome_code IS NEW.outcome_code
          AND job.updated_at = NEW.occurred_at
    )
    AND (
        (
            NOT EXISTS (
                SELECT 1 FROM fca_reprocessing_job_events
                WHERE job_id = NEW.job_id
            )
            AND NEW.from_state IS NULL
            AND NEW.to_state = 'pending'
            AND NEW.attempt_count = 0
            AND NEW.outcome_code IS NULL
        )
        OR (
            NEW.from_state = (
                SELECT to_state FROM fca_reprocessing_job_events
                WHERE job_id = NEW.job_id ORDER BY id DESC LIMIT 1
            )
            AND (
                (NEW.from_state = 'pending' AND NEW.to_state = 'running')
                OR (NEW.from_state = 'running' AND NEW.to_state IN (
                    'pending', 'succeeded', 'failed'
                ))
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'reprocessing event history mismatch');
END;

CREATE TRIGGER fca_reprocessing_jobs_immutable_identity
BEFORE UPDATE ON fca_reprocessing_jobs
WHEN
    NEW.firm_id != OLD.firm_id
    OR NEW.source_job_id != OLD.source_job_id
    OR NEW.source_record_hash != OLD.source_record_hash
    OR NEW.website_evidence_event_id != OLD.website_evidence_event_id
    OR NEW.company_verification_attempt_id != OLD.company_verification_attempt_id
    OR NEW.input_hash != OLD.input_hash
    OR NEW.requested_by != OLD.requested_by
    OR NEW.request_reason != OLD.request_reason
    OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'reprocessing job identity is immutable');
END;

CREATE TRIGGER fca_reprocessing_jobs_legal_transition
BEFORE UPDATE ON fca_reprocessing_jobs
WHEN NOT (
    (OLD.state = 'pending' AND NEW.state = 'running'
        AND NEW.attempt_count = OLD.attempt_count + 1)
    OR (OLD.state = 'running' AND NEW.state = 'pending'
        AND NEW.attempt_count = OLD.attempt_count)
    OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed')
        AND NEW.attempt_count = OLD.attempt_count)
)
BEGIN
    SELECT RAISE(ABORT, 'illegal or terminal reprocessing job transition');
END;

CREATE TRIGGER fca_reprocessing_jobs_no_delete
BEFORE DELETE ON fca_reprocessing_jobs
BEGIN
    SELECT RAISE(ABORT, 'reprocessing jobs cannot be deleted');
END;

CREATE TRIGGER firm_archive_events_no_running_reprocessing
BEFORE INSERT ON firm_archive_events
WHEN NEW.action = 'archive' AND EXISTS (
    SELECT 1 FROM fca_reprocessing_jobs
    WHERE firm_id = NEW.firm_id AND state = 'running'
)
BEGIN
    SELECT RAISE(ABORT, 'cannot archive while reprocessing is running');
END;

CREATE TRIGGER fca_reprocessing_jobs_record_insert
AFTER INSERT ON fca_reprocessing_jobs
BEGIN
    INSERT INTO fca_reprocessing_job_events (
        job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
    ) VALUES (
        NEW.id, NULL, NEW.state, NEW.attempt_count, NEW.outcome_code, NEW.updated_at
    );
END;

CREATE TRIGGER fca_reprocessing_jobs_record_transition
AFTER UPDATE ON fca_reprocessing_jobs
BEGIN
    INSERT INTO fca_reprocessing_job_events (
        job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
    ) VALUES (
        NEW.id, OLD.state, NEW.state, NEW.attempt_count, NEW.outcome_code, NEW.updated_at
    );
END;

CREATE TRIGGER fca_reprocessing_job_events_no_update
BEFORE UPDATE ON fca_reprocessing_job_events
BEGIN
    SELECT RAISE(ABORT, 'reprocessing job events are immutable');
END;

CREATE TRIGGER fca_reprocessing_job_events_no_delete
BEFORE DELETE ON fca_reprocessing_job_events
BEGIN
    SELECT RAISE(ABORT, 'reprocessing job events cannot be deleted');
END;

ALTER TABLE enrichment_runs
ADD COLUMN website_evidence_event_id INTEGER
REFERENCES firm_website_evidence_events(id);

ALTER TABLE enrichment_runs
ADD COLUMN company_verification_attempt_id INTEGER
REFERENCES company_verification_attempts(id);

ALTER TABLE qc_runs
ADD COLUMN website_evidence_event_id INTEGER
REFERENCES firm_website_evidence_events(id);

CREATE TRIGGER enrichment_runs_reprocessing_identity_no_update
BEFORE UPDATE OF website_evidence_event_id, company_verification_attempt_id
ON enrichment_runs
WHEN
    NEW.website_evidence_event_id IS NOT OLD.website_evidence_event_id
    OR NEW.company_verification_attempt_id IS NOT OLD.company_verification_attempt_id
BEGIN
    SELECT RAISE(ABORT, 'enrichment reprocessing identity is immutable');
END;

CREATE TRIGGER enrichment_runs_reprocessing_identity_insert_guard
BEFORE INSERT ON enrichment_runs
WHEN
    (NEW.website_evidence_event_id IS NULL)
    != (NEW.company_verification_attempt_id IS NULL)
    OR (
        NEW.website_evidence_event_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM firm_website_evidence_events AS evidence
            JOIN company_verification_attempts AS attempt
              ON attempt.id = NEW.company_verification_attempt_id
            WHERE evidence.id = NEW.website_evidence_event_id
              AND evidence.firm_id = NEW.firm_id
              AND attempt.firm_id = NEW.firm_id
              AND evidence.fca_source_record_hash = attempt.fca_source_record_hash
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'enrichment reprocessing identity is invalid');
END;

CREATE TRIGGER qc_runs_website_evidence_insert_guard
BEFORE INSERT ON qc_runs
WHEN
    NEW.website_evidence_event_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM enrichment_runs AS run
        WHERE run.id = NEW.enrichment_run_id
          AND run.firm_id = NEW.firm_id
          AND run.website_evidence_event_id = NEW.website_evidence_event_id
    )
BEGIN
    SELECT RAISE(ABORT, 'QC website evidence must match enrichment');
END;
