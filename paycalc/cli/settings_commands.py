"""Settings CLI commands for Pay Calc.

Manages settings.json - data directory, preferences, paths.
"""

import click
from pathlib import Path

from paycalc.sdk import (
    load_settings,
    save_settings,
    get_setting,
    set_setting,
    get_settings_path,
    get_data_path,
)


@click.group()
def settings():
    """Manage settings (settings.json).

    Available settings:
    - data_dir: custom data directory path
    - profile: path to profile.yaml (set via 'profile use')
    """
    pass


@settings.command("show")
def settings_show():
    """Show current settings and their values."""
    settings_path = get_settings_path()
    current = load_settings()

    click.echo(f"Settings file: {settings_path}")
    click.echo(f"File exists: {settings_path.exists()}")
    click.echo()

    if not current:
        click.echo("No settings configured (using defaults).")
        click.echo()
        click.echo("Effective paths:")
        click.echo(f"  data_dir: {get_data_path()} (default)")
        return

    click.echo("Current settings:")
    for key, value in current.items():
        click.echo(f"  {key}: {value}")

    click.echo()
    click.echo("Effective paths:")
    click.echo(f"  data_dir: {get_data_path()}")


@settings.command("data-dir")
@click.argument("path", required=False, type=click.Path())
@click.option("--clear", is_flag=True, help="Clear custom data_dir, revert to default")
def settings_data_dir(path, clear):
    """Set or clear the custom data directory.

    PATH is the directory where pay-calc stores data (records, analysis output).

    Examples:
        pay-calc settings data-dir ~/ws/personal-agent/pay-calc/data
        pay-calc settings data-dir --clear
    """
    if clear:
        current = load_settings()
        if "data_dir" in current:
            del current["data_dir"]
            save_settings(current)
            click.echo("Cleared data_dir setting.")
            click.echo(f"Data directory is now: {get_data_path()} (default)")
        else:
            click.echo("data_dir was not set.")
        return

    if not path:
        # Show current value
        current_data_dir = get_setting("data_dir")
        if current_data_dir:
            click.echo(f"Current data_dir: {current_data_dir}")
        else:
            click.echo(f"No custom data_dir set. Using default: {get_data_path()}")
        return

    # Validate and set the path
    data_path = Path(path).expanduser().resolve()

    # Check if path exists or can be created
    if data_path.exists():
        if not data_path.is_dir():
            raise click.ClickException(f"Path exists but is not a directory: {data_path}")
    else:
        # Try to create it
        try:
            data_path.mkdir(parents=True, exist_ok=True)
            click.echo(f"Created directory: {data_path}")
        except OSError as e:
            raise click.ClickException(f"Cannot create directory: {data_path}\n{e}")

    # Check it's writable
    test_file = data_path / ".write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError as e:
        raise click.ClickException(f"Directory is not writable: {data_path}\n{e}")

    # Save the setting
    set_setting("data_dir", str(data_path))
    click.echo(f"Set data_dir: {data_path}")
    click.echo(f"Saved to: {get_settings_path()}")
