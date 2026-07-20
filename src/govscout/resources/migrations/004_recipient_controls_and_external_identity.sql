CREATE TRIGGER leads_control_characters_insert
BEFORE INSERT ON leads
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 32
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.contact_email, char(code)) > 0
)
OR instr(NEW.contact_email, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'contact_email contains a forbidden control character');
END;

CREATE TRIGGER leads_control_characters_update
BEFORE UPDATE OF contact_email ON leads
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 32
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.contact_email, char(code)) > 0
)
OR instr(NEW.contact_email, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'contact_email contains a forbidden control character');
END;

CREATE TRIGGER sends_control_characters_insert
BEFORE INSERT ON sends
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 32
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.to_email, char(code)) > 0
)
OR instr(NEW.to_email, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'to_email contains a forbidden control character');
END;

CREATE TRIGGER sends_control_characters_update
BEFORE UPDATE OF to_email ON sends
WHEN EXISTS (
    WITH RECURSIVE controls(code) AS (
        SELECT 0
        UNION ALL
        SELECT code + 1 FROM controls WHERE code < 32
    )
    SELECT 1
    FROM controls
    WHERE instr(NEW.to_email, char(code)) > 0
)
OR instr(NEW.to_email, char(127)) > 0
BEGIN
    SELECT RAISE(ABORT, 'to_email contains a forbidden control character');
END;

CREATE TRIGGER sends_external_identity_insert
BEFORE INSERT ON sends
WHEN (
    NEW.state IN ('draft', 'sent', 'void')
    AND (NEW.gmail_draft_id IS NULL OR length(trim(NEW.gmail_draft_id)) = 0)
)
OR (
    NEW.state = 'sent'
    AND (NEW.gmail_message_id IS NULL OR length(trim(NEW.gmail_message_id)) = 0)
)
BEGIN
    SELECT RAISE(ABORT, 'draft and sent rows require concrete Gmail identity');
END;

CREATE TRIGGER sends_external_identity_update
BEFORE UPDATE ON sends
WHEN (
    NEW.state IN ('draft', 'sent', 'void')
    AND (NEW.gmail_draft_id IS NULL OR length(trim(NEW.gmail_draft_id)) = 0)
)
OR (
    NEW.state = 'sent'
    AND (NEW.gmail_message_id IS NULL OR length(trim(NEW.gmail_message_id)) = 0)
)
BEGIN
    SELECT RAISE(ABORT, 'draft and sent rows require concrete Gmail identity');
END;

UPDATE leads SET contact_email = contact_email;
UPDATE sends SET to_email = to_email;
UPDATE sends SET state = state;
