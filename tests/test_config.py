from pathlib import Path

import pytest

from govscout.config import ConfigError, Settings, load_default_settings, load_settings


ROOT = Path(__file__).resolve().parents[1]


def test_default_config_matches_mise_p1_contract():
    settings = load_settings(ROOT / "config/default.toml")

    assert settings.sender_email == "harrison@misegroup.co.uk"
    assert settings.sender_name == "Harrison — Mise"
    assert settings.soft_limit == 10
    assert settings.hard_limit == 15
    assert settings.warmup == ((14, 5), (28, 8))
    assert settings.followup_days == (4, 12, 25)
    assert settings.window_uk == (8, 11)
    assert settings.preferred_weekdays == (2, 3, 4)
    assert settings.timezone == "Europe/London"


def test_packaged_default_config_matches_operator_copy():
    assert load_default_settings() == load_settings(ROOT / "config/default.toml")


def test_direct_settings_construction_cannot_bypass_fixed_p1_limits():
    with pytest.raises(ConfigError, match="hard_limit"):
        Settings(
            sender_email="harrison@misegroup.co.uk",
            sender_name="Harrison — Mise",
            soft_limit=99,
            hard_limit=100,
            warmup=((14, 98), (28, 98)),
            window_uk=(0, 24),
            preferred_weekdays=(1, 2, 3, 4, 5, 6, 7),
            followups_first=False,
            followup_days=(1,),
            timezone="UTC",
        )


def test_operator_config_override_cannot_raise_fixed_p1_limits(tmp_path):
    config = tmp_path / "unsafe.toml"
    config.write_text(
        """
[sender]
email = "harrison@misegroup.co.uk"
name = "Harrison — Mise"
[send]
soft_limit = 99
hard_limit = 100
warmup = [[14, 98], [28, 98]]
window_uk = [0, 24]
preferred_weekdays = [1, 2, 3, 4, 5, 6, 7]
followups_first = false
followup_days = [1]
timezone = "UTC"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="hard_limit"):
        load_settings(config)


def test_alternate_sender_is_rejected(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text(
        """
[sender]
email = "someone@example.com"
name = "Someone"
[send]
soft_limit = 10
hard_limit = 15
warmup = [[14, 5], [28, 8]]
window_uk = [8, 11]
preferred_weekdays = [2, 3, 4]
followups_first = true
followup_days = [4, 12, 25]
timezone = "Europe/London"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="sender"):
        load_settings(config)


def test_soft_limit_cannot_exceed_hard_limit(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text(
        """
[sender]
email = "harrison@misegroup.co.uk"
name = "Harrison — Mise"
[send]
soft_limit = 16
hard_limit = 15
warmup = [[14, 5], [28, 8]]
window_uk = [8, 11]
preferred_weekdays = [2, 3, 4]
followups_first = true
followup_days = [4, 12, 25]
timezone = "Europe/London"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="soft_limit"):
        load_settings(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("soft_limit", "10.5"),
        ("hard_limit", "true"),
        ("followups_first", '"false"'),
    ],
)
def test_safety_values_reject_coercible_wrong_toml_types(tmp_path, field, value):
    values = {
        "soft_limit": "10",
        "hard_limit": "15",
        "followups_first": "true",
    }
    values[field] = value
    config = tmp_path / "bad.toml"
    config.write_text(
        f"""
[sender]
email = "harrison@misegroup.co.uk"
name = "Harrison — Mise"
[send]
soft_limit = {values['soft_limit']}
hard_limit = {values['hard_limit']}
warmup = [[14, 5], [28, 8]]
window_uk = [8, 11]
preferred_weekdays = [2, 3, 4]
followups_first = {values['followups_first']}
followup_days = [4, 12, 25]
timezone = "Europe/London"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field):
        load_settings(config)


def test_non_table_sections_raise_config_error(tmp_path):
    config = tmp_path / "bad.toml"
    config.write_text('sender = "wrong"\nsend = "wrong"', encoding="utf-8")

    with pytest.raises(ConfigError, match="sections"):
        load_settings(config)
