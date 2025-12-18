"""Profile CLI commands for Pay Calc.

Manages user profile data (profile.yaml) - Drive IDs, parties, employers.
"""

import click
import yaml

from paycalc.sdk import (
    # Settings (for profile use command)
    get_config_dir,
    load_settings,
    save_settings,
    set_setting,
    # Profile (user data)
    get_profile_path,
    load_profile,
    get_profile_value,
    set_profile_value,
    ProfileNotFoundError,
    # Profile validation
    validate_profile,
    validate_profile_key,
    validate_folder_id,
)


def _validate_profile_file(path):
    """Validate a profile file at the given path.

    Args:
        path: Path to profile.yaml file

    Returns:
        Tuple of (profile_dict, validation_result) if valid

    Raises:
        click.ClickException: If file is invalid YAML or fails schema validation
    """
    from pathlib import Path

    path = Path(path)

    # Check file exists
    if not path.exists():
        raise click.ClickException(f"Profile file not found: {path}")

    # Check file extension
    if path.suffix not in (".yaml", ".yml"):
        raise click.ClickException(f"Profile must be a YAML file: {path}")

    # Load and validate YAML syntax
    try:
        with open(path, "r") as f:
            profile_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise click.ClickException(f"Invalid YAML in {path}: {e}")

    if not isinstance(profile_data, dict):
        raise click.ClickException(f"Profile must be a YAML dictionary, got {type(profile_data).__name__}")

    if not profile_data:
        raise click.ClickException(f"Profile is empty: {path}")

    # Validate schema using SDK validation
    try:
        validation = validate_profile(profile=profile_data)
    except Exception as e:
        raise click.ClickException(f"Profile validation failed: {e}")

    # Check for validation errors (uses shared display, raises if errors)
    if validation.errors:
        # Override location_path to show the file being validated, not active profile
        validation.location_path = path
        _display_validation(validation, show_contents=False, raise_on_errors=True)

    return profile_data, validation


def _display_validation(validation, show_contents=True, raise_on_errors=False):
    """Display validation results consistently across commands.

    Args:
        validation: ProfileValidationResult from validate_profile()
        show_contents: Whether to show full profile YAML
        raise_on_errors: If True, raise ClickException for validation errors

    Returns:
        True if valid (no errors), False if has errors
    """
    has_errors = bool(validation.errors)

    # Show errors first if any
    if validation.errors:
        click.echo()
        click.echo("Validation Errors (profile is invalid):")
        for error in validation.errors:
            click.echo(f"  ! {error}")
        click.echo()
        click.echo(f"Profile path: {validation.location_path}")

        if raise_on_errors:
            raise click.ClickException("Profile has validation errors. Fix them before continuing.")

    click.echo()
    click.echo("Feature Readiness:")
    for feature, status in validation.features.items():
        icon = "+" if status["ready"] else "-"
        click.echo(f"  {icon} {feature}: {status['message']}")

    # Show missing items if any
    all_missing = []
    for feature, status in validation.features.items():
        if status["missing"]:
            all_missing.extend(status["missing"])

    if all_missing:
        click.echo()
        click.echo("Missing configuration:")
        for item in sorted(set(all_missing)):
            click.echo(f"  - {item}")

    # Show warnings if any
    if validation.warnings:
        click.echo()
        click.echo("Warnings:")
        for warning in validation.warnings:
            click.echo(f"  - {warning}")

    if show_contents:
        click.echo()
        click.echo("---")
        click.echo(yaml.dump(validation.profile, default_flow_style=False, sort_keys=False))

    return not has_errors


# =============================================================================
# PROFILE commands - user profile data (profile.yaml)
# =============================================================================

@click.group()
def profile():
    """Manage your profile configuration (profile.yaml).

    Profile contains your personal/private data:
    - drive: Google Drive folder IDs
    - parties: him/her definitions with employer keywords

    This data is consequential - store it in a config repo
    you control for backup and portability.
    """
    pass


@profile.command("show")
def profile_show():
    """Show the active profile, its location, and feature readiness."""
    # Get profile path (doesn't require it to exist)
    profile_path = get_profile_path(require_exists=False)

    # Determine location type
    settings = load_settings()
    custom_profile_setting = settings.get("profile")
    if custom_profile_setting:
        location_type = "custom"
    elif profile_path.exists():
        location_type = "central"
    else:
        location_type = "none"

    location_label = {
        "central": "central (default)",
        "custom": "custom",
        "none": "not created",
    }.get(location_type, location_type)

    click.echo(f"Profile: {profile_path}")
    click.echo(f"Location: {location_label}")

    if not profile_path.exists():
        click.echo()
        click.echo("Profile does not exist yet. Create with:")
        click.echo("  pay-calc profile set drive.pay_stubs_folder_id YOUR_FOLDER_ID")
        return

    # Profile exists - show validation
    try:
        validation = validate_profile()
        _display_validation(validation, show_contents=True)

    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))


@profile.command("get")
@click.argument("key")
def profile_get(key):
    """Get a profile configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'

    For scalar values, outputs the plain value.
    For complex values (dicts/lists), use 'profile show' instead.
    """
    value = get_profile_value(key)
    if value is None:
        raise click.ClickException(f"Key '{key}' not found in profile")

    if isinstance(value, (dict, list)):
        raise click.ClickException(
            f"Key '{key}' is a complex value. Use 'pay-calc profile show' to view."
        )

    click.echo(value)


