from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://www.copart.com/saleListResultAll/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881&qId=762f7720-565d-488e-830f-8edfdd4d44f4-1785367340866"
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
        page = await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        # Click visible export button
        btn = page.locator("button.export-csv-button:has-text('Export'), button:has-text('Export'), a:has-text('Export')").first
        print({"btn_count": await btn.count(), "visible": await btn.is_visible() if await btn.count() else False})
        if await btn.count() > 0:
            await btn.click(timeout=15000)
            await page.wait_for_timeout(4000)

        info = await page.evaluate(
            """() => {
                const dialogs = [...document.querySelectorAll('[role="dialog"], .modal, .p-dialog, .overlay, .popup, .popover')];
                const candidates = [...document.querySelectorAll('button, a, input[type="button"], input[type="submit"]')]
                    .filter((n) => /download|csv|export|ok|submit|confirm/i.test((n.textContent || n.value || '').trim()))
                    .map((n) => ({
                        tag: n.tagName,
                        text: (n.textContent || n.value || '').trim().slice(0, 100),
                        id: n.id || null,
                        className: n.className || null,
                        href: n.getAttribute('href'),
                        onclick: n.getAttribute('onclick'),
                    }));
                return {
                    url: location.href,
                    title: document.title,
                    dialogCount: dialogs.length,
                    candidateActions: candidates.slice(0, 40),
                };
            }"""
        )

        print(info)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
