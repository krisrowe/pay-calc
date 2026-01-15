.PHONY: install install-dev clean test venv

# Default: install with pipx for CLI usage
install:
	@command -v pipx >/dev/null 2>&1 || (echo "pipx not found; install with: pip install pipx"; exit 1)
	@echo "Installing paycalc CLI with pipx..."
	@pipx install -e . --force 2>/dev/null || pipx install . --force
	@echo "Done. Run 'pay-calc --help' to get started."

# Install with MCP server support
install-mcp:
	@command -v pipx >/dev/null 2>&1 || (echo "pipx not found; install with: pip install pipx"; exit 1)
	@echo "Installing paycalc with MCP support..."
	@pipx install -e '.[mcp]' --force 2>/dev/null || pipx install '.[mcp]' --force
	@echo "Done. Run 'pay-calc-mcp' to start MCP server."

# Development install (editable with dev deps)
install-dev:
	@echo "Creating virtual environment and installing dev dependencies..."
	@python3 -m venv .venv
	@. .venv/bin/activate && pip install --upgrade pip
	@. .venv/bin/activate && pip install -e '.[dev,mcp,filter]'
	@echo "Done. Activate with: source .venv/bin/activate"

# Quick dev install without venv (for active development)
dev:
	pip install -e '.[dev,mcp,filter]'

# Run tests
test:
	@if [ ! -d ".venv" ]; then \
		echo "Virtual environment not found. Creating it..."; \
		python3 -m venv .venv; \
		. .venv/bin/activate && pip install --upgrade pip && pip install -e '.[dev]'; \
	fi
	@. .venv/bin/activate && pytest tests/

# Run tests (quick, assumes deps installed)
test-quick:
	pytest tests/ -q

# Clean build artifacts
clean:
	rm -rf .venv
	rm -rf build dist
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

# Uninstall from pipx
uninstall:
	pipx uninstall paycalc 2>/dev/null || true

# Reinstall (clean + install)
reinstall: uninstall install

# Format code with ruff
fmt:
	@. .venv/bin/activate && ruff format paycalc/
	@. .venv/bin/activate && ruff check --fix paycalc/

# Lint check
lint:
	@. .venv/bin/activate && ruff check paycalc/

# Build wheel for distribution
build: clean
	python3 -m build

# Show help
help:
	@echo "Available targets:"
	@echo "  install      - Install CLI with pipx"
	@echo "  install-mcp  - Install CLI + MCP server with pipx"
	@echo "  install-dev  - Create venv and install dev dependencies"
	@echo "  dev          - Quick editable install (no venv)"
	@echo "  test         - Run tests"
	@echo "  clean        - Remove build artifacts"
	@echo "  uninstall    - Remove from pipx"
	@echo "  reinstall    - Clean reinstall with pipx"
	@echo "  fmt          - Format code with ruff"
	@echo "  lint         - Check code with ruff"
	@echo "  build        - Build wheel for distribution"
