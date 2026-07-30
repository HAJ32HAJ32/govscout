CREATE TABLE collector_devices (
    device_id TEXT PRIMARY KEY CHECK (
        length(device_id) = 32 AND device_id NOT GLOB '*[^0-9a-f]*'
    ),
    display_name TEXT NOT NULL CHECK (
        length(display_name) BETWEEN 1 AND 80 AND display_name = trim(display_name)
    ),
    token_hash TEXT NOT NULL UNIQUE CHECK (
        length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'
    ),
    scope TEXT NOT NULL DEFAULT 'fca_upload' CHECK (scope = 'fca_upload'),
    created_at TEXT NOT NULL CHECK (substr(created_at, -6) = '+00:00'),
    request_window_started_at TEXT CHECK (
        request_window_started_at IS NULL
        OR substr(request_window_started_at, -6) = '+00:00'
    ),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count BETWEEN 0 AND 12),
    last_used_at TEXT CHECK (
        last_used_at IS NULL OR substr(last_used_at, -6) = '+00:00'
    ),
    revoked_at TEXT CHECK (
        revoked_at IS NULL OR substr(revoked_at, -6) = '+00:00'
    ),
    CHECK (last_used_at IS NULL OR last_used_at >= created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    CHECK (
        (request_window_started_at IS NULL AND request_count = 0)
        OR (request_window_started_at IS NOT NULL AND request_count > 0)
    )
);

CREATE TABLE collector_imports (
    import_id TEXT PRIMARY KEY CHECK (
        length(import_id) = 32 AND import_id NOT GLOB '*[^0-9a-f]*'
    ),
    device_id TEXT NOT NULL REFERENCES collector_devices(device_id),
    payload_sha256 TEXT NOT NULL CHECK (
        length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    payload_json TEXT NOT NULL CHECK (
        length(CAST(payload_json AS BLOB)) BETWEEN 1 AND 1000000
        AND json_valid(payload_json)
    ),
    state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'rejected')),
    received_at TEXT NOT NULL CHECK (substr(received_at, -6) = '+00:00'),
    processed_at TEXT CHECK (
        processed_at IS NULL OR substr(processed_at, -6) = '+00:00'
    ),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_code TEXT CHECK (
        error_code IS NULL OR (
            length(error_code) BETWEEN 2 AND 80
            AND error_code = upper(error_code)
            AND error_code NOT GLOB '*[^A-Z0-9_]*'
        )
    ),
    CHECK (
        (state = 'pending' AND processed_at IS NULL AND result_json IS NULL AND error_code IS NULL)
        OR (state = 'accepted' AND processed_at IS NOT NULL AND result_json IS NOT NULL AND error_code IS NULL)
        OR (state = 'rejected' AND processed_at IS NOT NULL AND result_json IS NULL AND error_code IS NOT NULL)
    ),
    CHECK (processed_at IS NULL OR processed_at >= received_at)
);

CREATE INDEX collector_imports_state_received
ON collector_imports (state, received_at, import_id);

CREATE TRIGGER collector_devices_no_delete
BEFORE DELETE ON collector_devices
BEGIN
    SELECT RAISE(ABORT, 'collector devices cannot be deleted; revoke them');
END;

CREATE TRIGGER collector_devices_identity_immutable
BEFORE UPDATE OF device_id, display_name, token_hash, created_at ON collector_devices
BEGIN
    SELECT RAISE(ABORT, 'collector device identity is immutable');
END;

CREATE TRIGGER collector_devices_revocation_monotonic
BEFORE UPDATE OF revoked_at ON collector_devices
WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN
    SELECT RAISE(ABORT, 'collector device revocation is immutable');
END;

CREATE TRIGGER collector_imports_no_delete
BEFORE DELETE ON collector_imports
BEGIN
    SELECT RAISE(ABORT, 'collector imports cannot be deleted');
END;

CREATE TRIGGER collector_imports_payload_immutable
BEFORE UPDATE OF import_id, device_id, payload_sha256, payload_json, received_at
ON collector_imports
BEGIN
    SELECT RAISE(ABORT, 'collector import payload is immutable');
END;

CREATE TRIGGER collector_imports_terminal_immutable
BEFORE UPDATE ON collector_imports
WHEN OLD.state IN ('accepted', 'rejected')
BEGIN
    SELECT RAISE(ABORT, 'terminal collector imports are immutable');
END;

CREATE TRIGGER collector_imports_transition_guard
BEFORE UPDATE OF state ON collector_imports
WHEN NOT (OLD.state = 'pending' AND NEW.state IN ('accepted', 'rejected'))
BEGIN
    SELECT RAISE(ABORT, 'invalid collector import transition');
END;
