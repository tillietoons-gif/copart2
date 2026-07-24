"""Auction CSV export support for Copart sale list pages.

Copart sale-list pages expose an Export button similar to::

    <a id="exportButton" data-uname="lotsearchExport" ng-csv="getCSVResult()">Export</a>

Clicking that button produces a CSV containing the lots currently loaded for
that auction.  This module downloads that account-accessible CSV through the
same authenticated Playwright page and converts the rows into ``Vehicle``
models for database storage.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import Page

from copart_automation.app.config import settings
from copart_automation.app.logger import get_logger
from copart_automation.app.models import Vehicle
from copart_automation.app.parser import VehicleParser

logger = get_logger(__name__)

BASE_URL = "https://www.copart.com"


class AuctionExportManager:
    """Downloads and parses Copart auction lot CSV exports.

    The export is triggered by normal browser interaction, so it uses the
    authenticated session and does not attempt to call undocumented endpoints
    directly.  If Copart changes the button markup, the selector list can be
    updated here without touching the rest of the scraping workflow.
    """

    EXPORT_BUTTON_SELECTOR = (
        "#exportButton, "
        "[data-uname='lotsearchExport'], "
        "a.search-export-btn, "
        "a[ng-csv='getCSVResult()'], "
        "button[data-uname='lotsearchExport']"
    )

    LOT_ALIASES = (
        "lot number",
        "lot #",
        "lot no",
        "lot num",
        "lot",
        "lotnumber",
    )
    VIN_ALIASES = (
        "vin",
        "vin number",
        "vehicle identification number",
    )
    TITLE_ALIASES = (
        "title",
        "title code",
        "title type",
        "title status",
        "doc type",
        "document type",
        "ownership doc type",
    )
    YEAR_ALIASES = ("year", "vehicle year", "model year")
    MAKE_ALIASES = ("make", "manufacturer")
    MODEL_ALIASES = ("model", "vehicle model")
    ODOMETER_ALIASES = (
        "odometer",
        "odo",
        "mileage",
        "odometer reading",
        "actual odometer",
    )
    PRIMARY_DAMAGE_ALIASES = (
        "primary damage",
        "damage",
        "damage description",
        "loss type",
    )
    SECONDARY_DAMAGE_ALIASES = (
        "secondary damage",
        "second damage",
    )
    SALE_DATE_ALIASES = (
        "sale date",
        "auction date",
        "sale date time",
        "sale time",
    )
    BID_ALIASES = (
        "current bid",
        "current high bid",
        "high bid",
        "bid",
        "bid amount",
        "current bid amount",
        "pre bid",
        "price",
        "sale price",
        "buy it now price",
    )
    STATUS_ALIASES = (
        "status",
        "sale status",
        "auction status",
        "lot status",
    )
    DETAIL_URL_ALIASES = (
        "detail url",
        "vehicle url",
        "listing url",
        "lot url",
        "url",
        "link",
        "href",
    )
    IMAGE_URL_ALIASES = (
        "image url",
        "image urls",
        "thumbnail url",
        "thumbnail",
        "photo url",
        "photo urls",
        "image",
        "images",
    )
    DESCRIPTION_ALIASES = (
        "description",
        "vehicle",
        "vehicle description",
        "year make model",
        "lot description",
    )

    async def download_lots_csv(
        self,
        page: Page,
        save_dir: Path | None = None,
        timeout: int | None = None,
    ) -> Path | None:
        """Click the sale-list Export button and save the resulting CSV.

        Args:
            page: A Playwright page already navigated to a Copart sale list
                result page.
            save_dir: Directory where the CSV should be saved. Defaults to
                ``downloads/auction_exports``.
            timeout: Maximum time in milliseconds for the button/download.

        Returns:
            The saved CSV path, or ``None`` when an export button/download is
            not available on the page.
        """
        save_dir = save_dir or settings.download_dir / "auction_exports"
        timeout = timeout or settings.navigation_timeout
        save_dir.mkdir(parents=True, exist_ok=True)

        try:
            load_state_timeout = min(timeout, settings.action_timeout)
            await page.wait_for_load_state("networkidle", timeout=load_state_timeout)
        except Exception:
            # Sale pages may keep long-polling; the export button wait below is
            # the real readiness check.
            pass

        try:
            await page.wait_for_selector(
                self.EXPORT_BUTTON_SELECTOR,
                state="visible",
                timeout=timeout,
            )
        except Exception as exc:
            logger.info(
                "No CSV export button found on {}: {}",
                getattr(page, "url", "unknown"),
                exc,
            )
            return None

        export_button = await page.query_selector(self.EXPORT_BUTTON_SELECTOR)
        if export_button is None:
            logger.info("CSV export selector matched during wait but no element was returned.")
            return None

        try:
            logger.info("Clicking Copart lot export button on {}", getattr(page, "url", "unknown"))
            async with page.expect_download(timeout=timeout) as download_info:
                await export_button.click()
            download = await download_info.value
        except Exception as exc:
            logger.warning("CSV export click did not produce a downloadable file: {}", exc)
            return None

        suggested_name = getattr(download, "suggested_filename", None) or "LotSearchresults.csv"
        filename = self._sanitize_filename(str(suggested_name))
        if not filename.lower().endswith(".csv"):
            filename = f"{filename}.csv"
        save_path = self._unique_path(save_dir / filename)

        try:
            await download.save_as(str(save_path))
            logger.info("Saved Copart lot export CSV to {}", save_path)
            return save_path.resolve()
        except Exception as exc:
            logger.warning("Failed to save Copart lot export CSV: {}", exc)
            return None

    def parse_csv_file(self, csv_path: Path, source_url: str | None = None) -> list[Vehicle]:
        """Parse a downloaded Copart CSV file into ``Vehicle`` models."""
        text = self._read_csv_file(csv_path)
        vehicles = self.parse_csv_text(text, source_url=source_url)
        logger.info("Parsed {} vehicles from Copart export CSV {}", len(vehicles), csv_path)
        return vehicles

    def read_csv_rows(self, csv_path: Path) -> list[dict[str, str]]:
        """Read a CSV file and return normalized row dictionaries for raw storage."""
        return self.parse_csv_rows(self._read_csv_file(csv_path))

    def parse_csv_rows(self, csv_text: str) -> list[dict[str, str]]:
        """Return normalized CSV rows while preserving all exported columns."""
        if not csv_text or not csv_text.strip():
            return []

        reader = self._make_dict_reader(csv_text)
        if not reader.fieldnames:
            return []

        rows: list[dict[str, str]] = []
        for raw_row in reader:
            normalized_row = self._normalize_row(raw_row)
            if any((value or "").strip() for value in normalized_row.values()):
                rows.append(normalized_row)
        return rows

    def extract_row_identifiers(self, row: dict[str, Any]) -> tuple[str | None, str | None]:
        """Extract ``(lot_number, vin)`` from a normalized or raw CSV row."""
        normalized_row = self._normalize_row(row)
        lot_number = VehicleParser._clean_lot(self._get_value(normalized_row, self.LOT_ALIASES))
        vin_raw = self._get_value(normalized_row, self.VIN_ALIASES)
        vin = VehicleParser._clean_vin(vin_raw.upper() if vin_raw else None)
        return lot_number, vin

    def parse_csv_text(self, csv_text: str, source_url: str | None = None) -> list[Vehicle]:
        """Parse Copart CSV text into ``Vehicle`` models.

        The Copart export has changed header names over time, so this method
        maps multiple common aliases to the fields used by the local model and
        skips rows that do not contain the required VIN and lot number.
        """
        vehicles: list[Vehicle] = []
        for row_number, normalized_row in enumerate(self.parse_csv_rows(csv_text), start=2):
            try:
                vehicle = self._row_to_vehicle(normalized_row, source_url=source_url)
            except Exception as exc:
                logger.warning("Skipping Copart CSV row {}: {}", row_number, exc)
                continue

            if vehicle is not None:
                vehicles.append(vehicle)

        return vehicles

    def _row_to_vehicle(self, row: dict[str, str], source_url: str | None = None) -> Vehicle | None:
        lot_raw = self._get_value(row, self.LOT_ALIASES)
        vin_raw = self._get_value(row, self.VIN_ALIASES)

        lot_number = VehicleParser._clean_lot(lot_raw)
        vin = VehicleParser._clean_vin(vin_raw.upper() if vin_raw else None)

        if not lot_number or not vin:
            logger.debug(
                "Skipping CSV row because VIN or lot number is missing: lot={}, vin={}",
                lot_raw,
                vin_raw,
            )
            return None

        description = self._get_value(row, self.DESCRIPTION_ALIASES)
        parsed_year, parsed_make, parsed_model = self._parse_year_make_model(description)

        year = self._clean_int(self._get_value(row, self.YEAR_ALIASES)) or parsed_year
        make = self._get_value(row, self.MAKE_ALIASES) or parsed_make
        model = self._get_value(row, self.MODEL_ALIASES) or parsed_model

        primary_damage = self._get_value(row, self.PRIMARY_DAMAGE_ALIASES)
        secondary_damage = self._get_value(row, self.SECONDARY_DAMAGE_ALIASES)
        damage_description = self._combine_damage(primary_damage, secondary_damage)

        explicit_url = self._get_value(row, self.DETAIL_URL_ALIASES)
        detail_url = self._normalize_detail_url(
            explicit_url,
            lot_number=lot_number,
            source_url=source_url,
        )

        image_urls = self._extract_image_urls(row)

        return Vehicle(
            vin=vin,
            lot_number=lot_number,
            title_text=self._get_value(row, self.TITLE_ALIASES),
            year=year,
            make=make,
            model=model,
            odometer=self._clean_int(self._get_value(row, self.ODOMETER_ALIASES)),
            damage_description=damage_description,
            sale_date=self._get_value(row, self.SALE_DATE_ALIASES),
            current_bid=self._clean_float(self._get_value(row, self.BID_ALIASES)),
            auction_status=self._get_value(row, self.STATUS_ALIASES),
            detail_url=detail_url,
            image_urls=image_urls or None,
        )

    @staticmethod
    def _read_csv_file(csv_path: Path) -> str:
        try:
            return csv_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return csv_path.read_text(encoding="latin-1")

    @staticmethod
    def _make_dict_reader(csv_text: str) -> csv.DictReader[str]:
        sample = csv_text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return csv.DictReader(io.StringIO(csv_text), dialect=dialect)

    @classmethod
    def _normalize_row(cls, row: dict[str | None, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            normalized_key = cls._normalize_header(key)
            if not normalized_key:
                continue
            normalized_value = "" if value is None else str(value).strip()
            if normalized_key not in normalized or normalized_value:
                normalized[normalized_key] = normalized_value
        return normalized

    @staticmethod
    def _normalize_header(header: str) -> str:
        header = str(header).replace("\ufeff", "").strip().lower()
        header = header.replace("#", " number ").replace("&", " and ")
        header = re.sub(r"[^a-z0-9]+", " ", header)
        header = re.sub(r"\s+", " ", header).strip()
        return header

    @classmethod
    def _get_value(cls, row: dict[str, str], aliases: Iterable[str]) -> str | None:
        normalized_aliases = [cls._normalize_header(alias) for alias in aliases]

        for alias in normalized_aliases:
            value = row.get(alias)
            if value:
                return value.strip()

        # Copart sometimes appends units/help text to headers, for example
        # "Sale Date (MM/DD/YYYY)".  Treat specific alias prefixes as matches,
        # but avoid broad prefixes such as "lot" matching "lot status".
        broad_aliases = {"lot", "url", "bid", "link", "href"}
        for key, value in row.items():
            if not value:
                continue
            for alias in normalized_aliases:
                if key == alias:
                    return value.strip()
                if alias not in broad_aliases and key.startswith(f"{alias} "):
                    return value.strip()

        return None

    @staticmethod
    def _clean_int(text: str | None) -> int | None:
        if not text:
            return None
        match = re.search(r"\d[\d,]*", str(text))
        if not match:
            return None
        try:
            return int(match.group(0).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _clean_float(text: str | None) -> float | None:
        if not text:
            return None
        matches = re.findall(r"\d[\d,]*(?:\.\d+)?", str(text))
        if not matches:
            return None
        # Use the first numeric amount; exported currency values are usually
        # formatted as "$1,200.00 USD" or "1,200".
        try:
            return float(matches[0].replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _combine_damage(primary: str | None, secondary: str | None) -> str | None:
        parts: list[str] = []
        for value in (primary, secondary):
            if value and value.strip() and value.strip() not in parts:
                parts.append(value.strip())
        return " / ".join(parts) if parts else None

    @staticmethod
    def _parse_year_make_model(
        description: str | None,
    ) -> tuple[int | None, str | None, str | None]:
        if not description:
            return None, None, None
        match = re.match(
            r"^\s*((?:19|20)\d{2})\s+([A-Za-z][A-Za-z0-9\-]*)\s+(.+?)\s*$",
            description,
        )
        if not match:
            return None, None, None
        year_text, make, model = match.groups()
        try:
            year = int(year_text)
        except ValueError:
            year = None
        return year, make.strip(), model.strip()

    @staticmethod
    def _normalize_detail_url(
        value: str | None,
        lot_number: str,
        source_url: str | None = None,
    ) -> str | None:
        if value:
            url_match = re.search(r"https?://[^\s,;]+", value)
            if url_match:
                return url_match.group(0)
            cleaned = value.strip()
            if cleaned.startswith("/"):
                return f"{BASE_URL}{cleaned}"
            if cleaned.startswith("lot/"):
                return f"{BASE_URL}/{cleaned}"

        # A lot-number URL is enough to get back to the vehicle detail page and
        # avoids storing the auction search URL as every vehicle's detail URL.
        if lot_number:
            return f"{BASE_URL}/lot/{lot_number}"
        return source_url

    @classmethod
    def _extract_image_urls(cls, row: dict[str, str]) -> list[str]:
        urls: list[str] = []
        image_blob = cls._get_value(row, cls.IMAGE_URL_ALIASES)
        candidates = [image_blob] if image_blob else []

        # Also scan all fields for image-looking URLs, since Copart exports may
        # name these columns differently by locale/account tier.
        candidates.extend(row.values())
        for value in candidates:
            if not value:
                continue
            for url in re.findall(r"https?://[^\s,;|]+", value):
                cleaned = url.strip().strip("\"'")
                lowered = cleaned.lower()
                is_image = any(
                    token in lowered
                    for token in ("image", "img", "photo", ".jpg", ".jpeg", ".png", ".webp")
                )
                if is_image and cleaned not in urls:
                    urls.append(cleaned)
        return urls

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        sanitized = re.sub(r"[<>:\"/\\|?*]", "_", name).strip()
        sanitized = re.sub(r"\s+", " ", sanitized)
        if len(sanitized) > 180:
            suffix = Path(sanitized).suffix
            sanitized = sanitized[: 180 - len(suffix)] + suffix
        return sanitized or f"LotSearchresults_{datetime.utcnow():%Y%m%d_%H%M%S}.csv"

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 2
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1