@profile.command("set")
@click.argument("key")
@click.argument("value")
def profile_set(key, value):
    """Set a profile configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'
    VALUE is the value to set (string or number)

    Examples:
        pay-calc profile set drive.pay_stubs_folder_id YOUR_FOLDER_ID
        pay-calc profile set drive.w2_pay_records.2026 FOLDER_ID
    """
    # Validate key against schema
    is_valid, error_msg = validate_profile_key(key)
    if not is_valid:
        raise click.ClickException(error_msg)

    # Pre-validate folder IDs for drive.* keys (fail fast before writing)
    if key.startswith("drive."):
        is_valid, msg = validate_folder_id(value)
        if not is_valid:
            raise click.ClickException(msg)

    # Try to parse as number (but not for folder IDs which are strings)
    parsed_value = value
    if not key.startswith("drive."):
        try:
            if "." in value:
                parsed_value = float(value)
            else:
                parsed_value = int(value)
        except ValueError:
            pass

    profile_file = set_profile_value(key, parsed_value)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to: {profile_file}")

    # Show full validation after setting
    try:
        validation = validate_profile()
        _display_validation(validation, show_contents=False)
    except ProfileNotFoundError:
        pass  # Profile was just created, validation will work next time


@profile.command("import")
@click.argument("source_path", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Overwrite existing central profile")
def profile_import(source_path, force):
    """Import a profile from an external location into central config.

    Copies SOURCE_PATH to ~/.config/pay-calc/profile.yaml and clears
    any custom profile path in settings.json so the central location is used.

    The profile is validated before being imported.

    Use this when you want to consolidate a profile from a config repo
    into the standard XDG location.

    Examples:
        pay-calc profile import ~/repos/my-config/pay-calc/profile.yaml
        pay-calc profile import ./profile.yaml --force
    """
    from pathlib import Path
    import shutil

    source = Path(source_path).expanduser().resolve()
    target = get_config_dir() / "profile.yaml"

    # Validate source profile before any changes (raises on errors)
    profile_data, validation = _validate_profile_file(source)

    # Check target
    if target.exists() and not force:
        raise click.ClickException(
            f"Central profile already exists: {target}\n"
            f"Use --force to overwrite, or 'profile export' to back it up first."
        )

    # Copy file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    click.echo(f"Copied: {source}")
    click.echo(f"    to: {target}")

    # Clear custom profile path if set
    settings = load_settings()
    if settings.get("profile"):
        old_path = settings["profile"]
        del settings["profile"]
        save_settings(settings)
        click.echo()
        click.echo(f"Cleared custom profile path from settings.json")
        click.echo(f"  (was: {old_path})")

    click.echo()
    click.echo(f"Active profile is now: {target}")

    # Show full validation
    _display_validation(validation, show_contents=False)


@profile.command("use")
@click.argument("profile_path", type=click.Path(exists=True))
def profile_use(profile_path):
    """Set the active profile to an external file.

    PROFILE_PATH is the path to a profile.yaml file, typically in a
    config repo you manage separately.

    This updates settings.json to point to the external profile, so
    pay-calc will use that file for all configuration.

    The profile is validated before being set as active.

    Examples:
        pay-calc profile use ~/repos/my-config/pay-calc/profile.yaml
        pay-calc profile use ../personal-agent/pay-calc/profile.yaml
    """
    from pathlib import Path

    path = Path(profile_path).expanduser().resolve()

    # Validate profile before switching (raises on errors)
    profile_data, validation = _validate_profile_file(path)

    # Set the profile path
    settings_file = set_setting("profile", str(path))
    click.echo(f"Active profile set to: {path}")
    click.echo(f"Saved to: {settings_file}")

    # Show full validation
    _display_validation(validation, show_contents=False)


@profile.command("export")
@click.argument("dest_path", type=click.Path())
@click.option("--force", is_flag=True, help="Overwrite existing destination file")
@click.option("--set-path", is_flag=True, help="Also set this as the active profile path")
def profile_export(dest_path, force, set_path):
    """Export the active profile to an external location.

    Copies the currently active profile.yaml to DEST_PATH.
    Use this to back up your profile or move it to a config repo.

    With --set-path, also updates settings.json to point to the
    new location, so pay-calc will use the exported copy going forward.

    Examples:
        pay-calc profile export ~/repos/my-config/pay-calc/profile.yaml
        pay-calc profile export ./backup-profile.yaml
        pay-calc profile export ~/config-repo/profile.yaml --set-path
    """
    from pathlib import Path
    import shutil

    dest = Path(dest_path).expanduser().resolve()

    # Get current profile
    try:
        source = get_profile_path(require_exists=True)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))

    # Check if source and dest are the same
    if source.resolve() == dest.resolve():
        raise click.ClickException(f"Source and destination are the same file: {source}")

    # Check destination
    if dest.exists() and not force:
        raise click.ClickException(
            f"Destination already exists: {dest}\n"
            f"Use --force to overwrite."
        )

    # Create parent directories
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Copy file
    shutil.copy2(source, dest)
    click.echo(f"Exported: {source}")
    click.echo(f"      to: {dest}")

    # Optionally set as active profile
    if set_path:
        set_setting("profile", str(dest))
        click.echo()
        click.echo(f"Updated settings.json to use: {dest}")
