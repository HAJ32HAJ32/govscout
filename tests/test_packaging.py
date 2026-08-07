import tomllib
from importlib import resources
from importlib.util import find_spec
from pathlib import Path

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

    assert "GovScout — Review firms" in template
    assert "Not yet researched" in template
    assert "Email drafts" not in template


def test_retired_lca_harvester_is_not_an_importable_product_module():
    assert not (ROOT / "src/govscout/lca_harvest.py").exists()
    assert find_spec("govscout.lca_harvest") is None


def test_collector_has_a_native_entry_point_and_bounded_runtime_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["govscout-collector"] == (
        "govscout_collector.app:main"
    )
    assert pyproject["project"]["optional-dependencies"]["collector"] == [
        "keyring==25.7.0"
    ]


def test_collector_release_workflow_builds_windows_and_both_macos_architectures():
    workflow = (ROOT / ".github/workflows/collector-release.yml").read_text(
        encoding="utf-8"
    )

    assert "windows-latest" in workflow
    assert "macos-15-intel" in workflow
    assert "runner: macos-15\n" in workflow
    assert "pyinstaller" in workflow.casefold()
    assert '"pyinstaller==6.21.0"' in workflow
    assert "GovScout-Collector-Windows-x86_64.exe" in workflow
    assert "GovScout-Collector-macOS-x86_64" in workflow
    assert "GovScout-Collector-macOS-arm64" in workflow
    assert "Smoke-test Windows executable" in workflow
    assert "Smoke-test macOS application" in workflow
    assert "sha256" in workflow.casefold()
    assert "gh release upload" in workflow
    assert "src/govscout_collector/__main__.py" in workflow


def test_collector_release_builds_have_read_only_repository_credentials():
    workflow = (ROOT / ".github/workflows/collector-release.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  contents: read" in workflow
    assert workflow.count("persist-credentials: false") == 2
    assert "publish-release:" in workflow
    assert "needs: [windows, macos]" in workflow
    assert "permissions:\n      contents: write" in workflow


def test_collector_module_has_a_real_launcher():
    launcher = (ROOT / "src/govscout_collector/__main__.py").read_text(encoding="utf-8")

    assert "main()" in launcher
    assert "SystemExit" in launcher
