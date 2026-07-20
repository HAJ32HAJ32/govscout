CREATE TABLE candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_register TEXT NOT NULL CHECK (
        source_register = 'LCA member directory'
    ),
    source_url TEXT NOT NULL CHECK (
        substr(source_url, 1, 55) =
            'https://www.legionellacontrolassociation.co.uk/company/'
        AND length(source_url) > 55
    ),
    company_name TEXT NOT NULL CHECK (length(trim(company_name)) > 0),
    source_location TEXT CHECK (
        source_location IS NULL OR trim(source_location) <> ''
    ),
    source_record_hash TEXT NOT NULL CHECK (
        length(source_record_hash) = 64
        AND source_record_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL DEFAULT 'discovered' CHECK (status = 'discovered'),
    discovered_at TEXT NOT NULL CHECK (substr(discovered_at, -6) = '+00:00'),
    last_seen_at TEXT NOT NULL CHECK (substr(last_seen_at, -6) = '+00:00'),
    UNIQUE (source_register, source_url),
    CHECK (last_seen_at >= discovered_at)
);

CREATE INDEX idx_candidates_status_id ON candidates(status, id);

CREATE TRIGGER candidates_control_characters_insert
BEFORE INSERT ON candidates
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 31
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.company_name, char(code)) > 0
       OR instr(NEW.source_location, char(code)) > 0
       OR instr(NEW.source_url, char(code)) > 0
)
OR instr(NEW.company_name, char(127)) > 0
OR instr(NEW.source_location, char(127)) > 0
OR instr(NEW.source_url, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'candidate text contains a control character');
END;

CREATE TRIGGER candidates_control_characters_update
BEFORE UPDATE ON candidates
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 31
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.company_name, char(code)) > 0
       OR instr(NEW.source_location, char(code)) > 0
       OR instr(NEW.source_url, char(code)) > 0
)
OR instr(NEW.company_name, char(127)) > 0
OR instr(NEW.source_location, char(127)) > 0
OR instr(NEW.source_url, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'candidate text contains a control character');
END;
