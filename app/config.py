"""Centralized runtime configuration, loaded once from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    warden_mode: Literal["local", "cloud"] = "local"

    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"

    gemini_api_key: str | None = None
    warden_use_vertex: bool = False
    # Gemini 3.5 Flash is not published in us-central1 (404 NOT_FOUND).
    # Vertex PayGo for 3.5 is global / us / eu; keep google_cloud_location
    # as the Cloud Run / BigQuery region.
    warden_vertex_location: str = "global"

    warden_orchestrator_model: str = "gemini-3.1-pro-preview"
    warden_patcher_model: str = "gemini-3.5-flash"
    warden_governance_model: str = "gemini-3.5-flash"

    warden_gemma_endpoint: str | None = None

    warden_bq_sandbox_dataset_prefix: str = "warden_sandbox_"
    warden_bq_sandbox_expiration_hours: int = 24

    # Post-Phase-6 addition: real-pipeline health check (see
    # app/agents/pipeline_health.py). Points at the separate
    # datamesh_pipeline project's Cloud Run Job -- same GCP project as
    # Warden itself, so no cross-project credentials are needed, just IAM
    # roles (run.viewer, logging.viewer) granted to warden-api-run.
    warden_monitored_job_region: str = "us-central1"
    warden_monitored_job_name: str = "pg-to-bq-sync"
    warden_monitored_job_resource_uri: str = "postgres://neon/bronze"

    warden_max_turns: int = 8
    warden_turn_timeout_s: int = 90
    warden_tool_timeout_s: int = 60
    warden_incident_timeout_s: int = 240

    warden_api_host: str = "0.0.0.0"
    warden_api_port: int = 8080
    warden_api_base_url: str = "http://localhost:8080"

    warden_log_level: str = "INFO"
    warden_log_json: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
