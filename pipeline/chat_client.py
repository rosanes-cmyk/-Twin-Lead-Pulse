"""Read lead notifications from the Google Chat space via a logged-in browser.

Google Chat has no simple message API without a Cloud/Workspace app setup, so —
exactly like the REI step — we drive a **real, logged-in Chromium** using a
persistent profile. You sign into Google ONCE by hand in a visible window
(scripts/chat_login.py) and it's reused every run.

Why `channel="chrome"`: Google frequently refuses sign-in on Playwright's
bundled Chromium ("this browser or app may not be secure"). Using your real
installed Chrome avoids that. Configurable via `chat.channel`.

Reliability note: Google Chat's message list is virtualized and its markup is
undocumented and changes often. This scraper is **best-effort** and marked
`# TUNE`. It also writes everything it captured to `chat_dump.txt` so you can
eyeball it (and share it) even if per-message parsing needs adjusting.
"""

from __future__ import annotations

from typing import Optional

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore
    PWTimeout = Exception  # type: ignore

from .config import ChatConfig

# TUNE: candidate selectors for individual chat messages, tried in order.
_MESSAGE_SELECTORS = [
    "[data-message-id]",
    "[jsname][data-topic-id]",
    "div[role='listitem']",
    "[data-message-text]",
]
# TUNE: the scrollable message pane.
_SCROLLER_SELECTORS = [
    "[role='list']",
    "div[jsname][style*='overflow']",
    "main",
]


class ChatClient:
    def __init__(self, cfg: ChatConfig):
        if sync_playwright is None:
            raise RuntimeError("playwright not installed. Run: pip install -r requirements.txt")
        self.cfg = cfg
        self._pw = None
        self._ctx = None
        self.page = None

    def __enter__(self) -> "ChatClient":
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=self.cfg.profile_dir,
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
        )
        if self.cfg.channel:
            kwargs["channel"] = self.cfg.channel
        self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(self.cfg.nav_timeout_ms)
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _find_scroller(self):
        for sel in _SCROLLER_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    def _collect_messages(self) -> list[str]:
        """Grab message texts from whatever is currently rendered."""
        out: list[str] = []
        for sel in _MESSAGE_SELECTORS:
            try:
                texts = self.page.eval_on_selector_all(
                    sel, "els => els.map(e => (e.innerText||'').trim())"
                )
            except Exception:
                texts = []
            for t in texts:
                if t and t not in out:
                    out.append(t)
            if out:
                break
        return out

    def fetch_space_messages(self, space_url: str) -> list[str]:
        """Open the space, scroll through history, return message texts (oldest first)."""
        if not space_url:
            raise ValueError("chat.space_url is empty — set it in config.json")
        try:
            self.page.goto(space_url, wait_until="domcontentloaded")
        except PWTimeout:
            pass
        self.page.wait_for_timeout(4000)  # let Chat's app shell render

        scroller = self._find_scroller()
        # Scroll UP to force older messages to load, collecting as we go so we
        # don't lose virtualized rows that unmount.
        collected: list[str] = []
        stable = 0
        for _ in range(self.cfg.max_scrolls):
            for m in self._collect_messages():
                if m not in collected:
                    collected.append(m)
            try:
                if scroller is not None:
                    scroller.evaluate("el => el.scrollBy(0, -el.clientHeight)")
                else:
                    self.page.mouse.wheel(0, -2500)
            except Exception:
                self.page.mouse.wheel(0, -2500)
            self.page.wait_for_timeout(self.cfg.scroll_pause_ms)
            new = [m for m in self._collect_messages() if m not in collected]
            if not new:
                stable += 1
                if stable >= 5:      # nothing new after several scrolls -> at top
                    break
            else:
                stable = 0

        # Google Chat renders oldest-at-top; collected roughly reverse of scroll.
        # Preserve first-seen order but reverse so output reads oldest -> newest.
        return list(reversed(collected))
