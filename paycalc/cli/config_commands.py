"""Config CLI commands for Pay Calc."""

import click
import yaml

from paycalc.sdk import (
    get_config_path,
    get_cache_path,
    get_data_path,
    load_config,
    save_config,
    get_config_value,
    set_config_value,
    ConfigNotFoundError,
)


@click.group()
def config():
    """Manage pay-calc configuration."""
    pass


@config.command("path")
def config_path():
    """Show the active configuration path.

    Displays which configuration directory is being used based on
    the resolution order: PAY_CALC_CONFIG_PATH env → ./pay-calc/ → ~/.config/pay-calc/
    """
    import os

    click.echo("Configuration path resolution:")

    # Check env var
    env_path = os.environ.get("PAY_CALC_CONFIG_PATH")
    if env_path:
        click.echo(f"  1. PAY_CALC_CONFIG_PATH: {env_path} [ACTIVE]")
    else:
        click.echo("  1. PAY_CALC_CONFIG_PATH: (not set)")

    # Check local
    from pathlib import Path
    local_path = Path.cwd() / "pay-calc" / "config.yaml"
    if not env_path and local_path.exists():
        click.echo(f"  2. Local ./pay-calc/: {local_path.parent} [ACTIVE]")
    else:
        exists = "exists" if local_path.exists() else "not found"
        click.echo(f"  2. Local ./pay-calc/: {local_path.parent} ({exists})")

    # XDG path
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    xdg_path = Path(xdg_config_home) / "pay-calc"
    xdg_config_file = xdg_path / "config.yaml"

    if not env_path and not local_path.exists():
        status = "[ACTIVE]" if xdg_config_file.exists() else "(not found)"
        click.echo(f"  3. XDG config: {xdg_path} {status}")
    else:
        exists = "exists" if xdg_config_file.exists() else "not found"
        click.echo(f"  3. XDG config: {xdg_path} ({exists})")

    click.echo()
    click.echo("Other paths (XDG spec):")
    click.echo(f"  Cache: {get_cache_path()}")
    click.echo(f"  Data:  {get_data_path()}")


@config.command("show")
def config_show():
    """Show current configuration."""
    try:
        cfg = load_config(require_exists=True)
        config_path_val = get_config_path(require_exists=True)
        click.echo(f"# Config from: {config_path_val / 'config.yaml'}")
        click.echo(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    except ConfigNotFoundError as e:
        raise click.ClickException(str(e))


@config.command("get")
@click.argument("key")
def config_get(key):
    """Get a configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'
    """
    value = get_config_value(key)
    if value is None:
        raise click.ClickException(f"Key '{key}' not found in configuration")

    if isinstance(value, (dict, list)):
        click.echo(yaml.dump(value, default_flow_style=False))
    else:
        click.echo(value)


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a configuration value.

    KEY is a dot-notation path like 'drive.w2_pay_records.2024'
    VALUE is the value to set (strings, numbers supported)
    """
    # Try to parse as number
    try:
        if "." in value:
            parsed_value = float(value)
        else:
            parsed_value = int(value)
    except ValueError:
        parsed_value = value

    config_file = set_config_value(key, parsed_value)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to: {config_file}")


@config.command("init")
@click.option("--local", is_flag=True, help="Create config in ./pay-calc/ instead of ~/.config/pay-calc/")
def config_init(local):
    """Initialize a new configuration file.

    Creates a config.yaml with default structure. Use --local to create
    in the current directory's ./pay-calc/ folder.
    """
    from pathlib import Path
    import os

    if local:
        config_dir = Path.cwd() / "pay-calc"
    else:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        config_dir = Path(xdg_config_home) / "pay-calc"

    config_file = config_dir / "config.yaml"

    if config_file.exists():
        raise click.ClickException(f"Config already exists at {config_file}")

    config_dir.mkdir(parents=True, exist_ok=True)

    default_config = {
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

    with open(config_file, "w") as f:
        yaml.dump(default_config, f, default_flow_style=False, sort_keys=False)

    click.echo(f"Created configuration at: {config_file}")
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Edit the config file to add your Drive folder IDs")
    click.echo("  2. Add company keywords for W-2 identification")
    click.echo()
    click.echo("Or use 'pay-calc config set' commands:")
    click.echo("  pay-calc config set drive.pay_stubs_folder_id YOUR_FOLDER_ID")
    click.echo("  pay-calc config set drive.w2_pay_records.2024 YOUR_FOLDER_ID")


@config.command("edit")
def config_edit():
    """Open configuration file in default editor."""
    import os
    import subprocess

    try:
        config_file = get_config_path(require_exists=True) / "config.yaml"
    except ConfigNotFoundError as e:
        raise click.ClickException(str(e))

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_file)], check=True)
    except FileNotFoundError:
        raise click.ClickException(f"Editor '{editor}' not found. Set EDITOR env var.")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"Editor exited with code {e.returncode}")
