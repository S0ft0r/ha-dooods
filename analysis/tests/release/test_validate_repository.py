"""Regression tests for the read-only pre-publication repository validator."""

import json
import zipfile

import pytest

from validate_repository import (
    ROOT,
    _is_safe_archive_member,
    validate_repository,
)


def test_current_repository_passes_static_validation() -> None:
    """Project metadata, staged App, baseline, and saved ZIPs remain consistent."""
    errors = validate_repository(ROOT)
    assert errors == [], "\n".join(errors)


def test_0_1_17_full_and_cumulative_archives_include_previous_ui_fixes() -> None:
    """Both install paths carry the profile-editor fixes from 0.1.16.1."""
    releases = ROOT / "releases"
    ui_path = "custom_components/dooods/frontend/profile-manager.js"
    previous_patch = releases / "dooods_update_0.1.16.1__0.1.15.2-dev.zip"
    full_package = releases / "dooods_full_0.1.17__0.1.15.2-dev.zip"
    cumulative_patch = (
        releases / "dooods_update_0.1.17__0.1.15.2-dev_from_0.1.16.zip"
    )

    with zipfile.ZipFile(previous_patch) as archive:
        expected_ui = archive.read(ui_path)
    with zipfile.ZipFile(full_package) as archive:
        assert archive.read(ui_path) == expected_ui
        manifest = json.loads(archive.read("custom_components/dooods/manifest.json"))
        app_config = archive.read("local_apps/dooods2/config.yaml").decode("utf-8")
    with zipfile.ZipFile(cumulative_patch) as archive:
        assert archive.read(ui_path) == expected_ui
        metadata = json.loads(archive.read("DOOODS-UPDATE.json"))

    assert manifest["version"] == "0.1.17"
    assert 'version: "0.1.15.2-dev"' in app_config
    assert metadata["base_versions"] == {
        "integration": "0.1.16",
        "app": "0.1.15.2-dev",
    }
    assert metadata["target_versions"] == {
        "integration": "0.1.17",
        "app": "0.1.15.2-dev",
    }
    assert ui_path in {item["path"] for item in metadata["changed_files"]}


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("custom_components/dooods/manifest.json", True),
        ("folder/file.txt", True),
        ("../outside.txt", False),
        ("folder/../../outside.txt", False),
        ("/absolute/path.txt", False),
        ("C:/absolute/path.txt", False),
        (r"..\\outside.txt", False),
        ("", False),
    ],
)
def test_archive_member_path_safety(member: str, expected: bool) -> None:
    """ZIP entries must stay inside their install root and use POSIX paths."""
    assert _is_safe_archive_member(member) is expected
