.PHONY: install run-api run-ui run-local test lint typecheck

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
	$(VENV)/Scripts/mypy.exe app ui
