# Copart Automation

A production-quality Python automation framework for interacting with a personal Copart account. This project respects Copart's authentication and security requirements and does not attempt to bypass anti-bot protections, evade security mechanisms, or interact with bidding systems automatically.

## Features

- **Authenticated browser automation** using Playwright (Chromium)
- **Session persistence** via Playwright storage state (reuse login across runs)
- **Multi-Factor Authentication (MFA) support** — pauses for user verification rather than attempting automatic bypass
- **Structured data extraction** using BeautifulSoup4 and Pydantic validation
- **Auction CSV export ingestion** via Copart sale-list Export button (`lotsearchExport`) for fast full-auction lot imports
- **SQLite database** for persistent storage of vehicles, searches, downloads, auction CSV exports, and raw CSV lot rows
- **Download management** with per-vehicle folder organization
- **Export support** (CSV, Excel, JSON)
- **Modular architecture** following SOLID principles and clean code practices
- **Comprehensive logging** with Loguru and automatic rotation
- **Retry logic** with exponential backoff for transient network errors (excludes authentication retries)
- **Type hints**, docstrings, and code quality enforcement (Black, Ruff, mypy)

## Project Structure

```
copart_automation/
├── app/
│   ├── browser.py       # Playwright browser manager
│   ├── auth.py          # Login, session persistence, MFA handling
│   ├── session.py       # Session lifecycle and verification
│   ├── navigation.py    # Reusable page navigation helpers
│   ├── search.py        # Search module (VIN, lot, make/model, year)
│   ├── calendar.py      # Auction calendar scraping
│   ├── auction_export.py # Auction lot CSV export download/parsing
│   ├── parser.py        # HTML parsing using BeautifulSoup4
│   ├── downloader.py    # File download management
│   ├── database.py      # SQLite CRUD and exports
│   ├── models.py        # Pydantic data models
│   ├── config.py        # Environment-based settings
│   ├── logger.py        # Centralized Loguru setup
│   ├── utils.py         # Utilities and retry decorators
│   └── exceptions.py    # Custom exception hierarchy
├── sql/
│   └── schema.sql       # SQLite schema definition
├── tests/
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_parser.py
│   ├── test_auction_export.py
│   ├── test_database.py
│   └── test_session.py
├── data/                # Database and auth state
├── downloads/           # Downloaded files
├── logs/                # Rotated log files
├── .env.example         # Example environment variables
├── requirements.txt     # Python dependencies
├── pyproject.toml       # Project metadata and tool configs
└── main.py              # Main entry point
```

## Installation

### Prerequisites

- Python 3.12 or higher
- Playwright browsers installed (`python -m playwright install chromium`)

### Setup

1. Clone or copy the repository.
2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # .venv\Scripts\activate  # Windows
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
4. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your Copart credentials and preferences
   ```

## Environment Setup

Copy `.env.example` to `.env` and configure:

| Variable | Description | Default |
|----------|-------------|---------|
| `COPART_EMAIL` | Copart account email | (empty) |
| `COPART_PASSWORD` | Copart account password | (empty) |
| `HEADLESS` | Run browser in headless mode | `true` |
| `BROWSER_CHANNEL` | Playwright browser channel | `chromium` |
| `DOWNLOAD_DIR` | Download directory path | `./downloads` |
| `DATABASE_PATH` | SQLite database file path | `./data/copart.db` |
| `STORAGE_STATE_PATH` | Auth session file path | `./data/auth_state.json` |
| `NAVIGATION_TIMEOUT` | Page navigation timeout (ms) | `30000` |
| `ACTION_TIMEOUT` | Action timeout (ms) | `10000` |
| `RETRY_MAX_ATTEMPTS` | Max retries for transient errors | `3` |
| `LOG_LEVEL` | Minimum log level | `INFO` |
| `LOG_FILE` | Log file path | `./logs/copart_automation.log` |

**Security Note:** Never commit `.env` to version control. The `.env` file contains sensitive credentials.

## Running the Application

### Basic Execution

```bash
python main.py
```

The main workflow:
1. Initializes a Playwright browser session.
2. Loads an existing authenticated session (if available) or performs login.
3. If additional verification (e.g., MFA) is required, pauses for user input.
4. Scrapes the auction calendar and opens each auction's lots page.
5. Clicks the Copart sale-list **Export** button when available and imports the downloaded lot CSV into SQLite.
6. Stores both normalized vehicle records and raw CSV rows (`auction_lot_exports` / `auction_lot_rows`) so columns not mapped to the `vehicles` table are still preserved.
7. Falls back to parsing individual lot pages if the CSV export is unavailable or incomplete.
8. Navigates to the dashboard and performs an example VIN search.
9. Parses results, saves to SQLite, and optionally downloads images.
10. Exports data and closes resources cleanly.

### Running with Custom Search

The `main.py` workflow uses demonstration values. For production use, modify `main.py` or import modules programmatically:

```python
import asyncio
from copart_automation.app.session import SessionManager
from copart_automation.app.navigation import NavigationHelper
from copart_automation.app.search import SearchModule

