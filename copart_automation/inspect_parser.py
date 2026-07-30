import asyncio
from bs4 import BeautifulSoup
from copart_automation.app.session import SessionManager
from copart_automation.app.calendar import AuctionCalendarParser

async def main():
    session = SessionManager()
    async with session:
        await session.verify_session(auto_reauth=True)
        context = session.browser.get_context()
        page = await context.new_page()
        await page.goto('https://www.copart.com/auctionCalendar', timeout=60000, wait_until='domcontentloaded')
        await asyncio.sleep(8)
        html = await page.content()
        soup = BeautifulSoup(html, 'lxml')
        tables = soup.select('table.auction-table') or soup.select('table')
        print('selected table count', len(tables))
        for idx, table in enumerate(tables[:5], start=1):
            print('TABLE', idx, 'class=', table.get('class'))
            for row_idx, row in enumerate(table.select('tr')[:8], start=1):
                cells = []
                for cell in row.select('td, th'):
                    text = ' '.join(cell.get_text(' ', strip=True).split())
                    cells.append(text)
                print(' row', row_idx, cells)
            print('---')
        parser = AuctionCalendarParser()
        entries = await parser.parse_calendar(page)
        print('PARSED ENTRIES', len(entries))
        for entry in entries[:10]:
            print(entry.model_dump())

asyncio.run(main())
