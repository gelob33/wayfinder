"""Playwright browser session: persistent profile, headed by default (so the human can sign in)."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

ACTION_TIMEOUT_MS = 8000
NAV_TIMEOUT_MS = 30000


class BrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        headless: bool = False,
        channel: str | None = None,
        cdp_url: str | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        # Attach mode: drive a browser the person launched (and signed in to) themselves.
        self.cdp_url = cdp_url
        self._browser = None
        self.headless = headless
        self.channel = channel
        self._pw = None
        self.context: BrowserContext | None = None
        self._page: Page | None = None
        self.dialog_notes: list[str] = []

    @property
    def page(self) -> Page:
        """The current tab. If it was closed (login popup, redirect tab, ...), fall back to the newest open one."""
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else self.context.new_page()
            self._wire(self._page)
        return self._page

    @page.setter
    def page(self, value: Page) -> None:
        self._page = value

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        if self.cdp_url:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            self.context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self.context = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel=self.channel,
                viewport={"width": 1280, "height": 900},
            )
        self.context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.context.on("page", self._adopt)
        for existing in self.context.pages:
            self._wire(existing)
        # In attach mode start from the person's most recent tab; otherwise the first/new page.
        self.page = self.context.pages[-1] if self.context.pages else self.context.new_page()
        self._wire(self.page)
        return self

    def close(self) -> None:
        try:
            if self.cdp_url:
                # Disconnect only: the person's browser (and their sign-in) stays open.
                if self._browser:
                    self._browser.close()
            elif self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- pages / popups / dialogs -------------------------------------
    def _wire(self, page: Page) -> None:
        def on_dialog(dialog):
            self.dialog_notes.append(f"{dialog.type} dialog: {dialog.message[:120]}")
            if dialog.type == "alert":
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", on_dialog)

    def _adopt(self, page: Page) -> None:
        """A click opened a new tab/popup: follow it."""
        self._wire(page)
        self._page = page

    def pop_notes(self) -> list[str]:
        notes, self.dialog_notes = self.dialog_notes, []
        return notes

    # -- settle --------------------------------------------------------
    def settle(self) -> None:
        page = self.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass  # chatty pages never go idle; the snapshot is still usable

    def screenshot(self, path: Path) -> None:
        try:
            self.page.screenshot(path=str(path))
        except Exception:
            pass
