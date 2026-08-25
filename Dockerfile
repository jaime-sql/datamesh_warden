# DataMesh Warden -- API service (`warden-orchestrator` on Cloud Run).
#
# Serves the FastAPI app in app/api/main.py: event ingestion, the
# orchestrator/tool-calling loop, and the human-in-the-loop
# execute/reject endpoints. See docs/architecture.md section 1.
#
# The Streamlit UI is a separate Cloud Run service with its own image
# (Dockerfile.ui, Phase 5) so the two can scale and deploy independently.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WARDEN_API_HOST=0.0.0.0 \
    WARDEN_API_PORT=8080

WORKDIR /srv

# Copied separately from the source so `pip install` only re-runs when
# dependencies actually change, not on every code edit.
COPY pyproject.toml README.md ./
COPY app ./app
COPY ui ./ui

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
