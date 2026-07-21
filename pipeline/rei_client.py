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

# Prevent "restore previous session" tab spam and first-run prompts that make
# a persistent-profile Chrome open many tabs at once and hang.
_STABILITY_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--restore-last-session=false",
    "--window-size=1500,1000",
]


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
            # Force a wide desktop viewport so REI shows the full layout with the
            # search box visible (narrow windows collapse it into a mobile menu).
            viewport={"width": 1500, "height": 950},
        )
        if self.cfg.chromium_executable_path:
            launch_kwargs["executable_path"] = self.cfg.chromium_executable_path
        elif getattr(self.cfg, "channel", ""):
            launch_kwargs["channel"] = self.cfg.channel
        args = _STABILITY_ARGS + list(getattr(self.cfg, "extra_args", []) or [])
        if getattr(self.cfg, "proxy_auto_detect", False):
            args.append("--proxy-auto-detect")
        launch_kwargs["args"] = args
        if getattr(self.cfg, "proxy_server", ""):
            launch_kwargs["proxy"] = {"server": self.cfg.proxy_server}
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            launch_kwargs.pop("channel", None)   # fall back to bundled Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        # Use a FRESH tab; the restored first tab is often stuck loading.
        self.page = self._ctx.new_page()
        for p in list(self._ctx.pages):
            if p is not self.page:
                try:
                    p.close()
                except Exception:
                    pass
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

    def _numeric_contact_ids(self) -> list[str]:
        """Distinct numeric /contacts/<id> ids currently on the page (order kept)."""
        try:
            hrefs = self.page.eval_on_selector_all(
                "a[href*='/contacts/']", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []
        ids: list[str] = []
        for href in hrefs or []:
            m = _CONTACT_ID_RE.search(href or "")
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        return ids

    def _search_contacts(self, query: str) -> Optional[str]:
        """Type into REI's live 'Search By Name, Phone' box and return the
        contact id ONLY if the list actually narrows to a small result set
        (avoids returning the top of the unfiltered list as a false match)."""
        if not query.strip():
            return None
        try:
            self.page.goto(self.cfg.contacts_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1800)
        except Exception:
            return None
        box = self._first_visible(_SEARCH_INPUT_SELECTORS)
        if box is None:
            # Search may be hidden behind a mobile menu toggle — try to reveal it.
            for tsel in ["#mobile_menu_toggle", "[id*='menu_toggle']", "[aria-label*='menu' i]"]:
                try:
                    tog = self.page.locator(tsel).first
                    if tog.count() > 0:
                        tog.click(timeout=2000)
                        self.page.wait_for_timeout(600)
                        break
                except Exception:
                    continue
            box = self._first_visible(_SEARCH_INPUT_SELECTORS)
        if box is None:
            return None
        # Let the list finish loading, then record the unfiltered size.
        full_count = 0
        for _ in range(8):
            self.page.wait_for_timeout(400)
            c = len(self._numeric_contact_ids())
            if c and c == full_count:
                break
            full_count = c
        try:
            box.click()
            box.fill("")
            box.type(query, delay=40)
        except Exception:
            return None
        # Poll until the live filter narrows the list (up to ~7s).
        for _ in range(14):
            self.page.wait_for_timeout(500)
            ids = self._numeric_contact_ids()
            if ids and len(ids) <= 8 and (full_count == 0 or len(ids) < full_count):
                return ids[0]
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
            phone = lead.seller_phone or ""
            phone10 = "".join(ch for ch in phone if ch.isdigit())[-10:]
            # Phone is the most reliable key — search it first, then name.
            attempts = [
                phone10,
                phone,
                lead.seller_name,
                lead.property_address,
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
