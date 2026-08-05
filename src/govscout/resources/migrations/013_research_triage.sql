CREATE TABLE firm_archive_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_id INTEGER NOT NULL REFERENCES fca_firms(id),
    action TEXT NOT NULL CHECK (action IN ('archive', 'restore')),
    reason TEXT NOT NULL CHECK (
        length(trim(reason)) BETWEEN 1 AND 500
        AND reason = trim(reason)
    ),
    expected_previous_event_id INTEGER REFERENCES firm_archive_events(id),
    occurred_at TEXT NOT NULL CHECK (substr(occurred_at, -6) = '+00:00')
);

CREATE INDEX idx_firm_archive_events_latest
ON firm_archive_events(firm_id, id DESC);

CREATE TRIGGER firm_archive_events_legal_insert
BEFORE INSERT ON firm_archive_events
BEGIN
    SELECT CASE WHEN NEW.expected_previous_event_id IS NOT (
        SELECT id FROM firm_archive_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ) THEN RAISE(ABORT, 'stale archive event') END;

    SELECT CASE WHEN NEW.action = 'archive' AND COALESCE((
        SELECT action FROM firm_archive_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ), 'restore') != 'restore'
    THEN RAISE(ABORT, 'firm is already archived') END;

    SELECT CASE WHEN NEW.action = 'restore' AND COALESCE((
        SELECT action FROM firm_archive_events
        WHERE firm_id = NEW.firm_id ORDER BY id DESC LIMIT 1
    ), 'restore') != 'archive'
    THEN RAISE(ABORT, 'firm is not archived') END;
END;

CREATE TRIGGER firm_archive_events_no_update
BEFORE UPDATE ON firm_archive_events
BEGIN
    SELECT RAISE(ABORT, 'archive history is immutable');
END;

CREATE TRIGGER firm_archive_events_no_delete
BEFORE DELETE ON firm_archive_events
BEGIN
    SELECT RAISE(ABORT, 'archive history is immutable');
END;

ALTER TABLE firm_reviews
ADD COLUMN archive_event_id INTEGER REFERENCES firm_archive_events(id);
