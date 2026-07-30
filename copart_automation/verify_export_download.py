from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from copart_automation.app.search import SearchModule

URLS = [
    "https://www.copart.com/saleListResult/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881",
    "https://www.copart.com/saleListResultAll/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881&qId=762f7720-565d-488e-830f-8edfdd4d44f4-1785367340866",
]
STATE_PATH = Path("data/auth_state.json")


async def main() -> None:
    if not STATE_PATH.exists():
        print({"ok": False, "reason": "missing_storage_state", "path": str(STATE_PATH)})
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        context = await browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
        page = await context.new_page()

        for url in URLS:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(5000)

            export_anchor = await page.evaluate(
                """() => {
                    const el = document.querySelector('a#exportButton.search-export-btn[data-uname="lotsearchExport"], a#exportButton, a.search-export-btn[data-uname="lotsearchExport"]');
                    if (!el) return null;
                    return {
                        tag: el.tagName,
                        id: el.getAttribute('id'),
                        className: el.getAttribute('class'),
                        dataUname: el.getAttribute('data-uname'),
                        ngCsv: el.getAttribute('ng-csv'),
                        filename: el.getAttribute('filename'),
                        text: (el.textContent || '').trim(),
                    };
                }"""
            )
            print({"url": page.url, "export_anchor": export_anchor})

        before = await page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('button, a, [id], [class]')];
                const items = [];
                for (const n of nodes) {
                    const text = (n.textContent || '').trim();
                    const cls = n.getAttribute('class') || '';
                    const id = n.getAttribute('id') || '';
                    if (/export/i.test(text) || /export/i.test(cls) || /export/i.test(id)) {
                        items.push({
                            tag: n.tagName,
                            id,
                            className: cls,
                            text: text.slice(0, 120),
                            href: n.getAttribute('href'),
                            onclick: n.getAttribute('onclick'),
                        });
                    }
                }
                return { count: items.length, items: items.slice(0, 20) };
            }"""
        )
        print({"before_export_nodes": before})

        search = SearchModule(None)  # type: ignore[arg-type]
        download_path = await search.click_export_button(page, timeout=25000)

        after = await page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('button, a, [id], [class]')];
                const items = [];
                for (const n of nodes) {
                    const text = (n.textContent || '').trim();
                    const cls = n.getAttribute('class') || '';
                    const id = n.getAttribute('id') || '';
                    if (/export/i.test(text) || /export/i.test(cls) || /export/i.test(id)) {
                        items.push({
                            tag: n.tagName,
                            id,
                            className: cls,
                            text: text.slice(0, 120),
                            href: n.getAttribute('href'),
                            onclick: n.getAttribute('onclick'),
                        });
                    }
                }
                return {
                    count: items.length,
                    items: items.slice(0, 20),
                    title: document.title,
                    url: location.href,
                };
            }"""
        )

        print(
            {
                "ok": bool(download_path),
                "url": page.url,
                "download_path": str(download_path) if download_path else None,
                "exists": download_path.exists() if download_path else False,
                "after_export_nodes": after,
            }
        )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
