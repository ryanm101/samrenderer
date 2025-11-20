.PHONY: install test lint format build clean all help render-example check-env

# Default target
all: check-env format lint test build ## Run all checks and build

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-env: ## Check if uv is installed
	@command -v uv >/dev/null 2>&1 || { echo >&2 "Error: 'uv' is not installed. Please install it from https://github.com/astral-sh/uv"; exit 1; }

install: check-env ## Install dependencies
	uv sync

test: check-env ## Run tests
	uv run pytest

lint: check-env ## Run linter with auto-fix
	uv run ruff check --fix .

format: check-env ## Format code
	uv run ruff format .

build: check-env install ## Build distribution packages
	uv build

clean: ## Clean build artifacts and caches
	rm -rf dist/
	rm -rf .pytest_cache/
	rm -rf .ruff_cache/
	find . -type d -name "__pycache__" -exec rm -rf {} +

render-example: check-env ## Render example template for dev environment
	uv run sam-render examples/template.yml --config examples/samconfig.toml --env dev

render-example-compare: check-env ## Compare dev and stag environments
	uv run sam-render examples/template.yml --config examples/samconfig.toml --env dev --env2 stag
