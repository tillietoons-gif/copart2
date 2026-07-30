from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "https://www.copart.com/saleListResultAll/881/2026-07-29?location=*NCS%20-%20Central%20Region&saleDate=1785373200000&liveAuction=false&from=&yardNum=881&qId=762f7720-565d-488e-830f-8edfdd4d44f4-1785367340866"
STATE_PATH = Path("data/auth_state.json")


print({"state_exists": STATE_PATH.exists(), "state_path": str(STATE_PATH)})

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chromium", headless=True)
    context = browser.new_context(storage_state=str(STATE_PATH) if STATE_PATH.exists() else None)
    page = context.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(5000)

    result = page.evaluate(
        """() => {
            const byId = document.querySelector('#exportButton');
            const byClass = document.querySelector('.export-csv-button');
            const exportAnchors = [...document.querySelectorAll('a')].filter((a) =>
                /export/i.test((a.textContent || '').trim())
            );
            const exportButtons = [...document.querySelectorAll('button')].filter((b) =>
                /export/i.test((b.textContent || '').trim())
            );

            return {
                currentUrl: location.href,
                title: document.title,
                byIdFound: !!byId,
                byIdTag: byId ? byId.tagName : null,
                byIdHtml: byId ? byId.outerHTML.slice(0, 300) : null,
                byClassFound: !!byClass,
                byClassTag: byClass ? byClass.tagName : null,
                byClassHtml: byClass ? byClass.outerHTML.slice(0, 300) : null,
                exportAnchorCount: exportAnchors.length,
                exportButtonCount: exportButtons.length,
                hasLiteralExportButton: document.documentElement.outerHTML.includes("exportButton"),
                exportIdLikeCount: document.querySelectorAll("[id*='export' i]").length,
                exportClassLikeCount: document.querySelectorAll("[class*='export' i]").length,
            };
        }"""
    )
    print(result)

    context.close()
    browser.close()
