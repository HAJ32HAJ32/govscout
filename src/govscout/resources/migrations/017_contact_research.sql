CREATE TABLE firm_contact_evidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    action TEXT NOT NULL CHECK (action IN ('assert', 'withdraw')),
    email TEXT CHECK (
        email IS NULL OR (
            length(email) BETWEEN 3 AND 254
            AND email = trim(email)
            AND instr(email, char(0)) = 0
            AND email NOT GLOB '*[^ -~]*'
            AND instr(email, '@') > 1
        )
    ),
    phone TEXT CHECK (
        phone IS NULL OR (
            length(trim(phone)) BETWEEN 3 AND 40
            AND phone = trim(phone)
            AND instr(phone, char(0)) = 0
            AND phone NOT GLOB '*[^ -~]*'
        )
    ),
    contact_name TEXT CHECK (
        contact_name IS NULL OR (
            length(trim(contact_name)) BETWEEN 1 AND 200
            AND contact_name = trim(contact_name)
            AND instr(contact_name, char(0)) = 0
            AND contact_name NOT GLOB '*[^ -~]*'
        )
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
        REFERENCES firm_contact_evidence_events(id),
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
    ),
    CHECK (
        action = 'withdraw'
        OR email IS NOT NULL
        OR phone IS NOT NULL
        OR contact_name IS NOT NULL
    )
);

CREATE INDEX idx_firm_contact_evidence_events_latest
ON firm_contact_evidence_events(firm_id, id DESC);

CREATE TRIGGER firm_contact_evidence_events_legal_insert
BEFORE INSERT ON firm_contact_evidence_events
BEGIN
    SELECT CASE WHEN NEW.fca_source_record_hash IS NOT (
        SELECT source_record_hash FROM fca_firms WHERE id = NEW.firm_id
    ) THEN RAISE(ABORT, 'contact evidence FCA identity mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM fca_processing_jobs AS job
        JOIN collector_imports AS imported ON imported.import_id = job.import_id
        WHERE job.firm_id = NEW.firm_id
          AND job.source_record_hash = NEW.fca_source_record_hash
          AND job.import_id = NEW.collector_import_id
          AND imported.state = 'accepted'
    ) THEN RAISE(ABORT, 'contact evidence Collector import mismatch') END;
    SELECT CASE WHEN NEW.expected_previous_event_id IS NOT (
        SELECT id FROM firm_contact_evidence_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ) THEN RAISE(ABORT, 'stale contact evidence event') END;
    SELECT CASE WHEN NEW.action = 'withdraw' AND COALESCE((
        SELECT action FROM firm_contact_evidence_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ), 'withdraw') != 'assert'
    THEN RAISE(ABORT, 'contact evidence is already absent') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM firm_archive_events AS archive
        WHERE archive.id = (
            SELECT id FROM firm_archive_events
            WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
        ) AND archive.action = 'archive'
    ) THEN RAISE(ABORT, 'archived firm cannot receive contact evidence') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM firm_contact_evidence_events
        WHERE firm_id = NEW.firm_id
          AND occurred_at > NEW.occurred_at
    ) THEN RAISE(ABORT, 'contact evidence time moved backwards') END;
END;

CREATE TRIGGER firm_contact_evidence_events_no_update
BEFORE UPDATE ON firm_contact_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'contact evidence history is immutable');
END;

CREATE TRIGGER firm_contact_evidence_events_no_delete
BEFORE DELETE ON firm_contact_evidence_events
BEGIN
    SELECT RAISE(ABORT, 'contact evidence history cannot be deleted');
END;
