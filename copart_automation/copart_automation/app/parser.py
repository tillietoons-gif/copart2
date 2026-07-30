"""HTML parsing module for Copart vehicle listings.

Design decisions:
- BeautifulSoup4 is used for its simplicity and resilience with
  malformed or changing HTML. Playwright provides the raw HTML after
  JavaScript execution, ensuring dynamic content is captured.
- Parsing never relies on a single CSS selector; fallbacks are used
  for common data points to improve resilience against site updates.
- All parsed data is validated through Pydantic models before being
  returned, ensuring data quality.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Page

from copart_automation.app.config import settings
from copart_automation.app.exceptions import ParseError
from copart_automation.app.logger import get_logger
from copart_automation.app.models import Vehicle

logger = get_logger(__name__)


class VehicleParser:
    """Parses HTML from Copart pages into structured Vehicle models.

    This parser operates on the assumption that pages have completed
    their JavaScript execution (managed by Playwright) and provides
    approximate selectors. If Copart updates its site structure,
    selectors must be updated accordingly.
    """

    def __init__(self) -> None:
        # Pre-compile common selectors is not practical with BeautifulSoup
        # since selectors are evaluated at parse time.
        pass

    async def parse_search_results(self, page: Page) -> list[Vehicle]:
        """Parse a search results page and return Vehicle instances.

        Args:
            page: The Playwright Page instance after navigation.

        Returns:
            A list of Vehicle objects extracted from the results page.

        Raises:
            ParseError: If the page structure does not match expected patterns.
        """
        logger.info("Parsing search results from {}", page.url)
        html_content = await page.content()
        soup = BeautifulSoup(html_content, "lxml")

        vehicles: list[Vehicle] = []
        # Common container selectors for result cards
        result_containers = soup.select(
            ".search-result, .vehicle-card, .lot-item, .listing, [data-testid*='vehicle'], "
            ".p-datatable-tbody tr[data-lotnumber], tr[data-lotnumber], .p-selectable-row[data-lotnumber], "
            ".search_result_lot_detail_block"
        )
        if not result_containers:
            # Fallback: look for any table rows with lot numbers
            result_containers = soup.select(
                "tr[data-lot], tr[data-lotnumber], .results-row, .search-row, .p-datatable-tbody tr"
            )

        for container in result_containers:
            try:
                vehicle = await self._parse_result_container(container)
                if vehicle:
                    vehicles.append(vehicle)
            except Exception as exc:
                logger.warning(f"Failed to parse result container: {exc}")
                # Continue with remaining results rather than failing entirely
                continue

        if not vehicles and result_containers:
            logger.info("No complete vehicles parsed from {} containers.", len(result_containers))
        elif not vehicles:
            logger.info("No result containers found on page.")

        return vehicles

    async def parse_vehicle_details(self, page: Page) -> Vehicle | None:
        """Parse an individual vehicle detail page.

        Args:
            page: The Playwright Page instance for the detail page.

        Returns:
            A Vehicle instance if parsing succeeds; None otherwise.
        """
        logger.info("Parsing vehicle details from {}", page.url)
        html_content = await page.content()
        soup = BeautifulSoup(html_content, "lxml")

        try:
            return await self._parse_detail_page(soup, page_url=page.url)
        except Exception as exc:
            logger.error(f"Failed to parse vehicle details: {exc}")
            raise ParseError(f"Could not parse vehicle details: {exc}") from exc

    async def _parse_result_container(self, container: Any) -> Vehicle | None:
        """Parse a single result container into a Vehicle model."""
        # Extract VIN (look for text patterns or specific selectors)
        vin_text = self._find_text(container, [
            ".vin-label", ".vehicle-vin", "[data-field='vin']", "td[data-label='VIN']",
            ".search_result_vehicle_info_vin", "[class*='vin' i]", "[data-uname*='vin' i]",
        ])
        vin = self._clean_vin(vin_text)
        if not vin:
            # Try extracting VIN-like token from the full row text.
            row_text = container.get_text(" ", strip=True)
            vin = self._clean_vin(row_text)

        # Extract lot number
        lot_text = self._find_text(container, [
            ".lot-number", ".lot-label", "[data-field='lot']", "a[href*='lot/']",
            ".search_result_lot_number", ".lot-number-row", "[data-lotnumber]",
        ])
        lot_number = self._clean_lot(lot_text)
        if not lot_number:
            lot_attr = container.get("data-lotnumber") if hasattr(container, "get") else None
            if isinstance(lot_attr, str) and lot_attr.strip():
                lot_number = lot_attr.strip()

        # Extract title
        title_text = self._find_text(container, [
            ".title-status", ".vehicle-title", "[data-field='title']",
            ".search_result_lot_detail", ".search_result_lot_detail_block",
        ])

        # Extract year, make, model
        year_text = self._find_text(container, [
            ".year", ".model-year", "[data-field='year']",
        ])
        year = self._clean_int(year_text)
        if year is None and title_text:
            year = self._clean_int(title_text)

        make_text = self._find_text(container, [
            ".make", "[data-field='make']",
        ])
        model_text = self._find_text(container, [
            ".model", "[data-field='model']",
        ])

        # Odometer
        odometer_text = self._find_text(container, [
            ".odometer", ".mileage", "[data-field='odometer']",
        ])
        odometer = self._clean_int(odometer_text)

        # Damage description
        damage_text = self._find_text(container, [
            ".damage", ".damage-desc", "[data-field='damage']",
        ])

        # Sale date
        sale_date_text = self._find_text(container, [
            ".sale-date", ".auction-date", "[data-field='sale_date']",
        ])

        # Current bid
        bid_text = self._find_text(container, [
            ".current-bid", ".bid-amount", ".price", "[data-field='current_bid']",
        ])
        current_bid = self._clean_float(bid_text)

        # Auction status
        status_text = self._find_text(container, [
            ".auction-status", ".status", "[data-field='status']",
        ])

        # Detail URL
        link = container.select_one("a[href*='lot/'], a[href*='vehicle/']")
        detail_url = str(link["href"]) if link else None
        if detail_url and not detail_url.startswith("http"):
            detail_url = f"https://www.copart.com{detail_url}"

        # Image URLs (look for img tags within container)
        image_urls: list[str] = []
        for img in container.select("img[src*='copart']"):
            src = img.get("src") or img.get("data-src")
            if src and isinstance(src, str) and src.startswith("http"):
                image_urls.append(src)

        if not vin or not lot_number:
            # For sale-list pages, VIN is sometimes not rendered until lot detail
            # page load. Keep the record using a stable synthetic VIN keyed by lot.
            if lot_number and not vin:
                vin = f"UNKNOWN-{lot_number}"
            else:
                logger.debug("Skipping container: missing VIN or lot number.")
                return None

        return Vehicle(
            vin=vin,
            lot_number=lot_number,
            title_text=title_text,
            year=year,
            make=make_text,
            model=model_text,
            odometer=odometer,
            damage_description=damage_text,
            sale_date=sale_date_text,
            current_bid=current_bid,
            auction_status=status_text,
            detail_url=detail_url,
            image_urls=image_urls if image_urls else None,
        )

    async def _parse_detail_page(self, soup: BeautifulSoup, page_url: str | None = None) -> Vehicle:
        """Parse a full vehicle detail page with broader selectors."""
        # For detail pages, we try to gather more comprehensive data
        # This is a fallback/extended version of the result parser.
        container = soup

        vin_text = self._find_text(soup, [
            ".vin", ".vehicle-vin", "span:contains('VIN'), td:contains('VIN') + td",
        ])
        vin = self._clean_vin(vin_text)
        lot_text = self._find_text(soup, [
            ".lot-number", ".lot-number-detail", "span:contains('Lot'), td:contains('Lot') + td",
        ])
        lot_number = self._clean_lot(lot_text)

        # If VIN/lot are still missing, try to extract from URL or title
        if not vin:
            title_tag = soup.select_one("title")
            if title_tag:
                title_str = title_tag.get_text()
                # Very rough VIN extraction from title
                import re
                match = re.search(r"VIN[:\s]*([A-HJ-NPR-Z0-9]{17})", title_str)
                if match:
                    vin = match.group(1)

        # Build a minimal Vehicle if core data is present
        year_text = self._find_text(soup, [".year", ".model-year"])
        make_text = self._find_text(soup, [".make"])
        model_text = self._find_text(soup, [".model"])
        odometer_text = self._find_text(soup, [".odometer", ".mileage"])
        damage_text = self._find_text(soup, [".damage", ".damage-desc"])
        sale_date_text = self._find_text(soup, [".sale-date", ".auction-date"])
        bid_text = self._find_text(soup, [".current-bid", ".price"])
        status_text = self._find_text(soup, [".auction-status", ".status"])
        title_text = self._find_text(soup, [".title-status", ".title"])

        # Image URLs from detail page
        image_urls = []
        for img in soup.select("img[src*='copart'], img[data-src*='copart']"):
            src = img.get("src") or img.get("data-src")
            if src and isinstance(src, str) and src.startswith("http"):
                image_urls.append(src)

        if not vin or not lot_number:
            # Try URL-based lot extraction
            from urllib.parse import urlparse
            # Not easily available here without page URL; rely on text
            pass

        # We must have VIN and lot to create a record
        if not vin or not lot_number:
            raise ParseError("Could not extract VIN and lot number from vehicle details.")

        return Vehicle(
            vin=vin,
            lot_number=lot_number,
            title_text=title_text,
            year=self._clean_int(year_text),
            make=make_text,
            model=model_text,
            odometer=self._clean_int(odometer_text),
            damage_description=damage_text,
            sale_date=sale_date_text,
            current_bid=self._clean_float(bid_text),
            auction_status=status_text,
            detail_url=page_url if page_url else None,
            image_urls=image_urls if image_urls else None,
        )

    @staticmethod
    def _find_text(container: Any, selectors: list[str]) -> str | None:
        """Find text content using a prioritized list of selectors.

        Returns the first non-empty text match.
        """
        for selector in selectors:
            element = container.select_one(selector)
            if element:
                text = element.get_text(strip=True)
                if text:
                    return text
        return None

    @staticmethod
    def _clean_vin(text: Any) -> str | None:
        if not text or not isinstance(text, str):
            return None
        # Remove common prefixes and whitespace
        cleaned = text.replace("VIN:", "").replace("VIN", "").strip()
        # VINs are 17 characters; take the first 17 alphanumeric chars that look right
        import re
        match = re.search(r"[A-HJ-NPR-Z0-9]{17}", cleaned)
        if match:
            return match.group(0)
        # If shorter, return as-is if it looks somewhat valid
        if len(cleaned) >= 10:
            return cleaned[:17]
        return None

    @staticmethod
    def _clean_lot(text: Any) -> str | None:
        if not text or not isinstance(text, str):
            return None
        cleaned = text.replace("Lot:", "").replace("Lot", "").strip()
        # Remove URL fragments and extra whitespace
        import re
        match = re.search(r"[\w\-/]+", cleaned)
        if match:
            return match.group(0)
        if cleaned:
            return cleaned
        return None

    @staticmethod
    def _clean_int(text: Any) -> int | None:
        if not text or not isinstance(text, str):
            return None
        import re
        match = re.search(r"\d+", text)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
        return None

    @staticmethod
    def _clean_float(text: Any) -> float | None:
        if not text or not isinstance(text, str):
            return None
        import re
        # Match currency amounts like $1,234.56 or 1234.56
        match = re.search(r"[\$]?([\d,]+\.\d{2})", text)
        if match:
            cleaned = match.group(1).replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        # Try integer-style amounts
        int_match = re.search(r"[\$]?([\d,]+)", text)
        if int_match:
            cleaned = int_match.group(1).replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None
