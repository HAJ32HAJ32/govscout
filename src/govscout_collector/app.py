from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass
from typing import Protocol

from govscout_collector.core import (
    CollectorQueue,
    GovScoutUploadTransport,
    SyncResult,
    UploadTransport,
)
from govscout_collector.credentials import (
    CollectorCredentials,
    CredentialStoreError,
    SecureCredentialStore,
)
from govscout_collector.fca_api import FcaApiError, FcaRegisterClient
from govscout_collector.paths import default_queue_path


class FcaCollector(Protocol):
    def collect(
        self,
        *,
        search_terms: tuple[str, ...],
        limit: int,
        email: str,
        api_key: str,
    ) -> bytes: ...


@dataclass(slots=True)
class CollectorService:
    fca_client: FcaCollector
    queue: CollectorQueue
    transport: UploadTransport

    def collect_and_upload(
        self,
        *,
        credentials: CollectorCredentials,
        search_terms: tuple[str, ...],
        limit: int,
    ) -> SyncResult:
        payload = self.fca_client.collect(
            search_terms=search_terms,
            limit=limit,
            email=credentials.fca_email,
            api_key=credentials.fca_api_key,
        )
        self.queue.stage(payload)
        return self.queue.retry_pending(
            transport=self.transport,
            token=credentials.upload_token,
        )

    def retry_pending(self, *, credentials: CollectorCredentials) -> SyncResult:
        return self.queue.retry_pending(
            transport=self.transport,
            token=credentials.upload_token,
        )


class CollectorDesktop:
    def __init__(self, root, *, store: SecureCredentialStore, service: CollectorService) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._root = root
        self._store = store
        self._service = service
        self._ttk = ttk
        root.title("GovScout Collector")
        root.minsize(580, 500)

        frame = ttk.Frame(root, padding=20)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="GovScout Collector", font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            frame,
            text=(
                "Search the official FCA Register, collect up to 25 active firms, "
                "and upload them securely for review."
            ),
            wraplength=520,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 18))

        self._email = tk.StringVar()
        self._api_key = tk.StringVar()
        self._upload_token = tk.StringVar()
        self._search_terms = tk.StringVar(value="finance, wealth, mortgage")
        self._limit = tk.IntVar(value=25)
        self._status = tk.StringVar(value="Enter your setup details, then collect your first batch.")

        fields = (
            ("FCA registered email", self._email, None),
            ("FCA API key", self._api_key, "•"),
            ("GovScout upload token", self._upload_token, "•"),
            ("Search terms (comma-separated)", self._search_terms, None),
        )
        for row, (label, variable, mask) in enumerate(fields, start=2):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            ttk.Entry(frame, textvariable=variable, show=mask or "").grid(
                row=row, column=1, sticky="ew", pady=6
            )
        ttk.Label(frame, text="Maximum firms").grid(row=6, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Spinbox(frame, from_=1, to=25, textvariable=self._limit, width=6).grid(
            row=6, column=1, sticky="w", pady=6
        )

        controls = ttk.Frame(frame)
        controls.grid(row=7, column=0, columnspan=2, sticky="w", pady=(18, 8))
        self._collect_button = ttk.Button(
            controls, text="Collect and upload", command=self._start_collect
        )
        self._collect_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Button(controls, text="Retry pending uploads", command=self._start_retry).grid(
            row=0, column=1
        )
        ttk.Label(frame, textvariable=self._status, wraplength=520).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(14, 0)
        )
        ttk.Label(
            frame,
            text=(
                "Credentials are stored in Windows Credential Manager or macOS Keychain. "
                "Collector never creates leads, drafts, or sends email."
            ),
            wraplength=520,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(22, 0))

        try:
            existing = store.load()
        except CredentialStoreError as exc:
            self._status.set(str(exc))
        else:
            if existing is not None:
                self._email.set(existing.fca_email)
                self._api_key.set(existing.fca_api_key)
                self._upload_token.set(existing.upload_token)
                self._status.set("Setup loaded securely. Ready to collect.")

    def _credentials(self) -> CollectorCredentials:
        credentials = CollectorCredentials(
            fca_email=self._email.get(),
            fca_api_key=self._api_key.get(),
            upload_token=self._upload_token.get(),
        )
        self._store.save(credentials)
        return credentials

    def _set_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        self._collect_button.state(state)
        self._retry_button.state(state)

    def _run(self, operation) -> None:
        self._set_busy(True)
        self._status.set("Working with the official FCA Register…")

        def worker() -> None:
            try:
                result = operation()
            except (CredentialStoreError, FcaApiError, ValueError, RuntimeError) as exc:
                message = str(exc)
                self._root.after(0, lambda: self._finish_error(message))
            else:
                self._root.after(0, lambda: self._finish_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _start_collect(self) -> None:
        try:
            credentials = self._credentials()
            terms = tuple(term.strip() for term in self._search_terms.get().split(",") if term.strip())
            limit = int(self._limit.get())
        except (CredentialStoreError, ValueError) as exc:
            self._finish_error(str(exc))
            return
        self._run(
            lambda: self._service.collect_and_upload(
                credentials=credentials,
                search_terms=terms,
                limit=limit,
            )
        )

    def _start_retry(self) -> None:
        try:
            credentials = self._credentials()
        except (CredentialStoreError, ValueError) as exc:
            self._finish_error(str(exc))
            return
        self._run(lambda: self._service.retry_pending(credentials=credentials))

    def _finish_error(self, message: str) -> None:
        self._set_busy(False)
        self._status.set(f"Could not complete the batch: {message}")

    def _finish_success(self, result: SyncResult) -> None:
        self._set_busy(False)
        if result.errors:
            self._status.set(
                f"Uploaded {result.uploaded}; {result.pending} still pending. "
                f"{result.errors[0]}"
            )
        else:
            self._status.set(
                f"Done — uploaded {result.uploaded} batch; {result.pending} pending. "
                "Open GovScout in your browser to review the firms."
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="govscout-collector",
        description="Collect a bounded FCA Register batch and upload it to GovScout.",
    )
    parser.add_argument("--version", action="version", version="GovScout Collector 0.1.0")
    parser.parse_args(argv)

    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    try:
        store = SecureCredentialStore()
        service = CollectorService(
            fca_client=FcaRegisterClient(),
            queue=CollectorQueue(default_queue_path()),
            transport=GovScoutUploadTransport(),
        )
        CollectorDesktop(root, store=store, service=service)
    except (CredentialStoreError, RuntimeError) as exc:
        messagebox.showerror("GovScout Collector", str(exc))
        root.destroy()
        return 2
    root.mainloop()
    return 0
