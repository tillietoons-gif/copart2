"""SQLite database module for persisting automation data.

Design decisions:
- SQLite is chosen for portability and zero-config setup.
- All database operations use parameterized queries to prevent SQL injection.
- The database module uses the Vehicle model's `to_database_dict()`
  and `from_database_row()` methods to maintain type safety.
- Duplicate prevention uses unique constraints on (vin, lot_number)
  rather than relying solely on Python-side checks, ensuring data
  integrity even if multiple processes access the database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from copart_automation.app.config import settings
from copart_automation.app.exceptions import CopartAutomationError
from copart_automation.app.logger import get_logger
from copart_automation.app.models import AuctionCalendarEntry, DownloadRecord, SearchQuery, Vehicle

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "sql" / "schema.sql"


class DatabaseModule:
    """Manages SQLite connections, schema initialization, and CRUD operations.

    The database file is configured through the application settings.
    Schema initialization runs automatically if tables are missing.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.database_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema from SQL file if not present."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            if SCHEMA_PATH.exists():
                schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
                conn.executescript(schema_sql)
                conn.commit()
            else:
                # Minimal fallback schema creation
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vehicles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        vin TEXT NOT NULL,
                        lot_number TEXT NOT NULL UNIQUE,
                        title_text TEXT,
                        year INTEGER,
                        make TEXT,
                        model TEXT,
                        odometer INTEGER,
                        damage_description TEXT,
                        sale_date TEXT,
                        current_bid REAL,
                        auction_status TEXT,
                        detail_url TEXT,
                        image_urls TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(vin, lot_number)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_vin ON vehicles(vin)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS searches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query_type TEXT NOT NULL,
                        query_value TEXT NOT NULL,
                        result_count INTEGER DEFAULT 0,
                        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        vehicle_id INTEGER NOT NULL,
                        file_path TEXT NOT NULL,
                        file_type TEXT,
                        download_url TEXT,
                        downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        file_size_bytes INTEGER,
                        FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
                        UNIQUE(vehicle_id, file_path)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS auction_calendar (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_date TEXT NOT NULL,
                        auction_time TEXT,
                        description TEXT,
                        table_section TEXT,
                        row_index INTEGER,
                        column_index INTEGER,
                        lots_view_url TEXT,
                        lots_view_text TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(event_date, auction_time, description)
                    )
                """)
                conn.commit()

            # Migration: ensure new columns exist for existing DBs
            try:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(auction_calendar)").fetchall()]
                if "lots_view_url" not in cols:
                    conn.execute("ALTER TABLE auction_calendar ADD COLUMN lots_view_url TEXT")
                    logger.info("Migrated auction_calendar: added lots_view_url")
                if "lots_view_text" not in cols:
                    conn.execute("ALTER TABLE auction_calendar ADD COLUMN lots_view_text TEXT")
                    logger.info("Migrated auction_calendar: added lots_view_text")
                conn.commit()
                # Ensure index for new column
                conn.execute("CREATE INDEX IF NOT EXISTS idx_auction_calendar_lots_url ON auction_calendar(lots_view_url)")
                conn.commit()
            except Exception as exc:
                logger.warning(f"Auction calendar migration check failed: {exc}")

            logger.info("Database schema initialized at {}", self.db_path)
        finally:
            conn.close()

    def insert_vehicle(self, vehicle: Vehicle) -> int:
        """Insert or update a vehicle, avoiding duplicates.

        Args:
            vehicle: The Vehicle instance to persist.

        Returns:
            The database ID of the inserted or existing record.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            data = vehicle.to_database_dict()
            # Remove id if present; SQLite will generate it
            data.pop("id", None)
            # Try insert; catch unique constraint violation and return existing
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO vehicles (
                        vin, lot_number, title_text, year, make, model,
                        odometer, damage_description, sale_date, current_bid,
                        auction_status, detail_url, image_urls, created_at, updated_at
                    ) VALUES (
                        :vin, :lot_number, :title_text, :year, :make, :model,
                        :odometer, :damage_description, :sale_date, :current_bid,
                        :auction_status, :detail_url, :image_urls, :created_at, :updated_at
                    )
                    """,
                    data,
                )
                conn.commit()
                vehicle_id = cursor.lastrowid
                logger.info("Inserted vehicle {} (id={})", vehicle.lot_number, vehicle_id)
                return vehicle_id or 0
            except sqlite3.IntegrityError:
                # Duplicate: retrieve existing ID
                row = conn.execute(
                    "SELECT id FROM vehicles WHERE vin = ? AND lot_number = ?",
                    (vehicle.vin, vehicle.lot_number),
                ).fetchone()
                conn.rollback()
                if row:
                    logger.info("Vehicle {} already exists (id={}); skipped insert.", vehicle.lot_number, row[0])
                    return row[0]
                else:
                    raise CopartAutomationError("Duplicate key conflict but existing record not found.")
        finally:
            conn.close()

    def get_vehicle_by_lot(self, lot_number: str) -> Vehicle | None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM vehicles WHERE lot_number = ?", (lot_number,)
            ).fetchone()
            if row:
                return Vehicle.from_database_row(dict(row))
            return None
        finally:
            conn.close()

    def insert_search(self, query: SearchQuery) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """
                INSERT INTO searches (query_type, query_value, result_count, executed_at)
                VALUES (?, ?, ?, ?)
                """,
                (query.query_type, query.query_value, query.result_count, query.executed_at.isoformat()),
            )
            conn.commit()
            return cursor.lastrowid or 0
        finally:
            conn.close()

    def insert_download(self, record: DownloadRecord) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO downloads (vehicle_id, file_path, file_type, download_url, downloaded_at, file_size_bytes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.vehicle_id,
                    record.file_path,
                    record.file_type,
                    record.download_url,
                    record.downloaded_at.isoformat() if record.downloaded_at else None,
                    record.file_size_bytes,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def insert_auction_calendar_entry(self, entry: 'AuctionCalendarEntry') -> int:
        conn = self._connect()
        try:
            data = entry.to_database_dict()
            normalized_event_date = data["event_date"].strip()
            normalized_time = (data.get("auction_time") or "").strip().lower()
            normalized_description = (data.get("description") or "").strip().lower()

            existing = conn.execute(
                """
                SELECT id FROM auction_calendar
                WHERE event_date = ?
                  AND LOWER(COALESCE(auction_time, '')) = ?
                  AND LOWER(COALESCE(description, '')) = ?
                """,
                (normalized_event_date, normalized_time, normalized_description),
            ).fetchone()
            if existing:
                row_id = existing[0]
                logger.info("Auction calendar entry already exists (id={}); skipped insert.", row_id)
                return row_id

            cursor = conn.execute(
                """
                INSERT INTO auction_calendar (
                    event_date, auction_time, description, table_section,
                    row_index, column_index, lots_view_url, lots_view_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_date,
                    data.get("auction_time"),
                    data.get("description"),
                    data.get("table_section"),
                    data.get("row_index"),
                    data.get("column_index"),
                    str(data.get("lots_view_url")) if data.get("lots_view_url") else None,
                    data.get("lots_view_text"),
                    data.get("created_at"),
                    data.get("updated_at"),
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0
        finally:
            conn.close()

    def get_all_vehicles(self) -> list[Vehicle]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM vehicles ORDER BY updated_at DESC").fetchall()
            return [Vehicle.from_database_row(dict(row)) for row in rows]
        finally:
            conn.close()

    def get_vehicles_by_make_model(self, make: str, model: str | None = None) -> list[Vehicle]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            if model:
                rows = conn.execute(
                    "SELECT * FROM vehicles WHERE LOWER(make) = LOWER(?) AND LOWER(COALESCE(model,'')) = LOWER(COALESCE(?,''))",
                    (make, model),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM vehicles WHERE LOWER(make) = LOWER(?)", (make,)
                ).fetchall()
            return [Vehicle.from_database_row(dict(row)) for row in rows]
        finally:
            conn.close()

    def export_to_csv(self, output_path: Path) -> Path:
        conn = sqlite3.connect(str(self.db_path))
        try:
            df = pd.read_sql_query("SELECT * FROM vehicles", conn)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False)
            logger.info("Exported {} vehicles to CSV: {}", len(df), output_path)
            return output_path.resolve()
        finally:
            conn.close()

    def export_to_excel(self, output_path: Path) -> Path:
        conn = sqlite3.connect(str(self.db_path))
        try:
            df = pd.read_sql_query("SELECT * FROM vehicles", conn)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_excel(output_path, index=False, engine="openpyxl")
            logger.info("Exported {} vehicles to Excel: {}", len(df), output_path)
            return output_path.resolve()
        finally:
            conn.close()

    def export_to_json(self, output_path: Path) -> Path:
        import json

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM vehicles").fetchall()
            data = [Vehicle.from_database_row(dict(row)).model_dump(mode="json") for row in rows]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.info("Exported {} vehicles to JSON: {}", len(data), output_path)
            return output_path.resolve()
        finally:
            conn.close()

    def close(self) -> None:
        """No-op for SQLite; connections are per-operation."""
        pass
