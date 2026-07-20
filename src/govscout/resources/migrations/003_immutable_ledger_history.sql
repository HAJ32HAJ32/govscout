CREATE TRIGGER sends_no_delete
BEFORE DELETE ON sends
BEGIN
    SELECT RAISE(ABORT, 'send ledger rows cannot be deleted');
END;

CREATE TRIGGER sends_core_history_immutable
BEFORE UPDATE OF
    lead_id, sequence_id, to_email, stage, template, subject,
    body_hash, word_count, created_at
ON sends
WHEN NEW.lead_id IS NOT OLD.lead_id
    OR NEW.sequence_id IS NOT OLD.sequence_id
    OR NEW.to_email IS NOT OLD.to_email
    OR NEW.stage IS NOT OLD.stage
    OR NEW.template IS NOT OLD.template
    OR NEW.subject IS NOT OLD.subject
    OR NEW.body_hash IS NOT OLD.body_hash
    OR NEW.word_count IS NOT OLD.word_count
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'send audit identity is immutable');
END;

CREATE TRIGGER sends_state_transition_guard
BEFORE UPDATE OF state ON sends
WHEN NOT (
    (OLD.state = 'reserved' AND NEW.state IN ('reserved', 'draft', 'failed'))
    OR (OLD.state = 'draft' AND NEW.state IN ('draft', 'sent', 'void'))
    OR (OLD.state IN ('sent', 'void', 'failed') AND NEW.state = OLD.state)
)
BEGIN
    SELECT RAISE(ABORT, 'invalid send state transition');
END;

CREATE TRIGGER sends_terminal_evidence_immutable
BEFORE UPDATE OF
    drafted_at, sent_at, voided_at, failed_at, failure_reason,
    gmail_draft_id, gmail_message_id, gmail_thread_id
ON sends
WHEN OLD.state IN ('sent', 'void', 'failed')
    AND (
        NEW.drafted_at IS NOT OLD.drafted_at
        OR NEW.sent_at IS NOT OLD.sent_at
        OR NEW.voided_at IS NOT OLD.voided_at
        OR NEW.failed_at IS NOT OLD.failed_at
        OR NEW.failure_reason IS NOT OLD.failure_reason
        OR NEW.gmail_draft_id IS NOT OLD.gmail_draft_id
        OR NEW.gmail_message_id IS NOT OLD.gmail_message_id
        OR NEW.gmail_thread_id IS NOT OLD.gmail_thread_id
    )
BEGIN
    SELECT RAISE(ABORT, 'terminal send evidence is immutable');
END;

CREATE TRIGGER sends_reply_classification_immutable
BEFORE UPDATE OF reply_class, replied_at ON sends
WHEN OLD.state = 'sent'
    AND OLD.reply_class IS NOT NULL
    AND (
        NEW.reply_class IS NOT OLD.reply_class
        OR NEW.replied_at IS NOT OLD.replied_at
    )
BEGIN
    SELECT RAISE(ABORT, 'reply classification is immutable once recorded');
END;

CREATE TRIGGER leads_evidence_immutable_after_send
BEFORE UPDATE OF
    company_number, legal_name, legal_form, company_status,
    verification_source, companies_house_verified_at,
    companies_house_profile_hash, contact_email, source_register
ON leads
WHEN EXISTS (SELECT 1 FROM sends WHERE lead_id = OLD.id)
    AND (
        NEW.company_number IS NOT OLD.company_number
        OR NEW.legal_name IS NOT OLD.legal_name
        OR NEW.legal_form IS NOT OLD.legal_form
        OR NEW.company_status IS NOT OLD.company_status
        OR NEW.verification_source IS NOT OLD.verification_source
        OR NEW.companies_house_verified_at IS NOT OLD.companies_house_verified_at
        OR NEW.companies_house_profile_hash IS NOT OLD.companies_house_profile_hash
        OR NEW.contact_email IS NOT OLD.contact_email
        OR NEW.source_register IS NOT OLD.source_register
    )
BEGIN
    SELECT RAISE(ABORT, 'lead evidence is immutable after send history exists');
END;

UPDATE sends SET state = state;
