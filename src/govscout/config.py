from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import tomllib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EXPECTED_SENDER_EMAIL = "harrison@misegroup.co.uk"
EXPECTED_SENDER_NAME = "Harrison — Mise"


class ConfigError(ValueError):
    """Raised when GovScout safety configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    sender_email: str
    sender_name: str
    soft_limit: int
    hard_limit: int
    warmup: tuple[tuple[int, int], ...]
    window_uk: tuple[int, int]
    preferred_weekdays: tuple[int, ...]
    followups_first: bool
    followup_days: tuple[int, ...]
    timezone: str

    def __post_init__(self) -> None:
        if self.sender_email != EXPECTED_SENDER_EMAIL:
            raise ConfigError("sender_email must be harrison@misegroup.co.uk")
        if self.sender_name != EXPECTED_SENDER_NAME:
            raise ConfigError("sender_name must be Harrison — Mise")
        if type(self.hard_limit) is not int or self.hard_limit != 15:
            raise ConfigError("hard_limit must be the fixed P1 value 15")
        if type(self.soft_limit) is not int or self.soft_limit != 10:
            raise ConfigError("soft_limit must be the fixed P1 value 10")
        if self.warmup != ((14, 5), (28, 8)):
            raise ConfigError("warmup must be the fixed P1 ramp ((14, 5), (28, 8))")
        if self.window_uk != (8, 11):
            raise ConfigError("window_uk must be the fixed P1 window (8, 11)")
        if self.preferred_weekdays != (2, 3, 4):
            raise ConfigError("preferred_weekdays must be Tuesday–Thursday")
        if self.followups_first is not True:
            raise ConfigError("followups_first must be true")
        if self.followup_days != (4, 12, 25):
            raise ConfigError("followup_days must be the fixed P1 offsets (4, 12, 25)")
        if self.timezone != "Europe/London":
            raise ConfigError("timezone must be Europe/London")


def _table(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} sections must be TOML tables")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def _int_sequence(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be an integer list")
    return tuple(_integer(item, name) for item in value)


def _pair(value: object, name: str) -> tuple[int, int]:
    items = _int_sequence(value, name)
    if len(items) != 2:
        raise ConfigError(f"{name} must contain exactly two integers")
    return items[0], items[1]


def _parse_settings(raw: Mapping[str, Any]) -> Settings:
    try:
        sender = _table(raw["sender"], "sender/send")
        send = _table(raw["send"], "sender/send")
        sender_email = _string(sender["email"], "sender.email").strip().lower()
        sender_name = _string(sender["name"], "sender.name").strip()
        soft_limit = _integer(send["soft_limit"], "soft_limit")
        hard_limit = _integer(send["hard_limit"], "hard_limit")
        raw_warmup = send["warmup"]
        if not isinstance(raw_warmup, list):
            raise ConfigError("warmup must be a list")
        warmup = tuple(_pair(item, "warmup entry") for item in raw_warmup)
        window_uk = _pair(send["window_uk"], "window_uk")
        preferred_weekdays = _int_sequence(
            send["preferred_weekdays"], "preferred_weekdays"
        )
        followups_first = _boolean(send["followups_first"], "followups_first")
        followup_days = _int_sequence(send["followup_days"], "followup_days")
        timezone = _string(send["timezone"], "timezone")
    except KeyError as exc:
        raise ConfigError(f"missing configuration key: {exc.args[0]}") from exc

    if sender_email != EXPECTED_SENDER_EMAIL or sender_name != EXPECTED_SENDER_NAME:
        raise ConfigError("sender must be Harrison — Mise <harrison@misegroup.co.uk>")
    if soft_limit <= 0 or hard_limit <= 0 or soft_limit > hard_limit:
        raise ConfigError("soft_limit must be positive and no greater than hard_limit")
    if not warmup:
        raise ConfigError("warmup must contain at least one ramp entry")
    previous_day = 0
    for through_day, limit in warmup:
        if through_day <= previous_day or limit <= 0 or limit >= soft_limit:
            raise ConfigError(
                "warmup days must increase and limits must be below soft_limit"
            )
        previous_day = through_day
    if not 0 <= window_uk[0] < window_uk[1] <= 24:
        raise ConfigError("window_uk must be an increasing hour range within 0..24")
    if (
        not preferred_weekdays
        or len(set(preferred_weekdays)) != len(preferred_weekdays)
        or any(day < 1 or day > 7 for day in preferred_weekdays)
    ):
        raise ConfigError("preferred_weekdays must be unique ISO weekday values 1..7")
    if (
        not followup_days
        or tuple(sorted(set(followup_days))) != followup_days
        or any(day <= 0 for day in followup_days)
    ):
        raise ConfigError("followup_days must be unique increasing positive offsets")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone: {timezone}") from exc

    return Settings(
        sender_email=sender_email,
        sender_name=sender_name,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        warmup=warmup,
        window_uk=window_uk,
        preferred_weekdays=preferred_weekdays,
        followups_first=followups_first,
        followup_days=followup_days,
        timezone=timezone,
    )


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc
    return _parse_settings(_table(raw, "configuration"))


def load_default_settings() -> Settings:
    resource = resources.files("govscout.resources").joinpath("default.toml")
    try:
        raw = tomllib.loads(resource.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load packaged configuration: {exc}") from exc
    return _parse_settings(_table(raw, "configuration"))
