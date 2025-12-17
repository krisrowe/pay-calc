"""Pay Calc SDK - Core functionality for pay and tax projections."""

from .config import (
    get_config_path,
    get_cache_path,
    get_data_path,
    get_year_cache_path,
    get_year_data_path,
    load_config,
    save_config,
    get_config_value,
    set_config_value,
    ensure_config_exists,
    ConfigNotFoundError,
)

from .tax import (
    generate_tax_projection,
    generate_projection,
    write_projection_csv,
    load_tax_rules,
    load_party_w2_data,
)

__all__ = [
    # Config
    "get_config_path",
    "get_cache_path",
    "get_data_path",
    "get_year_cache_path",
    "get_year_data_path",
    "load_config",
    "save_config",
    "get_config_value",
    "set_config_value",
    "ensure_config_exists",
    "ConfigNotFoundError",
    # Tax
    "generate_tax_projection",
    "generate_projection",
    "write_projection_csv",
    "load_tax_rules",
    "load_party_w2_data",
]
