.DEFAULT_GOAL := help
DIST := dist
UV := uv

.PHONY: help install dev lint fmt test build clean smoke check web-install pages shared-check

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
	rm -rf $(DIST) build src/*.egg-info src/argus_quarry/*.egg-info web/.next web/out

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

# ── Hosted demo (quarry.dragonhound.dev): a static replay of a recorded run, see DESIGN.md §9 ──
web-install:  ## Install the demo site's dependencies (web/)
	cd web && npm ci

pages: web-install  ## Static export of the recorded tape to web/out/, with a hash CSP on every page
	cd web && npm run build

shared-check:  ## The vendored Dragonhound shared layer is unedited and matches the published v1 manifest
	node web/vendor/dh/sync-shared.mjs --check . --against https://dragonhound.dev/brand/v1/manifest.json --allow-outdated
