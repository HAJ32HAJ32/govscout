from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast

from govscout_collector.core import is_valid_upload_token

SERVICE_NAME = "GovScout Collector"
_FCA_EMAIL = "fca-email"
_FCA_API_KEY = "fca-api-key"
_UPLOAD_TOKEN = "upload-token"
_CREDENTIAL_NAMES = (_FCA_EMAIL, _FCA_API_KEY, _UPLOAD_TOKEN)


class CredentialStoreError(RuntimeError):
    """The native credential store could not safely complete an operation."""


class KeyringBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CollectorCredentials:
    fca_email: str
    fca_api_key: str
    upload_token: str


class SecureCredentialStore:
    def __init__(self, *, backend: KeyringBackend | None = None) -> None:
        if backend is None:
            if sys.platform not in {"win32", "darwin"}:
                raise CredentialStoreError(
                    "GovScout Collector supports native credentials on Windows and macOS"
                )
            try:
                backend = cast(KeyringBackend, import_module("keyring"))
            except ModuleNotFoundError:
                raise CredentialStoreError(
                    "GovScout Collector credential support is not installed"
                ) from None
        assert backend is not None
        self._backend = backend

    @staticmethod
    def _validate(credentials: CollectorCredentials) -> None:
        email = credentials.fca_email
        if (
            not isinstance(email, str)
            or len(email) > 254
            or email != email.strip()
            or email.count("@") != 1
            or not email.isascii()
            or any(character.isspace() for character in email)
        ):
            raise ValueError("FCA registered email is invalid")
        api_key = credentials.fca_api_key
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key) > 512
            or api_key != api_key.strip()
            or not api_key.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in api_key)
        ):
            raise ValueError("FCA API credential is invalid")
        if not is_valid_upload_token(credentials.upload_token):
            raise ValueError("GovScout upload credential is invalid")

    def save(self, credentials: CollectorCredentials) -> None:
        self._validate(credentials)
        values = {
            _FCA_EMAIL: credentials.fca_email,
            _FCA_API_KEY: credentials.fca_api_key,
            _UPLOAD_TOKEN: credentials.upload_token,
        }
        try:
            for name, value in values.items():
                self._backend.set_password(SERVICE_NAME, name, value)
        except Exception as exc:
            self._clear_best_effort()
            raise CredentialStoreError("Native credential storage failed") from exc

    def load(self) -> CollectorCredentials | None:
        try:
            values = {
                name: self._backend.get_password(SERVICE_NAME, name)
                for name in _CREDENTIAL_NAMES
            }
        except Exception as exc:
            raise CredentialStoreError("Native credential retrieval failed") from exc
        present = [value is not None for value in values.values()]
        if not any(present):
            return None
        if not all(present):
            raise CredentialStoreError("Native credential setup is incomplete")
        credentials = CollectorCredentials(
            fca_email=values[_FCA_EMAIL] or "",
            fca_api_key=values[_FCA_API_KEY] or "",
            upload_token=values[_UPLOAD_TOKEN] or "",
        )
        try:
            self._validate(credentials)
        except ValueError as exc:
            raise CredentialStoreError("Stored Collector credentials are invalid") from exc
        return credentials

    def _clear_best_effort(self) -> bool:
        failed = False
        for name in _CREDENTIAL_NAMES:
            try:
                self._backend.delete_password(SERVICE_NAME, name)
            except Exception:  # noqa: BLE001 - keyring backends expose backend-specific errors
                failed = True
        return not failed

    def clear(self) -> None:
        if not self._clear_best_effort():
            raise CredentialStoreError("Native credential removal was incomplete")
