from importlib import resources
from importlib.util import find_spec
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_wheel_manifest_declares_web_template_package_data():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["setuptools"]["package-data"]["govscout.web"] == [
        "templates/*.html"
    ]


def test_today_template_is_available_as_a_package_resource():
    template = (
        resources.files("govscout.web")
        .joinpath("templates", "today.html")
        .read_text(encoding="utf-8")
    )

    assert "GovScout — Today" in template
    assert "Production drafting locked: LINT_NOT_READY" in template


def test_retired_lca_harvester_is_not_an_importable_product_module():
    assert not (ROOT / "src/govscout/lca_harvest.py").exists()
    assert find_spec("govscout.lca_harvest") is None
