"""Tests for Copart auction CSV export support."""

from __future__ import annotations

from pathlib import Path

import pytest

from copart_automation.app.auction_export import AuctionExportManager


class TestAuctionExportParsing:
    def test_parse_copart_export_csv(self) -> None:
        csv_text = """\ufeffLot #,VIN,Year,Make,Model,Odometer,Primary Damage,Secondary Damage,Sale Date,Current Bid,Title Code,Status,Image URL
12345678,1HGCM82633A004352,2018,HONDA,ACCORD,"12,345 mi",FRONT END,MINOR DENT/SCRATCHES,07/24/2026,"$1,250.00",CT CLEAN TITLE,Upcoming,https://images.copart.com/example.jpg
"""
        manager = AuctionExportManager()

        vehicles = manager.parse_csv_text(
            csv_text,
            source_url="https://www.copart.com/saleListResult/23/2026-07-24",
        )

        assert len(vehicles) == 1
        vehicle = vehicles[0]
        assert vehicle.lot_number == "12345678"
        assert vehicle.vin == "1HGCM82633A004352"
        assert vehicle.year == 2018
        assert vehicle.make == "HONDA"
        assert vehicle.model == "ACCORD"
        assert vehicle.odometer == 12345
        assert vehicle.damage_description == "FRONT END / MINOR DENT/SCRATCHES"
        assert vehicle.sale_date == "07/24/2026"
        assert vehicle.current_bid == 1250.0
        assert vehicle.title_text == "CT CLEAN TITLE"
        assert vehicle.auction_status == "Upcoming"
        assert str(vehicle.detail_url) == "https://www.copart.com/lot/12345678"
        assert vehicle.image_urls == ["https://images.copart.com/example.jpg"]

        rows = manager.parse_csv_rows(csv_text)
        assert rows[0]["lot number"] == "12345678"
        assert rows[0]["primary damage"] == "FRONT END"
        assert manager.extract_row_identifiers(rows[0]) == (
            "12345678",
            "1HGCM82633A004352",
        )

    def test_parse_description_when_year_make_model_columns_missing(self) -> None:
        csv_text = """Lot Number,VIN,Vehicle Description,Odometer
87654321,2T1BURHE0JC034567,2020 Toyota Corolla LE,"5,432"
"""
        manager = AuctionExportManager()

        vehicles = manager.parse_csv_text(csv_text)

        assert len(vehicles) == 1
        assert vehicles[0].year == 2020
        assert vehicles[0].make == "Toyota"
        assert vehicles[0].model == "Corolla LE"
        assert vehicles[0].odometer == 5432

    def test_parse_csv_skips_rows_missing_required_identifiers(self) -> None:
        csv_text = """Lot #,VIN,Year,Make,Model
12345678,,2018,HONDA,ACCORD
,1HGCM82633A004352,2018,HONDA,ACCORD
87654321,2T1BURHE0JC034567,2020,TOYOTA,COROLLA
"""
        manager = AuctionExportManager()

        vehicles = manager.parse_csv_text(csv_text)

        assert len(vehicles) == 1
        assert vehicles[0].lot_number == "87654321"


class FakeDownload:
    suggested_filename = "LotSearchresults_2026 July 24.csv"

    async def save_as(self, path: str) -> None:
        Path(path).write_text("Lot #,VIN\n12345678,1HGCM82633A004352\n", encoding="utf-8")


class FakeDownloadInfo:
    def __init__(self) -> None:
        self.value = self._value()

    async def __aenter__(self) -> "FakeDownloadInfo":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def _value(self) -> FakeDownload:
        return FakeDownload()


class FakeExportButton:
    def __init__(self) -> None:
        self.clicked = False

    async def click(self) -> None:
        self.clicked = True


class FakePage:
    url = "https://www.copart.com/saleListResult/23/2026-07-24"

    def __init__(self) -> None:
        self.button = FakeExportButton()

    async def wait_for_selector(self, selector: str, state: str, timeout: int) -> None:
        assert "exportButton" in selector
        assert state == "visible"

    async def query_selector(self, selector: str) -> FakeExportButton:
        assert "lotsearchExport" in selector
        return self.button

    def expect_download(self, timeout: int) -> FakeDownloadInfo:
        return FakeDownloadInfo()


@pytest.mark.asyncio
async def test_download_lots_csv_clicks_export_button(tmp_path: Path) -> None:
    manager = AuctionExportManager()
    page = FakePage()

    saved_path = await manager.download_lots_csv(page, save_dir=tmp_path, timeout=1000)  # type: ignore[arg-type]

    assert saved_path is not None
    assert saved_path.name == "LotSearchresults_2026 July 24.csv"
    assert saved_path.exists()
    assert page.button.clicked is True
