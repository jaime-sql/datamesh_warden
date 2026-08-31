"""Singleton `google-genai` client, configured for either the Gemini API
(API key) or Vertex AI, based on `Settings`.

Constructing `genai.Client(...)` makes no network calls, so this module is
always safe to import; errors only surface once a method like
`generate_content` is actually invoked with no valid credentials.
"""

from __future__ import annotations

from functools import lru_cache

from google import genai

from app.config import get_settings


class GenAIConfigurationError(RuntimeError):
    """Raised when neither an API key nor a Vertex AI project is configured."""


def is_genai_configured() -> bool:
    """Cheap, side-effect-free check tools use to decide whether to call
    Gemini or fall back to a local heuristic implementation."""
    settings = get_settings()
    if settings.warden_use_vertex:
        return bool(settings.google_cloud_project)
    return bool(settings.gemini_api_key)


@lru_cache
def get_genai_client() -> genai.Client:
    settings = get_settings()

    if settings.warden_use_vertex:
        if not settings.google_cloud_project:
            raise GenAIConfigurationError(
                "WARDEN_USE_VERTEX=true requires GOOGLE_CLOUD_PROJECT to be set in .env."
            )
        return genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            location=settings.warden_vertex_location,
        )

    if not settings.gemini_api_key:
        raise GenAIConfigurationError(
            "GEMINI_API_KEY is not set. Get a free key from "
            "https://aistudio.google.com/apikey and add it to .env, or set "
            "WARDEN_USE_VERTEX=true to use Vertex AI instead."
        )
    return genai.Client(api_key=settings.gemini_api_key)
