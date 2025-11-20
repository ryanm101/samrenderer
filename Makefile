.PHONY: install test lint format build clean all help render-example check-env

# Default target
all: check-env format lint test build

help:
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-env:
	@command -v uv >/dev/null 2>&1 || { echo >&2 "Error: 'uv' is not installed. Please install it from https://github.com/astral-sh/uv"; exit 1; }

install: check-env
	uv sync

test: check-env
	uv run pytest

lint: check-env
	uv run ruff check --fix .

format: check-env
	uv run ruff format .

build: check-env install
	uv build

clean:
	rm -rf dist/
	rm -rf .pytest_cache/
	rm -rf .ruff_cache/
	find . -type d -name "__pycache__" -exec rm -rf {} +

render-example: check-env
	uv run sam-render examples/template.yml --config examples/samconfig.toml --env dev

render-example-compare: check-env
	uv run sam-render examples/template.yml --config examples/samconfig.toml --env dev --env2 stag
