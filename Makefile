# devdocs development tasks (GNU make 3.81+). Python tools run through `uv run`,
# so uv.lock pins them; override UV, PYTHON or IMAGE on the command line.

UV ?= uv
PYTHON ?= python3
IMAGE ?= devdocs:dev
RUN = $(UV) run --locked --extra dev
AUDIT_REQUIREMENTS = build/requirements-audit.txt

.DEFAULT_GOAL := help
.PHONY: help dev test lint typecheck coverage security check build npm docker clean

help: ## List the targets
	@printf 'usage: make <target>\n\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-z][a-z-]*:.*## / { printf "  %-10s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

dev: ## Create .venv with the package and the locked dev tools
	$(UV) sync --locked --extra dev

test: ## Run the unit tests
	$(RUN) python -m unittest discover -s tests

lint: ## Lint the package, tests and scripts with ruff
	$(RUN) ruff check devdocs tests scripts

typecheck: ## Type-check the package and the release script with mypy --strict
	$(RUN) mypy --strict devdocs scripts/release.py

coverage: ## Run the tests under coverage; fails below the configured floor
	$(RUN) coverage run -m unittest discover -s tests
	$(RUN) coverage report

security: ## Run bandit on the package and pip-audit on the locked dev tools
	$(RUN) bandit -r devdocs -ll
	$(UV) export --locked --extra dev --no-hashes --no-emit-project --quiet --output-file $(AUDIT_REQUIREMENTS)
	$(RUN) pip-audit --requirement $(AUDIT_REQUIREMENTS) --no-deps --disable-pip --progress-spinner off

check: lint typecheck test ## Lint, type-check and test

build: ## Build the sdist and wheel into dist/
	$(UV) build

npm: ## Pack the npm wrapper into npm/*.tgz (prepack bundles the package)
	cd npm && npm pack

docker: ## Build the container image devdocs:dev with version labels
	version=$$($(PYTHON) scripts/release.py current) && \
	revision=$$(git rev-parse HEAD) && \
	created=$$(date -u +%Y-%m-%dT%H:%M:%SZ) && \
	docker build --build-arg VERSION="$$version" --build-arg REVISION="$$revision" \
		--build-arg CREATED="$$created" --tag $(IMAGE) .

clean: ## Remove build products and caches
	rm -rf dist build *.egg-info npm/python npm/LICENSE npm/*.tgz .coverage .coverage.* htmlcov .mypy_cache .ruff_cache
	find . \( -name .git -o -name .venv -o -name node_modules \) -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
