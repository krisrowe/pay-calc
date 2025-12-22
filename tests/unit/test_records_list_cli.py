"""Tests for records list CLI command.

Tests the --count option and related functionality.
"""

import json
import pytest
from pathlib import Path
from click.testing import CliRunner

from paycalc.cli.records_commands import records_cli


def make_stub_record(record_id: str, year: str, party: str, pay_date: str, employer: str,
                     earnings: list = None):
    """Create a stub record for testing.

    Args:
        earnings: Optional list of earning dicts with 'type' and 'current_amount'.
                  Defaults to a single Regular Pay earning.
    """
    if earnings is None:
        earnings = [{"type": "Regular Pay", "current_amount": 1000.00}]

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
            "earnings": earnings,
        }
    }


def make_w2_record(record_id: str, year: str, party: str, employer: str, wages: float):
    """Create a W-2 record for testing."""
    return {
        "id": record_id,
        "meta": {
            "type": "w2",
            "year": year,
            "party": party,
        },
        "data": {
            "tax_year": int(year),
            "employer_name": employer,
            "wages": wages,
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

    def test_count_with_type_filter(self, isolated_records):
        """--count with --type filter counts only matching record types."""
        records_dir = isolated_records["records_dir"]

        # Create mix of stubs and W-2s
        records_data = [
            make_stub_record("stub0001", "2020", "him", "2020-01-15", "Acme Corp"),
            make_stub_record("stub0002", "2020", "him", "2020-02-15", "Acme Corp"),
            make_w2_record("w2_00001", "2020", "him", "Acme Corp", 50000.00),
            make_stub_record("stub0003", "2020", "her", "2020-03-15", "Other Inc"),
            make_w2_record("w2_00002", "2020", "her", "Other Inc", 45000.00),
        ]

        for rec in records_data:
            (records_dir / f"{rec['id']}.json").write_text(json.dumps(rec))

        runner = CliRunner()

        # Count stubs only
        result = runner.invoke(records_cli, ["list", "--type", "stub", "--count"])
        assert result.exit_code == 0
        assert result.output.strip() == "3"

        # Count W-2s only
        result = runner.invoke(records_cli, ["list", "--type", "w2", "--count"])
        assert result.exit_code == 0
        assert result.output.strip() == "2"

    def test_count_with_year_and_data_filter_combined(self, isolated_records):
        """--count with year filter (meta) and --data-filter (content) combined."""
        records_dir = isolated_records["records_dir"]

        # Create stubs across years with different earning types
        records_data = [
            # 2020 records
            make_stub_record("stub0001", "2020", "him", "2020-01-15", "Acme Corp",
                             earnings=[{"type": "Regular Pay", "current_amount": 5000.00}]),
            make_stub_record("stub0002", "2020", "him", "2020-02-15", "Acme Corp",
                             earnings=[{"type": "Bonus", "current_amount": 2000.00}]),
            # 2021 records
            make_stub_record("stub0003", "2021", "him", "2021-01-15", "Acme Corp",
                             earnings=[{"type": "Bonus", "current_amount": 3000.00}]),
            make_stub_record("stub0004", "2021", "him", "2021-02-15", "Acme Corp",
                             earnings=[{"type": "Bonus", "current_amount": 1500.00}]),
            make_stub_record("stub0005", "2021", "him", "2021-03-15", "Acme Corp",
                             earnings=[{"type": "Regular Pay", "current_amount": 5000.00}]),
        ]

        for rec in records_data:
            (records_dir / f"{rec['id']}.json").write_text(json.dumps(rec))

        runner = CliRunner()

        # Count 2021 Bonus stubs only (year filter + data filter)
        result = runner.invoke(records_cli, [
            "list", "2021", "--data-filter", '$.earnings[?type=="Bonus"]', "--count"
        ])
        assert result.exit_code == 0
        assert result.output.strip() == "2"  # stub0003 and stub0004

        # Count 2020 Bonus stubs
        result = runner.invoke(records_cli, [
            "list", "2020", "--data-filter", '$.earnings[?type=="Bonus"]', "--count"
        ])
        assert result.exit_code == 0
        assert result.output.strip() == "1"  # only stub0002
