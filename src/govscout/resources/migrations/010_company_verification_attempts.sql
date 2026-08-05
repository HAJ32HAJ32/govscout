CREATE TABLE company_verification_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    company_number TEXT NOT NULL CHECK (
        length(company_number) = 8
        AND company_number = upper(company_number)
        AND company_number NOT GLOB '*[^A-Z0-9]*'
    ),
    state TEXT NOT NULL CHECK (state IN ('verified', 'ineligible', 'error')),
    reason_code TEXT NOT NULL CHECK (
        length(reason_code) BETWEEN 2 AND 80
        AND reason_code = upper(reason_code)
        AND reason_code NOT GLOB '*[^A-Z0-9_]*'
    ),
    checked_at TEXT NOT NULL CHECK (substr(checked_at, -6) = '+00:00'),
    fca_source_record_hash TEXT NOT NULL CHECK (
        length(fca_source_record_hash) = 64
        AND fca_source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    legal_name TEXT,
    legal_form TEXT CHECK (
        legal_form IS NULL OR legal_form IN ('ltd', 'plc', 'llp', 'cic', 'charitable_company')
    ),
    company_status TEXT CHECK (company_status IS NULL OR company_status = 'active'),
    profile_hash TEXT CHECK (
        profile_hash IS NULL OR (
            length(profile_hash) = 64
            AND profile_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        (state = 'verified'
            AND reason_code = 'VERIFIED'
            AND legal_name IS NOT NULL
            AND length(trim(legal_name)) > 0
            AND legal_form IS NOT NULL
            AND company_status = 'active'
            AND profile_hash IS NOT NULL)
        OR (state IN ('ineligible', 'error')
            AND reason_code != 'VERIFIED'
            AND legal_name IS NULL
            AND legal_form IS NULL
            AND company_status IS NULL
            AND profile_hash IS NULL)
    )
);

CREATE INDEX idx_company_verification_attempts_firm_time
ON company_verification_attempts(firm_id, id);

CREATE INDEX idx_company_verification_attempts_company
ON company_verification_attempts(company_number, state, id);

INSERT INTO company_verification_attempts (
    firm_id, company_number, state, reason_code, checked_at,
    fca_source_record_hash, legal_name, legal_form, company_status, profile_hash
)
SELECT f.id, l.company_number, 'verified', 'VERIFIED',
       l.companies_house_verified_at, f.source_record_hash,
       l.legal_name, l.legal_form, l.company_status,
       l.companies_house_profile_hash
FROM fca_firms f
JOIN leads l ON l.id = f.lead_id;

CREATE TRIGGER company_verification_attempts_no_update
BEFORE UPDATE ON company_verification_attempts
BEGIN
    SELECT RAISE(ABORT, 'company verification attempts are immutable');
END;

CREATE TRIGGER company_verification_attempts_no_delete
BEFORE DELETE ON company_verification_attempts
BEGIN
    SELECT RAISE(ABORT, 'company verification attempts cannot be deleted');
END;

ALTER TABLE qc_runs
ADD COLUMN company_verification_attempt_id INTEGER
REFERENCES company_verification_attempts(id);

CREATE TRIGGER qc_runs_company_verification_insert_guard
BEFORE INSERT ON qc_runs
WHEN
    (NEW.state = 'pass' AND NEW.company_verification_attempt_id IS NULL)
    OR (
        NEW.company_verification_attempt_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM company_verification_attempts AS attempt
            WHERE attempt.id = NEW.company_verification_attempt_id
              AND attempt.firm_id = NEW.firm_id
              AND attempt.state = 'verified'
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'QC requires a verified Companies House verification attempt for this firm'
    );
END;
