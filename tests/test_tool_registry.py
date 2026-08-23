from __future__ import annotations

from app.agents.tool_registry import TOOL_IMPL, TOOL_NAMES, TOOLS

_EXPECTED_TOOL_NAMES = {
    "investigate_incident_logs",
    "generate_and_test_patch",
    "verify_governance_policy",
}


def test_tool_registry_exposes_all_three_subagent_tools() -> None:
    assert set(TOOL_NAMES) == _EXPECTED_TOOL_NAMES
    assert set(TOOL_IMPL.keys()) == _EXPECTED_TOOL_NAMES


def test_tool_schemas_are_generated_from_live_function_signatures() -> None:
    assert len(TOOLS) == 1
    declarations = TOOLS[0].function_declarations
    assert declarations is not None

    names = {d.name for d in declarations}
    assert names == _EXPECTED_TOOL_NAMES

    for declaration in declarations:
        assert declaration.description
        assert declaration.parameters is not None
