from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from copart_automation.app.navigation import NavigationHelper
from copart_automation.app.search import SearchModule

URL = "https://www.copart.com/saleListResult/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881"
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)

        navigation = NavigationHelper(context)
        search = SearchModule(navigation)
        vehicles = await search.fetch_auction_data_from_url(URL, timeout=60000, max_fallback_lots=20)

        print({"count": len(vehicles)})
        for v in vehicles[:5]:
            print(
                {
                    "vin": v.vin,
                    "lot_number": v.lot_number,
                    "year": v.year,
                    "make": v.make,
                    "model": v.model,
                    "detail_url": str(v.detail_url) if v.detail_url else None,
                }
            )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
