"""Tests for records import efficiency and deduplication.

Tests the two-tier dedup strategy:
- Folder imports: file-level tracking skips already-processed files (no download)
- Targeted imports: bypasses file-level check, uses stub-level dedup

Mock strategy:
- PAY_CALC_CONFIG_PATH env var points to temp config dir
- settings.json in config dir has data_dir pointing to temp data dir
- Mock subprocess calls to gwsa (Drive list/download)
- Mock "PDF" files contain JSON stub data directly
- Mock OCR just reads the JSON from the file
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def make_stub_data(pay_date: str, employer: str, gross: float, medicare_taxable: float):
    """Create stub data structure matching current schema."""
    return {
        "employer": employer,
        "pay_date": pay_date,
        "period": {
            "start": pay_date,
            "end": pay_date,
        },
        "document_id": None,
        "net_pay": gross * 0.7,
        "earnings": [{"type": "Regular Pay", "current_amount": gross, "ytd_amount": gross}],
        "taxes": {
            "federal_income_tax": {
                "taxable_wages": gross,
                "current_withheld": gross * 0.15,
                "ytd_withheld": gross * 0.15,
            },
            "social_security": {
                "taxable_wages": gross,
                "current_withheld": gross * 0.062,
                "ytd_withheld": gross * 0.062,
            },
            "medicare": {
                "taxable_wages": medicare_taxable,
                "current_withheld": gross * 0.0145,
                "ytd_withheld": gross * 0.0145,
            },
        },
        "deductions": [],
        "pay_summary": {
            "current": {
                "gross": gross,
                "fit_taxable_wages": gross,
                "taxes": gross * 0.22,
                "deductions": 0,
                "net_pay": gross * 0.7,
            },
            "ytd": {
                "gross": gross,
                "fit_taxable_wages": gross,
                "taxes": gross * 0.22,
                "deductions": 0,
                "net_pay": gross * 0.7,
            },
        },
    }


# Stub data for each mock file
FILE_STUB_DATA = {
    "file_a_single": [make_stub_data("2025-01-15", "Acme Corp", 5000, 5000)],
    "file_b_multi3": [
        make_stub_data("2025-01-31", "Acme Corp", 5000, 5100),
        make_stub_data("2025-02-14", "Acme Corp", 5000, 5200),
        make_stub_data("2025-02-28", "Acme Corp", 5000, 5300),
    ],
    "file_b_multi4": [
        make_stub_data("2025-01-31", "Acme Corp", 5000, 5100),
        make_stub_data("2025-02-14", "Acme Corp", 5000, 5200),
        make_stub_data("2025-02-28", "Acme Corp", 5000, 5300),
        make_stub_data("2025-03-14", "Acme Corp", 5000, 5400),  # NEW stub
    ],
    "file_c_dupe": [make_stub_data("2025-01-15", "Acme Corp", 5000, 5000)],  # Same as file_a
    "file_d_new": [make_stub_data("2025-02-15", "Acme Corp", 5500, 5500)],
    # Not recognized - not a pay stub or W-2 (missing required fields)
    "file_not_stub": [{"title": "Random PDF", "content": "Some text that is not a pay stub"}],
    # Unknown party - valid stub but employer doesn't match any profile keywords
    "file_unknown_party": [make_stub_data("2025-01-20", "Unknown Corp XYZ", 6000, 6000)],
}

# Realistic Drive IDs (must be >20 chars for is_drive_folder_id)
FOLDER_ID = "1ABCDEFGHIJKLMNOPQRSTUVWXYZabc"  # 30 chars
FILE_A_ID = "1file_a_single_ABCDEFGHIJKLMNO"
FILE_B_ID = "1file_b_multi3_ABCDEFGHIJKLMNO"
FILE_C_ID = "1file_c_dupe_ABCDEFGHIJKLMNOPQ"
FILE_D_ID = "1file_d_new_ABCDEFGHIJKLMNOPQR"
FILE_NOT_STUB_ID = "1file_not_stub_ABCDEFGHIJKLM"
FILE_UNKNOWN_PARTY_ID = "1file_unknown_party_ABCDEFGH"
FILE_E_DUP_CONTENT_ID = "1file_e_dup_content_ABCDEFGH"  # New file with duplicate stub content

# Map file IDs to their stub data keys
FILE_ID_TO_DATA = {
    FILE_A_ID: "file_a_single",
    FILE_B_ID: "file_b_multi3",
    FILE_C_ID: "file_c_dupe",
    FILE_D_ID: "file_d_new",
    FILE_NOT_STUB_ID: "file_not_stub",
    FILE_UNKNOWN_PARTY_ID: "file_unknown_party",
    FILE_E_DUP_CONTENT_ID: "file_c_dupe",  # Same stub data as file_a (duplicate content)
}

# Mock Drive folder listings
DRIVE_FILES_INITIAL = [
    {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
    {"id": FILE_B_ID, "name": "2025_Q1_stubs.pdf"},
    {"id": FILE_C_ID, "name": "2025-01-15_duplicate.pdf"},
]

DRIVE_FILES_WITH_NEW = DRIVE_FILES_INITIAL + [
    {"id": FILE_D_ID, "name": "2025-02-15_stub.pdf"},
]

# Folder with unrecognized file (not a pay stub or W-2)
DRIVE_FILES_WITH_NOT_STUB = [
    {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
    {"id": FILE_NOT_STUB_ID, "name": "random_document.pdf"},
]

# Folder with unknown party file (valid stub but employer not in profile)
DRIVE_FILES_WITH_UNKNOWN_PARTY = [
    {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
    {"id": FILE_UNKNOWN_PARTY_ID, "name": "2025-01-20_unknown_employer.pdf"},
]

# Folder with new file that has duplicate stub content (different file ID, same stub)
DRIVE_FILES_WITH_DUP_CONTENT = [
    {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
    {"id": FILE_E_DUP_CONTENT_ID, "name": "2025-01-15_another_file.pdf"},  # Same stub as file_a
]


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Set up isolated config and data directories."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    monkeypatch.setenv("PAY_CALC_CONFIG_PATH", str(config_dir))

    settings = {"data_dir": str(data_dir)}
    (config_dir / "settings.json").write_text(json.dumps(settings))

    # Profile must be YAML (profile.yaml)
    # Format: parties.<name>.companies[].keywords[]
    import yaml
    profile = {
        "parties": {
            "him": {
                "companies": [
                    {"name": "Acme Corp", "keywords": ["Acme", "Acme Corp"]}
                ]
            }
        },
        "drive": {"pay_records": [{"id": FOLDER_ID, "comment": "test"}]},
    }
    (config_dir / "profile.yaml").write_text(yaml.dump(profile))

    return {"config_dir": config_dir, "data_dir": data_dir}


class DriveMock:
    """Mock for Drive operations. Downloads write JSON stub data to files."""

    def __init__(self):
        self.download_calls = []
        self.folder_contents = {}  # folder_id -> list of file dicts
        self.file_data = {}  # file_id -> stub data key override
        self.file_names = {}  # file_id -> filename (for gwsa drive get)

    def set_folder(self, folder_id: str, files: list):
        """Set contents of a mock Drive folder."""
        self.folder_contents[folder_id] = files
        # Also populate file_names map
        for f in files:
            self.file_names[f["id"]] = f["name"]

    def set_file_data(self, file_id: str, data_key: str):
        """Map a file ID to its stub data key (for simulating updates)."""
        self.file_data[file_id] = data_key

    def subprocess_run(self, cmd, **kwargs):
        """Mock subprocess.run for gwsa drive commands."""
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if len(cmd) >= 3 and cmd[0] == "gwsa" and cmd[1] == "drive":
            if cmd[2] == "list":
                # Command: gwsa drive list --folder-id <folder_id>
                folder_id = cmd[4] if len(cmd) > 4 else None
                files = self.folder_contents.get(folder_id, [])
                # Real gwsa returns {"items": [...]}
                result.stdout = json.dumps({"items": files})

            elif cmd[2] == "get":
                # Command: gwsa drive get <file_id>
                file_id = cmd[3]
                filename = self.file_names.get(file_id, f"{file_id}.pdf")
                result.stdout = json.dumps({"id": file_id, "name": filename})

            elif cmd[2] == "download":
                # Command: gwsa drive download <file_id> <local_path>
                file_id = cmd[3]
                local_path = Path(cmd[4])
                self.download_calls.append(file_id)

                # Get stub data for this file:
                # 1. Check override map (for simulating updates)
                # 2. Check FILE_ID_TO_DATA (file ID -> data key)
                # 3. Fall back to file_id directly (legacy)
                data_key = self.file_data.get(file_id) or FILE_ID_TO_DATA.get(file_id, file_id)
                stubs = FILE_STUB_DATA.get(data_key, [])

                # Write stub data as JSON to the "PDF" file
                content = {"stubs": stubs, "page_count": len(stubs)}
                local_path.write_text(json.dumps(content))

        return result

    def reset(self):
        """Reset download tracking."""
        self.download_calls.clear()


def mock_get_pdf_page_count(pdf_path):
    """Read page count from mock PDF (which is really JSON)."""
    try:
        content = json.loads(Path(pdf_path).read_text())
        return content.get("page_count", 1)
    except (json.JSONDecodeError, FileNotFoundError):
        return 1


def mock_split_pdf_pages(pdf_path, output_dir):
    """Split mock PDF into individual page files.

    For multi-page mock PDFs, creates one file per page with that page's stub data.
    Returns list of page file paths in order.
    """
    try:
        content = json.loads(Path(pdf_path).read_text())
        stubs = content.get("stubs", [])
        page_count = content.get("page_count", 1)

        if page_count <= 1:
            return []  # No split needed for single-page

        page_files = []
        for i, stub in enumerate(stubs):
            page_file = output_dir / f"page_{i+1}.pdf"
            # Each page file contains just that one stub
            page_content = {"stubs": [stub], "page_count": 1}
            page_file.write_text(json.dumps(page_content))
            page_files.append(page_file)

        return page_files
    except (json.JSONDecodeError, FileNotFoundError):
        return []


class MockOCR:
    """Mock Gemini OCR that tracks page index per file."""

    def __init__(self):
        self._page_idx = {}

    def process_file(self, prompt, file_path):
        """Read stub data from mock PDF (which is really JSON)."""
        content = json.loads(Path(file_path).read_text())
        stubs = content.get("stubs", [])

        # Track page index per file path
        file_key = str(file_path)
        idx = self._page_idx.get(file_key, 0)
        if idx < len(stubs):
            result = stubs[idx]
            self._page_idx[file_key] = idx + 1
            return result
        return stubs[0] if stubs else {}

    def reset(self):
        """Reset page tracking."""
        self._page_idx.clear()


class TestImportEfficiency:
    """Test import skips downloads for already-processed files."""

    def test_folder_import_only_downloads_new_files(self, isolated_config):
        """Scenario 1: Folder with new file - only new file downloaded.

        Setup: 3 PDFs imported (1 single + 3 multi + 1 dupe = 4 unique stubs)
        Action: 4th PDF added to folder, run folder import again
        Expected: All previously processed files skipped, only new file downloaded
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, DRIVE_FILES_INITIAL)
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import of 3 files
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 4, f"Expected 4 imported, got {stats1}"
            assert stats1["skipped"] == 1, f"Expected 1 skipped (dupe), got {stats1}"
            assert len(drive.download_calls) == 3

            # Reset and add new file to folder
            drive.reset()
            ocr.reset()
            drive.set_folder(FOLDER_ID, DRIVE_FILES_WITH_NEW)

            # Second import - all 3 original files skipped (tracked), only file_d downloaded
            stats2 = records.import_from_folder_auto(FOLDER_ID)
            assert len(drive.download_calls) == 1, \
                f"Expected 1 download (only file_d), got {drive.download_calls}"
            assert FILE_D_ID in drive.download_calls
            assert stats2["imported"] == 1  # Only file_d's stub is new
            # Skipped = 5 (file_a: 1 + file_b: 3 + file_c: 1 tracking record)
            assert stats2["skipped"] == 5

    def test_folder_import_skips_imported_files(self, isolated_config):
        """Scenario 2a: Folder re-import skips files with imported records.

        Setup: 2 PDFs imported (file_a: 1 stub, file_b: 3 stubs)
        Action: Run folder import again
        Expected: Both files skipped (have imported records), NO downloads
        """
        from paycalc.sdk import records

        drive = DriveMock()
        # Only file_a and file_b (no file_c which has duplicate stubs)
        drive.set_folder(FOLDER_ID, [
            {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
            {"id": FILE_B_ID, "name": "2025_Q1_stubs.pdf"},
        ])
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 4  # 1 + 3 stubs
            assert len(drive.download_calls) == 2

            # Reset and re-import
            drive.reset()
            ocr.reset()

            # Second import - both files skipped (have imported records)
            stats2 = records.import_from_folder_auto(FOLDER_ID)
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads (all imported), got {drive.download_calls}"
            assert stats2["imported"] == 0
            assert stats2["skipped"] == 4  # file_a: 1 + file_b: 3

    def test_folder_import_skips_tracking_files(self, isolated_config):
        """Scenario 2b: Folder re-import skips files with tracking records (all-dupe stubs).

        Setup: file_a imported, file_c downloaded but all stubs duplicate (tracking record)
        Action: Run folder import again
        Expected: file_c skipped via tracking record, NOT re-downloaded
        """
        from paycalc.sdk import records

        drive = DriveMock()
        # file_a imports, file_c's stub is duplicate of file_a
        drive.set_folder(FOLDER_ID, [
            {"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"},
            {"id": FILE_C_ID, "name": "2025-01-15_duplicate.pdf"},
        ])
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import: file_a imports, file_c's stub skipped as duplicate
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 1  # Only file_a's stub
            assert stats1["skipped"] == 1   # file_c's stub is duplicate
            assert len(drive.download_calls) == 2  # Both downloaded

            # Reset and re-import
            drive.reset()
            ocr.reset()

            # Second import - file_c skipped via tracking (not re-downloaded)
            stats2 = records.import_from_folder_auto(FOLDER_ID)
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads, got {drive.download_calls}"
            assert stats2["imported"] == 0
            assert stats2["skipped"] == 2  # file_a: 1 imported + file_c: 1 tracking

    def test_folder_import_skips_mixed_imported_and_tracking(self, isolated_config):
        """Scenario 2c: Folder re-import skips both imported and tracking files.

        Setup: 3 PDFs - file_a/b imported, file_c has tracking record (all-dupe stubs)
        Action: Run folder import again
        Expected: All files skipped, NO downloads

        This verifies both skip paths work together.
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, DRIVE_FILES_INITIAL)
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 4  # file_a: 1 + file_b: 3
            assert stats1["skipped"] == 1   # file_c's stub is duplicate

            # Reset and re-import
            drive.reset()
            ocr.reset()

            # Second import - ALL files skipped
            stats2 = records.import_from_folder_auto(FOLDER_ID)
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads, got {drive.download_calls}"
            assert stats2["imported"] == 0
            # Skipped = 5 (file_a: 1 + file_b: 3 imported + file_c: 1 tracking)
            assert stats2["skipped"] == 5

    def test_targeted_file_import_reprocesses(self, isolated_config):
        """Scenario 3: Targeted file ID import bypasses file-level dedup.

        Setup: 3 PDFs imported (one multi-page with 3 stubs)
        Action: Multi-page PDF updated to 4 stubs, import by file ID directly
        Expected: Bypasses file-level dedup, downloads, processes all 4 pages,
                  stub-level dedup finds 3 dupes, imports only the new 4th stub

        This is the recovery workflow for re-importing a specific file.
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, DRIVE_FILES_INITIAL)
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import via folder
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 4

            # Reset and "update" multi-page PDF
            drive.reset()
            ocr.reset()
            drive.set_file_data(FILE_B_ID, "file_b_multi4")  # Now has 4 stubs

            # Targeted import by file ID - should reprocess the file
            stats2 = records.import_from_drive_file(FILE_B_ID)

            # Should have downloaded (bypassed file-level dedup)
            assert len(drive.download_calls) == 1
            assert drive.download_calls[0] == FILE_B_ID

            # Should import only the new 4th stub (stub-level dedup)
            assert stats2["imported"] == 1, \
                f"Expected 1 imported (new stub), got {stats2}"
            assert stats2["skipped"] == 3, \
                f"Expected 3 skipped (stub-level dupes), got {stats2}"

    def test_unrelated_file_download_discarded_and_later_skipped(self, isolated_config):
        """Scenario 2a: Non-pay PDF is downloaded once, then skipped on re-import.

        Setup: Folder contains a PDF that is NOT a pay stub or W-2
        Action: Import folder twice
        Expected:
        - First import: downloaded, marked as "unrelated" (type), discarded=1
        - Second import: NOT downloaded, skipped with "unrelated" reason (info, not warning)

        This tests that irrelevant PDFs are:
        1. Downloaded once (we must download to determine content type)
        2. Marked as unrelated (not an error, just not pay-related)
        3. Never re-downloaded on subsequent imports
        4. No warning emitted (unrelated is normal, not actionable)
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, [{"id": FILE_NOT_STUB_ID, "name": "random_document.pdf"}])
        ocr = MockOCR()

        events = []
        def capture_callback(event, data):
            events.append({"event": event, "data": data})

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # === FIRST IMPORT ===
            stats1 = records.import_from_folder_auto(FOLDER_ID, callback=capture_callback)

            # File WAS downloaded (must download to determine content type)
            assert len(drive.download_calls) == 1
            assert FILE_NOT_STUB_ID in drive.download_calls

            # File was discarded (not a stub or W-2)
            assert stats1["imported"] == 0
            assert stats1["discarded"] == 1

            # Verify discard event has "not_recognized" reason
            discard_events = [e for e in events if e["event"] == "discarded"]
            assert len(discard_events) == 1
            assert "not_recognized" in discard_events[0]["data"].get("reason", "")

            # === SECOND IMPORT ===
            drive.reset()
            ocr.reset()
            events.clear()

            stats2 = records.import_from_folder_auto(FOLDER_ID, callback=capture_callback)

            # KEY: No downloads on second import
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads, got {drive.download_calls}"

            # File was skipped (not re-processed)
            assert stats2["imported"] == 0
            assert stats2["discarded"] == 0
            assert stats2["skipped"] == 1

            # Skip reason is "unrelated" (info, not warning)
            skip_events = [e for e in events if e["event"] == "skipped"]
            assert len(skip_events) == 1
            assert skip_events[0]["data"].get("reason") == "unrelated"

            # NO warning emitted (unrelated is normal, not actionable)
            warning_events = [e for e in events if e["event"] == "warning"]
            assert len(warning_events) == 0, \
                f"Unrelated files should not emit warnings, got: {warning_events}"

    def test_folder_import_warns_about_unknown_party_files(self, isolated_config):
        """Scenario 2b: Folder import warns about unknown_party discards EVERY time.

        Setup: Import folder with 1 valid stub + 1 stub with unknown employer
        Action: First import discards, second import should WARN again (no download)
        Expected:
        - First import: downloaded, discarded with "unknown_party" reason
        - Second import: NOT downloaded, but emits WARNING with guidance
        - Warning must guide user: update profile.yaml, then `records import file <id>`

        Unlike "not_recognized" files (which can be silently skipped), unknown_party
        files represent actionable issues the user can fix. We warn EVERY time.
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, DRIVE_FILES_WITH_UNKNOWN_PARTY)
        ocr = MockOCR()

        # Capture emitted events
        events = []
        def capture_callback(event, data):
            events.append({"event": event, "data": data})

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # First import - valid stub imports, unknown_party file discarded
            stats1 = records.import_from_folder_auto(FOLDER_ID, callback=capture_callback)
            assert stats1["imported"] == 1, f"Expected 1 imported, got {stats1}"
            assert stats1["discarded"] == 1, f"Expected 1 discarded, got {stats1}"
            assert len(drive.download_calls) == 2, "First import should download both files"

            # Verify the discard event has unknown_party reason
            discard_events = [e for e in events if e["event"] == "discarded"]
            assert len(discard_events) == 1
            assert "unknown_party" in discard_events[0]["data"].get("reason", "")
            discarded_file_id = discard_events[0]["data"].get("file_id")

            # Reset and re-import
            drive.reset()
            ocr.reset()
            events.clear()

            # Second import - unknown_party file NOT downloaded but WARNS
            stats2 = records.import_from_folder_auto(FOLDER_ID, callback=capture_callback)
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads on re-import, got {drive.download_calls}"
            assert stats2["skipped"] == 2  # 1 imported + 1 discarded

            # KEY ASSERTION: Warning event emitted for unknown_party file
            warning_events = [e for e in events if e["event"] == "warning"]
            assert len(warning_events) >= 1, \
                f"Expected warning event for unknown_party file, got events: {events}"

            # Warning must include actionable guidance
            warning_data = warning_events[0]["data"]
            warning_msg = warning_data.get("message", "")

            # Must mention the issue type
            assert "unknown" in warning_msg.lower(), \
                f"Warning should mention 'unknown' party/employer, got: {warning_msg}"

            # Must guide user to update profile config
            assert "profile" in warning_msg.lower(), \
                f"Warning should mention updating profile config, got: {warning_msg}"

            # Must guide user to use `records import file <id>` to retry
            assert "import file" in warning_msg.lower() or discarded_file_id in warning_msg, \
                f"Warning should mention 'import file <id>' to retry, got: {warning_msg}"

    def test_new_file_dupe_stubs_downloads_skips(self, isolated_config):
        """Scenario 4: New file with duplicate stub content is downloaded but stubs skipped.

        Setup: Import file_a (contains one stub: 2025-01-15, Acme Corp)
        Action: New file_e added with SAME stub content (different file ID)
        Expected:
        - First import: file_a imports successfully
        - Second import: file_e downloads (new file ID), stub skipped (duplicate content)

        This tests stub-level duplicate detection across different source files.
        Medicare taxable wages is the primary duplicate detector.
        """
        from paycalc.sdk import records

        drive = DriveMock()
        ocr = MockOCR()

        # First: import just file_a
        drive.set_folder(FOLDER_ID, [{"id": FILE_A_ID, "name": "2025-01-15_stub.pdf"}])

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Import file_a
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 1, f"Expected 1 imported, got {stats1}"
            assert len(drive.download_calls) == 1
            assert FILE_A_ID in drive.download_calls

            # Reset and add file_e with duplicate stub content
            drive.reset()
            ocr.reset()
            drive.set_folder(FOLDER_ID, DRIVE_FILES_WITH_DUP_CONTENT)

            # Import again - file_a skipped (has records), file_e downloaded
            stats2 = records.import_from_folder_auto(FOLDER_ID)

            # file_a should be skipped at file-level (already imported)
            # file_e should be downloaded (new file ID) but stub skipped (duplicate content)
            assert FILE_E_DUP_CONTENT_ID in drive.download_calls, \
                f"Expected file_e to be downloaded, got {drive.download_calls}"
            assert len(drive.download_calls) == 1  # Only file_e downloaded
            assert stats2["imported"] == 0  # file_e's stub is duplicate
            assert stats2["skipped"] == 2  # file_a (file-level) + file_e stub (stub-level)

    def test_all_dupe_stubs_file_not_redownloaded(self, isolated_config):
        """Files with all-duplicate stubs should NOT be re-downloaded.

        Setup: Import file_a, then file_e with duplicate stub content
        Action: Run folder import again (third time)
        Expected:
        - First import: file_a imports, file_e downloads but all stubs are duplicates
        - Second import: NEITHER file is downloaded (both have tracking records)

        A file whose stubs are all duplicates should still have a tracking record
        saved so we don't waste bandwidth re-downloading it on subsequent imports.
        """
        from paycalc.sdk import records

        drive = DriveMock()
        ocr = MockOCR()

        # Setup: file_a + file_e (with duplicate content)
        drive.set_folder(FOLDER_ID, DRIVE_FILES_WITH_DUP_CONTENT)

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # First import: file_a imports, file_e's stub skipped as duplicate
            stats1 = records.import_from_folder_auto(FOLDER_ID)
            assert stats1["imported"] == 1
            assert stats1["skipped"] == 1  # file_e's stub is duplicate
            assert len(drive.download_calls) == 2

            # Reset and import again
            drive.reset()
            ocr.reset()

            # Second import: NEITHER file should be downloaded
            stats2 = records.import_from_folder_auto(FOLDER_ID)

            # KEY ASSERTION: No downloads - both files have tracking records
            assert len(drive.download_calls) == 0, \
                f"Expected 0 downloads, got {drive.download_calls}"
            assert stats2["imported"] == 0
            assert stats2["skipped"] == 2  # Both files skipped at file-level

    def test_targeted_reimport_overwrites_existing_record(self, isolated_config):
        """Targeted reimport should overwrite existing record, not skip.

        Setup: Import file with gross=5000
        Action: Reimport same file with gross=6000
        Assert before: 1 record with gross=5000
        Assert after: 1 record with gross=6000

        Fails if: skip (content unchanged) OR duplicate (2 records)
        Passes only if: overwrite (1 record, new content)
        """
        from paycalc.sdk import records

        drive = DriveMock()
        drive.set_folder(FOLDER_ID, [{"id": FILE_A_ID, "name": "stub.pdf"}])
        ocr = MockOCR()

        with patch("subprocess.run", drive.subprocess_run), \
             patch("paycalc.sdk.records._get_pdf_page_count", mock_get_pdf_page_count), \
             patch("paycalc.sdk.records._split_pdf_pages", mock_split_pdf_pages), \
             patch("paycalc.gemini_client.process_file", ocr.process_file):

            # Initial import - gross=5000
            records.import_from_folder_auto(FOLDER_ID)

            # Assert before: 1 record with gross=5000
            recs_before = records.find_all_by_drive_id(FILE_A_ID)
            assert len(recs_before) == 1, f"Expected 1 record, got {len(recs_before)}"
            gross_before = recs_before[0]["data"]["pay_summary"]["current"]["gross"]
            assert gross_before == 5000, f"Expected gross=5000, got {gross_before}"

            # Change file content to gross=6000
            drive.reset()
            ocr.reset()
            FILE_STUB_DATA["file_a_updated"] = [make_stub_data("2025-01-15", "Acme Corp", 6000, 6000)]
            drive.set_file_data(FILE_A_ID, "file_a_updated")

            # Targeted reimport
            records.import_from_drive_file(FILE_A_ID)

            # Assert after: 1 record with gross=6000
            recs_after = records.find_all_by_drive_id(FILE_A_ID)
            assert len(recs_after) == 1, \
                f"Expected 1 record (overwrite), got {len(recs_after)} (duplicate created)"
            gross_after = recs_after[0]["data"]["pay_summary"]["current"]["gross"]
            assert gross_after == 6000, \
                f"Expected gross=6000 (overwritten), got {gross_after} (skipped or wrong content)"
