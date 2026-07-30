"""Search functionality for Copart vehicle listings.

Design decisions:
- Search is performed through page interaction rather than direct API
  calls, ensuring that session cookies and anti-bot protections are
  respected.
- The module supports multiple search types but does not attempt to
  bypass rate limits. Delays between searches are the caller's
  responsibility (see main.py for example usage with delays).
"""

from __future__ import annotations

import time
from pathlib import Path
import re
from urllib.parse import urljoin
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from copart_automation.app.config import settings
from copart_automation.app.navigation import NavigationHelper
from copart_automation.app.exceptions import NavigationError, ParseError
from copart_automation.app.logger import get_logger
from copart_automation.app.models import SearchQuery, Vehicle
from copart_automation.app.parser import VehicleParser

logger = get_logger(__name__)

EXPORT_BUTTON_SELECTORS: tuple[str, ...] = (
    "#exportButton",
    "a#exportButton",
    "a#exportButton.search-export-btn[data-uname='lotsearchExport']",
    "a.search-export-btn[data-uname='lotsearchExport']",
    "button.export-csv-button:has(span.csv-download)",
    "button.export-csv-button:has-text('Export')",
    "a.export-csv-button:has-text('Export')",
    "button:has-text('Export')",
    "a:has-text('Export')",
    "button.export-csv-button",
    ".export-csv-button",
)


