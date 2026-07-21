"""REI BlackBook enrichment via Playwright (READ-ONLY).

REI BlackBook has no public API, so we drive the real site with a **persistent**
Chromium profile: you log in by hand ONCE in a visible window (see
scripts/rei_login.py), and every later run reuses that session.

Guardrails enforced here:
  - Read-only. We never click send/save/tag/delete — only navigate & read.
  - Any selector we can't find -> we DON'T guess; the field is left blank and
    the caller records "Needs Verification".

IMPORTANT: REI's DOM (CSS selectors) is not publicly documented. The selector
lists below are best-effort and marked `# TUNE`. On first real run, if matches
fail, capture the page HTML and adjust these lists. Because everything degrades
to "Needs Verification" instead of crashing, a wrong selector is safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore
except ImportError:  # pragma: no cover
    sync_playwright = None  # type: ignore
    PWTimeout = Exception  # type: ignore

from .config import ReiConfig
from .models import Lead

_CONTACT_ID_RE = re.compile(r"/contacts/(\d+)")

# TUNE: candidate selectors, tried in order.
_SEARCH_INPUT_SELECTORS = [
    "input[type='search']",
    "input[placeholder*='Search' i]",
    "input[name*='search' i]",
    "[data-testid*='search'] input",
]
_TAG_SELECTORS = [
    "[class*='tag' i]",
    "[data-testid*='tag']",
    ".chip, .badge",
]
_PHONE_SELECTORS = [
    "a[href^='tel:']",
    "[class*='phone' i]",
]
_HISTORY_TAB_LABELS = ["Notes", "Activities", "Activity", "Chat", "Text", "Messages", "History"]


@dataclass
class ReiResult:
    match: str = "No"           # Yes / No
    contact_link: str = ""
    tags: str = ""
    status: str = ""
    notes: str = ""             # extra note for the Lead (e.g. what couldn't be read)


class ReiClient:
    """Context-manager wrapper around a persistent Chromium context."""

    def __init__(self, cfg: ReiConfig):
        if sync_playwright is None:
            raise RuntimeError("playwright not installed. Run: pip install -r requirements.txt "
                               "&& playwright install chromium")
        self.cfg = cfg
        self._pw = None
        self._ctx = None
        self.page = None

    def __enter__(self) -> "ReiClient":
        self._pw = sync_playwright().start()
        launch_kwargs = dict(
            user_data_dir=self.cfg.profile_dir,
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
        )
        if self.cfg.chromium_executable_path:
            launch_kwargs["executable_path"] = self.cfg.chromium_executable_path
        elif getattr(self.cfg, "channel", ""):
            launch_kwargs["channel"] = self.cfg.channel
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            launch_kwargs.pop("channel", None)   # fall back to bundled Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
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

    # --- low-level helpers ----------------------------------------------
    def _first_visible(self, selectors: list[str]):
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _search_contacts(self, query: str) -> Optional[str]:
        """Run one contacts search; return a contact id if a result links to one."""
        if not query.strip():
            return None
        try:
            self.page.goto(self.cfg.contacts_url, wait_until="domcontentloaded")
        except Exception:
            return None
        box = self._first_visible(_SEARCH_INPUT_SELECTORS)
        if box is None:
            return None
        try:
            box.fill("")
            box.type(query, delay=20)
            box.press("Enter")
            self.page.wait_for_timeout(1500)  # let results render
        except Exception:
            return None
        # Read the first result that links to /contacts/<id>.
        try:
            hrefs = self.page.eval_on_selector_all(
                "a[href*='/contacts/']", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []
        for href in hrefs or []:
            m = _CONTACT_ID_RE.search(href or "")
            if m:
                return m.group(1)
        return None

    def _read_contact(self, contact_id: str) -> ReiResult:
        url = f"https://my.reiblackbook.com/contacts/{contact_id}"
        res = ReiResult(match="Yes", contact_link=url)
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1200)
        except Exception:
            res.notes = "contact page load timed out"
            return res

        # Tags (best-effort).
        tags: list[str] = []
        for sel in _TAG_SELECTORS:
            try:
                texts = self.page.eval_on_selector_all(
                    sel, "els => els.map(e => (e.textContent||'').trim())"
                )
                tags += [t for t in texts if t]
            except Exception:
                continue
        seen = []
        for t in tags:
            if t and t not in seen and len(t) < 40:
                seen.append(t)
        res.tags = "; ".join(seen)

        # Walk history tabs and gather text to infer a status.
        history_text = self._read_history_tabs()
        res.status = self._infer_status(history_text) or "Needs Verification"
        return res

    def _read_history_tabs(self) -> str:
        collected = []
        for label in _HISTORY_TAB_LABELS:
            try:
                tab = self.page.get_by_role("tab", name=re.compile(label, re.I))
                if tab.count() == 0:
                    tab = self.page.get_by_text(re.compile(rf"^{label}$", re.I)).first
                if tab.count() > 0 and tab.first.is_visible():
                    tab.first.click()
                    self.page.wait_for_timeout(800)
                    self._scroll_message_list_to_top()
                    collected.append(self.page.inner_text("body"))
            except Exception:
                continue
        return "\n".join(collected)

    def _scroll_message_list_to_top(self) -> None:
        """Lazy-loaded chats load older messages as you scroll up; scroll to top."""
        try:
            for _ in range(25):
                before = self.page.evaluate("document.body.scrollHeight")
                self.page.mouse.wheel(0, -3000)
                self.page.wait_for_timeout(300)
                after = self.page.evaluate("document.body.scrollHeight")
                if after == before:
                    break
        except Exception:
            pass

    @staticmethod
    def _infer_status(text: str) -> str:
        low = (text or "").lower()
        rules = [
            ("Opted-out", ["stop", "unsubscribe", "opt out", "opted out", "do not text", "do not contact"]),
            ("Sold", ["sold", "under contract", "closed"]),
            ("Listed", ["listed", "on the market", "with an agent", "realtor"]),
            ("Wrong Number", ["wrong number", "who is this", "don't know", "not me"]),
            ("Not Interested", ["not interested", "no thanks", "no thank you", "remove me"]),
            ("Active", ["interested", "call me", "available", "yes", "let's talk"]),
        ]
        for status, kws in rules:
            if any(k in low for k in kws):
                return status
        return "No Contact Yet" if low.strip() else ""

    # --- public API ------------------------------------------------------
    def enrich(self, lead: Lead) -> ReiResult:
        """Try the 6 searches in order; stop at first match, then read contact.

        Never raises — any browser/navigation error degrades to a safe result
        with a note, so one bad lead can't abort the whole batch.
        """
        try:
            street_only = lead.property_address.split(",")[0].strip() if lead.property_address else ""
            attempts = [
                lead.property_address,
                street_only,
                lead.seller_name,
                lead.seller_phone,
                lead.seller_email,
            ]
            for query in attempts:
                cid = self._search_contacts(query or "")
                if cid:
                    return self._read_contact(cid)

            # Step 6: property pipeline inbox by address -> linked contact.
            cid = self._search_property_inbox(lead.property_address or street_only)
            if cid:
                return self._read_contact(cid)

            return ReiResult(match="No", notes="no REI contact matched (6 searches)")
        except Exception as e:
            return ReiResult(match="No", notes=f"REI lookup error: {type(e).__name__}")

    def _search_property_inbox(self, query: str) -> Optional[str]:
        if not (query or "").strip():
            return None
        try:
            self.page.goto(self.cfg.properties_inbox_url, wait_until="domcontentloaded")
        except Exception:
            return None
        box = self._first_visible(_SEARCH_INPUT_SELECTORS)
        if box is None:
            return None
        try:
            box.fill("")
            box.type(query, delay=20)
            box.press("Enter")
            self.page.wait_for_timeout(1500)
            hrefs = self.page.eval_on_selector_all(
                "a[href*='/contacts/']", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []
        for href in hrefs or []:
            m = _CONTACT_ID_RE.search(href or "")
            if m:
                return m.group(1)
        return None
