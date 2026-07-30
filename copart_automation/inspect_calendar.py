import asyncio
import re
from bs4 import BeautifulSoup
from copart_automation.app.session import SessionManager

async def main():
    session = SessionManager()
    async with session:
        await session.verify_session(auto_reauth=True)
        context = session.browser.get_context()
        if not context:
            print('no context')
            return
        page = await context.new_page()
        await page.goto('https://www.copart.com/auctionCalendar', timeout=60000, wait_until='domcontentloaded')
        await asyncio.sleep(8)
        html = await page.content()
        soup = BeautifulSoup(html, 'lxml')
        print('URL:', page.url)
        print('TITLE:', await page.title())
        print('TABLE COUNT:', len(soup.select('table')))
        print('AUCTION TABLE COUNT:', len(soup.select('table.auction-table')))
        print('TR COUNT:', len(soup.select('tr')))
        print('TD COUNT:', len(soup.select('td')))
        print('TH COUNT:', len(soup.select('th')))
        print('BODY TEXT SNIPPET:')
        body_text = await page.locator('body').inner_text()
        print(body_text[:12000])
        print('--- TABLE SNIPPETS ---')
        for idx, table in enumerate(soup.select('table')[:5], start=1):
            print(idx, '=>', str(table)[:800])
            print('---')

asyncio.run(main())
