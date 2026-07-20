from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from govscout.sendguard import ReservationRequest


@dataclass(frozen=True, slots=True)
class PolicyResult:
    passed: bool
    reasons: tuple[str, ...] = ()


class DraftPolicy(Protocol):
    def evaluate(self, request: ReservationRequest) -> PolicyResult: ...


class LintNotReadyPolicy:
    """Production P1 policy: refuse every real draft until P4 lint exists."""

    def evaluate(self, request: ReservationRequest) -> PolicyResult:
        return PolicyResult(passed=False, reasons=("LINT_NOT_READY",))
