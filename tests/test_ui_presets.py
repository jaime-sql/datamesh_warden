"""Tests for `ui/presets.py::build_presets`."""

from __future__ import annotations

from ui.presets import DEFAULT_PROJECT, build_presets


def test_build_presets_defaults_to_placeholder_project() -> None:
    presets = build_presets()
    assert len(presets) == 4
    for preset in presets:
        assert preset["resource_uri"] == f"bq://{DEFAULT_PROJECT}.sales.orders"


def test_build_presets_uses_real_project_when_given() -> None:
    # This is the actual bug this covers: WARDEN_MODE=cloud tools make
    # real BigQuery calls against resource_uri, so a fake placeholder
    # project causes a real 404 partway through diagnosis (see
    # docs/architecture.md's Phase 6 note).
    presets = build_presets(project="my-real-project")
    for preset in presets:
        assert preset["resource_uri"] == "bq://my-real-project.sales.orders"


def test_build_presets_supports_custom_dataset_and_table() -> None:
    presets = build_presets(project="p", dataset="d", table="t")
    for preset in presets:
        assert preset["resource_uri"] == "bq://p.d.t"
        assert preset["raw_event"].get("table", "t") == "t"
