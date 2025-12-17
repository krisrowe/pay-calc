"""Config and Profile CLI commands for Pay Calc.

Two command groups:
- config: Machine-specific settings (settings.json) - profile path, preferences
- profile: User profile data (profile.yaml) - Drive IDs, parties, employers
"""

import click
import json
import yaml

from paycalc.sdk import (
    # Settings (machine-specific)
    get_config_dir,
    get_settings_path,
    load_settings,
    save_settings,
    get_setting,
    set_setting,
    # Profile (user data)
    get_profile_path,
    load_profile,
    save_profile,
    get_profile_value,
    set_profile_value,
    ProfileNotFoundError,
    # Profile validation
    validate_profile,
    # XDG paths
    get_cache_path,
    get_data_path,
)


# =============================================================================
# CONFIG commands - machine-specific settings (settings.json)
# =============================================================================

@click.group()
def config():
    """Manage machine-specific settings (settings.json).

    Settings include:
    - profile: path to your profile.yaml
    - default_output_format: preferred output format
    - Other tool preferences

    These are ephemeral, machine-specific settings that can be
    recreated easily if lost.
    """
    pass


@config.command("path")
def config_path():
    """Show configuration paths and active profile location."""
    import os

    click.echo("Configuration paths:")
    click.echo()

    # Config directory
    env_path = os.environ.get("PAY_CALC_CONFIG_PATH")
    config_dir = get_config_dir()

    if env_path:
        click.echo(f"  Config directory: {config_dir}")
        click.echo(f"    (from PAY_CALC_CONFIG_PATH)")
    else:
        click.echo(f"  Config directory: {config_dir}")
        click.echo(f"    (XDG default)")

    # Settings file
    settings_path = get_settings_path()
    if settings_path.exists():
        click.echo(f"  Settings file:    {settings_path} [exists]")
    else:
        click.echo(f"  Settings file:    {settings_path} [not found]")

    # Profile resolution
    click.echo()
    click.echo("Profile resolution:")

    settings = load_settings()
    custom_profile = settings.get("profile")

    if custom_profile:
        from pathlib import Path
        profile_path = Path(custom_profile)
        exists = "[exists]" if profile_path.exists() else "[NOT FOUND]"
        click.echo(f"  1. settings.json 'profile': {profile_path} {exists}")
    else:
        click.echo(f"  1. settings.json 'profile': (not set)")

    default_profile = config_dir / "profile.yaml"
    legacy_profile = config_dir / "config.yaml"

    if not custom_profile:
        if default_profile.exists():
            click.echo(f"  2. Default profile: {default_profile} [ACTIVE]")
        elif legacy_profile.exists():
            click.echo(f"  2. Default profile: {default_profile} (not found)")
            click.echo(f"  3. Legacy config:   {legacy_profile} [ACTIVE - migrate recommended]")
        else:
            click.echo(f"  2. Default profile: {default_profile} (not found)")

    # Other XDG paths
    click.echo()
    click.echo("Data paths (XDG spec):")
    click.echo(f"  Cache: {get_cache_path()}")
    click.echo(f"  Data:  {get_data_path()}")


@config.command("set-profile")
@click.argument("profile_path", type=click.Path())
def config_set_profile(profile_path):
    """Set the path to your profile.yaml.

    PROFILE_PATH is the path to your profile.yaml file,
    typically in a config repo you manage separately.

    Example:
        pay-calc config set-profile ~/repos/my-config/pay-calc/profile.yaml
    """
    from pathlib import Path

    path = Path(profile_path).expanduser().resolve()

    if not path.exists():
        click.echo(f"Warning: Profile does not exist yet: {path}", err=True)
        click.echo("The path will be saved, but you'll need to create the file.", err=True)
        click.echo()

    settings_file = set_setting("profile", str(path))
    click.echo(f"Profile path set to: {path}")
    click.echo(f"Saved to: {settings_file}")


