from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

SALE_URL = "https://www.copart.com/saleListResult/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881"
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
        page = await context.new_page()
        await page.goto(SALE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(12000)

        lot_href = await page.evaluate(
            """() => {
                const a = document.querySelector('a[href*="/lot/"]');
                return a ? a.getAttribute('href') : null;
            }"""
        )
        if not lot_href:
            print({"error": "no lot link found"})
            await context.close()
            await browser.close()
            return

        detail_url = urljoin(page.url, lot_href)
        detail = await context.new_page()
        await detail.goto(detail_url, wait_until="domcontentloaded", timeout=90000)
        await detail.wait_for_timeout(8000)

        html = await detail.content()
        soup = BeautifulSoup(html, "lxml")

        text = soup.get_text(" ", strip=True)
        vin_match = re.search(r"[A-HJ-NPR-Z0-9]{17}", text)
        lot_match = re.search(r"\b\d{8}\b", text)

        candidates = []
        for sel in [
            "[data-uname*='vin' i]",
            "[id*='vin' i]",
            "[class*='vin' i]",
            "[data-uname*='lot' i]",
            "[id*='lot' i]",
            "[class*='lot' i]",
            "span", "div", "td",
        ]:
            els = soup.select(sel)
            if els:
                sample = []
                for e in els[:5]:
                    sample.append((e.get_text(' ', strip=True) or '')[:120])
                candidates.append({"selector": sel, "count": len(els), "sample": sample})

        print(
            {
                "detail_url": detail.url,
                "title": await detail.title(),
                "has_signin": "sign in" in text.lower(),
                "vin_regex": vin_match.group(0) if vin_match else None,
                "lot8_regex": lot_match.group(0) if lot_match else None,
                "candidates": candidates[:8],
            }
        )

        await detail.close()
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
