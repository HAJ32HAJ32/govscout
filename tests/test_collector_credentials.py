import pytest

from govscout_collector.credentials import (
    CollectorCredentials,
    CredentialStoreError,
    SecureCredentialStore,
)


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


def _credentials():
    return CollectorCredentials(
        fca_email="operator@example.test",
        fca_api_key="fca-secret-value",
        upload_token="gsc_" + "a" * 32 + "_" + "b" * 43,
    )


def test_secure_credential_store_round_trips_without_plaintext_files():
    backend = MemoryKeyring()
    store = SecureCredentialStore(backend=backend)

    store.save(_credentials())

    assert store.load() == _credentials()
    assert set(backend.values) == {
        ("GovScout Collector", "fca-email"),
        ("GovScout Collector", "fca-api-key"),
        ("GovScout Collector", "upload-token"),
    }


def test_secure_credential_store_rejects_partial_or_malformed_setup():
    backend = MemoryKeyring()
    store = SecureCredentialStore(backend=backend)
    backend.set_password("GovScout Collector", "fca-email", "operator@example.test")

    with pytest.raises(CredentialStoreError, match="incomplete"):
        store.load()

    with pytest.raises(ValueError, match="upload credential"):
        store.save(
            CollectorCredentials(
                fca_email="operator@example.test",
                fca_api_key="fca-secret-value",
                upload_token="not-a-token",
            )
        )


def test_secure_credential_store_clears_all_values():
    backend = MemoryKeyring()
    store = SecureCredentialStore(backend=backend)
    store.save(_credentials())

    store.clear()

    assert store.load() is None
    assert backend.values == {}
