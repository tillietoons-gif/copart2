"""Tests for database operations.

These tests use temporary SQLite databases to verify CRUD operations,
export functionality, and duplicate prevention without affecting
the production database.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from copart_automation.app.database import DatabaseModule
from copart_automation.app.models import SearchQuery, Vehicle




class TestDatabaseOperations:
    """Verify database module behavior with temporary files."""

    def test_database_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            assert db_path.exists()

    def test_database_initialization_creates_export_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            DatabaseModule(db_path=db_path)
            conn = sqlite3.connect(str(db_path))
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "auction_lot_exports" in tables
                assert "auction_lot_rows" in tables
                assert "lot_scrape_status" in tables
                vehicle_cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(vehicles)").fetchall()
                }
                assert "auction_calendar_id" in vehicle_cols
                assert "source_auction_url" in vehicle_cols
                assert "source_csv_path" in vehicle_cols
            finally:
                conn.close()

    def test_insert_and_retrieve_vehicle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            vehicle = Vehicle(
                vin="TESTVIN1234567890",
                lot_number="TESTLOT001",
                year=2020,
                make="Toyota",
                model="Camry",
            )
            vehicle_id = db.insert_vehicle(vehicle)
            assert vehicle_id > 0

            retrieved = db.get_vehicle_by_lot("TESTLOT001")
            assert retrieved is not None
            assert retrieved.vin == "TESTVIN1234567890"

    def test_duplicate_prevention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            vehicle = Vehicle(
                vin="TESTVIN1234567890",
                lot_number="TESTLOT002",
            )
            id1 = db.insert_vehicle(vehicle)
            id2 = db.insert_vehicle(vehicle)
            assert id1 == id2  # Should return existing record, not create new

    def test_duplicate_lot_with_different_vin_returns_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            id1 = db.insert_vehicle(Vehicle(vin="TESTVIN1234567890", lot_number="TESTLOT003"))
            id2 = db.insert_vehicle(Vehicle(vin="DIFFVIN1234567890", lot_number="TESTLOT003"))
            assert id1 == id2

    def test_insert_auction_lot_export_and_raw_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            export_id = db.insert_auction_lot_export(
                source_url="https://www.copart.com/saleListResult/23/2026-07-24",
                csv_file_path=Path(tmp) / "LotSearchresults.csv",
                row_count=1,
                imported_vehicle_count=1,
            )
            row_id = db.insert_auction_lot_row(
                export_id,
                {"lot number": "12345678", "vin": "TESTVIN1234567890", "extra": "value"},
                lot_number="12345678",
                vin="TESTVIN1234567890",
            )

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT lot_number, vin, row_json FROM auction_lot_rows WHERE id = ?",
                    (row_id,),
                ).fetchone()
                assert row is not None
                assert row[0] == "12345678"
                assert row[1] == "TESTVIN1234567890"
                assert json.loads(row[2])["extra"] == "value"
            finally:
                conn.close()

    def test_insert_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = DatabaseModule(db_path=db_path)
            query = SearchQuery(query_type="vin", query_value="TEST", result_count=1)
            query_id = db.insert_search(query)
            assert query_id > 0

    def test_export_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            export_path = Path(tmp) / "export.csv"
            db = DatabaseModule(db_path=db_path)
            vehicle = Vehicle(vin="TESTVIN1234567890", lot_number="EXPORT001")
            db.insert_vehicle(vehicle)
            result_path = db.export_to_csv(export_path)
            assert result_path.exists()

    def test_export_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            export_path = Path(tmp) / "export.json"
            db = DatabaseModule(db_path=db_path)
            vehicle = Vehicle(vin="TESTVIN1234567890", lot_number="EXPORT002")
            db.insert_vehicle(vehicle)
            result_path = db.export_to_json(export_path)
            assert result_path.exists()
