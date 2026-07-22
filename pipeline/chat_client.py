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

    def _scroll_up_all(self) -> None:
        """Jump every scrollable panel to the top so Chat lazy-loads older
        messages, then nudge with the wheel and Home key."""
        js = """
        () => {
          const nodes = Array.from(document.querySelectorAll('*'))
            .filter(e => e.scrollHeight > e.clientHeight + 80);
          for (const e of nodes) { e.scrollTop = 0; }
          if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
        }"""
        try:
            self.page.evaluate(js)
        except Exception:
            pass
        try:
            self.page.mouse.wheel(0, -8000)
            self.page.keyboard.press("Home")
        except Exception:
            pass

    def _dom_lead_timestamps(self) -> list:
        """Read each lead message's EXACT timestamp from the DOM.

        Google Chat stores an epoch timestamp as a data-* attribute on each
        message. For every element containing exactly one lead, we find the
        nearest epoch attribute. Returns [{ts, text}] (ts may be None).
        """
        js = r"""
        () => {
          const out = []; const seen = new Set();
          const nodes = document.querySelectorAll('div,span,li,section,c-wiz');
          for (const el of nodes) {
            const txt = el.innerText || '';
            if ((txt.match(/NEW LEAD - PROPERTY LEADS/g) || []).length !== 1) continue;
            if (txt.length > 1600) continue;              // skip big ancestors
            let ts = null, node = el;
            for (let up = 0; up < 6 && node && !ts; up++) {
              const cands = [node].concat(Array.from(node.children || []));
              for (const c of cands) {
                if (!c.attributes) continue;
                for (const a of c.attributes) {
                  if (/(timestamp|time|ts|date|created)/i.test(a.name) && /^\d{10,13}$/.test(a.value)) {
                    ts = a.value; break;
                  }
                }
                if (ts) break;
              }
              node = node.parentElement;
            }
            const key = txt.slice(0, 90);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({ts: ts, text: txt});
          }
          return out;
        }"""
        try:
            return self.page.evaluate(js) or []
        except Exception:
            return []

    @staticmethod
    def _epoch_to_iso(ts) -> str:
        try:
            n = int(ts)
        except (TypeError, ValueError):
            return ""
        sec = n / 1000.0 if n > 10_000_000_000 else float(n)
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(sec, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    @staticmethod
    def _inject_ts(text: str, iso: str) -> str:
        lines = text.splitlines()
        out, done = [], False
        for ln in lines:
            out.append(ln)
            if not done and "NEW LEAD" in ln.upper():
                out.append(f"Google Chat Timestamp: {iso}")
                done = True
        if not done:
            out.insert(0, f"Google Chat Timestamp: {iso}")
        return "\n".join(out)

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

        merged: dict[str, str] = {}     # dedup-key -> enriched block
        dom_keys: set[str] = set()      # leads whose EXACT time came from the DOM
        snapshots: list[str] = []
        stable = 0
        stable_limit = max(12, getattr(self.cfg, "stable_rounds", 12))
        for _ in range(self.cfg.max_scrolls):
            snap = self._main_text()
            if snap and (not snapshots or snap != snapshots[-1]):
                snapshots.append(snap)
            added = 0

            # 1) EXACT timestamps from the DOM (authoritative, per message).
            for item in self._dom_lead_timestamps():
                iso = self._epoch_to_iso(item.get("ts"))
                if not iso:
                    continue
                block = self._inject_ts(item.get("text", ""), iso)
                key = self._dedup_key(block)
                if key not in merged:
                    added += 1
                merged[key] = block         # DOM time wins over any text guess
                dom_keys.add(key)

            # 2) Text fallback only for leads the DOM didn't give us a time for.
            for block in leads_from_conversation(snap, now):
                key = self._dedup_key(block)
                if key in dom_keys:
                    continue
                has_ts = "Google Chat Timestamp:" in block
                if key not in merged:
                    merged[key] = block
                    added += 1
                elif has_ts and "Google Chat Timestamp:" not in merged[key]:
                    merged[key] = block

            self._scroll_up_all()
            self.page.wait_for_timeout(self.cfg.scroll_pause_ms)
            stable = stable + 1 if added == 0 else 0
            if stable >= stable_limit:
                break
        print(f"  (scanned to top; found {len(merged)} lead message(s); "
              f"{len(dom_keys)} with exact DOM timestamps)")

        try:
            from pathlib import Path
            # Save the oldest-captured snapshots (top of the space) for tuning.
            Path("chat_raw.txt").write_text(
                "\n\n===== SNAPSHOT (oldest at end) =====\n\n".join(snapshots[-4:]),
                encoding="utf-8",
            )
        except Exception:
            pass

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
