"""Auction calendar scraping for Copart automation.

This module scrapes the Copart auction calendar page after authentication
and converts calendar tables into structured entries for database storage.
It now also extracts the "View Lots" / auction detail link present as a
button inside each calendar row/cell.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Page

from copart_automation.app.logger import get_logger
from copart_automation.app.models import AuctionCalendarEntry

logger = get_logger(__name__)

BASE_URL = "https://www.copart.com"


class AuctionCalendarParser:
    """Parses Copart auction calendar pages into structured calendar entries."""

    async def parse_calendar(self, page: Page) -> list[AuctionCalendarEntry]:
        """Parse the auction calendar from the given page."""
        page_url = getattr(page, "url", "unknown")
        logger.info("Parsing auction calendar from {}", page_url)

        html_content = await page.content()
        if not html_content:
            logger.warning("Auction calendar page content was empty or did not load correctly.")
            return []

        # Give the page a brief moment to finish rendering dynamic content.
        # Copart often loads the calendar markup asynchronously and then updates it.
        try:
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        html_content = await page.content()
        soup = BeautifulSoup(html_content, "lxml")

        table_selectors = ["table.auction-table", "table"]
        tables = []
        for selector in table_selectors:
            tables.extend(soup.select(selector))
            if tables:
                break

        if not tables:
            logger.warning("No calendar tables were found in the auction calendar HTML.")
            return []

        entries: list[AuctionCalendarEntry] = []
        for table_index, table in enumerate(tables, start=1):
            entries.extend(self._parse_calendar_table(table, table_index))

        logger.info("Parsed {} auction calendar entries", len(entries))
        return entries

    def _parse_calendar_table(self, table: Any, table_index: int) -> list[AuctionCalendarEntry]:
        rows = table.select("tr")
        if not rows:
            return []

        header_row = rows[0]
        headers = self._extract_headers(header_row)

        # Heuristic to determine if this is a grid calendar (dates as columns)
        header_cells_for_check = header_row.select("td, th")
        grid_date_count = 0
        for txt_cell in header_cells_for_check:
            txt = self._clean_cell_text(txt_cell)
            if self._looks_like_date(txt):
                grid_date_count += 1
        # Also check parsed headers for ISO dates
        for h in headers:
            if re.match(r"\d{4}-\d{2}-\d{2}", h):
                grid_date_count += 1

        is_grid = grid_date_count >= 1 and len(headers) >= 1

        if is_grid:
            return self._parse_grid_table(table, table_index, headers, rows)
        else:
            return self._parse_list_table(table, table_index, rows)

    # ---------- Grid style (time in first col, dates as headers) ----------
    def _parse_grid_table(
        self, table: Any, table_index: int, headers: list[str], rows: list[Any]
    ) -> list[AuctionCalendarEntry]:
        entries: list[AuctionCalendarEntry] = []
        for row_index, row in enumerate(rows[1:], start=1):
            cells = row.select("td, th")
            if len(cells) < 2:
                continue

            auction_time = self._clean_cell_text(cells[0])
            if not self._looks_like_time(auction_time):
                # Skip rows that appear to be repeated header rows or non-event labels
                if self._looks_like_date(auction_time):
                    continue

            # Row-level fallback link (covers cases where button is outside date cell or in action column)
            row_lots_url, row_lots_text = self._extract_lots_link_data(row)

            for column_index, cell in enumerate(cells[1:], start=1):
                # Keep original description but allow link-only cells
                description = self._clean_cell_text(cell)
                cell_url, cell_text = self._extract_lots_link_data(cell)

                # If both description empty and no link, skip
                if not description and not cell_url:
                    continue

                # If description empty but link exists, use link text as description fallback
                if not description and cell_text:
                    description = cell_text

                event_date = headers[column_index - 1] if column_index - 1 < len(headers) else ""
                if not event_date:
                    # This column may be an action column beyond date headers.
                    # Skip creating an entry for it, but row_lots_url already captured.
                    continue

                # Skip columns whose header is not a date (e.g., Action, View Lots)
                if not self._is_date_header(event_date):
                    continue

                # Normalize date to YYYY-MM-DD if possible (headers already normalized usually)
                if not re.match(r"\d{4}-\d{2}-\d{2}", event_date):
                    normalized = self._normalize_date(event_date)
                    # Use normalized if it became ISO, otherwise keep original
                    if re.match(r"\d{4}-\d{2}-\d{2}", normalized):
                        event_date = normalized

                final_url = cell_url or row_lots_url
                final_text = cell_text or row_lots_text

                try:
                    entries.append(
                        AuctionCalendarEntry(
                            event_date=event_date,
                            auction_time=auction_time if self._looks_like_time(auction_time) else None,
                            description=description,
                            table_section=f"table-{table_index}",
                            row_index=row_index,
                            column_index=column_index,
                            lots_view_url=final_url,
                            lots_view_text=final_text,
                        )
                    )
                except Exception as exc:
                    logger.warning(f"Failed to create AuctionCalendarEntry: {exc}")
                    continue
        return entries

    # ---------- List style (each row is an auction) ----------
    def _parse_list_table(self, table: Any, table_index: int, rows: list[Any]) -> list[AuctionCalendarEntry]:
        entries: list[AuctionCalendarEntry] = []
        header_row = rows[0]
        header_cells = header_row.select("td, th")
        header_names = [self._clean_cell_text(c).lower() for c in header_cells]

        date_idx = None
        time_idx = None
        for i, name in enumerate(header_names):
            if "date" in name and date_idx is None:
                date_idx = i
            if "time" in name and time_idx is None:
                time_idx = i

        for row_index, row in enumerate(rows[1:], start=1):
            cells = row.select("td, th")
            if not cells or len(cells) < 2:
                continue

            lots_url, lots_text = self._extract_lots_link_data(row)

            event_date_raw = ""
            auction_time_raw = ""

            if date_idx is not None and date_idx < len(cells):
                event_date_raw = self._clean_cell_text(cells[date_idx])
            if time_idx is not None and time_idx < len(cells):
                auction_time_raw = self._clean_cell_text(cells[time_idx])

            # Fallback heuristics
            if not event_date_raw or not self._looks_like_date(event_date_raw):
                # Try to find date in any cell if current doesn't look like date
                for c in cells:
                    txt = self._clean_cell_text(c)
                    if self._looks_like_date(txt):
                        event_date_raw = txt
                        break

            if not auction_time_raw or not self._looks_like_time(auction_time_raw):
                for c in cells:
                    txt = self._clean_cell_text(c)
                    if self._looks_like_time(txt):
                        auction_time_raw = txt
                        break

            # Build description from cells excluding date/time indexes
            description_parts: list[str] = []
            for idx, c in enumerate(cells):
                if idx == date_idx or idx == time_idx:
                    continue
                txt = self._clean_cell_text(c)
                if not txt:
                    continue
                # If this cell is purely the lots link button, and its text matches lots_text, skip to avoid duplication
                # but keep if there is other info.
                if lots_text and txt == lots_text and len(cells) > 2:
                    # Check if cell contains only that link and no other descriptive text
                    # If description already has other parts, we can skip this pure button text
                    # We'll include it only if no other description yet
                    if description_parts:
                        continue
                description_parts.append(txt)

            description = " | ".join(description_parts) if description_parts else None

            if not event_date_raw:
                # Try extract date from description string as last resort
                if description:
                    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", description)
                    if m:
                        event_date_raw = m.group(0)

            if not event_date_raw:
                # If still no date, skip this row – model requires event_date
                continue

            normalized_date = self._normalize_date(event_date_raw)
            if not normalized_date:
                normalized_date = event_date_raw

            # Ensure ISO-like for consistency if possible
            if not re.match(r"\d{4}-\d{2}-\d{2}", normalized_date):
                # keep original if not convertible, but try to keep normalized version
                # _normalize_date returns original if not matching MM/DD/YYYY, so we allow it
                pass

            # Skip if still no meaningful date and no description? already handled
            try:
                entries.append(
                    AuctionCalendarEntry(
                        event_date=normalized_date,
                        auction_time=auction_time_raw
                        if self._looks_like_time(auction_time_raw)
                        else (auction_time_raw or None),
                        description=description,
                        table_section=f"table-{table_index}",
                        row_index=row_index,
                        column_index=None,
                        lots_view_url=lots_url,
                        lots_view_text=lots_text,
                    )
                )
            except Exception as exc:
                logger.warning(f"Failed to create AuctionCalendarEntry for list row {row_index}: {exc}")
                continue

        return entries

    # ---------- Link extraction ----------
    def _extract_lots_link_data(self, element: Any) -> tuple[str | None, str | None]:
        """Extract (url, link_text) for View Lots / auction link from an element.

        Scans <a href> first with scoring, then buttons with data attributes and onclick JS.
        """
        best_url: str | None = None
        best_text: str | None = None
        best_score: int = -1

        # 1) <a href> candidates
        for a in element.select("a[href]"):
            href = a.get("href")
            if not href:
                continue
            norm_url = self._normalize_url(href)
            if not norm_url:
                continue

            anchor_text = a.get_text(separator=" ", strip=True) or ""
            title_attr = (a.get("title") or "") + " " + (a.get("aria-label") or "")
            combined_text = f"{anchor_text} {title_attr}".strip()
            combined_lower = combined_text.lower()
            href_lower = f"{href} {norm_url}".lower()

            score = 0
            if "view" in combined_lower:
                score += 2
            if "lot" in combined_lower or "lots" in combined_lower:
                score += 3
            if "auction" in combined_lower:
                score += 2
            if "inventory" in combined_lower:
                score += 2
            if "sale" in combined_lower or "sales" in combined_lower:
                score += 1
            if "detail" in combined_lower:
                score += 1
            # strong phrase matches
            if "view lots" in combined_lower:
                score += 5
            if "view auction" in combined_lower or "view inventory" in combined_lower:
                score += 4
            if combined_lower.strip() in {"view", "view lots", "view auction", "lots"}:
                score += 3

            # href signals
            if "lot" in href_lower:
                score += 2
            if "auction" in href_lower or "sale" in href_lower:
                score += 2
            if "search" in href_lower or "inventory" in href_lower or "auctioncalendar" in href_lower:
                score += 1
            if any(p in href_lower for p in ["/lot/", "/search/", "/auction/", "/sale/", "copart.com"]):
                score += 2
            if "copart.com" in href_lower:
                score += 1

            # Prefer higher score; keep first encountered if tie with existing best being None
            if score > best_score:
                best_score = score
                best_url = norm_url
                best_text = combined_text if combined_text else anchor_text or None

        if best_url:
            # Even low-score links are valid if they are the only link; return best found
            return best_url, best_text

        # 2) Buttons and elements with data-* url attributes
        for btn in element.select("button, [data-url], [data-href], [data-link], [data-auction-url]"):
            for attr in ("data-url", "data-href", "data-link", "data-auction-url", "formaction", "value"):
                val = btn.get(attr)
                if val and isinstance(val, str):
                    norm = self._normalize_url(val)
                    if norm:
                        txt = btn.get_text(separator=" ", strip=True) or None
                        return norm, txt

            onclick = btn.get("onclick") or btn.get("data-onclick") or ""
            if onclick and isinstance(onclick, str):
                extracted = self._extract_url_from_js(onclick)
                if extracted:
                    txt = btn.get_text(separator=" ", strip=True) or None
                    return extracted, txt

        # 3) Any element with onclick
        for el in element.select("[onclick]"):
            onclick = el.get("onclick") or ""
            if isinstance(onclick, str):
                extracted = self._extract_url_from_js(onclick)
                if extracted:
                    txt = el.get_text(separator=" ", strip=True) or None
                    return extracted, txt

        return None, None

    @staticmethod
    def _extract_url_from_js(js_code: str) -> str | None:
        """Try to pull a URL out of an onclick JavaScript snippet."""
        if not js_code:
            return None
        # Look for quoted strings that contain auction/lot/search/sale keywords
        pattern = re.compile(
            r"""['\"](?P<url>(?:https?://[^'\"\s]+)|(?:/[^'\"\s]*?(?:auction|lot|search|sale|inventory)[^'\"\s]*))['\"]""",
            re.I,
        )
        m = pattern.search(js_code)
        if m:
            cand = m.group("url")
            norm = AuctionCalendarParser._normalize_url(cand)
            if norm:
                return norm

        # Generic absolute URL
        generic = re.search(r"(https?://[^\s'\"\)]+)", js_code)
        if generic:
            norm = AuctionCalendarParser._normalize_url(generic.group(1))
            if norm:
                return norm

        # Relative URL with key terms
        rel = re.search(r"(/[A-Za-z0-9/_\-\.?=&%]+)", js_code)
        if rel:
            cand = rel.group(1)
            if len(cand) > 2 and any(k in cand.lower() for k in ["auction", "lot", "search", "sale", "inventory"]):
                norm = AuctionCalendarParser._normalize_url(cand)
                if norm:
                    return norm

        return None

    @staticmethod
    def _normalize_url(href: str) -> str | None:
        if not href or not isinstance(href, str):
            return None
        href = href.strip()
        if not href:
            return None
        lowered = href.lower()
        if href.startswith("#") or lowered.startswith("javascript:"):
            return None
        if lowered.startswith("mailto:") or lowered.startswith("tel:"):
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return f"{BASE_URL}{href}"
        # Relative path like auctionCalendar/... or lot/123
        # Heuristic: if looks like a path or query, prepend base
        if re.match(r"^[a-zA-Z0-9_\-/.?=&%]+$", href) and ("/" in href or "?" in href or "=" in href):
            if not href.startswith("/"):
                href = "/" + href
            return f"{BASE_URL}{href}"
        return None

    def _extract_headers(self, header_row: Any) -> list[str]:
        headers: list[str] = []
        cells = header_row.select("td, th")[1:]
        for cell in cells:
            text = self._clean_cell_text(cell)
            date_value = self._normalize_date(text)
            if date_value:
                headers.append(date_value)
            elif text:
                headers.append(text)
        return headers

    @staticmethod
    def _clean_cell_text(cell: Any) -> str:
        if not cell:
            return ""
        text = cell.get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_date(text: str) -> str:
        if not text:
            return ""
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if match:
            month, day, year = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return text

    @staticmethod
    def _looks_like_time(text: str) -> bool:
        return bool(re.search(r"\b\d{1,2}:\d{2}\s*(AM|PM|am|pm)\b", text))

    @staticmethod
    def _looks_like_date(text: str) -> bool:
        return bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", text))

    @staticmethod
    def _is_date_header(text: str) -> bool:
        """Check if a header string represents a date column."""
        if not text:
            return False
        text = text.strip()
        # ISO date
        if re.match(r"\d{4}-\d{2}-\d{2}", text):
            return True
        # MM/DD/YYYY
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", text):
            return True
        # Month name based date e.g., December 31, 2025 or Mar 15
        if re.search(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
            text,
            re.I,
        ):
            if re.search(r"\d{1,2}|\d{4}", text):
                return True
        lowered = text.lower()
        if lowered in {
            "action",
            "actions",
            "view",
            "view lots",
            "lots",
            "inventory",
            "sale",
            "status",
            "sale name",
            "location",
        }:
            return False
        if any(k in lowered for k in ["action", "view lots", "view auction", "view inventory"]):
            if not re.search(r"\d", text):
                return False
        return False
