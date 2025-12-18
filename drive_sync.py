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


def get_pay_records_folders() -> List[Dict[str, str]]:
    """Get all configured pay records folders.

    Returns:
        List of dicts with 'id' and optional 'comment' keys.
    """
    config = load_config()
    pay_records = config.get("drive", {}).get("pay_records", [])
    if isinstance(pay_records, list):
        return pay_records
    return []


def list_drive_folder(folder_id: str) -> List[Dict[str, str]]:
    """List files in a Drive folder."""
    result = run_gwsa_command(["drive", "list", "--folder-id", folder_id])
    return result.get("items", [])


def download_drive_file(file_id: str, save_path: str) -> dict:
    """Download a file from Drive."""
    return run_gwsa_command(["drive", "download", file_id, save_path])


def sync_pay_records(folder_id: Optional[str] = None, use_cache: bool = False, verbose: bool = True) -> Path:
    """
    Sync pay records from Drive.

    Args:
        folder_id: Specific folder ID to sync. If None, syncs ALL configured folders.
        use_cache: If True, use XDG cache directory; if False, use temp directory
        verbose: Print progress messages

    Returns:
        Path to the local directory containing the synced files
    """
    # Determine which folders to sync
    if folder_id:
        folders = [{"id": folder_id, "comment": "explicit"}]
    else:
        folders = get_pay_records_folders()
        if not folders:
            raise ValueError("No pay_records folders configured in profile")

    # Determine working directory
    if use_cache:
        work_dir = get_cache_path() / "pay_records"
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="pay_records_"))

    if verbose:
        print(f"Syncing pay records from {len(folders)} folder(s)...")
        print(f"  Local dir: {work_dir}")

    total_files = 0
    for folder in folders:
        fid = folder.get("id")
        comment = folder.get("comment", "")

        if verbose:
            print(f"\n  Folder: {comment or fid}")

        # List files in Drive folder
        files = list_drive_folder(fid)
        if verbose:
            print(f"    Found {len(files)} file(s)")

        # Download each file
        for f in files:
            file_name = f["name"]
            file_id = f["id"]
            local_path = work_dir / file_name

            # Skip if already cached
            if use_cache and local_path.exists():
                if verbose:
                    print(f"      Cached: {file_name}")
                continue

            if verbose:
                print(f"      Downloading: {file_name}")
            download_drive_file(file_id, str(local_path))
            total_files += 1

    if verbose:
        print(f"\n  Downloaded {total_files} new file(s)")

    return work_dir
