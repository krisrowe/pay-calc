"""Pay Calc SDK - Core functionality for pay and tax projections."""

from .config import (
    # New config architecture
    get_config_dir,
    get_settings_path,
    load_settings,
    save_settings,
    get_setting,
    set_setting,
    get_profile_path,
    load_profile,
    save_profile,
    get_profile_value,
    set_profile_value,
    ProfileNotFoundError,
    # Profile validation
    validate_profile,
    ProfileValidationResult,
    validate_profile_key,
    validate_folder_id,
    # Legacy (deprecated, but kept for compatibility)
    get_config_path,
    load_config,
    save_config,
    get_config_value,
    set_config_value,
    ConfigNotFoundError,
    # XDG paths
    get_cache_path,
    get_data_path,
    get_year_cache_path,
    get_year_data_path,
    ensure_config_exists,
)

from .tax import (
    generate_tax_projection,
    generate_projection,
    write_projection_csv,
    load_tax_rules,
    load_party_w2_data,
)

__all__ = [
    # New config architecture
    "get_config_dir",
    "get_settings_path",
    "load_settings",
    "save_settings",
    "get_setting",
    "set_setting",
    "get_profile_path",
    "load_profile",
    "save_profile",
    "get_profile_value",
    "set_profile_value",
    "ProfileNotFoundError",
    # Profile validation
    "validate_profile",
    "ProfileValidationResult",
    "validate_profile_key",
    "validate_folder_id",
    # Legacy (deprecated)
    "get_config_path",
    "load_config",
    "save_config",
    "get_config_value",
    "set_config_value",
    "ConfigNotFoundError",
    # XDG paths
    "get_cache_path",
    "get_data_path",
    "get_year_cache_path",
    "get_year_data_path",
    "ensure_config_exists",
    # Tax
    "generate_tax_projection",
    "generate_projection",
    "write_projection_csv",
    "load_tax_rules",
    "load_party_w2_data",
]
