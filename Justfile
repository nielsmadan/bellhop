[private]
default:
    @just --list

setup:
    @uv sync --locked --all-groups
    @lefthook install

typescript:
    @test -f .cache/typescript/node_modules/@earendil-works/pi-coding-agent/dist/index.d.ts -a -f .cache/typescript/node_modules/@types/node/package.json -a -x .cache/typescript/node_modules/typescript/bin/tsc || npm install --prefix .cache/typescript --no-save --ignore-scripts --omit=optional --no-audit --no-fund typescript@5.9.3 @earendil-works/pi-coding-agent@1.5.2 @types/node@26.6.6

package-check:
    @UV_CACHE_DIR=.cache/uv uv run --locked pytest -q -m packaging

test: typescript
    @uv run --locked pytest -q

coverage: typescript
    @uv run --locked pytest --cov --cov-report=term-missing --cov-report=html --cov-report=xml -q

smoke:
    @uv run --locked pytest -m smoke -q

lint:
    @uv run --locked ruff check

format:
    @uv run --locked ruff format

typecheck:
    @uv run --locked mypy

check-static:
    @uv run --locked ruff check
    @uv run --locked ruff format --check
    @uv run --locked pylint src/bellhop
    @uv run --locked mypy

check: check-static test

build:
    @rm -rf dist build
    @uv build --sdist
    @uv build --wheel dist/bellhop-*.tar.gz

install:
    @uv tool install --reinstall --force .

install-editable:
    @uv tool install --reinstall --force --editable .

uninstall:
    @uv tool uninstall bellhop

# Cut a release with curtains: --dry-run, patch|minor|major or X.Y.Z, --yes.
release *args:
    @LEFTHOOK=0 curtains {{args}}

# Regenerate CHANGELOG.md as if VERSION were tagged.
changelog version:
    @env UV_TOOL_DIR="$PWD/.uv/tools" UV_CACHE_DIR="$PWD/.uv/cache" UV_PYTHON_INSTALL_DIR="$PWD/.uv/python" uvx git-cliff@2.13.1 --config cliff.toml --tag 'v{{version}}' -o CHANGELOG.md

clean:
    @rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
