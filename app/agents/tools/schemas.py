"""Builds `google-genai` `FunctionDeclaration` objects directly from the
tool functions' live signatures and docstrings, so the schema sent to
Gemini can never drift out of sync with the actual Python implementation.
"""

from __future__ import annotations

from typing import Literal

from google.genai import types

from app.agents.tools.governance import verify_governance_policy
from app.agents.tools.investigate import investigate_incident_logs
from app.agents.tools.patch import generate_and_test_patch

ApiOption = Literal["GEMINI_API", "VERTEX_AI"]

_TOOL_FUNCTIONS = (investigate_incident_logs, generate_and_test_patch, verify_governance_policy)


def build_tools(api_option: ApiOption = "GEMINI_API") -> list[types.Tool]:
    declarations = [
        types.FunctionDeclaration.from_callable_with_api_option(callable=fn, api_option=api_option)
        for fn in _TOOL_FUNCTIONS
    ]
    return [types.Tool(function_declarations=declarations)]
