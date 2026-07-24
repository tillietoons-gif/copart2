"""Main entry point for the Copart automation project.

Design decisions:
- The entry point uses an async context manager (`SessionManager`) to
  ensure that browser and session resources are always cleaned up,
  even when exceptions occur.
- The workflow demonstrates a safe pattern: initialize session,
  verify authentication, perform a search, parse results, and optionally
  download images. It does not attempt to interact with auction bidding.
- A short delay is included between major steps to respect server
  capacity and reduce the risk of rate limiting.
- MFA and additional verification are handled by pausing the workflow
  and prompting the user, rather than attempting automatic bypasses.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure the project root is available for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from copart_automation.app.auction_export import AuctionExportManager
from copart_automation.app.auth import AuthManager
from copart_automation.app.browser import BrowserManager
from copart_automation.app.config import settings
from copart_automation.app.database import DatabaseModule
from copart_automation.app.downloader import DownloadManager
from copart_automation.app.logger import get_logger, setup_logging
from copart_automation.app.navigation import NavigationHelper
from copart_automation.app.search import SearchModule
from copart_automation.app.session import SessionManager
from copart_automation.app.calendar import AuctionCalendarParser
from copart_automation.app.models import AuctionCalendarEntry
from copart_automation.app.parser import VehicleParser

logger = get_logger(__name__)


async def run_automation_workflow() -> int:
    """Execute the standard automation workflow.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    setup_logging()
    logger.info("Starting Copart automation workflow")
    logger.info("Configuration: headless={}, db={}, download_dir={}", settings.headless, settings.database_path, settings.download_dir)

    session_manager = SessionManager()
    try:
        async with session_manager:
            # Initialize and verify session
            logger.info("Initializing session...")
            await session_manager.verify_session(auto_reauth=True)

            # Create navigation helper using active session
            context = session_manager.browser.get_context()
            if not context:
                logger.error("No active browser context after session initialization.")
                return 1

            navigation = NavigationHelper(context)
            search_module = SearchModule(navigation)
            download_manager = DownloadManager(session_manager.browser)
            db = DatabaseModule()
            auction_parser = AuctionCalendarParser()
            auction_export_manager = AuctionExportManager()
            vehicle_parser = VehicleParser()

            # Scrape the auction calendar immediately after login
            logger.info("Navigating to auction calendar page...")
            calendar_page = await navigation.navigate_to_auction_calendar()
            try:
                calendar_entries = await auction_parser.parse_calendar(calendar_page)
                logger.info("Auction calendar parse returned {} entries", len(calendar_entries))
                    # Normalize and deduplicate calendar entries (merge lots_view_url when available)
                normalized: dict[tuple[str, str, str], AuctionCalendarEntry] = {}
                for e in calendar_entries:
                    key = (
                        (e.event_date or "").strip(),
                        ((e.auction_time or "") if e.auction_time is not None else "").strip(),
                        ((e.description or "") if e.description is not None else "").strip().lower(),
                    )
                    existing = normalized.get(key)
                    if not existing:
                        normalized[key] = e
                    else:
                        # Prefer a non-empty lots_view_url
                        if not existing.lots_view_url and e.lots_view_url:
                            existing.lots_view_url = e.lots_view_url
                        if not existing.lots_view_text and e.lots_view_text:
                            existing.lots_view_text = e.lots_view_text
                calendar_entries = list(normalized.values())
                logger.info("Deduplicated auction calendar entries to {} unique entries", len(calendar_entries))
                for entry in calendar_entries:
                    db.insert_auction_calendar_entry(entry)
                # After inserting calendar entries, iterate through auctions that have a lots view URL
                auction_entries = db.get_auction_calendar_entries()
                logger.info("Found {} auction entries with lots URLs to scrape.", len(auction_entries))
                for auction in auction_entries:
                    if not auction.lots_view_url:
                        continue
                    try:
                        logger.info("Opening lots view for auction: {} -> {}", auction.description, auction.lots_view_url)
                        lots_page = await navigation.navigate_to_page(auction.lots_view_url, timeout=settings.navigation_timeout)
                        try:
                            # Fast path: Copart sale-list pages include an Export button
                            # (id="exportButton", data-uname="lotsearchExport") that downloads
                            # a CSV containing all lots in the auction.  Prefer this over visiting
                            # every lot detail page individually.
                            csv_path = await auction_export_manager.download_lots_csv(
                                lots_page,
                                timeout=settings.navigation_timeout,
                            )
                            if csv_path:
                                auction_url = str(auction.lots_view_url)
                                auction_id = db.get_auction_id_by_lots_url(auction_url)
                                csv_rows = auction_export_manager.read_csv_rows(csv_path)
                                csv_vehicles = auction_export_manager.parse_csv_file(
                                    csv_path,
                                    source_url=auction_url,
                                )
                                export_id = db.insert_auction_lot_export(
                                    source_url=auction_url,
                                    csv_file_path=csv_path,
                                    row_count=len(csv_rows),
                                    imported_vehicle_count=len(csv_vehicles),
                                )
                                if export_id:
                                    for row in csv_rows:
                                        lot_number, vin = auction_export_manager.extract_row_identifiers(row)
                                        db.insert_auction_lot_row(
                                            export_id,
                                            row,
                                            lot_number=lot_number,
                                            vin=vin,
                                            auction_id=auction_id,
                                        )
                                elif csv_rows:
                                    logger.warning(
                                        "Could not record CSV export metadata for {}; raw rows not stored.",
                                        csv_path,
                                    )
                                if csv_vehicles:
                                    for vehicle in csv_vehicles:
                                        db.insert_vehicle(
                                            vehicle,
                                            auction_id=auction_id,
                                            source_auction_url=auction_url,
                                            source_csv_path=csv_path,
                                        )
                                    logger.info(
                                        "Imported {} lots for auction {} from CSV export {}",
                                        len(csv_vehicles),
                                        auction.description,
                                        csv_path,
                                    )
                                    continue
                                logger.info(
                                    "CSV export {} did not yield complete vehicle rows; falling back to lot pages.",
                                    csv_path,
                                )
                            else:
                                logger.info("CSV export unavailable; falling back to lot page parsing.")

                            lot_urls = await vehicle_parser.parse_lots_list(lots_page)
                            logger.info("Found {} lots for auction {}", len(lot_urls), auction.description)
                            # Visit each lot URL and parse details concurrently (bounded)
                            semaphore = asyncio.Semaphore(settings.lot_concurrency)
                            tasks = []

                            async def process_lot(lot_url: str) -> None:
                                async with semaphore:
                                    # Ensure record exists and mark in-progress
                                    db.ensure_lot_record(auction.lots_view_url if auction.lots_view_url else None, lot_url)
                                    db.update_lot_status(lot_url, "in_progress")
                                    try:
                                        lot_page = await navigation.navigate_to_page(lot_url, timeout=settings.navigation_timeout)
                                        try:
                                            vehicle = await vehicle_parser.parse_vehicle_details(lot_page)
                                            if vehicle:
                                                db.insert_vehicle(vehicle)
                                            db.update_lot_status(lot_url, "done")
                                        finally:
                                            await lot_page.close()
                                    except Exception as exc:
                                        logger.warning("Failed to parse lot {}: {}", lot_url, exc)
                                        db.update_lot_status(lot_url, "failed", error=str(exc))

                            for lot_url in lot_urls:
                                tasks.append(asyncio.create_task(process_lot(lot_url)))

                            # Wait for all lot tasks to complete for this auction
                            if tasks:
                                await asyncio.gather(*tasks)
                        finally:
                            await lots_page.close()
                    except Exception as exc:
                        logger.warning("Failed to open lots view {}: {}", auction.lots_view_url, exc)
            finally:
                await calendar_page.close()

            # Example: navigate to dashboard to confirm session
            logger.info("Navigating to dashboard for confirmation...")
            page = await navigation.navigate_to_dashboard()
            await page.close()

            # Example: perform a search (use safe test values or user-configured values)
            # Note: This does not attempt to interact with bidding systems.
            # Real searches should be configured by the user via environment
            # or additional command-line arguments.
            test_vin = "5GZCZ43D13S812715"  # Example VIN for demonstration only
            logger.info("Performing example VIN search (demonstration): {}", test_vin)
            try:
                vehicles = await search_module.search_by_vin(test_vin)
                logger.info("Search returned {} vehicle(s)", len(vehicles))
                for vehicle in vehicles:
                    logger.info(
                        "Vehicle: {} | {} {} | Lot: {} | Bid: {}",
                        vehicle.vin,
                        vehicle.year,
                        vehicle.make,
                        vehicle.lot_number,
                        vehicle.current_bid,
                    )
                    # Persist to database
                    vehicle_id = db.insert_vehicle(vehicle)
                    # Optionally download images (limited for demonstration)
                    if vehicle.image_urls:
                        logger.info("Attempting to download images for lot {}", vehicle.lot_number)
                        try:
                            records = await download_manager.download_vehicle_images(vehicle, max_images=2)
                            for record in records:
                                # Update with actual database vehicle ID
                                if vehicle_id:
                                    record.vehicle_id = vehicle_id
                                db.insert_download(record)
                            logger.info("Downloaded {} images.", len(records))
                        except Exception as exc:
                            logger.warning(f"Image download failed (not blocking): {exc}")
            except Exception as exc:
                logger.warning(f"Search or parsing error (continuing): {exc}")

            # Example: export existing database to CSV
            try:
                export_path = settings.download_dir / "exports" / "vehicles.csv"
                db.export_to_csv(export_path)
            except Exception as exc:
                logger.warning(f"Export failed (not blocking): {exc}")

            # Graceful delay before closing to allow any pending downloads to finalize
            logger.info("Workflow complete. Waiting briefly for pending operations...")
            await asyncio.sleep(2)

        logger.info("Automation workflow completed successfully.")
        return 0
    except Exception as exc:
        logger.critical("Automation workflow failed: {}", exc, exc_info=True)
        return 1


def main() -> int:
    """Synchronous entry point that runs the async workflow.

    This allows the application to be called from standard Python scripts,
    command-line interfaces, or schedulers without requiring the caller
    to manage the event loop.
    """
    try:
        exit_code = asyncio.run(run_automation_workflow())
    except KeyboardInterrupt:
        logger.info("Workflow interrupted by user (SIGINT).")
        exit_code = 130
    except Exception as exc:
        logger.critical("Unhandled exception in main: {}", exc, exc_info=True)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
