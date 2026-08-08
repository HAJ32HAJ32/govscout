"""Identify which commit is actually running, for the /today footer.

Read once at application startup (see create_app in app.py), never per
request. Production releases are built via `git archive` and genuinely ship
with no `.git` directory (see deploy/production/v1/scripts/deploy.sh), so
the `RELEASE` marker file written at build time is the correct primary
source here - a git-based lookup would always fail in production. The git
fallback below exists for local development, where a checkout is present
but no RELEASE file has been written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_COMMIT_DISPLAY_LENGTH = 7


def _from_release_file(release_dir: Path) -> str | None:
    release_file = release_dir / "RELEASE"
    try:
        text = release_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("commit="):
            commit = line.split("=", 1)[1].strip()
            if commit:
                return commit
    return None


def _from_git(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"--short={_COMMIT_DISPLAY_LENGTH}", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def read_deployed_commit(*, release_dir: Path | None = None) -> str:
    """Best-effort identification of the running commit, never raises.

    Tries the `RELEASE` marker deploy.sh writes at the release root, then a
    `git rev-parse` fallback for local development, then the literal string
    "unknown" - this must never be able to fail application startup.
    """
    directory = release_dir if release_dir is not None else Path.cwd()
    commit = _from_release_file(directory) or _from_git(Path(__file__).resolve().parent)
    if commit is None:
        return "unknown"
    return commit[:_COMMIT_DISPLAY_LENGTH]
