"""Tests for authentication and session reauthentication logic."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from copart_automation.app import auth as auth_module
from copart_automation.app.auth import AuthManager
from copart_automation.app.browser import BrowserManager


class DummyPage:
    url = "https://www.copart.com/"

    async def goto(self, *args, **kwargs) -> None:
        return None

    async def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        return None

    async def close(self) -> None:
        return None


class DummyContext:
    async def new_page(self) -> DummyPage:
        return DummyPage()

    async def close(self) -> None:
        return None


class DummyBrowser:
    async def new_context(self, **kwargs) -> DummyContext:
        return DummyContext()


class DummyBrowserManager(BrowserManager):
    def __init__(self) -> None:
        super().__init__()
        self.started = False

    async def start(self) -> None:
        self.started = True
        self._browser = DummyBrowser()
        self._context = None


@pytest.mark.asyncio
async def test_auth_manager_load_existing_session_starts_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth_module.settings, "storage_state_path", state_file)

    manager = DummyBrowserManager()
    auth = AuthManager(manager)

    loaded = await auth.load_existing_session()
    assert loaded is True
    assert manager.started is True


@pytest.mark.asyncio
async def test_login_continues_after_initial_page_load_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutElement:
        def __init__(self) -> None:
            self.value: str | None = None
            self.clicked = False

        async def fill(self, value: str) -> None:
            self.value = value

        async def click(self) -> None:
            self.clicked = True

        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            return True

    class TimeoutPage:
        def __init__(self) -> None:
            self.url = "https://www.copart.com/login"
            self.waited_for = None

        async def goto(self, *args, **kwargs) -> None:
            self.waited_for = kwargs.get("wait_until")
            raise TimeoutError("simulated timeout")

        async def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
            return None

        async def query_selector(self, selector: str) -> TimeoutElement | None:
            if "email" in selector or "username" in selector:
                return TimeoutElement()
            if "password" in selector:
                return TimeoutElement()
            return None

        async def query_selector_all(self, selector: str) -> list[TimeoutElement]:
            return [TimeoutElement()]

        async def keyboard_press(self, key: str) -> None:
            return None

        async def wait_for_load_state(self, *args, **kwargs) -> None:
            return None

        async def text_content(self, selector: str, timeout: int = 0) -> str | None:
            raise TimeoutError("simulated text_content timeout")

        async def close(self) -> None:
            return None

    class TimeoutContext:
        def __init__(self, page: TimeoutPage) -> None:
            self.page = page

        async def new_page(self) -> TimeoutPage:
            return self.page

        async def close(self) -> None:
            return None

    class TimeoutBrowser:
        def __init__(self) -> None:
            self.page = TimeoutPage()

        async def new_context(self, **kwargs) -> TimeoutContext:
            return TimeoutContext(self.page)

    class TimeoutBrowserManager(DummyBrowserManager):
        async def start(self) -> None:
            self.started = True
            self._browser = TimeoutBrowser()
            self._context = await self._browser.new_context()

    monkeypatch.setattr(auth_module.settings, "copart_password", SecretStr("secret"))
    manager = TimeoutBrowserManager()
    auth = AuthManager(manager)

    succeeded = await auth.login(email="user@example.com", password="secret")

    assert succeeded is True
