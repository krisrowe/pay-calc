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


# =============================================================================
# Profile validation and health assessment
# =============================================================================

class ProfileValidationResult:
    """Result of profile validation with feature readiness status."""

    def __init__(
        self,
        location_type: str,
        location_path: Path,
        features: dict,
        profile: dict,
        errors: list = None,
        warnings: list = None,
    ):
        """
        Args:
            location_type: "central", "custom", or "legacy"
            location_path: Path to the profile file
            features: Dict of feature_name -> dict with keys:
                      ready (bool), missing (list), message (str)
            profile: The loaded profile dict
            errors: List of validation errors (invalid values)
            warnings: List of validation warnings (suspicious but allowed)
        """
        self.location_type = location_type
        self.location_path = location_path
        self.features = features
        self.profile = profile
        self.errors = errors or []
        self.warnings = warnings or []

    @property
    def all_ready(self) -> bool:
        """True if all features are ready."""
        return all(f["ready"] for f in self.features.values())

    def is_ready(self, feature: str) -> bool:
        """Check if a specific feature is ready."""
        return self.features.get(feature, {}).get("ready", False)

    def require_feature(self, feature: str) -> None:
        """Raise exception if profile has errors or feature is not ready.

        Checks in order:
        1. Schema validation errors (corrupt profile)
        2. Feature-specific missing config

        Args:
            feature: Feature name to check

        Raises:
            ConfigNotFoundError: If profile has errors or feature is not ready
        """
        # Check for schema errors first (corrupt profile)
        if self.errors:
            error_str = "\n  ! ".join(self.errors)
            raise ConfigNotFoundError(
                f"Profile has validation errors (corrupt configuration):\n\n"
                f"  ! {error_str}\n\n"
                f"Profile: {self.location_path}\n"
                f"Fix errors and retry, or use: pay-calc profile show"
            )

        if feature not in self.features:
            raise ConfigNotFoundError(f"Unknown feature: {feature}")

        status = self.features[feature]
        if not status["ready"]:
            missing_str = "\n  - ".join(status["missing"])
            raise ConfigNotFoundError(
                f"Profile not configured for '{feature}'.\n\n"
                f"Missing:\n  - {missing_str}\n\n"
                f"Profile: {self.location_path}\n"
                f"View with: pay-calc profile show"
            )


def validate_profile(profile: Optional[dict] = None) -> ProfileValidationResult:
    """Validate profile and check feature readiness.

    Args:
        profile: Optional profile dict (loads from file if not provided)

    Returns:
        ProfileValidationResult with location info and feature status

    Raises:
        ProfileNotFoundError: If no profile exists
    """
    # Determine location type
    config_dir = get_config_dir()
    settings = load_settings()
    custom_profile_setting = settings.get("profile")

    if custom_profile_setting:
        location_type = "custom"
        location_path = Path(custom_profile_setting)
    else:
        # Check which file exists
        profile_path = config_dir / PROFILE_FILENAME
        legacy_path = config_dir / LEGACY_CONFIG_FILENAME

        if profile_path.exists():
            location_type = "central"
            location_path = profile_path
        elif legacy_path.exists():
            location_type = "legacy"
            location_path = legacy_path
        else:
            location_type = "central"
            location_path = profile_path

    # Load profile if not provided
    if profile is None:
        profile = load_profile(require_exists=True)

    # Validate folder IDs in drive.* settings
    errors, warnings = _validate_drive_folder_ids(profile)

    # Validate each feature
    features = {}

    # Feature: pay_stubs (pay-analysis command)
    features["pay_stubs"] = _validate_pay_stubs(profile)

    # Feature: w2_extract (w2-extract command)
    features["w2_extract"] = _validate_w2_extract(profile)

    # Feature: tax_projection (tax-projection command)
    features["tax_projection"] = _validate_tax_projection(profile)

    # Feature: employers (employer identification)
    features["employers"] = _validate_employers(profile)

    return ProfileValidationResult(
        location_type=location_type,
        location_path=location_path,
        features=features,
        profile=profile,
        errors=errors,
        warnings=warnings,
    )


