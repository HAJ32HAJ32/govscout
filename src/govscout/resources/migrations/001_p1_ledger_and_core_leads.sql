CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_number TEXT NOT NULL UNIQUE CHECK (
        length(company_number) = 8
        AND company_number = upper(company_number)
        AND company_number NOT GLOB '*[^A-Z0-9]*'
    ),
    legal_name TEXT NOT NULL CHECK (length(trim(legal_name)) > 0),
    legal_form TEXT NOT NULL CHECK (
        legal_form IN ('ltd', 'plc', 'llp', 'cic', 'charitable_company')
    ),
    company_status TEXT NOT NULL CHECK (company_status = 'active'),
    verification_source TEXT NOT NULL CHECK (
        verification_source = 'companies_house_api'
    ),
    companies_house_verified_at TEXT NOT NULL CHECK (
        substr(companies_house_verified_at, -6) = '+00:00'
    ),
    companies_house_profile_hash TEXT NOT NULL CHECK (
        length(companies_house_profile_hash) = 64
        AND companies_house_profile_hash NOT GLOB '*[^0-9a-f]*'
    ),
    contact_name TEXT,
    contact_email TEXT NOT NULL CHECK (length(trim(contact_email)) > 0),
    source_register TEXT NOT NULL CHECK (length(trim(source_register)) > 0),
    eu_facing INTEGER NOT NULL DEFAULT 0 CHECK (eu_facing IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'new' CHECK (
        status IN (
            'new', 'queued', 'sequence_active', 'replied', 'interested',
            'dead', 'won', 'not_interested', 'sequence_complete'
        )
    ),
    outreach_stage INTEGER NOT NULL DEFAULT 0 CHECK (outreach_stage BETWEEN 0 AND 3),
    next_due_at TEXT,
    gmail_thread_id TEXT,
    created_at TEXT NOT NULL CHECK (substr(created_at, -6) = '+00:00')
);

CREATE TABLE sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    sequence_id INTEGER NOT NULL DEFAULT 1 CHECK (sequence_id > 0),
    to_email TEXT NOT NULL CHECK (length(trim(to_email)) > 0),
    stage INTEGER NOT NULL CHECK (stage BETWEEN 0 AND 3),
    template TEXT NOT NULL CHECK (length(trim(template)) > 0),
    subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
    body_hash TEXT NOT NULL CHECK (
        length(body_hash) = 64
        AND body_hash NOT GLOB '*[^0-9a-f]*'
    ),
    word_count INTEGER NOT NULL CHECK (word_count >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('reserved', 'draft', 'sent', 'void', 'failed')
    ),
    created_at TEXT NOT NULL CHECK (substr(created_at, -6) = '+00:00'),
    drafted_at TEXT CHECK (drafted_at IS NULL OR substr(drafted_at, -6) = '+00:00'),
    sent_at TEXT CHECK (sent_at IS NULL OR substr(sent_at, -6) = '+00:00'),
    voided_at TEXT CHECK (voided_at IS NULL OR substr(voided_at, -6) = '+00:00'),
    failed_at TEXT CHECK (failed_at IS NULL OR substr(failed_at, -6) = '+00:00'),
    failure_reason TEXT,
    gmail_draft_id TEXT UNIQUE,
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    reply_class TEXT CHECK (
        reply_class IS NULL OR reply_class IN (
            'positive', 'price', 'question', 'negative', 'bounce'
        )
    ),
    replied_at TEXT CHECK (replied_at IS NULL OR substr(replied_at, -6) = '+00:00'),
    UNIQUE (lead_id, sequence_id, stage),
    CHECK (
        (state = 'reserved' AND drafted_at IS NULL AND sent_at IS NULL
            AND voided_at IS NULL AND failed_at IS NULL
            AND gmail_draft_id IS NULL)
        OR (state = 'draft' AND drafted_at IS NOT NULL AND sent_at IS NULL
            AND voided_at IS NULL AND failed_at IS NULL
            AND gmail_draft_id IS NOT NULL)
        OR (state = 'sent' AND drafted_at IS NOT NULL AND sent_at IS NOT NULL
            AND voided_at IS NULL AND failed_at IS NULL
            AND gmail_draft_id IS NOT NULL)
        OR (state = 'void' AND drafted_at IS NOT NULL AND sent_at IS NULL
            AND voided_at IS NOT NULL AND failed_at IS NULL
            AND gmail_draft_id IS NOT NULL)
        OR (state = 'failed' AND drafted_at IS NULL AND sent_at IS NULL
            AND voided_at IS NULL AND failed_at IS NOT NULL
            AND failure_reason IS NOT NULL AND gmail_draft_id IS NULL)
    )
);

CREATE INDEX idx_sends_created_at ON sends(created_at);
CREATE INDEX idx_sends_lead_id ON sends(lead_id);
CREATE INDEX idx_sends_state_created_at ON sends(state, created_at);
