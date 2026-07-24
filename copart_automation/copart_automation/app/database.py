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

import hashlib
import json
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
        """Initialize database schema and run lightweight migrations."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            if SCHEMA_PATH.exists():
                schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
                conn.executescript(schema_sql)
            else:
                logger.warning("Schema file {} not found; creating fallback schema.", SCHEMA_PATH)
                self._create_fallback_schema(conn)

            self._run_migrations(conn)
            conn.commit()
            logger.info("Database schema initialized at {}", self.db_path)
        finally:
            conn.close()

        self._cleanup_duplicate_calendar_rows()

    @staticmethod
    def _create_fallback_schema(conn: sqlite3.Connection) -> None:
        """Create a minimal schema if sql/schema.sql is unavailable."""
        conn.executescript(
            """
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
            );

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
                auction_calendar_id INTEGER,
                source_auction_url TEXT,
                source_csv_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (auction_calendar_id) REFERENCES auction_calendar(id) ON DELETE SET NULL,
                UNIQUE(vin, lot_number)
            );

            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_type TEXT NOT NULL,
                query_value TEXT NOT NULL,
                result_count INTEGER DEFAULT 0,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

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
            );
            """
        )

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Bring existing SQLite databases up to the current schema."""
        self._ensure_auction_calendar_columns(conn)
        self._ensure_vehicle_source_columns(conn)
        self._ensure_csv_export_schema(conn)

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    def _ensure_auction_calendar_columns(self, conn: sqlite3.Connection) -> None:
        try:
            cols = self._table_columns(conn, "auction_calendar")
            if "lots_view_url" not in cols:
                conn.execute("ALTER TABLE auction_calendar ADD COLUMN lots_view_url TEXT")
                logger.info("Migrated auction_calendar: added lots_view_url")
            if "lots_view_text" not in cols:
                conn.execute("ALTER TABLE auction_calendar ADD COLUMN lots_view_text TEXT")
                logger.info("Migrated auction_calendar: added lots_view_text")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auction_calendar_lots_url "
                "ON auction_calendar(lots_view_url)"
            )
        except Exception as exc:
            logger.warning(f"Auction calendar migration check failed: {exc}")

    def _ensure_vehicle_source_columns(self, conn: sqlite3.Connection) -> None:
        try:
            cols = self._table_columns(conn, "vehicles")
            migrations = {
                "auction_calendar_id": (
                    "ALTER TABLE vehicles ADD COLUMN auction_calendar_id INTEGER "
                    "REFERENCES auction_calendar(id) ON DELETE SET NULL"
                ),
                "source_auction_url": "ALTER TABLE vehicles ADD COLUMN source_auction_url TEXT",
                "source_csv_path": "ALTER TABLE vehicles ADD COLUMN source_csv_path TEXT",
            }
            for column_name, ddl in migrations.items():
                if column_name not in cols:
                    conn.execute(ddl)
                    logger.info("Migrated vehicles: added {}", column_name)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vehicles_auction_calendar "
                "ON vehicles(auction_calendar_id)"
            )
        except Exception as exc:
            logger.warning(f"Vehicle source migration check failed: {exc}")

    @staticmethod
    def _ensure_csv_export_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auction_lot_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER,
                source_url TEXT NOT NULL,
                csv_file_path TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                imported_vehicle_count INTEGER NOT NULL DEFAULT 0,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (auction_id) REFERENCES auction_calendar(id) ON DELETE SET NULL,
                UNIQUE(source_url, csv_file_path)
            );
            CREATE INDEX IF NOT EXISTS idx_auction_lot_exports_auction
                ON auction_lot_exports(auction_id);
            CREATE INDEX IF NOT EXISTS idx_auction_lot_exports_source_url
                ON auction_lot_exports(source_url);

            CREATE TABLE IF NOT EXISTS auction_lot_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                export_id INTEGER NOT NULL,
                auction_id INTEGER,
                lot_number TEXT,
                vin TEXT,
                row_json TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (export_id) REFERENCES auction_lot_exports(id) ON DELETE CASCADE,
                FOREIGN KEY (auction_id) REFERENCES auction_calendar(id) ON DELETE SET NULL,
                UNIQUE(export_id, row_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_export
                ON auction_lot_rows(export_id);
            CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_auction
                ON auction_lot_rows(auction_id);
            CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_lot
                ON auction_lot_rows(lot_number);
            CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_vin
                ON auction_lot_rows(vin);

            CREATE TABLE IF NOT EXISTS lot_scrape_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_id INTEGER,
                lot_url TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_attempt_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (auction_id) REFERENCES auction_calendar(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lot_scrape_status_auction
                ON lot_scrape_status(auction_id);
            CREATE INDEX IF NOT EXISTS idx_lot_scrape_status_status
                ON lot_scrape_status(status);
            """
        )

    def _cleanup_duplicate_calendar_rows(self) -> None:
        """Remove duplicate calendar rows, preferring rows with lots_view_url."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, event_date, COALESCE(auction_time,'') as auction_time, "
                "COALESCE(description,'') as description, lots_view_url "
                "FROM auction_calendar ORDER BY id"
            ).fetchall()
            seen: dict[tuple[str, str, str], int] = {}
            to_delete: list[int] = []
            for row in rows:
                key = (row["event_date"], row["auction_time"], row["description"].lower())
                if key in seen:
                    existing_id = seen[key]
                    existing_row = conn.execute(
                        "SELECT lots_view_url FROM auction_calendar WHERE id = ?",
                        (existing_id,),
                    ).fetchone()
                    if (existing_row and existing_row[0]) or (row["lots_view_url"] is None):
                        to_delete.append(row["id"])
                    else:
                        to_delete.append(existing_id)
                        seen[key] = row["id"]
                else:
                    seen[key] = row["id"]
            if to_delete:
                conn.executemany("DELETE FROM auction_calendar WHERE id = ?", [(i,) for i in to_delete])
                conn.commit()
                logger.info("Cleaned up {} duplicate auction_calendar rows", len(to_delete))
        except Exception as exc:
            logger.warning(f"Could not cleanup duplicate auction_calendar rows: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def insert_vehicle(
        self,
        vehicle: Vehicle,
        auction_id: int | None = None,
        source_auction_url: str | None = None,
        source_csv_path: str | Path | None = None,
    ) -> int:
        """Insert or update a vehicle, avoiding duplicates.

        Args:
            vehicle: The Vehicle instance to persist.
            auction_id: Optional ``auction_calendar.id`` for source tracing.
            source_auction_url: Optional lots-view/sale-list URL that produced the row.
            source_csv_path: Optional CSV path when imported through the export button.

        Returns:
            The database ID of the inserted or existing record.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            data = vehicle.to_database_dict()
            data.pop("id", None)
            data["auction_calendar_id"] = auction_id
            data["source_auction_url"] = str(source_auction_url) if source_auction_url else None
            data["source_csv_path"] = str(source_csv_path) if source_csv_path else None

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO vehicles (
                        vin, lot_number, title_text, year, make, model,
                        odometer, damage_description, sale_date, current_bid,
                        auction_status, detail_url, image_urls,
                        auction_calendar_id, source_auction_url, source_csv_path,
                        created_at, updated_at
                    ) VALUES (
                        :vin, :lot_number, :title_text, :year, :make, :model,
                        :odometer, :damage_description, :sale_date, :current_bid,
                        :auction_status, :detail_url, :image_urls,
                        :auction_calendar_id, :source_auction_url, :source_csv_path,
                        :created_at, :updated_at
                    )
                    """,
                    data,
                )
                conn.commit()
                vehicle_id = cursor.lastrowid
                logger.info("Inserted vehicle {} (id={})", vehicle.lot_number, vehicle_id)
                return vehicle_id or 0
            except sqlite3.IntegrityError:
                # Duplicate: retrieve existing ID.  The schema has both
                # UNIQUE(lot_number) and UNIQUE(vin, lot_number), so a reused
                # lot number can conflict even when the VIN representation has
                # changed (for example, masked vs. full VIN from different
                # Copart views).
                row = conn.execute(
                    "SELECT id FROM vehicles "
                    "WHERE lot_number = ? OR (vin = ? AND lot_number = ?) LIMIT 1",
                    (vehicle.lot_number, vehicle.vin, vehicle.lot_number),
                ).fetchone()
                conn.rollback()
                if row:
                    vehicle_id = int(row[0])
                    self._update_vehicle_source(
                        conn,
                        vehicle_id,
                        auction_id=auction_id,
                        source_auction_url=source_auction_url,
                        source_csv_path=source_csv_path,
                    )
                    conn.commit()
                    logger.info("Vehicle {} already exists (id={}); skipped insert.", vehicle.lot_number, vehicle_id)
                    return vehicle_id
                raise CopartAutomationError("Duplicate key conflict but existing record not found.")
        finally:
            conn.close()

    @staticmethod
    def _update_vehicle_source(
        conn: sqlite3.Connection,
        vehicle_id: int,
        auction_id: int | None = None,
        source_auction_url: str | None = None,
        source_csv_path: str | Path | None = None,
    ) -> None:
        """Backfill source metadata on an existing vehicle row."""
        if auction_id is None and source_auction_url is None and source_csv_path is None:
            return
        conn.execute(
            """
            UPDATE vehicles
            SET auction_calendar_id = COALESCE(auction_calendar_id, ?),
                source_auction_url = COALESCE(source_auction_url, ?),
                source_csv_path = COALESCE(source_csv_path, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                auction_id,
                str(source_auction_url) if source_auction_url else None,
                str(source_csv_path) if source_csv_path else None,
                vehicle_id,
            ),
        )

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

    def insert_auction_calendar_entry(self, entry: 'AuctionCalendarEntry') -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            data = entry.to_database_dict()
            # Ensure updated_at is now for upsert
            # Use INSERT ... ON CONFLICT to update lot view url when duplicate found
            cursor = conn.execute(
                """
                INSERT INTO auction_calendar (
                    event_date, auction_time, description, table_section,
                    row_index, column_index, lots_view_url, lots_view_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_date, auction_time, description) DO UPDATE SET
                    table_section = COALESCE(excluded.table_section, auction_calendar.table_section),
                    row_index = COALESCE(excluded.row_index, auction_calendar.row_index),
                    column_index = COALESCE(excluded.column_index, auction_calendar.column_index),
                    lots_view_url = COALESCE(excluded.lots_view_url, auction_calendar.lots_view_url),
                    lots_view_text = COALESCE(excluded.lots_view_text, auction_calendar.lots_view_text),
                    updated_at = excluded.updated_at
                """,
                (
                    data["event_date"],
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
            # If insert resulted in conflict, lastrowid may be 0; try to fetch id
            if cursor.lastrowid and cursor.lastrowid != 0:
                return cursor.lastrowid
            # Fetch existing id for the conflicted row
            row = conn.execute(
                "SELECT id FROM auction_calendar WHERE event_date = ? AND COALESCE(auction_time,'') = COALESCE(?, '') AND COALESCE(description,'') = COALESCE(?, '')",
                (data["event_date"], data.get("auction_time"), data.get("description")),
            ).fetchone()
            return row[0] if row else 0
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

    def get_auction_calendar_entries(self) -> list[AuctionCalendarEntry]:
        """Return all auction_calendar rows as AuctionCalendarEntry instances."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM auction_calendar "
                "WHERE COALESCE(lots_view_url,'') != '' ORDER BY event_date"
            ).fetchall()
            return [AuctionCalendarEntry.from_database_row(dict(row)) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _resolve_auction_id(
        conn: sqlite3.Connection,
        auction_lots_view_url: str | None,
    ) -> int | None:
        if not auction_lots_view_url:
            return None
        row = conn.execute(
            "SELECT id FROM auction_calendar WHERE COALESCE(lots_view_url,'') = ? LIMIT 1",
            (str(auction_lots_view_url),),
        ).fetchone()
        return int(row[0]) if row else None

    def get_auction_id_by_lots_url(self, auction_lots_view_url: str | None) -> int | None:
        """Resolve an auction_calendar id by its lots-view URL."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return self._resolve_auction_id(conn, auction_lots_view_url)
        finally:
            conn.close()

    def insert_auction_lot_export(
        self,
        source_url: str,
        csv_file_path: str | Path,
        row_count: int = 0,
        imported_vehicle_count: int = 0,
    ) -> int:
        """Record a downloaded Copart auction lot CSV export.

        Returns the export row id, updating counts if the same source/path was
        already recorded.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            source_url_str = str(source_url)
            csv_file_path_str = str(csv_file_path)
            auction_id = self._resolve_auction_id(conn, source_url_str)
            conn.execute(
                """
                INSERT INTO auction_lot_exports (
                    auction_id, source_url, csv_file_path, row_count,
                    imported_vehicle_count, downloaded_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(source_url, csv_file_path) DO UPDATE SET
                    auction_id = COALESCE(excluded.auction_id, auction_lot_exports.auction_id),
                    row_count = excluded.row_count,
                    imported_vehicle_count = excluded.imported_vehicle_count,
                    downloaded_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    auction_id,
                    source_url_str,
                    csv_file_path_str,
                    max(row_count, 0),
                    max(imported_vehicle_count, 0),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM auction_lot_exports WHERE source_url = ? AND csv_file_path = ?",
                (source_url_str, csv_file_path_str),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def insert_auction_lot_row(
        self,
        export_id: int,
        row_data: dict[str, Any],
        lot_number: str | None = None,
        vin: str | None = None,
        auction_id: int | None = None,
    ) -> int:
        """Persist a raw CSV row from a Copart auction lot export."""
        row_json = json.dumps(row_data, ensure_ascii=False, sort_keys=True, default=str)
        row_hash = hashlib.sha256(row_json.encode("utf-8")).hexdigest()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            if auction_id is None:
                auction_row = conn.execute(
                    "SELECT auction_id FROM auction_lot_exports WHERE id = ?",
                    (export_id,),
                ).fetchone()
                auction_id = int(auction_row[0]) if auction_row and auction_row[0] else None
            conn.execute(
                """
                INSERT INTO auction_lot_rows (
                    export_id, auction_id, lot_number, vin, row_json, row_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(export_id, row_hash) DO UPDATE SET
                    auction_id = COALESCE(excluded.auction_id, auction_lot_rows.auction_id),
                    lot_number = COALESCE(excluded.lot_number, auction_lot_rows.lot_number),
                    vin = COALESCE(excluded.vin, auction_lot_rows.vin),
                    row_json = excluded.row_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (export_id, auction_id, lot_number, vin, row_json, row_hash),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM auction_lot_rows WHERE export_id = ? AND row_hash = ?",
                (export_id, row_hash),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def ensure_lot_record(self, auction_lots_view_url: str | None, lot_url: str) -> int:
        """Insert a lot scrape record if missing and return its id.

        Attempts to resolve `auction_id` by matching `auction_calendar.lots_view_url`.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            auction_id = self._resolve_auction_id(conn, auction_lots_view_url)

            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO lot_scrape_status (
                        auction_id, lot_url, status, attempts, created_at, updated_at
                    ) VALUES (?, ?, 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (auction_id, lot_url),
                )
                conn.commit()
            finally:
                # Retrieve id whether we inserted or it existed
                row = conn.execute("SELECT id FROM lot_scrape_status WHERE lot_url = ?", (lot_url,)).fetchone()
                return row[0] if row else 0
        finally:
            conn.close()

    def update_lot_status(self, lot_url: str, status: str, error: str | None = None) -> None:
        """Update a lot scrape record's status, increment attempts, and record last error/time."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            # Increment attempts and set status and last_error
            conn.execute(
                """
                UPDATE lot_scrape_status
                SET status = ?,
                    attempts = attempts + 1,
                    last_error = ?,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lot_url = ?
                """,
                (status, error, lot_url),
            )
            conn.commit()
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
