import base64
from email import policy
from email.parser import BytesParser

import pytest

from govscout.gmail_drafts import GmailDraftAdapter, SenderMismatch


class Executable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeHttpError(RuntimeError):
    def __init__(self, status):
        self.resp = type("Response", (), {"status": status})()
        super().__init__(f"HTTP {status}")


class FakeDraftsApi:
    def __init__(self, found_draft=None, delete_error=None):
        self.create_calls = []
        self.list_calls = []
        self.get_calls = []
        self.delete_calls = []
        self.found_draft = found_draft
        self.delete_error = delete_error

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return Executable(
            {
                "id": "draft-123",
                "message": {"id": "message-123", "threadId": "thread-123"},
            }
        )

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        drafts = [{"id": self.found_draft["id"]}] if self.found_draft else []
        return Executable({"drafts": drafts})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Executable(self.found_draft)

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return Executable({})


class FakeUsersApi:
    def __init__(self, authenticated_email, found_draft=None, delete_error=None):
        self.authenticated_email = authenticated_email
        self.drafts_api = FakeDraftsApi(found_draft, delete_error)

    def getProfile(self, **kwargs):
        return Executable({"emailAddress": self.authenticated_email})

    def drafts(self):
        return self.drafts_api


class FakeGmailService:
    def __init__(
        self,
        authenticated_email="harrison@misegroup.co.uk",
        found_draft=None,
        delete_error=None,
    ):
        self.users_api = FakeUsersApi(
            authenticated_email,
            found_draft,
            delete_error,
        )

    def users(self):
        return self.users_api


def test_adapter_creates_plain_text_draft_with_fixed_identity_and_ledger_header():
    service = FakeGmailService()
    adapter = GmailDraftAdapter(service)

    result = adapter.create_draft(
        to_email="director@example.test",
        from_email="harrison@misegroup.co.uk",
        from_name="Harrison — Mise",
        subject="your privacy notice and AI",
        body="Plain text only. No tracking.",
        ledger_id=42,
    )

    assert result == {
        "draft_id": "draft-123",
        "message_id": "message-123",
        "thread_id": "thread-123",
    }
    call = service.users_api.drafts_api.create_calls[0]
    raw = base64.urlsafe_b64decode(call["body"]["message"]["raw"])
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message.get_content_type() == "text/plain"
    assert not message.is_multipart()
    assert message["To"] == "director@example.test"
    assert message["From"] == "Harrison — Mise <harrison@misegroup.co.uk>"
    assert message["X-GovScout-Ledger-ID"] == "42"
    assert message["Message-ID"] == "<govscout-42@misegroup.co.uk>"
    assert message.get_content().strip() == "Plain text only. No tracking."
    assert not hasattr(adapter, "send")


def test_adapter_rejects_wrong_authenticated_mailbox_before_draft_creation():
    service = FakeGmailService("misegroup.ai@gmail.com")
    adapter = GmailDraftAdapter(service)

    with pytest.raises(SenderMismatch, match="authenticated Gmail profile"):
        adapter.create_draft(
            to_email="director@example.test",
            from_email="harrison@misegroup.co.uk",
            from_name="Harrison — Mise",
            subject="your privacy notice and AI",
            body="Plain text only.",
            ledger_id=1,
        )

    assert service.users_api.drafts_api.create_calls == []


def test_adapter_finds_existing_draft_by_deterministic_ledger_message_id():
    service = FakeGmailService(
        found_draft={
            "id": "draft-found",
            "message": {"id": "message-found", "threadId": "thread-found"},
        }
    )
    adapter = GmailDraftAdapter(service)

    result = adapter.find_by_ledger_id(42)

    assert result == {
        "draft_id": "draft-found",
        "message_id": "message-found",
        "thread_id": "thread-found",
    }
    assert service.users_api.drafts_api.list_calls == [
        {
            "userId": "me",
            "q": "rfc822msgid:govscout-42@misegroup.co.uk",
            "maxResults": 2,
        }
    ]
    assert service.users_api.drafts_api.get_calls == [
        {"userId": "me", "id": "draft-found", "format": "minimal"}
    ]


def test_adapter_returns_none_when_ledger_message_id_is_absent():
    service = FakeGmailService()

    assert GmailDraftAdapter(service).find_by_ledger_id(99) is None


def test_adapter_deletes_draft_but_exposes_no_send_method():
    service = FakeGmailService()
    adapter = GmailDraftAdapter(service)

    adapter.delete_draft("draft-123")

    assert service.users_api.drafts_api.delete_calls == [
        {"userId": "me", "id": "draft-123"}
    ]
    assert not hasattr(adapter, "send")


def test_adapter_treats_missing_draft_as_idempotent_delete_success():
    service = FakeGmailService(delete_error=FakeHttpError(404))
    adapter = GmailDraftAdapter(service)

    adapter.delete_draft("draft-already-gone")

    assert service.users_api.drafts_api.delete_calls == [
        {"userId": "me", "id": "draft-already-gone"}
    ]


def test_adapter_rejects_multiple_recipients_before_gmail_call():
    service = FakeGmailService()
    adapter = GmailDraftAdapter(service)

    with pytest.raises(ValueError, match="single recipient"):
        adapter.create_draft(
            to_email="first@example.test, second@example.test",
            from_email="harrison@misegroup.co.uk",
            from_name="Harrison — Mise",
            subject="subject",
            body="body",
            ledger_id=1,
        )

    assert service.users_api.drafts_api.create_calls == []
