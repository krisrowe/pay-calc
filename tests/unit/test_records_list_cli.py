"""Tests for records list CLI command.

Tests the --count option and related functionality.
"""

import json
import pytest
from pathlib import Path
from click.testing import CliRunner

from paycalc.cli.records_commands import records_cli


def make_stub_record(record_id: str, year: str, party: str, pay_date: str, employer: str):
    """Create a minimal stub record for testing."""
    return {
        "id": record_id,
        "meta": {
            "type": "stub",
            "year": year,
            "party": party,
        },
        "data": {
            "pay_date": pay_date,
            "employer": employer,
            "net_pay": 1000.00,
        }
    }


@pytest.fixture
def isolated_records(tmp_path, monkeypatch):
    """Set up isolated records directory with test data."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    records_dir = data_dir / "records"

    config_dir.mkdir()
    data_dir.mkdir()
    records_dir.mkdir()

    monkeypatch.setenv("PAY_CALC_CONFIG_PATH", str(config_dir))

    settings = {"data_dir": str(data_dir)}
    (config_dir / "settings.json").write_text(json.dumps(settings))

    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "records_dir": records_dir,
    }


class TestRecordsListCount:
    """Tests for records list --count option."""

    def test_count_returns_zero_for_year_with_no_data(self, isolated_records):
        """--count returns 0 when filtering on a year with no records."""
        runner = CliRunner()

        # No records exist at all
        result = runner.invoke(records_cli, ["list", "1999", "--count"])

        assert result.exit_code == 0
        assert result.output.strip() == "0"

    def test_count_returns_one_for_year_with_single_record(self, isolated_records):
        """--count returns 1 when filtering on a year with exactly one record."""
        records_dir = isolated_records["records_dir"]

        # Create one record for 2020
        record = make_stub_record("abc12345", "2020", "him", "2020-06-15", "Acme Corp")
        (records_dir / "abc12345.json").write_text(json.dumps(record))

        runner = CliRunner()
        result = runner.invoke(records_cli, ["list", "2020", "--count"])

        assert result.exit_code == 0
        assert result.output.strip() == "1"

    def test_count_returns_multiple_without_filter(self, isolated_records):
        """--count returns total count when no year filter applied."""
        records_dir = isolated_records["records_dir"]

        # Create records across different years
        records_data = [
            make_stub_record("rec00001", "2020", "him", "2020-01-15", "Acme Corp"),
            make_stub_record("rec00002", "2020", "her", "2020-02-15", "Other Inc"),
            make_stub_record("rec00003", "2021", "him", "2021-03-15", "Acme Corp"),
            make_stub_record("rec00004", "2022", "him", "2022-04-15", "Acme Corp"),
            make_stub_record("rec00005", "2022", "her", "2022-05-15", "Other Inc"),
        ]

        for rec in records_data:
            (records_dir / f"{rec['id']}.json").write_text(json.dumps(rec))

        runner = CliRunner()
        result = runner.invoke(records_cli, ["list", "--count"])

        assert result.exit_code == 0
        assert result.output.strip() == "5"
