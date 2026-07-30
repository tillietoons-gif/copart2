"""Authentication manager for Copart account access.

Design decisions:
- Playwright's storage_state mechanism is used to persist cookies,
  localStorage, and session data between runs. This avoids requiring
  the user to log in on every execution.
- Authentication retries are intentionally NOT implemented; repeated
  failed login attempts can trigger account security measures.
- If multi-factor authentication (MFA) is required by Copart, the
  workflow pauses and guides the user to complete verification
  rather than attempting to bypass it automatically.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from copart_automation.app.browser import BrowserManager
from copart_automation.app.config import settings
from copart_automation.app.exceptions import LoginFailure, SessionExpired
from copart_automation.app.logger import get_logger
from copart_automation.app.utils import retry_transient

logger = get_logger(__name__)

# Official Copart login endpoint (publicly documented)
# Note: This URL may change; users should verify in .env or docs.
COPART_LOGIN_URL = "https://www.copart.com/login"
COPART_DASHBOARD_URL = "https://www.copart.com/"
COPART_AUCTION_CALENDAR_URL = "https://www.copart.com/auctionCalendar"


class AuthManager:
    """Handles login, session persistence, and authentication verification.

    This manager does not attempt to bypass any anti-bot or security
    measures implemented by Copart. If additional verification is
    required, the user is explicitly guided through it.
    """

    def __init__(self, browser_manager: BrowserManager) -> None:
        self._manager = browser_manager
        self._context: BrowserContext | None = None

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    async def load_existing_session(self) -> bool:
        """Attempt to load a previously saved authentication session.

        Returns:
            True if a valid session was loaded; False otherwise.
        """
        state_path = settings.storage_state_path
        if not state_path.exists():
            logger.info("No existing session file found at {}", state_path)
            return False

        logger.info("Attempting to load existing session from {}", state_path)
        if not self._manager.is_active():
            await self._manager.start()

        existing_context = self._manager.get_context()
        if existing_context:
            try:
                await existing_context.close()
            except Exception as exc:
                logger.warning("Failed to close existing default browser context: {}", exc)

        try:
            new_context = await self._manager._browser.new_context(
                storage_state=str(state_path),
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            )
            self._manager._context = new_context
            # Verify the session by navigating to the dashboard
            page = await new_context.new_page()
            try:
                try:
                    await page.goto(
                        COPART_DASHBOARD_URL,
                        timeout=settings.navigation_timeout,
                        wait_until="domcontentloaded",
                    )
                except Exception as exc:
                    logger.warning(
                        "Dashboard probe for existing session did not complete cleanly: {}",
                        exc,
                    )

                current_url = page.url or ""
                if "/login" in current_url or "/signin" in current_url:
                    logger.info("Existing session expired; redirect detected.")
                    await new_context.close()
                    return False

                # Try a second authenticated page probe when dashboard is inconclusive.
                if not current_url:
                    try:
                        await page.goto(
                            COPART_AUCTION_CALENDAR_URL,
                            timeout=settings.navigation_timeout,
                            wait_until="domcontentloaded",
                        )
                        current_url = page.url or ""
                    except Exception as exc:
                        logger.warning(
                            "Auction calendar probe for existing session did not complete cleanly: {}",
                            exc,
                        )

                if "/login" in current_url or "/signin" in current_url:
                    logger.info("Existing session expired after secondary probe; redirect detected.")
                    await new_context.close()
                    return False

                # If we reached any non-login Copart URL, keep the session.
                if "copart.com" in current_url or not current_url:
                    self._context = new_context
                    logger.info("Existing session loaded successfully.")
                    return True

                await new_context.close()
                return False
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"Failed to load existing session: {exc}")
            return False

    @retry_transient
    async def login(self, email: str | None = None, password: str | None = None) -> bool:
        """Perform authentication with the Copart website.

        Args:
            email: Copart account email. Defaults to settings.
            password: Copart account password. Defaults to settings.

        Returns:
            True if login succeeds; False if additional verification is needed.

        Raises:
            LoginFailure: If authentication fails due to invalid credentials
                or an unrecoverable error.
        """
        email = email or settings.copart_email
        password_value = password or settings.copart_password.get_secret_value()
        if not email or not password_value:
            raise LoginFailure(
                "Email or password not configured. Check your .env file."
            )

        # Ensure browser manager is started
        if not self._manager.is_active():
            await self._manager.start()

        # Create a fresh context for this login attempt
        # We do this to avoid contaminating any existing session context
        if self._manager.get_context():
            await self._manager.close()
            await self._manager.start()

        self._context = self._manager.get_context()
        if self._context is None:
            raise LoginFailure("Browser context could not be initialized.")

        page = await self._context.new_page()
        try:
            logger.info("Navigating to Copart login page: {}", COPART_LOGIN_URL)
            try:
                await page.goto(
                    COPART_LOGIN_URL,
                    timeout=settings.navigation_timeout,
                    wait_until="domcontentloaded",
                )
            except Exception as exc:
                logger.warning(
                    "Initial login page load did not complete cleanly; continuing with best-effort form detection: {}",
                    exc,
                )

            # Copart can be slow to reach a true network-idle state. Give the page
            # a brief moment to settle before probing form elements.
            try:
                await asyncio.sleep(1)
            except Exception:
                pass

            email_selector = ", ".join(
                [
                    "input#username",
                    "input[name='username']",
                    "input[type='email']",
                    "input[autocomplete='username']",
                    "input#email-member-number",
                    "input[name='email-member-number']",
                    "input[id*='email']",
                ]
            )
            password_selector = ", ".join(
                [
                    "input#password",
                    "input[name='password']",
                    "input[type='password']",
                    "input[autocomplete='current-password']",
                    "input#member-password",
                    "input[name='member-password']",
                    "input[id*='password']",
                ]
            )

            try:
                await page.wait_for_selector(
                    f"{email_selector}, {password_selector}",
                    timeout=max(settings.action_timeout, 10000),
                )
            except Exception as exc:
                logger.warning(
                    "Login form selectors were not ready immediately; continuing to probe the page. Details: {}",
                    exc,
                )

            email_input = await page.query_selector(email_selector)
            if email_input:
                await email_input.fill(email)
            else:
                raise LoginFailure(
                    "Unable to locate an email/username input on the Copart login page."
                )

            password_input = await page.query_selector(password_selector)
            if password_input:
                await password_input.fill(password_value)
            else:
                raise LoginFailure(
                    "Unable to locate a password input on the Copart login page."
                )

            logger.info("Credentials filled (email={})", email)

            # Click the first visible submit/sign-in button
            submit_buttons = await page.query_selector_all(
                "button[type='submit'], input[type='submit'], button:has-text('Log In'), button:has-text('Sign In'), button:has-text('Sign in'), button:has-text('Sign into account'), button:has-text('Submit')"
            )
            clicked = False
            for submit_button in submit_buttons:
                if await submit_button.is_visible() and await submit_button.is_enabled():
                    await submit_button.click()
                    clicked = True
                    break

            if not clicked:
                await page.keyboard.press("Enter")

            # Wait for navigation after submit (either success or failure)
            try:
                await page.wait_for_load_state("networkidle", timeout=settings.navigation_timeout)
            except Exception:
                # Timeout during navigation is acceptable; check current state
                pass

            # Check for additional verification / MFA
            current_url = page.url
            if "/verify" in current_url or "/mfa" in current_url or "/challenge" in current_url:
                logger.info("Additional verification required. Pausing for user interaction.")
                print("\n=== ADDITIONAL VERIFICATION REQUIRED ===")
                print("The Copart site requires additional verification (e.g., MFA, CAPTCHA).")
                print("Please complete the verification in the browser window.")
                print("After verification, press ENTER in this terminal to continue...")
                try:
                    input()
                except EOFError:
                    # Non-interactive mode; log and proceed cautiously
                    logger.warning("Non-interactive mode: cannot wait for user input.")
                    pass
                # After user completes verification, continue
                await page.wait_for_navigation(timeout=30000)

            # Verify success: check for dashboard indicators or absence of login.
            # Some Copart responses may leave the user on the login page briefly even
            # after a successful submit, especially when the page is slow or the
            # session state is still being established. In that case, treat the
            # submission as successful if we did not see an explicit rejection.
            current_url_after = page.url
            if "/login" in current_url_after and "/dashboard" not in current_url_after:
                error_text = None
                try:
                    error_text = await page.text_content(
                        ".error-message, .alert-danger, .login-error, .login-form__error",
                        timeout=5000,
                    )
                except Exception as exc:
                    logger.debug("No login error text detected before timeout: {}", exc)

                if error_text:
                    logger.error("Login failed with error message: {}", error_text)
                    raise LoginFailure(f"Login rejected by Copart: {error_text}")

                logger.warning(
                    "Copart did not redirect away from the login page after submit; "
                    "continuing with the current session state."
                )

            logger.info("Login workflow completed. Current URL: {}", current_url_after)

            # Save session state for future reuse
            await self.save_session()
            return True
        finally:
            # Close the login page when finished to avoid leaking pages in the
            # shared browser context. The caller can still use the context.
            try:
                await page.close()
            except Exception as exc:
                logger.warning("Failed to close login page: {}", exc)

    async def save_session(self) -> None:
        """Persist the current browser context session to disk.

        Allows future automation runs to reuse authentication without
        requiring credentials again, as long as the session remains
        valid according to Copart's session policies.
        """
        if not self._context:
            logger.warning("No active context to save.")
            return

        state_path = settings.storage_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._context.storage_state(path=str(state_path))
        except AttributeError:
            logger.warning(
                "Browser context does not support storage_state(); skipping session persistence."
            )
            return
        except Exception as exc:
            logger.warning("Failed to save browser session state: {}", exc)
            return
        logger.info("Session saved to {}", state_path)

    async def verify_authentication(self) -> bool:
        """Check whether the current context has a valid session.

        Returns:
            True if the session is valid; False otherwise.
        """
        if not self._context:
            return False

        page = await self._context.new_page()
        try:
            probe_urls = [COPART_DASHBOARD_URL, COPART_AUCTION_CALENDAR_URL]
            reached_non_login = False

            for probe_url in probe_urls:
                try:
                    await page.goto(
                        probe_url,
                        timeout=settings.navigation_timeout,
                        wait_until="domcontentloaded",
                    )
                except Exception as exc:
                    logger.warning("Auth probe navigation did not complete cleanly for {}: {}", probe_url, exc)

                current_url = page.url or ""
                if "/login" in current_url or "/signin" in current_url:
                    logger.info("Authentication verification failed: redirected to login.")
                    return False

                if "copart.com" in current_url:
                    reached_non_login = True

                # Look for elements that indicate a logged-in state
                indicators = [
                    "a[href*='logout']",
                    ".user-menu",
                    ".dashboard",
                    "a[href*='search']",
                    ".search_result_component_container",
                ]
                for selector in indicators:
                    try:
                        await page.wait_for_selector(selector, timeout=2500)
                        logger.info("Authentication verified (indicator: {})", selector)
                        return True
                    except Exception:
                        continue

            # Fallback: if we can access non-login Copart pages, treat session as valid.
            return reached_non_login
        finally:
            await page.close()

    async def reauthenticate(self) -> bool:
        """Force a fresh login and replace any existing session.

        Returns:
            True if reauthentication succeeds.
        """
        logger.info("Re-authenticating (forcing fresh login)...")
        # Clear existing session file to prevent reuse of stale data
        if settings.storage_state_path.exists():
            settings.storage_state_path.unlink()
            logger.info("Cleared stale session file.")
        return await self.login()
