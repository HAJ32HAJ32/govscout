CREATE TABLE login_throttle (
    bucket TEXT PRIMARY KEY CHECK (bucket = 'single-user'),
    failure_count INTEGER NOT NULL CHECK (failure_count >= 0),
    window_started_at TEXT NOT NULL CHECK (substr(window_started_at, -6) = '+00:00'),
    blocked_until TEXT CHECK (
        blocked_until IS NULL OR substr(blocked_until, -6) = '+00:00'
    ),
    updated_at TEXT NOT NULL CHECK (substr(updated_at, -6) = '+00:00'),
    CHECK (blocked_until IS NULL OR blocked_until > window_started_at)
);

CREATE TABLE login_attempt_reservations (
    token TEXT PRIMARY KEY CHECK (length(token) >= 32),
    bucket TEXT NOT NULL CHECK (bucket = 'single-user'),
    reserved_at TEXT NOT NULL CHECK (substr(reserved_at, -6) = '+00:00'),
    expires_at TEXT NOT NULL CHECK (substr(expires_at, -6) = '+00:00'),
    CHECK (expires_at > reserved_at)
);

CREATE INDEX login_attempt_reservations_bucket_expiry
ON login_attempt_reservations (bucket, expires_at);
