from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any


EXPECTED_SENDER_EMAIL = "harrison@misegroup.co.uk"
EXPECTED_SENDER_NAME = "Harrison — Mise"


class SenderMismatch(RuntimeError):
    """Raised when OAuth is authenticated as the wrong Gmail mailbox."""


class GmailDraftAdapter:
    """Narrow Gmail API adapter that exposes draft creation, never sending."""

    def __init__(self, service: Any):
        self.service = service
        self._profile_verified = False

    @staticmethod
    def _reject_header_injection(value: str, field: str) -> None:
        if "\r" in value or "\n" in value:
            raise ValueError(f"{field} contains a newline")

    @staticmethod
    def _single_recipient(value: str) -> str:
        recipient = value.strip().lower()
        if (
            recipient.count("@") != 1
            or any(character in recipient for character in "\r\n,; \t")
            or not all(recipient.split("@", 1))
        ):
            raise ValueError("to_email must be a single recipient address")
        return recipient

    def _verify_authenticated_profile(self) -> None:
        if self._profile_verified:
            return
        profile = self.service.users().getProfile(userId="me").execute()
        actual = str(profile.get("emailAddress", "")).strip().lower()
        if actual != EXPECTED_SENDER_EMAIL:
            raise SenderMismatch(
                "authenticated Gmail profile must be harrison@misegroup.co.uk"
            )
        self._profile_verified = True

    def find_by_ledger_id(self, ledger_id: int) -> dict[str, str | None] | None:
        if ledger_id <= 0:
            raise ValueError("ledger_id must be positive")
        self._verify_authenticated_profile()
        response = (
            self.service.users()
            .drafts()
            .list(
                userId="me",
                q=f"rfc822msgid:govscout-{ledger_id}@misegroup.co.uk",
                maxResults=2,
            )
            .execute()
        )
        drafts = response.get("drafts") or []
        if not drafts:
            return None
        if len(drafts) > 1:
            raise RuntimeError("multiple Gmail drafts share one GovScout ledger id")
        draft_id = drafts[0].get("id")
        if not draft_id:
            raise RuntimeError("Gmail draft search returned an item without an id")
        found = (
            self.service.users()
            .drafts()
            .get(userId="me", id=draft_id, format="minimal")
            .execute()
        )
        gmail_message = found.get("message") or {}
        return {
            "draft_id": found.get("id"),
            "message_id": gmail_message.get("id"),
            "thread_id": gmail_message.get("threadId"),
        }

    def delete_draft(self, draft_id: str) -> None:
        if not draft_id.strip():
            raise ValueError("draft_id is required")
        self._verify_authenticated_profile()
        try:
            (
                self.service.users()
                .drafts()
                .delete(userId="me", id=draft_id)
                .execute()
            )
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                return
            raise

    def create_draft(
        self,
        *,
        to_email: str,
        from_email: str,
        from_name: str,
        subject: str,
        body: str,
        ledger_id: int,
    ) -> dict[str, str | None]:
        if from_email.strip().lower() != EXPECTED_SENDER_EMAIL:
            raise SenderMismatch("draft sender email is not the fixed Mise sender")
        if from_name.strip() != EXPECTED_SENDER_NAME:
            raise SenderMismatch("draft sender name is not the fixed Mise sender")
        recipient = self._single_recipient(to_email)
        self._reject_header_injection(subject, "subject")
        self._verify_authenticated_profile()

        message = EmailMessage()
        message["To"] = recipient
        message["From"] = formataddr((EXPECTED_SENDER_NAME, EXPECTED_SENDER_EMAIL))
        message["Subject"] = subject
        message["Message-ID"] = f"<govscout-{ledger_id}@misegroup.co.uk>"
        message["X-GovScout-Ledger-ID"] = str(ledger_id)
        message.set_content(body, subtype="plain", charset="utf-8")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        response = (
            self.service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        gmail_message = response.get("message") or {}
        return {
            "draft_id": response.get("id"),
            "message_id": gmail_message.get("id"),
            "thread_id": gmail_message.get("threadId"),
        }
