CREATE TABLE fca_processing_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES fca_processing_jobs(id),
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
    occurred_at TEXT NOT NULL CHECK (substr(occurred_at, -6) = '+00:00')
);

CREATE INDEX idx_fca_processing_job_events_job
ON fca_processing_job_events(job_id, id);

INSERT INTO fca_processing_job_events (
    job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
)
SELECT id, NULL, state, attempt_count, outcome_code, updated_at
FROM fca_processing_jobs;

CREATE TRIGGER fca_processing_jobs_immutable_identity
BEFORE UPDATE ON fca_processing_jobs
WHEN
    NEW.firm_id != OLD.firm_id
    OR NEW.import_id != OLD.import_id
    OR NEW.source_record_hash != OLD.source_record_hash
    OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'processing job identity is immutable');
END;

CREATE TRIGGER fca_processing_jobs_legal_transition
BEFORE UPDATE ON fca_processing_jobs
WHEN
    NEW.firm_id = OLD.firm_id
    AND NEW.import_id = OLD.import_id
    AND NEW.source_record_hash = OLD.source_record_hash
    AND NEW.created_at = OLD.created_at
    AND NOT (
        (
            OLD.state = 'pending'
            AND NEW.state = 'running'
            AND NEW.attempt_count = OLD.attempt_count + 1
        )
        OR (
            OLD.state = 'running'
            AND NEW.state = 'pending'
            AND NEW.attempt_count = OLD.attempt_count
        )
        OR (
            OLD.state = 'running'
            AND NEW.state = 'succeeded'
            AND NEW.attempt_count = OLD.attempt_count
        )
        OR (
            OLD.state = 'running'
            AND NEW.state = 'failed'
            AND NEW.attempt_count = OLD.attempt_count
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'illegal or terminal processing job transition');
END;

CREATE TRIGGER fca_processing_jobs_no_delete
BEFORE DELETE ON fca_processing_jobs
BEGIN
    SELECT RAISE(ABORT, 'processing jobs cannot be deleted');
END;

CREATE TRIGGER fca_processing_jobs_record_insert
AFTER INSERT ON fca_processing_jobs
BEGIN
    INSERT INTO fca_processing_job_events (
        job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
    ) VALUES (
        NEW.id, NULL, NEW.state, NEW.attempt_count, NEW.outcome_code, NEW.updated_at
    );
END;

CREATE TRIGGER fca_processing_jobs_record_transition
AFTER UPDATE ON fca_processing_jobs
BEGIN
    INSERT INTO fca_processing_job_events (
        job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
    ) VALUES (
        NEW.id, OLD.state, NEW.state, NEW.attempt_count, NEW.outcome_code, NEW.updated_at
    );
END;

CREATE TRIGGER fca_processing_job_events_no_update
BEFORE UPDATE ON fca_processing_job_events
BEGIN
    SELECT RAISE(ABORT, 'processing job events are immutable');
END;

CREATE TRIGGER fca_processing_job_events_no_delete
BEFORE DELETE ON fca_processing_job_events
BEGIN
    SELECT RAISE(ABORT, 'processing job events cannot be deleted');
END;