def _validate_drive_folder_ids(profile: dict) -> tuple[list, list]:
    """Validate all folder IDs in drive.* settings.

    Returns:
        Tuple of (errors, warnings) lists
    """
    errors = []
    warnings = []

    drive = profile.get("drive", {})
    if not drive:
        return errors, warnings

    def check_folder_id(key: str, value):
        """Check a single folder ID value."""
        if not isinstance(value, str):
            return
        is_valid, msg = validate_folder_id(value)
        if not is_valid:
            errors.append(f"{key}: {msg}")
        elif msg:  # Warning
            warnings.append(f"{key}: {msg}")

    # Check pay_stubs_folder_id
    if "pay_stubs_folder_id" in drive:
        check_folder_id("drive.pay_stubs_folder_id", drive["pay_stubs_folder_id"])

    # Check output_folder_id
    if "output_folder_id" in drive:
        check_folder_id("drive.output_folder_id", drive["output_folder_id"])

    # Check w2_pay_records.<year>
    w2_records = drive.get("w2_pay_records", {})
    if isinstance(w2_records, dict):
        for year, folder_id in w2_records.items():
            check_folder_id(f"drive.w2_pay_records.{year}", folder_id)

    return errors, warnings


def _get_all_employers(profile: dict) -> list:
    """Extract all employers from profile.

    Schema: parties.<party>.companies[]
    """
    employers = []

    parties = profile.get("parties", {})
    for party_name, party_data in parties.items():
        if isinstance(party_data, dict):
            companies = party_data.get("companies", [])
            for company in companies:
                emp = dict(company)
                emp.setdefault("party", party_name)
                employers.append(emp)

    return employers


def _validate_pay_stubs(profile: dict) -> dict:
    """Validate configuration for pay stub processing."""
    missing = []

    # Check drive.pay_stubs_folder_id
    drive = profile.get("drive", {})
    folder_id = drive.get("pay_stubs_folder_id")
    if not folder_id:
        missing.append("drive.pay_stubs_folder_id (Google Drive folder for pay stubs)")

    # Check employers are configured
    employers = _get_all_employers(profile)
    if not employers:
        missing.append("parties.<party>.companies[] (employer configurations)")

    if missing:
        return {
            "ready": False,
            "missing": missing,
            "message": "Pay stub processing requires Drive folder and employer config",
        }

    return {
        "ready": True,
        "missing": [],
        "message": f"Ready ({len(employers)} employer(s) configured)",
    }


def _validate_w2_extract(profile: dict) -> dict:
    """Validate configuration for W-2 extraction."""
    missing = []

    # Check drive.w2_pay_records has at least one year
    drive = profile.get("drive", {})
    w2_records = drive.get("w2_pay_records", {})
    if not w2_records:
        missing.append("drive.w2_pay_records.<year> (Drive folder IDs by year)")

    # Check parties are configured
    parties = profile.get("parties", {})
    if not parties:
        missing.append("parties (him/her party definitions)")

    if missing:
        return {
            "ready": False,
            "missing": missing,
            "message": "W-2 extraction requires Drive folders and party config",
        }

    years = list(w2_records.keys())
    return {
        "ready": True,
        "missing": [],
        "message": f"Ready (years: {', '.join(years)})",
    }


def _validate_tax_projection(profile: dict) -> dict:
    """Validate configuration for tax projection."""
    missing = []

    # Check parties are configured
    parties = profile.get("parties", {})
    if "him" not in parties and "her" not in parties:
        missing.append("parties.him and/or parties.her (party definitions)")

    if missing:
        return {
            "ready": False,
            "missing": missing,
            "message": "Tax projection requires party configuration",
        }

    party_names = [p for p in ["him", "her"] if p in parties]
    return {
        "ready": True,
        "missing": [],
        "message": f"Ready (parties: {', '.join(party_names)})",
    }


