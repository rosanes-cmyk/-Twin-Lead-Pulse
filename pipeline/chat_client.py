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

    def _main_text(self) -> str:
        """innerText of the conversation region (not the member roster)."""
        js = """
        () => {
          const main = document.querySelector('main') || document.body;
          return main ? main.innerText : '';
        }"""
        try:
            return self.page.evaluate(js) or ""
        except Exception:
            return ""

    def fetch_space_messages(self, space_url: str) -> list[str]:
        """Open the space, scroll through history, return dated lead blocks.

        Google Chat virtualizes the message list (rows unmount as you scroll),
        so we take a text snapshot at each scroll step, resolve leads+timestamps
        from each snapshot (which has correct internal day-divider context), and
        merge them de-duplicated and sorted oldest -> newest. The full capture is
        also saved to chat_raw.txt for diagnosis.
        """
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/Los_Angeles"))
        except Exception:
            now = datetime.now()
        from .chat_parse import leads_from_conversation

        if not space_url:
            raise ValueError("chat.space_url is empty — set it in config.json")
        try:
            self.page.goto(space_url, wait_until="domcontentloaded")
        except PWTimeout:
            pass
        self.page.wait_for_timeout(5000)  # let Chat's app shell render

        scroller = self._find_scroller()
        merged: dict[str, str] = {}     # dedup-key -> enriched block
        snapshots: list[str] = []
        stable = 0
        for _ in range(self.cfg.max_scrolls):
            snap = self._main_text()
            if snap:
                snapshots.append(snap)
            added = 0
            for block in leads_from_conversation(snap, now):
                key = self._dedup_key(block)
                if key not in merged:
                    merged[key] = block
                    added += 1
            try:
                if scroller is not None:
                    scroller.evaluate("el => el.scrollBy(0, -el.clientHeight)")
                else:
                    self.page.mouse.wheel(0, -2500)
            except Exception:
                self.page.mouse.wheel(0, -2500)
            self.page.wait_for_timeout(self.cfg.scroll_pause_ms)
            stable = stable + 1 if added == 0 else 0
            if stable >= 6:             # no new leads after several scrolls -> done
                break

        try:
            from pathlib import Path
            Path("chat_raw.txt").write_text(
                "\n\n===== SNAPSHOT =====\n\n".join(snapshots[-3:]), encoding="utf-8"
            )
        except Exception:
            pass

        # Sort oldest -> newest by the resolved timestamp embedded in each block.
        return sorted(merged.values(), key=self._sort_key)

    @staticmethod
    def _dedup_key(block: str) -> str:
        import re
        name = re.search(r"Name\s*:\s*(.+)", block, re.I)
        addr = re.search(r"Property Address\s*:\s*(.+)", block, re.I)
        return f"{(name.group(1) if name else '').strip().lower()}|" \
               f"{(addr.group(1) if addr else '').strip().lower()}"

    @staticmethod
    def _sort_key(block: str) -> str:
        import re
        m = re.search(r"Google Chat Timestamp:\s*([0-9\-: ]+)", block)
        return m.group(1).strip() if m else ""
