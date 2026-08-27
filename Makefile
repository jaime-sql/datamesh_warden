.PHONY: install run-api run-ui run-local test lint typecheck docker-build docker-run docker-build-ui docker-run-ui deploy teardown reset-demo-data

VENV := .venv
PYTHON := $(VENV)/Scripts/python.exe
PIP := $(VENV)/Scripts/pip.exe

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PIP) install -e ".[dev]"

run-api:
	$(PYTHON) -m uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8080

run-ui:
	$(VENV)/Scripts/streamlit.exe run ui/streamlit_app.py

run-local:
	@echo "Run 'make run-api' and 'make run-ui' in two separate terminals."
	@echo "Both default to WARDEN_MODE=local (InMemoryStateManager, no GCP calls)."

test:
	$(VENV)/Scripts/pytest.exe -v

lint:
	$(VENV)/Scripts/ruff.exe check .

typecheck:
	$(VENV)/Scripts/mypy.exe app ui tests

docker-build:
	docker build -t datamesh-warden-api .

docker-run:
	docker run --rm -p 8080:8080 -e WARDEN_MODE=local datamesh-warden-api

docker-build-ui:
	docker build -f Dockerfile.ui -t datamesh-warden-ui .

docker-run-ui:
	docker run --rm -p 8501:8501 -e WARDEN_API_BASE_URL=http://host.docker.internal:8080 datamesh-warden-ui

deploy:
	powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1

teardown:
	powershell -ExecutionPolicy Bypass -File scripts/teardown.ps1

reset-demo-data:
	powershell -ExecutionPolicy Bypass -File scripts/reset-demo-data.ps1
