.DEFAULT_GOAL := help
DIST := dist
UV := uv

.PHONY: help install dev lint fmt test build clean smoke check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv + editable install (core deps only)
	$(UV) venv
	$(UV) pip install -e .

dev:  ## Create .venv + editable install with dev + cli extras
	$(UV) venv
	$(UV) pip install -e ".[dev,cli]"

lint:  ## Run ruff linter
	$(UV) run --no-sync ruff check src/ tests/

fmt:  ## Auto-format with ruff
	$(UV) run --no-sync ruff format src/ tests/
	$(UV) run --no-sync ruff check --fix src/ tests/

test:  ## Run pytest
	$(UV) run --no-sync pytest --tb=short -q

build: clean  ## Build sdist + wheel into dist/
	$(UV) build
	@echo ""
	@ls -lh $(DIST)/

clean:  ## Remove build artifacts
	rm -rf $(DIST) build src/*.egg-info src/argus_quarry/*.egg-info

smoke: build  ## Build wheel, install in throwaway venv, smoke-test import
	$(eval TMPVENV := $(shell mktemp -d))
	$(UV) venv $(TMPVENV)/venv
	$(UV) pip install --python $(TMPVENV)/venv $(DIST)/*.whl
	$(TMPVENV)/venv/bin/python -c \
		"from argus_quarry import PortraitRecord, __version__; print(f'argus-quarry {__version__} OK')"
	rm -rf $(TMPVENV)

check: lint test build  ## Full local CI: lint + test + build
	@echo ""
	@echo "All checks passed."