@config.command("show")
def config_show():
    """Show current machine settings (settings.json)."""
    settings_path = get_settings_path()
    settings = load_settings()

    if not settings:
        click.echo(f"# No settings configured yet")
        click.echo(f"# Settings file: {settings_path}")
        return

    click.echo(f"# Settings: {settings_path}")
    click.echo()
    click.echo(json.dumps(settings, indent=2))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a machine setting value.

    KEY is a setting name (e.g., 'default_output_format')
    VALUE is the value to set

    For setting the profile path, use 'config set-profile' instead.
    """
    if key == "profile":
        raise click.ClickException("Use 'pay-calc config set-profile <path>' to set profile path")

    # Try to parse as number or boolean
    if value.lower() == "true":
        parsed_value = True
    elif value.lower() == "false":
        parsed_value = False
    else:
        try:
            if "." in value:
                parsed_value = float(value)
            else:
                parsed_value = int(value)
        except ValueError:
            parsed_value = value

    settings_file = set_setting(key, parsed_value)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to: {settings_file}")


@config.command("get")
@click.argument("key")
def config_get(key):
    """Get a machine setting value."""
    value = get_setting(key)
    if value is None:
        raise click.ClickException(f"Setting '{key}' not found")

    if isinstance(value, (dict, list)):
        click.echo(json.dumps(value, indent=2))
    else:
        click.echo(value)


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
@click.option("--no-contents", is_flag=True, help="Show only location and health, not profile contents")
def profile_show(no_contents):
    """Show the active profile, its location, and feature readiness."""
    try:
        validation = validate_profile()

        # Location info
        location_label = {
            "central": "central (default)",
            "custom": "custom",
            "legacy": "legacy (migrate recommended)",
        }.get(validation.location_type, validation.location_type)

        click.echo(f"Profile: {validation.location_path}")
        click.echo(f"Location: {location_label}")
        click.echo()

        # Feature readiness
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

        # Profile contents
        if not no_contents:
            click.echo()
            click.echo("---")
            click.echo(yaml.dump(validation.profile, default_flow_style=False, sort_keys=False))

    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))


@profile.command("get")
@click.argument("key")
def profile_get(key):
    """Get a profile configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'
    """
    value = get_profile_value(key)
    if value is None:
        raise click.ClickException(f"Key '{key}' not found in profile")

    if isinstance(value, (dict, list)):
        click.echo(yaml.dump(value, default_flow_style=False))
    else:
        click.echo(value)


@profile.command("set")
@click.argument("key")
@click.argument("value")
def profile_set(key, value):
    """Set a profile configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'
    VALUE is the value to set (strings, numbers supported)

    Examples:
        pay-calc profile set drive.pay_stubs_folder_id YOUR_FOLDER_ID
        pay-calc profile set drive.w2_pay_records.2024 FOLDER_ID
    """
    # Try to parse as number
    try:
        if "." in value:
            parsed_value = float(value)
        else:
            parsed_value = int(value)
    except ValueError:
        parsed_value = value

    profile_file = set_profile_value(key, parsed_value)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to: {profile_file}")


@profile.command("init")
@click.option("--path", "profile_path", type=click.Path(), help="Create profile at custom path")
def profile_init(profile_path):
    """Initialize a new profile configuration.

    Creates a profile.yaml with default structure.
    Use --path to create in a custom location (e.g., your config repo).

    Examples:
        pay-calc profile init
        pay-calc profile init --path ~/repos/my-config/pay-calc/profile.yaml
    """
    from pathlib import Path

    if profile_path:
        target_path = Path(profile_path).expanduser().resolve()
    else:
        target_path = get_config_dir() / "profile.yaml"

    if target_path.exists():
        raise click.ClickException(f"Profile already exists at {target_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)

    default_profile = {
        "drive": {
            "pay_stubs_folder_id": "",
            "w2_pay_records": {},
            "output_folder_id": "",
        },
        "parties": {
            "him": {
                "companies": [],
            },
            "her": {
                "companies": [],
            },
        },
    }

    with open(target_path, "w") as f:
        yaml.dump(default_profile, f, default_flow_style=False, sort_keys=False)

    click.echo(f"Created profile at: {target_path}")

    # If custom path, offer to set it in settings
    if profile_path:
        click.echo()
        click.echo("To use this profile, run:")
        click.echo(f"  pay-calc config set-profile {target_path}")
    else:
        click.echo()
        click.echo("Next steps:")
        click.echo("  pay-calc profile set drive.pay_stubs_folder_id YOUR_FOLDER_ID")
        click.echo("  pay-calc profile set drive.w2_pay_records.2024 FOLDER_ID")


@profile.command("edit")
def profile_edit():
    """Open profile configuration in default editor."""
    import os
    import subprocess

    try:
        prof_path = get_profile_path(require_exists=True)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(prof_path)], check=True)
    except FileNotFoundError:
        raise click.ClickException(f"Editor '{editor}' not found. Set EDITOR env var.")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Editor exited with code {e.returncode}")


@profile.command("path")
def profile_path_cmd():
    """Show the path to the active profile."""
    try:
        prof_path = get_profile_path(require_exists=True)
        click.echo(prof_path)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))


@profile.command("import")
@click.argument("source_path", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Overwrite existing central profile")
def profile_import(source_path, force):
    """Import a profile from an external location into central config.

    Copies SOURCE_PATH to ~/.config/pay-calc/profile.yaml and clears
    any custom profile path in settings.json so the central location is used.

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

    # Validate source is YAML
    if source.suffix not in (".yaml", ".yml"):
        raise click.ClickException(f"Source must be a YAML file: {source}")

    # Check target
    if target.exists() and not force:
        raise click.ClickException(
            f"Central profile already exists: {target}\n"
            f"Use --force to overwrite, or 'profile export' to back it up first."
        )

    # Validate source is valid YAML
    try:
        with open(source, "r") as f:
            yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise click.ClickException(f"Invalid YAML in source file: {e}")

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
