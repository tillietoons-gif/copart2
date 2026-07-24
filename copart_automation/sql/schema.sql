-- Copart Automation Database Schema
-- SQLite database for storing auction calendar entries, lot CSV exports,
-- vehicle data, searches, and downloads.

-- Enable foreign keys for referential integrity
PRAGMA foreign_keys = ON;

-- Auction calendar: sale/date rows and the URL used to open the lot list.
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
CREATE INDEX IF NOT EXISTS idx_auction_calendar_lots_url ON auction_calendar(lots_view_url);
CREATE INDEX IF NOT EXISTS idx_auction_calendar_date ON auction_calendar(event_date);
CREATE INDEX IF NOT EXISTS idx_auction_calendar_section ON auction_calendar(table_section);

-- Vehicles: core data extracted from Copart listings or sale-list CSV exports.
-- The source_* columns let CSV-imported vehicles be traced back to the auction
-- and the downloaded export file used to create/update the record.
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
    image_urls TEXT,  -- Stored as JSON array string
    auction_calendar_id INTEGER,
    source_auction_url TEXT,
    source_csv_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (auction_calendar_id) REFERENCES auction_calendar(id) ON DELETE SET NULL,
    UNIQUE(vin, lot_number)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_vehicles_vin ON vehicles(vin);
CREATE INDEX IF NOT EXISTS idx_vehicles_lot ON vehicles(lot_number);
CREATE INDEX IF NOT EXISTS idx_vehicles_make_model ON vehicles(make, model);
CREATE INDEX IF NOT EXISTS idx_vehicles_auction_status ON vehicles(auction_status);
CREATE INDEX IF NOT EXISTS idx_vehicles_auction_calendar ON vehicles(auction_calendar_id);

-- Auction lot CSV exports: one row per downloaded sale-list Export CSV.
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
CREATE INDEX IF NOT EXISTS idx_auction_lot_exports_auction ON auction_lot_exports(auction_id);
CREATE INDEX IF NOT EXISTS idx_auction_lot_exports_source_url ON auction_lot_exports(source_url);

-- Raw auction lot CSV rows: preserves all columns returned by Copart's Export
-- button, even if the normalized vehicles table does not have a field for each
-- column. row_json is the normalized CSV row; row_hash deduplicates identical
-- rows within the same export.
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
CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_export ON auction_lot_rows(export_id);
CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_auction ON auction_lot_rows(auction_id);
CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_lot ON auction_lot_rows(lot_number);
CREATE INDEX IF NOT EXISTS idx_auction_lot_rows_vin ON auction_lot_rows(vin);

-- Lot detail scrape status for the fallback per-lot parser.
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
CREATE INDEX IF NOT EXISTS idx_lot_scrape_status_auction ON lot_scrape_status(auction_id);
CREATE INDEX IF NOT EXISTS idx_lot_scrape_status_status ON lot_scrape_status(status);

-- Searches: audit trail of user-initiated search queries
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_type TEXT NOT NULL CHECK(query_type IN ('vin', 'lot', 'make', 'model', 'year', 'general')),
    query_value TEXT NOT NULL,
    result_count INTEGER DEFAULT 0,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_searches_query_type ON searches(query_type);
CREATE INDEX IF NOT EXISTS idx_searches_executed_at ON searches(executed_at);

-- Downloads: tracking of downloaded files per vehicle/account
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT CHECK(file_type IN ('image', 'invoice', 'document', 'other')),
    download_url TEXT,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_size_bytes INTEGER,
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
    UNIQUE(vehicle_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_downloads_vehicle_id ON downloads(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_downloads_file_type ON downloads(file_type);

-- Trigger to update updated_at on vehicles updates
CREATE TRIGGER IF NOT EXISTS trg_vehicles_updated_at
AFTER UPDATE ON vehicles
FOR EACH ROW
BEGIN
    UPDATE vehicles SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_auction_calendar_updated_at
AFTER UPDATE ON auction_calendar
FOR EACH ROW
BEGIN
    UPDATE auction_calendar SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_auction_lot_exports_updated_at
AFTER UPDATE ON auction_lot_exports
FOR EACH ROW
BEGIN
    UPDATE auction_lot_exports SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_auction_lot_rows_updated_at
AFTER UPDATE ON auction_lot_rows
FOR EACH ROW
BEGIN
    UPDATE auction_lot_rows SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
