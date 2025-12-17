#!/usr/bin/env python3
"""
Shared routines for syncing files from Google Drive.

Used by extract_w2.py, process_year.py, and CLI commands.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional

from paycalc.sdk import load_config as sdk_load_config, get_cache_path, get_year_cache_path


def load_config() -> dict:
    """Load configuration from SDK profile path.

    Profile is loaded from (in order):
    1. PAY_CALC_CONFIG_PATH environment variable
    2. settings.json 'profile' key (if set)
    3. ~/.config/pay-calc/profile.yaml (XDG default)
    """
    return sdk_load_config(require_exists=True)


def run_gwsa_command(args: List[str]) -> dict:
    """Run a gwsa CLI command and return JSON output."""
    cmd = ["gwsa"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gwsa command failed: {result.stderr}")
    return json.loads(result.stdout)


def get_w2_pay_records_folder_id(year: str) -> Optional[str]:
    """Get the Drive folder ID for W-2/pay records for a given year."""
    config = load_config()
    w2_records = config.get("drive", {}).get("w2_pay_records", {})
    return w2_records.get(year) or w2_records.get(str(year))


def list_drive_folder(folder_id: str) -> List[Dict[str, str]]:
    """List files in a Drive folder."""
    result = run_gwsa_command(["drive", "list", "--folder-id", folder_id])
    return result.get("items", [])


def download_drive_file(file_id: str, save_path: str) -> dict:
    """Download a file from Drive."""
    return run_gwsa_command(["drive", "download", file_id, save_path])


def sync_w2_pay_records(year: str, use_cache: bool = False, verbose: bool = True) -> Path:
    """
    Sync W-2/pay records from Drive for a given year.

    Args:
        year: The year to sync (e.g., "2024")
        use_cache: If True, use XDG cache directory; if False, use temp directory
        verbose: Print progress messages

    Returns:
        Path to the local directory containing the synced files
    """
    folder_id = get_w2_pay_records_folder_id(year)
    if not folder_id:
        raise ValueError(f"No w2_pay_records folder configured for year {year}")

    # Determine working directory (XDG cache or temp)
    if use_cache:
        work_dir = get_year_cache_path(year, "w2_pay_records")
    else:
        work_dir = Path(tempfile.mkdtemp(prefix=f"w2_pay_records_{year}_"))

    if verbose:
        print(f"Syncing W-2/pay records for {year} from Drive...")
        print(f"  Folder ID: {folder_id}")
        print(f"  Local dir: {work_dir}")

    # List files in Drive folder
    files = list_drive_folder(folder_id)
    if verbose:
        print(f"  Found {len(files)} file(s)")

    # Download each file
    for f in files:
        file_name = f["name"]
        file_id = f["id"]
        local_path = work_dir / file_name

        # Skip if already cached
        if use_cache and local_path.exists():
            if verbose:
                print(f"    Using cached: {file_name}")
            continue

        if verbose:
            print(f"    Downloading: {file_name}")
        download_drive_file(file_id, str(local_path))

    return work_dir
