CREATE TRIGGER leads_single_recipient_insert
BEFORE INSERT ON leads
WHEN NEW.contact_email != lower(trim(NEW.contact_email))
    OR length(NEW.contact_email) > 254
    OR length(NEW.contact_email) - length(replace(NEW.contact_email, '@', '')) != 1
    OR instr(NEW.contact_email, '@') <= 1
    OR instr(NEW.contact_email, '@') = length(NEW.contact_email)
    OR substr(NEW.contact_email, 1, 1) = '.'
    OR substr(NEW.contact_email, instr(NEW.contact_email, '@') - 1, 1) = '.'
    OR instr(NEW.contact_email, ',') > 0
    OR instr(NEW.contact_email, ';') > 0
    OR instr(NEW.contact_email, '<') > 0
    OR instr(NEW.contact_email, '>') > 0
    OR instr(NEW.contact_email, ' ') > 0
    OR instr(NEW.contact_email, char(9)) > 0
    OR instr(NEW.contact_email, char(10)) > 0
    OR instr(NEW.contact_email, char(13)) > 0
BEGIN
    SELECT RAISE(ABORT, 'contact_email must be one canonical recipient');
END;

CREATE TRIGGER leads_single_recipient_update
BEFORE UPDATE OF contact_email ON leads
WHEN NEW.contact_email != lower(trim(NEW.contact_email))
    OR length(NEW.contact_email) > 254
    OR length(NEW.contact_email) - length(replace(NEW.contact_email, '@', '')) != 1
    OR instr(NEW.contact_email, '@') <= 1
    OR instr(NEW.contact_email, '@') = length(NEW.contact_email)
    OR substr(NEW.contact_email, 1, 1) = '.'
    OR substr(NEW.contact_email, instr(NEW.contact_email, '@') - 1, 1) = '.'
    OR instr(NEW.contact_email, ',') > 0
    OR instr(NEW.contact_email, ';') > 0
    OR instr(NEW.contact_email, '<') > 0
    OR instr(NEW.contact_email, '>') > 0
    OR instr(NEW.contact_email, ' ') > 0
    OR instr(NEW.contact_email, char(9)) > 0
    OR instr(NEW.contact_email, char(10)) > 0
    OR instr(NEW.contact_email, char(13)) > 0
BEGIN
    SELECT RAISE(ABORT, 'contact_email must be one canonical recipient');
END;

CREATE TRIGGER sends_single_recipient_insert
BEFORE INSERT ON sends
WHEN NEW.to_email != lower(trim(NEW.to_email))
    OR length(NEW.to_email) > 254
    OR length(NEW.to_email) - length(replace(NEW.to_email, '@', '')) != 1
    OR instr(NEW.to_email, '@') <= 1
    OR instr(NEW.to_email, '@') = length(NEW.to_email)
    OR substr(NEW.to_email, 1, 1) = '.'
    OR substr(NEW.to_email, instr(NEW.to_email, '@') - 1, 1) = '.'
    OR instr(NEW.to_email, ',') > 0
    OR instr(NEW.to_email, ';') > 0
    OR instr(NEW.to_email, '<') > 0
    OR instr(NEW.to_email, '>') > 0
    OR instr(NEW.to_email, ' ') > 0
    OR instr(NEW.to_email, char(9)) > 0
    OR instr(NEW.to_email, char(10)) > 0
    OR instr(NEW.to_email, char(13)) > 0
BEGIN
    SELECT RAISE(ABORT, 'to_email must be one canonical recipient');
END;

CREATE TRIGGER sends_single_recipient_update
BEFORE UPDATE OF to_email ON sends
WHEN NEW.to_email != lower(trim(NEW.to_email))
    OR length(NEW.to_email) > 254
    OR length(NEW.to_email) - length(replace(NEW.to_email, '@', '')) != 1
    OR instr(NEW.to_email, '@') <= 1
    OR instr(NEW.to_email, '@') = length(NEW.to_email)
    OR substr(NEW.to_email, 1, 1) = '.'
    OR substr(NEW.to_email, instr(NEW.to_email, '@') - 1, 1) = '.'
    OR instr(NEW.to_email, ',') > 0
    OR instr(NEW.to_email, ';') > 0
    OR instr(NEW.to_email, '<') > 0
    OR instr(NEW.to_email, '>') > 0
    OR instr(NEW.to_email, ' ') > 0
    OR instr(NEW.to_email, char(9)) > 0
    OR instr(NEW.to_email, char(10)) > 0
    OR instr(NEW.to_email, char(13)) > 0
BEGIN
    SELECT RAISE(ABORT, 'to_email must be one canonical recipient');
END;

CREATE TRIGGER sends_recipient_matches_lead_insert
BEFORE INSERT ON sends
WHEN NEW.to_email != (SELECT contact_email FROM leads WHERE id = NEW.lead_id)
BEGIN
    SELECT RAISE(ABORT, 'send recipient must match lead contact');
END;

