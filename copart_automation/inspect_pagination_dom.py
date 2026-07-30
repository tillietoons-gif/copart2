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
        await page.wait_for_timeout(10000)

        info = await page.evaluate(
            """() => {
                const pagerSelectors = [
                    '.p-paginator', '.p-paginator-next', '.p-paginator-page',
                    '[aria-label="Next Page"]', 'button[aria-label*="Next" i]',
                    'a[aria-label*="Next" i]'
                ];
                const out = {};
                for (const s of pagerSelectors) {
                    const nodes = Array.from(document.querySelectorAll(s));
                    out[s] = {
                        count: nodes.length,
                        sample: nodes.slice(0, 5).map((n) => ({
                            tag: n.tagName,
                            className: n.className,
                            text: (n.textContent || '').trim(),
                            aria: n.getAttribute('aria-label'),
                            disabled: n.getAttribute('disabled'),
                        })),
                    };
                }
                const links = Array.from(document.querySelectorAll('a[href*="/lot/"]')).map(a => a.getAttribute('href')).filter(Boolean);
                return {
                    url: location.href,
                    title: document.title,
                    lotCount: new Set(links).size,
                    pager: out,
                };
            }"""
        )

        print(info)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
