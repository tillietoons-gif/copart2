"""Tests for parsing helpers and utilities.

These tests verify that the VehicleParser's cleaning and extraction
methods behave correctly without requiring a real browser session.
"""

from __future__ import annotations

import pytest

from copart_automation.app.parser import VehicleParser


class TestParserHelpers:
    """Verify static parser utility methods."""

    def test_clean_vin_valid(self) -> None:
        assert VehicleParser._clean_vin("5GZCZ43D13S812715") == "5GZCZ43D13S812715"
        assert VehicleParser._clean_vin("VIN: 5GZCZ43D13S812715") == "5GZCZ43D13S812715"

    def test_clean_vin_invalid(self) -> None:
        assert VehicleParser._clean_vin("NOTAVIN") is None
        assert VehicleParser._clean_vin("") is None

    def test_clean_lot_valid(self) -> None:
        assert VehicleParser._clean_lot("12345678") == "12345678"
        assert VehicleParser._clean_lot("Lot: 12345678") == "12345678"

    def test_clean_int_valid(self) -> None:
        assert VehicleParser._clean_int("2020") == 2020
        assert VehicleParser._clean_int("Mileage: 45000 miles") == 45000
        assert VehicleParser._clean_int("Odometer: 12,345 mi") == 12345

    def test_clean_float_valid(self) -> None:
        assert VehicleParser._clean_float("$2,500.00") == 2500.0
        assert VehicleParser._clean_float("1500") == 1500.0

    def test_find_text(self) -> None:
        from bs4 import BeautifulSoup
        html = '<div><span class="vin">TESTVIN1234567890</span></div>'
        soup = BeautifulSoup(html, "lxml")
        result = VehicleParser._find_text(soup, [".vin"])
        assert result == "TESTVIN1234567890"

    def test_find_text_missing(self) -> None:
        from bs4 import BeautifulSoup
        html = '<div></div>'
        soup = BeautifulSoup(html, "lxml")
        result = VehicleParser._find_text(soup, [".missing"])
        assert result is None
