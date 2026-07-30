from __future__ import annotations

import asyncio
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

URL = "https://www.copart.com/saleListResult/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881"
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    if not STATE_PATH.exists():
        print({"error": "missing auth state", "path": str(STATE_PATH)})
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(10000)

        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        lot_links = [a.get("href") for a in soup.select("a[href*='/lot/']") if a.get("href")]
        unique_lot_links = list(dict.fromkeys(lot_links))

        classes = []
        for tag in soup.select("[class]"):
            cls = " ".join(tag.get("class", []))
            if any(k in cls.lower() for k in ["lot", "vehicle", "search", "result", "card", "row"]):
                classes.append(cls)
        classes = list(dict.fromkeys(classes))

        text_checks = {
            "contains_saved_searches": "saved searches" in html.lower(),
            "contains_sign_in_to_save": "sign in to save your searches" in html.lower(),
            "contains_lot_path": "/lot/" in html.lower(),
            "contains_search_result_component": "search_result_component_container" in html,
            "contains_vehicle_title": "vehicle title" in html.lower(),
        }

        print({
            "url": page.url,
            "title": await page.title(),
            "html_len": len(html),
            "lot_link_count": len(unique_lot_links),
            "lot_link_sample": unique_lot_links[:10],
            "text_checks": text_checks,
            "class_samples": classes[:40],
        })

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
