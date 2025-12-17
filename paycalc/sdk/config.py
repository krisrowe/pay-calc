"""Configuration management for Pay Calc.

Config path resolution:
1. PAY_CALC_CONFIG_PATH environment variable (if set)
2. ./pay-calc/ in current working directory (if config.yaml exists there)
3. ~/.config/pay-calc/ (XDG_CONFIG_HOME fallback)

Cache and data paths follow XDG spec:
- Cache: XDG_CACHE_HOME/pay-calc/ or ~/.cache/pay-calc/
- Data: XDG_DATA_HOME/pay-calc/ or ~/.local/share/pay-calc/
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml


APP_NAME = "pay-calc"
CONFIG_FILENAME = "config.yaml"


class ConfigNotFoundError(Exception):
    """Raised when no configuration is found."""
    pass


def get_config_path(require_exists: bool = False) -> Path:
    """Get the configuration directory path.

    Resolution order:
    1. PAY_CALC_CONFIG_PATH environment variable
    2. ./pay-calc/ if config.yaml exists there
    3. ~/.config/pay-calc/ (XDG_CONFIG_HOME)

    Args:
        require_exists: If True, raises ConfigNotFoundError if no config found

    Returns:
        Path to the configuration directory

    Raises:
        ConfigNotFoundError: If require_exists=True and no config is found
    """
    # 1. Check environment variable
    env_path = os.environ.get("PAY_CALC_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    # 2. Check local ./pay-calc/ directory
    local_config = Path.cwd() / APP_NAME / CONFIG_FILENAME
    if local_config.exists():
        return Path.cwd() / APP_NAME

    # 3. Fall back to XDG config path
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    xdg_path = Path(xdg_config_home) / APP_NAME

    if require_exists:
        xdg_config_file = xdg_path / CONFIG_FILENAME
        if not xdg_config_file.exists():
            raise ConfigNotFoundError(
                f"No configuration found. Checked:\n"
                f"  1. PAY_CALC_CONFIG_PATH env var (not set)\n"
                f"  2. {Path.cwd() / APP_NAME / CONFIG_FILENAME} (not found)\n"
                f"  3. {xdg_config_file} (not found)\n\n"
                f"Run 'pay-calc config init' to create a configuration."
            )

    return xdg_path


def get_cache_path() -> Path:
    """Get the cache directory path (XDG_CACHE_HOME/pay-calc/).

    Returns:
        Path to the cache directory (created if doesn't exist)
    """
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    cache_path = Path(xdg_cache_home) / APP_NAME
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


def get_data_path() -> Path:
    """Get the data directory path (XDG_DATA_HOME/pay-calc/).

    Returns:
        Path to the data directory (created if doesn't exist)
    """
    xdg_data_home = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    data_path = Path(xdg_data_home) / APP_NAME
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


def get_config_file_path(require_exists: bool = False) -> Path:
    """Get the full path to the config.yaml file.

    Args:
        require_exists: If True, raises ConfigNotFoundError if config doesn't exist

    Returns:
        Path to the config.yaml file
    """
    config_dir = get_config_path(require_exists=require_exists)
    return config_dir / CONFIG_FILENAME


def load_config(require_exists: bool = True) -> dict:
    """Load configuration from config.yaml.

    Args:
        require_exists: If True, raises ConfigNotFoundError if config doesn't exist

    Returns:
        Configuration dictionary (empty dict if file doesn't exist and not required)

    Raises:
        ConfigNotFoundError: If require_exists=True and no config is found
    """
    config_file = get_config_file_path(require_exists=require_exists)

    if not config_file.exists():
        return {}

    with open(config_file, "r") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict) -> Path:
    """Save configuration to config.yaml.

    Args:
        config: Configuration dictionary to save

    Returns:
        Path to the saved config file
    """
    config_dir = get_config_path(require_exists=False)
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / CONFIG_FILENAME

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return config_file


def get_config_value(key: str, default: Any = None) -> Any:
    """Get a configuration value by dot-notation key.

    Args:
        key: Dot-notation key (e.g., "drive.w2_pay_records.2024")
        default: Default value if key not found

    Returns:
        Configuration value or default
    """
    config = load_config(require_exists=False)

    parts = key.split(".")
    value = config

    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default

    return value


def set_config_value(key: str, value: Any) -> Path:
    """Set a configuration value by dot-notation key.

    Args:
        key: Dot-notation key (e.g., "drive.w2_pay_records.2024")
        value: Value to set

    Returns:
        Path to the saved config file
    """
    config = load_config(require_exists=False)

    parts = key.split(".")
    current = config

    # Navigate/create nested structure
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]

    # Set the value
    current[parts[-1]] = value

    return save_config(config)


def ensure_config_exists() -> bool:
    """Check if configuration exists and is valid.

    Returns:
        True if config exists and is valid, False otherwise
    """
    try:
        config = load_config(require_exists=True)
        return bool(config)
    except ConfigNotFoundError:
        return False


def get_year_cache_path(year: str, subdir: str = "") -> Path:
    """Get cache path for a specific year.

    Args:
        year: Year string (e.g., "2024")
        subdir: Optional subdirectory (e.g., "w2_pay_records", "paystubs")

    Returns:
        Path to year-specific cache directory (created if doesn't exist)
    """
    if subdir:
        path = get_cache_path() / year / subdir
    else:
        path = get_cache_path() / year
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_year_data_path(year: str) -> Path:
    """Get data path for a specific year.

    Args:
        year: Year string (e.g., "2024")

    Returns:
        Path to year-specific data directory (created if doesn't exist)
    """
    path = get_data_path() / year
    path.mkdir(parents=True, exist_ok=True)
    return path
