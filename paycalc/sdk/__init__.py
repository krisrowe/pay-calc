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
    format_data_sources,
    load_form_1040,
    reconcile_tax_return,
)

from .gaps import (
    Gap,
    GapAnalysis,
    detect_gaps,
    check_first_stub_ytd,
    MAX_INTERVAL_DAYS,
)

from .w2 import (
    generate_w2,
    generate_w2_with_projection,
    save_w2_forms,
    stub_to_w2,
    validate_stub_for_w2,
    validate_w2_tolerance,
    StubValidationResult,
    W2ToleranceError,
    SS_WAGE_BASE,
)

from .income_projection import (
    generate_projection as generate_income_projection_from_stubs,
    generate_income_projection,
    parse_pay_date,
    detect_employer_segments,
)

from . import records

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
    "format_data_sources",
    "load_form_1040",
    "reconcile_tax_return",
    # Gap detection
    "Gap",
    "GapAnalysis",
    "detect_gaps",
    "check_first_stub_ytd",
    "MAX_INTERVAL_DAYS",
    # W-2 generation
    "generate_w2",
    "generate_w2_with_projection",
    "save_w2_forms",
    "stub_to_w2",
    "validate_stub_for_w2",
    "validate_w2_tolerance",
    "StubValidationResult",
    "W2ToleranceError",
    "SS_WAGE_BASE",
    # Income projection
    "generate_income_projection_from_stubs",
    "generate_income_projection",
    "parse_pay_date",
    "detect_employer_segments",
    # Records module
    "records",
]