async def my_search():
    session = SessionManager()
    async with session:
        await session.verify_session(auto_reauth=True)
        nav = NavigationHelper(session.browser.get_context())
        search = SearchModule(nav)
        results = await search.search_by_vin("YOUR_VIN_HERE")
        print(f"Found {len(results)} results")

asyncio.run(my_search())
```

## Code Quality

This project uses automated code quality tools:

```bash
# Format code
black copart_automation/app copart_automation/main.py

# Lint and fix
ruff check --fix copart_automation/app

# Type check
mypy copart_automation/app

# Run tests
pytest --cov=copart_automation/app --cov-report=term-missing
```

## Design Decisions

- **Playwright over Selenium:** Playwright provides faster execution, better async support, and native download handling.
- **BeautifulSoup over pure CSS selectors:** BeautifulSoup is more resilient to small HTML changes and easier to maintain.
- **Pydantic for validation:** Data validation at the model layer prevents corrupt data from reaching the database.
- **Separate authentication retries:** Authentication is never retried automatically to avoid account lockout policies.
- **Session persistence:** Playwright storage state allows users to avoid repeated logins while maintaining security.
- **Async architecture:** All major operations use `async/await` to support concurrent downloads and non-blocking navigation.

## Updating Dependencies

```bash
pip install --upgrade -r requirements.txt
python -m playwright install chromium
```

## Troubleshooting

### Login Failures
- Verify `.env` contains the correct email and password.
- Check that `.env` is in the project root (same directory as `main.py` or `pyproject.toml`).
- If MFA is required, the application will pause. Complete the verification in the browser window and press Enter.
- Ensure the network can reach `https://www.copart.com`.

### Browser Launch Errors
- Run `python -m playwright install chromium`.
- Verify Python 3.12+ is installed.
- Check that `chromium` is available or try a different `BROWSER_CHANNEL`.

### Database Errors
- Check that `DATABASE_PATH` points to a writable directory.
- Verify the `sql/schema.sql` file exists and is readable.
- Delete the `.db` file to force a fresh schema creation if corruption is suspected.

### Download Failures
- Ensure the account has access to the file being downloaded (public links may require authentication).
- Check that `DOWNLOAD_DIR` exists and is writable.
- Verify that the file URL is accessible through the browser (some links may expire).

### Log Rotation Not Working
- Ensure `LOG_FILE` directory exists.
- Check disk permissions for the logs directory.
- Verify `LOG_ROTATION` and `LOG_RETENTION` values are valid for Loguru (e.g., `"10 MB"`, `"7 days"`).

## Example Usage

### Programmatic Search and Database Storage

```python
from copart_automation.app.session import SessionManager
from copart_automation.app.navigation import NavigationHelper
from copart_automation.app.search import SearchModule
from copart_automation.app.database import DatabaseModule

async def example():
    session = SessionManager()
    db = DatabaseModule()
    async with session:
        await session.verify_session(auto_reauth=True)
        nav = NavigationHelper(session.browser.get_context())
        search = SearchModule(nav)
        results = await search.search_by_make_model("Toyota", model="Camry")
        for vehicle in results:
            db.insert_vehicle(vehicle)
```

### Exporting Data

```python
db = DatabaseModule()
db.export_to_csv("exports/vehicles.csv")
db.export_to_excel("exports/vehicles.xlsx")
db.export_to_json("exports/vehicles.json")
```

## Security and Ethics

This project is designed for automation of a **personal Copart account**. It does not:
- Attempt to bypass CAPTCHA or anti-bot systems.
- Use proxy rotation or user-agent spoofing to evade detection.
- Automate bidding or auction interaction that could affect other users.
- Scrape data not available to the authenticated account.

If Copart implements additional security measures (such as stricter MFA or rate limits), the automation will pause or fail gracefully rather than attempting to circumvent them.

## License

MIT License. See `pyproject.toml` for details.

## Support and Contributions

Issues and feature requests should be directed to the repository issue tracker. Please include relevant log excerpts (`logs/copart_automation.log`) when reporting errors.
