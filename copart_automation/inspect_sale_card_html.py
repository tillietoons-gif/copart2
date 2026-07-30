from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.copart.com/saleListResult/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881"
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(12000)

        result = await page.evaluate(
            """() => {
                const link = document.querySelector('a[href*="/lot/"]');
                if (!link) return { found: false };

                let node = link;
                let depth = 0;
                while (node && depth < 8) {
                    if (node.className && String(node.className).toLowerCase().includes('result')) break;
                    node = node.parentElement;
                    depth += 1;
                }
                const container = node || link.parentElement;
                return {
                    found: true,
                    href: link.getAttribute('href'),
                    text: (link.textContent || '').trim(),
                    containerClass: container ? container.className : null,
                    containerHtml: container ? container.outerHTML.slice(0, 5000) : null,
                };
            }"""
        )

        print(result)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