class SearchModule:
    """Provides structured search capabilities against Copart listings.

    Search results are parsed and returned as structured Vehicle models.
    This module does not store results in the database; persistence is
    handled by the caller or DatabaseModule.
    """

    def __init__(self, navigation: NavigationHelper) -> None:
        self._navigation = navigation
        self._parser = VehicleParser()

    async def find_export_button_selector(self, page: Page, timeout: int = 10000) -> str | None:
        """Find the first visible export button selector on a search results page."""
        deadline = time.monotonic() + (timeout / 1000)
        while time.monotonic() < deadline:
            for selector in EXPORT_BUTTON_SELECTORS:
                locator = page.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        element_id = (await locator.get_attribute("id") or "").strip().lower()
                        data_uname = (await locator.get_attribute("data-uname") or "").strip().lower()
                        if element_id == "exportbutton" or data_uname == "lotsearchexport":
                            return selector

                        text = (await locator.inner_text()).strip().lower()
                        # Ignore non-export controls that reuse the same CSS classes.
                        if text and "show less" in text:
                            continue
                        if "export" in text or "#exportbutton" in selector.lower():
                            return selector
                except Exception:
                    continue

            # Fallback scan: find a visible button or anchor that contains Export text.
            try:
                text_locator = page.locator("button:has-text('Export'), a:has-text('Export')").first
                if await text_locator.count() > 0 and await text_locator.is_visible():
                    return "button:has-text('Export'), a:has-text('Export')"
            except Exception:
                pass

            await page.wait_for_timeout(300)
        return None

    async def click_export_button(
        self,
        page: Page,
        timeout: int = 15000,
        output_dir: Path | None = None,
    ) -> Path | None:
        """Click export and save the downloaded CSV file.

        Returns:
            Absolute file path if download succeeds, otherwise None.
        """
        selector = await self.find_export_button_selector(page, timeout=timeout)
        if not selector:
            logger.warning("Export button not found on page: {}", page.url)
            return None

        export_dir = output_dir or (settings.download_dir / "exports")
        export_dir.mkdir(parents=True, exist_ok=True)

        try:
            locator = page.locator(selector).first
            async with page.expect_download(timeout=timeout) as download_info:
                await locator.click(timeout=timeout)
            download = await download_info.value
            filename = download.suggested_filename or "copart_export.csv"
            save_path = export_dir / filename
            await download.save_as(str(save_path))
            logger.info("Export CSV downloaded using selector '{}' to {}", selector, save_path)
            return save_path.resolve()
        except PlaywrightTimeoutError:
            # Some pages open a confirmation dialog after Export click.
            try:
                confirm_selectors = (
                    "button:has-text('Submit')",
                    "button:has-text('Download')",
                    "button:has-text('Confirm')",
                    "button:has-text('OK')",
                    "a:has-text('Ok')",
                )
                for confirm_selector in confirm_selectors:
                    confirm = page.locator(confirm_selector).first
                    if await confirm.count() > 0 and await confirm.is_visible():
                        try:
                            async with page.expect_download(timeout=timeout) as download_info:
                                await confirm.click(timeout=timeout)
                            download = await download_info.value
                            filename = download.suggested_filename or "copart_export.csv"
                            save_path = export_dir / filename
                            await download.save_as(str(save_path))
                            logger.info("Export CSV downloaded via confirmation selector '{}' to {}", confirm_selector, save_path)
                            return save_path.resolve()
                        except Exception:
                            try:
                                async with page.expect_response(
                                    lambda r: (
                                        "csv" in (r.url or "").lower()
                                        or "export" in (r.url or "").lower()
                                        or "text/csv" in (r.headers.get("content-type", "").lower())
                                        or "attachment" in (r.headers.get("content-disposition", "").lower())
                                    ),
                                    timeout=timeout,
                                ) as response_info:
                                    await confirm.click(timeout=timeout)
                                response = await response_info.value
                                body = await response.body()
                                if body:
                                    disposition = response.headers.get("content-disposition", "")
                                    match = re.search(r'filename\*?="?([^";]+)"?', disposition, flags=re.IGNORECASE)
                                    filename = match.group(1) if match else "copart_export.csv"
                                    save_path = export_dir / filename
                                    save_path.write_bytes(body)
                                    logger.info("Export CSV captured via confirmation response '{}' to {}", confirm_selector, save_path)
                                    return save_path.resolve()
                            except Exception:
                                continue
            except Exception:
                pass

            # Some page versions return CSV via XHR/fetch instead of browser download.
            try:
                async with page.expect_response(
                    lambda r: (
                        "csv" in (r.url or "").lower()
                        or "export" in (r.url or "").lower()
                        or "text/csv" in (r.headers.get("content-type", "").lower())
                        or "attachment" in (r.headers.get("content-disposition", "").lower())
                    ),
                    timeout=timeout,
                ) as response_info:
                    await page.locator(selector).first.click(timeout=timeout)

                response = await response_info.value
                body = await response.body()
                if body:
                    disposition = response.headers.get("content-disposition", "")
                    match = re.search(r'filename\*?="?([^";]+)"?', disposition, flags=re.IGNORECASE)
                    filename = match.group(1) if match else "copart_export.csv"
                    save_path = export_dir / filename
                    save_path.write_bytes(body)
                    logger.info("Export CSV captured from network response to {}", save_path)
                    return save_path.resolve()
            except Exception:
                pass

            # Some page versions expose a secondary "Download sales data" link.
            try:
                export_link = page.locator("a[href*='downloadSalesData'], a:has-text('Download sales data')").first
                href: str | None = None
                if await export_link.count() > 0 and await export_link.is_visible():
                    href = await export_link.get_attribute("href")
                    try:
                        async with page.expect_download(timeout=timeout) as download_info:
                            await export_link.click(timeout=timeout)
                        download = await download_info.value
                        filename = download.suggested_filename or "copart_export.csv"
                        save_path = export_dir / filename
                        await download.save_as(str(save_path))
                        logger.info("Export CSV downloaded from secondary link to {}", save_path)
                        return save_path.resolve()
                    except Exception:
                        # Continue to direct request fallback below.
                        pass

                if href:
                    direct_url = urljoin(page.url, href)
                    response = await page.context.request.get(direct_url, timeout=timeout)
                    if response.ok:
                        body = await response.body()
                        if body:
                            disposition = response.headers.get("content-disposition", "")
                            match = re.search(r'filename\*?="?([^";]+)"?', disposition, flags=re.IGNORECASE)
                            filename = match.group(1) if match else "copart_export.csv"
                            save_path = export_dir / filename
                            save_path.write_bytes(body)
                            logger.info("Export CSV fetched directly from {} to {}", direct_url, save_path)
                            return save_path.resolve()
            except Exception:
                pass

            # Some page versions render an intermediate export link after the first click.
            try:
                link = page.locator("a[href*='.csv'], a[href*='export']").first
                if await link.count() > 0 and await link.is_visible():
                    async with page.expect_download(timeout=timeout) as download_info:
                        await link.click(timeout=timeout)
                    download = await download_info.value
                    filename = download.suggested_filename or "copart_export.csv"
                    save_path = export_dir / filename
                    await download.save_as(str(save_path))
                    logger.info("Export CSV downloaded using follow-up link to {}", save_path)
                    return save_path.resolve()
            except Exception:
                pass
            logger.warning("Export click did not trigger a browser download event on page: {}", page.url)
            return None
        except Exception as exc:
            logger.warning("Failed to download export CSV with selector '{}': {}", selector, exc)
            return None

    async def search_by_vin(self, vin: str, timeout: int = 30000) -> list[Vehicle]:
        """Search for vehicles by VIN.

        Args:
            vin: Vehicle Identification Number.
            timeout: Page interaction timeout.

        Returns:
            A list of Vehicle instances matching the VIN search.
        """
        logger.info("Starting VIN search for {}", vin)
        page = await self._navigation.navigate_to_search(timeout=timeout)
        try:
            # Interact with search form (selectors are approximate)
            await page.fill(
                "input[name='vin'], input#vin, input[type='text']",
                vin,
            )
            await page.click("button[type='submit'], input[type='submit']")
            await page.wait_for_load_state("networkidle", timeout=timeout)
            vehicles = await self._parser.parse_search_results(page)
            logger.info("VIN search for {} returned {} results", vin, len(vehicles))
            return vehicles
        finally:
            await page.close()

    async def search_by_lot(self, lot_number: str, timeout: int = 30000) -> list[Vehicle]:
        """Search for a specific lot by lot number.

        Args:
            lot_number: The Copart lot identifier.
            timeout: Page interaction timeout.

        Returns:
            A list containing the matching vehicle (typically one result).
        """
        logger.info("Starting lot search for {}", lot_number)
        # Direct navigation to lot page is often faster than form search
        try:
            page = await self._navigation.navigate_to_vehicle(lot_number, timeout=timeout)
            vehicle = await self._parser.parse_vehicle_details(page)
            await page.close()
            if vehicle:
                return [vehicle]
            return []
        except NavigationError:
            logger.warning("Direct lot navigation failed; falling back to search form.")
            page = await self._navigation.navigate_to_search(timeout=timeout)
            try:
                await page.fill("input[name='lot'], input#lot", lot_number)
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=timeout)
                vehicles = await self._parser.parse_search_results(page)
                return vehicles
            finally:
                await page.close()

    async def search_by_make_model(
        self, make: str, model: str | None = None, year: int | None = None, timeout: int = 30000
    ) -> list[Vehicle]:
        """Search by make, optional model, and optional year.

        Args:
            make: Vehicle manufacturer.
            model: Optional model name.
            year: Optional model year.
            timeout: Page interaction timeout.

        Returns:
            A list of Vehicle instances matching the criteria.
        """
        query_value = make
        if model:
            query_value += f" {model}"
        if year:
            query_value += f" {year}"
        logger.info("Starting make/model search: {}", query_value)
        page = await self._navigation.navigate_to_search(timeout=timeout)
        try:
            # Attempt to fill general search or specific fields
            await page.fill("input[type='text'], input[name='search']", query_value)
            await page.click("button[type='submit']")
            await page.wait_for_load_state("networkidle", timeout=timeout)
            vehicles = await self._parser.parse_search_results(page)
            logger.info("Make/model search returned {} results", len(vehicles))
            return vehicles
        finally:
            await page.close()

    async def search_by_year(self, year: int, timeout: int = 30000) -> list[Vehicle]:
        """Search for vehicles by model year.

        Args:
            year: The model year to search for.
            timeout: Page interaction timeout.

        Returns:
            A list of Vehicle instances for the specified year.
        """
        return await self.search_by_make_model("", year=year, timeout=timeout)

    async def fetch_auction_data_from_url(
        self,
        auction_url: str,
        timeout: int = 45000,
        max_fallback_lots: int = 10,
        max_pages: int = 50,
    ) -> list[Vehicle]:
        """Open an auction lots page and extract vehicle data.

        The primary path parses list/search results directly. If that yields
        no complete records, this method falls back to opening lot detail
        links and parsing individual lot pages.
        """
        logger.info("Fetching auction data from {}", auction_url)
        page = await self._navigation.navigate_to_page(
            auction_url,
            timeout=timeout,
            wait_until="domcontentloaded",
        )

        vehicles: list[Vehicle] = []
        try:
            # Auction sale-list pages are heavily dynamic. Wait for rendered
            # result content before parsing.
            render_wait_ms = min(timeout, 20000)
            deadline = time.monotonic() + (render_wait_ms / 1000)
            while time.monotonic() < deadline:
                try:
                    lot_count = await page.locator("a[href*='/lot/']").count()
                    has_results_block = await page.locator(".search_result_component_container").count()
                    if lot_count > 0 or has_results_block > 0:
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(500)

            vehicles = await self._parser.parse_search_results(page)
            if vehicles:
                logger.info("Parsed {} vehicles directly from auction results page", len(vehicles))
                return vehicles

            async def collect_lot_links() -> list[str]:
                return await page.evaluate(
                    """() => {
                        const links = Array.from(document.querySelectorAll('a[href*="/lot/"]'));
                        const hrefs = links
                            .map((a) => a.getAttribute('href'))
                            .filter((href) => typeof href === 'string' && href.length > 0);
                        return Array.from(new Set(hrefs));
                    }"""
                )

            lot_links: list[str] = []
            seen_links: set[str] = set()

            def add_links(items: list[str]) -> None:
                for href in items:
                    if href not in seen_links:
                        seen_links.add(href)
                        lot_links.append(href)

            # Collect first page links.
            add_links(await collect_lot_links())

            # Traverse paginated result pages when paginator exists.
            paginator = page.locator(".p-paginator").first
            paginator_present = False
            try:
                paginator_present = await paginator.count() > 0
            except Exception:
                paginator_present = False

            if paginator_present:
                page_index = 1
                while page_index < max_pages:
                    next_button = page.locator("button.p-paginator-next[aria-label='Next Page']").first
                    try:
                        if await next_button.count() == 0:
                            break
                        class_name = (await next_button.get_attribute("class") or "").lower()
                        disabled_attr = await next_button.get_attribute("disabled")
                        if "p-disabled" in class_name or disabled_attr is not None:
                            break

                        before_marker = ""
                        active_page = page.locator(".p-paginator-page.p-highlight").first
                        if await active_page.count() > 0:
                            before_marker = (await active_page.inner_text()).strip()

                        await next_button.click(timeout=timeout)

                        # Wait for paginator or lot content to update.
                        wait_deadline = time.monotonic() + 12
                        while time.monotonic() < wait_deadline:
                            await page.wait_for_timeout(400)
                            updated = False
                            try:
                                current_active = page.locator(".p-paginator-page.p-highlight").first
                                if await current_active.count() > 0:
                                    current_marker = (await current_active.inner_text()).strip()
                                    if before_marker and current_marker != before_marker:
                                        updated = True
                            except Exception:
                                pass

                            if not updated:
                                latest_links = await collect_lot_links()
                                if any(link not in seen_links for link in latest_links):
                                    updated = True

                            if updated:
                                break

                        add_links(await collect_lot_links())
                        page_index += 1
                    except Exception as exc:
                        logger.warning("Pagination traversal stopped at page {}: {}", page_index, exc)
                        break

                logger.info("Collected {} unique lot links across {} page(s)", len(lot_links), page_index)

            # If result containers are not parseable, derive minimal vehicle
            # records directly from lot URLs as a resilient fallback.
            link_derived: list[Vehicle] = []
            seen_lots: set[str] = set()
            for href in lot_links:
                vehicle = self._vehicle_from_lot_href(href, page.url)
                if vehicle and vehicle.lot_number not in seen_lots:
                    seen_lots.add(vehicle.lot_number)
                    link_derived.append(vehicle)

            if link_derived:
                logger.info("Derived {} vehicles from lot links", len(link_derived))
                return link_derived

            parsed_fallback = 0
            for href in lot_links[:max_fallback_lots]:
                detail_url = urljoin(page.url, href)
                try:
                    detail_page = await self._navigation.navigate_to_page(
                        detail_url,
                        timeout=timeout,
                        wait_until="domcontentloaded",
                    )
                    vehicle = await self._parser.parse_vehicle_details(detail_page)
                    if vehicle:
                        vehicles.append(vehicle)
                        parsed_fallback += 1
                except Exception as exc:
                    logger.warning("Failed to parse lot detail {}: {}", detail_url, exc)
                finally:
                    try:
                        await detail_page.close()
                    except Exception:
                        pass

            logger.info(
                "Parsed {} vehicles from fallback lot detail pages (from {} links)",
                parsed_fallback,
                min(len(lot_links), max_fallback_lots),
            )
            return vehicles
        finally:
            await page.close()

    @staticmethod
    def _vehicle_from_lot_href(href: str, base_url: str) -> Vehicle | None:
        """Create a minimal Vehicle from a /lot/... URL when page fields are unavailable."""
        if not href:
            return None

        match = re.search(r"/lot/(\d+)/(.*)$", href)
        if not match:
            return None

        lot_number = match.group(1)
        slug = (match.group(2) or "").replace("-", " ").upper().strip()
        tokens = [t for t in slug.split() if t]

        year: int | None = None
        make: str | None = None
        model: str | None = None

        year_index = next((i for i, t in enumerate(tokens) if t.isdigit() and len(t) == 4), -1)
        if year_index >= 0:
            try:
                year = int(tokens[year_index])
            except ValueError:
                year = None
            if year_index + 1 < len(tokens):
                make = tokens[year_index + 1]
            if year_index + 2 < len(tokens):
                model = " ".join(tokens[year_index + 2 : year_index + 6])

        title_text = " ".join(tokens[:8]) if tokens else None
        detail_url = urljoin(base_url, href)

        return Vehicle(
            vin=f"UNKNOWN-{lot_number}",
            lot_number=lot_number,
            title_text=title_text,
            year=year,
            make=make,
            model=model,
            detail_url=detail_url,
        )