def _validate_employers(profile: dict) -> dict:
    """Validate employer configurations."""
    employers = _get_all_employers(profile)

    if not employers:
        return {
            "ready": False,
            "missing": ["parties.<party>.companies[] (employer configurations)"],
            "message": "No employers configured",
        }

    # Check each employer has required fields
    issues = []
    for i, emp in enumerate(employers):
        if not emp.get("name"):
            issues.append(f"employer[{i}].name")
        if not emp.get("party"):
            issues.append(f"employer[{i}].party")
        # Accept keywords (old schema) or content_patterns/file_patterns (new schema)
        if not emp.get("content_patterns") and not emp.get("file_patterns") and not emp.get("keywords"):
            issues.append(f"employer[{i}] needs keywords, content_patterns, or file_patterns")

    if issues:
        return {
            "ready": False,
            "missing": issues,
            "message": f"Employer config incomplete ({len(issues)} issue(s))",
        }

    return {
        "ready": True,
        "missing": [],
        "message": f"Ready ({len(employers)} employer(s))",
    }


# =============================================================================
# Profile schema validation
# =============================================================================

# Valid top-level keys and their allowed nested patterns
PROFILE_SCHEMA = {
    "drive": {
        "pay_stubs_folder_id": str,
        "w2_pay_records": dict,  # keys are years like "2024"
        "output_folder_id": str,
    },
    "parties": {
        "him": dict,  # has "companies" list
        "her": dict,  # has "companies" list
    },
    "employers": list,  # alternative to parties.<party>.companies
}


def validate_profile_key(key: str) -> tuple[bool, str]:
    """Validate that a dot-notation key is allowed by the schema.

    Args:
        key: Dot-notation key like "drive.w2_pay_records.2024"

    Returns:
        Tuple of (is_valid, error_message)
    """
    parts = key.split(".")

    if not parts:
        return False, "Empty key"

    top_level = parts[0]

    if top_level not in PROFILE_SCHEMA:
        valid_keys = ", ".join(PROFILE_SCHEMA.keys())
        return False, f"Unknown top-level key '{top_level}'. Valid keys: {valid_keys}"

    if len(parts) == 1:
        # Setting entire top-level section - not allowed via set
        return False, f"Cannot set entire '{top_level}' section. Use 'profile edit' for complex changes."

    schema_section = PROFILE_SCHEMA[top_level]

    if isinstance(schema_section, dict):
        second_level = parts[1]

        # Check if second level is valid
        if second_level not in schema_section:
            valid_keys = ", ".join(schema_section.keys())
            return False, f"Unknown key '{second_level}' under '{top_level}'. Valid keys: {valid_keys}"

        expected_type = schema_section[second_level]

        # If it's a dict type (like w2_pay_records), allow setting nested keys
        if expected_type == dict and len(parts) >= 3:
            # e.g., drive.w2_pay_records.2024 - valid
            return True, ""

        # If it's a simple type, should be exactly 2 parts
        if expected_type == str and len(parts) == 2:
            return True, ""

        if expected_type == dict and len(parts) == 2:
            return False, f"Cannot set entire '{key}' section. Specify a sub-key or use 'profile edit'."

    return False, f"Invalid key path: {key}"


def validate_folder_id(value: str) -> tuple[bool, str]:
    """Validate that a value looks like a Google Drive folder ID.

    Google Drive IDs are typically:
    - 25-45 characters long
    - Alphanumeric with hyphens and underscores (URL-safe Base64)
    - No spaces or special characters

    Reference: https://developers.google.com/drive/api/guides/folder

    Args:
        value: The value to validate

    Returns:
        Tuple of (is_valid, warning_message)
        Returns (True, "") if valid, (True, "warning...") if suspicious but allowed
    """
    import re

    # Must be string
    if not isinstance(value, str):
        return False, "Folder ID must be a string"

    # Check for obviously wrong values
    if not value or value.isspace():
        return False, "Folder ID cannot be empty"

    # Check character set (alphanumeric, hyphen, underscore only)
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        return False, f"Folder ID contains invalid characters. Expected only letters, numbers, hyphens, underscores."

    # Warn on suspicious length but allow it
    if len(value) < 15:
        return True, f"Warning: Folder ID seems short ({len(value)} chars). Employer A IDs are typically 25-45 chars."

    if len(value) > 60:
        return True, f"Warning: Folder ID seems long ({len(value)} chars). Employer A IDs are typically 25-45 chars."

    return True, ""
