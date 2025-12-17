"""Configuration management for Pay Calc.

Configuration is split into two files:

1. settings.json - Machine-specific, ephemeral settings
   - profile: path to profile.yaml (optional, if not colocated)
   - default_output_format: tool behavior preferences
   - Other non-critical settings

2. profile.yaml - User's personal configuration
   - drive: folder IDs for Drive access
   - parties: him/her definitions with employer keywords
   - Personal/private data that matters

Config directory resolution:
1. PAY_CALC_CONFIG_PATH environment variable (if set)
2. ~/.config/pay-calc/ (XDG_CONFIG_HOME fallback)

Profile resolution:
1. settings.json "profile" key (if set via CLI)
2. profile.yaml in same config directory

Cache and data paths follow XDG spec:
- Cache: XDG_CACHE_HOME/pay-calc/ or ~/.cache/pay-calc/
- Data: XDG_DATA_HOME/pay-calc/ or ~/.local/share/pay-calc/
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml


APP_NAME = "pay-calc"
SETTINGS_FILENAME = "settings.json"
PROFILE_FILENAME = "profile.yaml"
# Legacy support
LEGACY_CONFIG_FILENAME = "config.yaml"


class ConfigNotFoundError(Exception):
    """Raised when no configuration is found."""
    pass


class ProfileNotFoundError(Exception):
    """Raised when no profile is found."""
    pass


def get_config_dir() -> Path:
    """Get the configuration directory path.

    Resolution order:
    1. PAY_CALC_CONFIG_PATH environment variable
    2. ~/.config/pay-calc/ (XDG_CONFIG_HOME)

    Returns:
        Path to the configuration directory
    """
    # 1. Check environment variable
    env_path = os.environ.get("PAY_CALC_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    # 2. Fall back to XDG config path
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(xdg_config_home) / APP_NAME


def get_settings_path() -> Path:
    """Get the path to settings.json.

    Returns:
        Path to settings.json (may not exist yet)
    """
    return get_config_dir() / SETTINGS_FILENAME


def load_settings() -> dict:
    """Load machine-specific settings from settings.json.

    Returns:
        Settings dictionary (empty dict if file doesn't exist)
    """
    settings_file = get_settings_path()

    if not settings_file.exists():
        return {}

    with open(settings_file, "r") as f:
        return json.load(f)


def save_settings(settings: dict) -> Path:
    """Save machine-specific settings to settings.json.

    Args:
        settings: Settings dictionary to save

    Returns:
        Path to the saved settings file
    """
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    settings_file = config_dir / SETTINGS_FILENAME

    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2)

    return settings_file


def get_setting(key: str, default: Any = None) -> Any:
    """Get a setting value from settings.json.

    Args:
        key: Setting key (e.g., "profile", "default_output_format")
        default: Default value if key not found

    Returns:
        Setting value or default
    """
    settings = load_settings()
    return settings.get(key, default)


def set_setting(key: str, value: Any) -> Path:
    """Set a setting value in settings.json.

    Args:
        key: Setting key
        value: Value to set

    Returns:
        Path to the saved settings file
    """
    settings = load_settings()
    settings[key] = value
    return save_settings(settings)


def get_profile_path(require_exists: bool = False) -> Path:
    """Get the path to the profile.yaml file.

    Resolution order:
    1. settings.json "profile" key (if set)
    2. profile.yaml in config directory
    3. Legacy: config.yaml in config directory (for migration)

    Args:
        require_exists: If True, raises ProfileNotFoundError if not found

    Returns:
        Path to profile.yaml

    Raises:
        ProfileNotFoundError: If require_exists=True and no profile found
    """
    config_dir = get_config_dir()

    # 1. Check settings.json for custom profile path
    settings = load_settings()
    custom_profile = settings.get("profile")
    if custom_profile:
        profile_path = Path(custom_profile)
        if require_exists and not profile_path.exists():
            raise ProfileNotFoundError(
                f"Profile not found at configured path: {profile_path}\n\n"
                f"Update with: pay-calc config set-profile /path/to/profile.yaml"
            )
        return profile_path

    # 2. Check for profile.yaml in config directory
    profile_path = config_dir / PROFILE_FILENAME
    if profile_path.exists():
        return profile_path

    # 3. Legacy: check for config.yaml (migration support)
    legacy_path = config_dir / LEGACY_CONFIG_FILENAME
    if legacy_path.exists():
        return legacy_path

    if require_exists:
        raise ProfileNotFoundError(
            f"No profile found. Checked:\n"
            f"  1. settings.json 'profile' key (not set)\n"
            f"  2. {profile_path} (not found)\n\n"
            f"Create a profile with: pay-calc config init\n"
            f"Or set a custom path: pay-calc config set-profile /path/to/profile.yaml"
        )

    return profile_path


def load_profile(require_exists: bool = True) -> dict:
    """Load user profile from profile.yaml.

    Args:
        require_exists: If True, raises ProfileNotFoundError if not found

    Returns:
        Profile dictionary (empty dict if not required and not found)

    Raises:
        ProfileNotFoundError: If require_exists=True and no profile found
    """
    profile_path = get_profile_path(require_exists=require_exists)

    if not profile_path.exists():
        return {}

    with open(profile_path, "r") as f:
        return yaml.safe_load(f) or {}


def save_profile(profile: dict, path: Optional[Path] = None) -> Path:
    """Save user profile to profile.yaml.

    Args:
        profile: Profile dictionary to save
        path: Optional custom path (uses default if not specified)

    Returns:
        Path to the saved profile file
    """
    if path is None:
        path = get_profile_path(require_exists=False)

    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False)

    return path


def get_profile_value(key: str, default: Any = None) -> Any:
    """Get a profile value by dot-notation key.

    Args:
        key: Dot-notation key (e.g., "drive.w2_pay_records.2024")
        default: Default value if key not found

    Returns:
        Profile value or default
    """
    profile = load_profile(require_exists=False)

    parts = key.split(".")
    value = profile

    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default

    return value


def set_profile_value(key: str, value: Any) -> Path:
    """Set a profile value by dot-notation key.

    Args:
        key: Dot-notation key (e.g., "drive.w2_pay_records.2024")
        value: Value to set

    Returns:
        Path to the saved profile file
    """
    profile = load_profile(require_exists=False)

    parts = key.split(".")
    current = profile

    # Navigate/create nested structure
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        current = current[part]

    # Set the value
    current[parts[-1]] = value

    return save_profile(profile)


# =============================================================================
# Legacy compatibility aliases (map old names to new functions)
# =============================================================================

def get_config_path(require_exists: bool = False) -> Path:
    """Legacy: Get config directory path.

    DEPRECATED: Use get_config_dir() instead.
    """
    if require_exists:
        # Check if profile exists
        try:
            get_profile_path(require_exists=True)
        except ProfileNotFoundError as e:
            raise ConfigNotFoundError(str(e))
    return get_config_dir()


def load_config(require_exists: bool = True) -> dict:
    """Legacy: Load configuration.

    DEPRECATED: Use load_profile() instead.
    """
    try:
        return load_profile(require_exists=require_exists)
    except ProfileNotFoundError as e:
        raise ConfigNotFoundError(str(e))


def save_config(config: dict) -> Path:
    """Legacy: Save configuration.

    DEPRECATED: Use save_profile() instead.
    """
    return save_profile(config)


def get_config_value(key: str, default: Any = None) -> Any:
    """Legacy: Get config value.

    DEPRECATED: Use get_profile_value() instead.
    """
    return get_profile_value(key, default)


def set_config_value(key: str, value: Any) -> Path:
    """Legacy: Set config value.

    DEPRECATED: Use set_profile_value() instead.
    """
    return set_profile_value(key, value)


# =============================================================================
# XDG path helpers (unchanged)
# =============================================================================

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


def ensure_config_exists() -> bool:
    """Check if configuration exists and is valid.

    Returns:
        True if profile exists and is valid, False otherwise
    """
    try:
        profile = load_profile(require_exists=True)
        return bool(profile)
    except ProfileNotFoundError:
        return False
