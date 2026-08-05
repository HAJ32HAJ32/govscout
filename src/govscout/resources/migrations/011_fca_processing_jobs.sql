CREATE TABLE fca_processing_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    import_id TEXT NOT NULL REFERENCES collector_imports(import_id),
    source_record_hash TEXT NOT NULL CHECK (
        length(source_record_hash) = 64
        AND source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    available_at TEXT NOT NULL CHECK (substr(available_at, -6) = '+00:00'),
    claimed_at TEXT CHECK (claimed_at IS NULL OR substr(claimed_at, -6) = '+00:00'),
    claim_token TEXT CHECK (
        claim_token IS NULL OR (
            length(claim_token) = 32
            AND claim_token NOT GLOB '*[^0-9a-f]*'
        )
    ),
    completed_at TEXT CHECK (completed_at IS NULL OR substr(completed_at, -6) = '+00:00'),
    outcome_code TEXT CHECK (
        outcome_code IS NULL OR (
            length(outcome_code) BETWEEN 2 AND 80
            AND outcome_code = upper(outcome_code)
            AND outcome_code NOT GLOB '*[^A-Z0-9_]*'
        )
    ),
    created_at TEXT NOT NULL CHECK (substr(created_at, -6) = '+00:00'),
    updated_at TEXT NOT NULL CHECK (substr(updated_at, -6) = '+00:00'),
    UNIQUE (firm_id, source_record_hash),
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

CREATE INDEX idx_fca_processing_jobs_due
ON fca_processing_jobs(state, available_at, id);
