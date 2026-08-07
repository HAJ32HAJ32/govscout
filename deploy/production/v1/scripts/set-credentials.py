#!/opt/govscout/current/.venv/bin/python
from __future__ import annotations

import base64
import getpass
import os
from pathlib import Path
import re
import secrets
import tempfile

from govscout.auth import hash_password

TARGET = Path("/etc/govscout/govscout.env")
USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}\Z")

if os.geteuid() != 0:
    raise SystemExit("Run this credential setup with sudo.")
if TARGET.exists():
    raise SystemExit(f"Refusing to overwrite existing credentials: {TARGET}")

username = input("GovScout username: ").strip()
if not USERNAME.fullmatch(username):
    raise SystemExit("Username must be 1–64 characters: letters, digits, dot, underscore, @ or hyphen.")
password = getpass.getpass("GovScout password: ")
confirmation = getpass.getpass("Repeat GovScout password: ")
if password != confirmation:
    raise SystemExit("Passwords did not match.")
if len(password) < 14:
    raise SystemExit("Use a password of at least 14 characters.")

password_hash = hash_password(password)
session_secret = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii")
content = "\n".join(
    (
        "GOVSCOUT_DEPLOYMENT_MODE=public-proxy",
        "GOVSCOUT_BIND_HOST=127.0.0.1",
        "GOVSCOUT_PUBLIC_HOST=leads.misegroup.co.uk",
        "GOVSCOUT_DATABASE=/var/lib/govscout/govscout.sqlite3",
        f"GOVSCOUT_USERNAME={username}",
        f"GOVSCOUT_PASSWORD_HASH={password_hash}",
        f"GOVSCOUT_SESSION_SECRET={session_secret}",
        "",
    )
)
TARGET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd, temporary = tempfile.mkstemp(prefix=".govscout.env-", dir=TARGET.parent, text=True)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, TARGET)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
os.chown(TARGET, 0, 0)
print(f"Created {TARGET} with mode 0600. Password and session secret were not printed.")