CREATE TRIGGER sends_recipient_matches_lead_update
BEFORE UPDATE OF lead_id, to_email ON sends
WHEN NEW.to_email != (SELECT contact_email FROM leads WHERE id = NEW.lead_id)
BEGIN
    SELECT RAISE(ABORT, 'send recipient must match lead contact');
END;

CREATE TRIGGER sends_lifecycle_insert
BEFORE INSERT ON sends
WHEN NOT (
    julianday(NEW.created_at) IS NOT NULL
    AND (
        (
            NEW.state = 'reserved'
            AND NEW.drafted_at IS NULL AND NEW.sent_at IS NULL
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.gmail_draft_id IS NULL AND NEW.gmail_message_id IS NULL
            AND NEW.gmail_thread_id IS NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'draft'
            AND NEW.drafted_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND NEW.sent_at IS NULL AND NEW.voided_at IS NULL
            AND NEW.failed_at IS NULL AND NEW.failure_reason IS NULL
            AND NEW.gmail_draft_id IS NOT NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'sent'
            AND NEW.drafted_at IS NOT NULL AND NEW.sent_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.sent_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND julianday(NEW.sent_at) >= julianday(NEW.drafted_at)
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.failure_reason IS NULL AND NEW.gmail_draft_id IS NOT NULL
            AND (
                (NEW.reply_class IS NULL AND NEW.replied_at IS NULL)
                OR (
                    NEW.reply_class IS NOT NULL AND NEW.replied_at IS NOT NULL
                    AND julianday(NEW.replied_at) IS NOT NULL
                    AND julianday(NEW.replied_at) >= julianday(NEW.sent_at)
                )
            )
        )
        OR (
            NEW.state = 'void'
            AND NEW.drafted_at IS NOT NULL AND NEW.voided_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.voided_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND julianday(NEW.voided_at) >= julianday(NEW.drafted_at)
            AND NEW.sent_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.failure_reason IS NULL AND NEW.gmail_draft_id IS NOT NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'failed'
            AND NEW.drafted_at IS NULL AND NEW.sent_at IS NULL
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NOT NULL
            AND julianday(NEW.failed_at) IS NOT NULL
            AND julianday(NEW.failed_at) >= julianday(NEW.created_at)
            AND length(trim(NEW.failure_reason)) > 0
            AND NEW.gmail_draft_id IS NULL AND NEW.gmail_message_id IS NULL
            AND NEW.gmail_thread_id IS NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid send lifecycle');
END;

CREATE TRIGGER sends_lifecycle_update
BEFORE UPDATE ON sends
WHEN NOT (
    julianday(NEW.created_at) IS NOT NULL
    AND (
        (
            NEW.state = 'reserved'
            AND NEW.drafted_at IS NULL AND NEW.sent_at IS NULL
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.gmail_draft_id IS NULL AND NEW.gmail_message_id IS NULL
            AND NEW.gmail_thread_id IS NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'draft'
            AND NEW.drafted_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND NEW.sent_at IS NULL AND NEW.voided_at IS NULL
            AND NEW.failed_at IS NULL AND NEW.failure_reason IS NULL
            AND NEW.gmail_draft_id IS NOT NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'sent'
            AND NEW.drafted_at IS NOT NULL AND NEW.sent_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.sent_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND julianday(NEW.sent_at) >= julianday(NEW.drafted_at)
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.failure_reason IS NULL AND NEW.gmail_draft_id IS NOT NULL
            AND (
                (NEW.reply_class IS NULL AND NEW.replied_at IS NULL)
                OR (
                    NEW.reply_class IS NOT NULL AND NEW.replied_at IS NOT NULL
                    AND julianday(NEW.replied_at) IS NOT NULL
                    AND julianday(NEW.replied_at) >= julianday(NEW.sent_at)
                )
            )
        )
        OR (
            NEW.state = 'void'
            AND NEW.drafted_at IS NOT NULL AND NEW.voided_at IS NOT NULL
            AND julianday(NEW.drafted_at) IS NOT NULL
            AND julianday(NEW.voided_at) IS NOT NULL
            AND julianday(NEW.drafted_at) >= julianday(NEW.created_at)
            AND julianday(NEW.voided_at) >= julianday(NEW.drafted_at)
            AND NEW.sent_at IS NULL AND NEW.failed_at IS NULL
            AND NEW.failure_reason IS NULL AND NEW.gmail_draft_id IS NOT NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
        OR (
            NEW.state = 'failed'
            AND NEW.drafted_at IS NULL AND NEW.sent_at IS NULL
            AND NEW.voided_at IS NULL AND NEW.failed_at IS NOT NULL
            AND julianday(NEW.failed_at) IS NOT NULL
            AND julianday(NEW.failed_at) >= julianday(NEW.created_at)
            AND length(trim(NEW.failure_reason)) > 0
            AND NEW.gmail_draft_id IS NULL AND NEW.gmail_message_id IS NULL
            AND NEW.gmail_thread_id IS NULL
            AND NEW.reply_class IS NULL AND NEW.replied_at IS NULL
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid send lifecycle');
END;

UPDATE leads SET contact_email = contact_email;
UPDATE sends SET to_email = to_email;
UPDATE sends SET state = state;
