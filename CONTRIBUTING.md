# Contributing

## Architecture

### XDG Base Directory Specification

This project follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) for file storage:

| XDG Variable | Default | Our Usage |
|--------------|---------|-----------|
| `XDG_CONFIG_HOME` | `~/.config` | `~/.config/pay-calc/config.yaml` |
| `XDG_CACHE_HOME` | `~/.cache` | `~/.cache/pay-calc/` (downloaded PDFs) |
| `XDG_DATA_HOME` | `~/.local/share` | `~/.local/share/pay-calc/` (outputs) |

### Config Path Resolution

Configuration is loaded with this precedence:

1. `PAY_CALC_CONFIG_PATH` environment variable (if set)
2. `./pay-calc/config.yaml` in current working directory (if file exists)
3. `~/.config/pay-calc/config.yaml` (XDG default)

This allows:
- **Development**: Local `./pay-calc/` config in project directory
- **Production**: XDG-compliant `~/.config/pay-calc/`
- **Docker/CI**: Environment variable override

### Package Structure

```
paycalc/
├── __init__.py          # Version
├── sdk/                 # Core logic (no CLI dependencies)
│   ├── __init__.py
│   ├── config.py        # Path resolution, config load/save
│   └── tax.py           # Tax calculations, projection generation
└── cli/                 # Click-based CLI (thin wrapper around SDK)
    ├── __init__.py
    ├── __main__.py      # Main CLI entry point
    └── config_commands.py
```

**Design principle**: Business logic lives in `sdk/`, CLI is a thin wrapper.

### Adding New Features

1. Add core logic to appropriate `sdk/` module
2. Add CLI command in `cli/` that calls SDK
3. Update tests
4. Update README if user-facing

## Development

```bash
# Install in editable mode
pip install -e .

# Run CLI
pay-calc --help

# Test config resolution
pay-calc config path
```
