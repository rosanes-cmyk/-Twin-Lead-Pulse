"""Configuration loading for the pipeline (plain JSON, no secrets inside)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReiConfig:
    enabled: bool = True
    profile_dir: str = ".rei_profile"
    headless: bool = False
    # Use real installed Chrome (bundled Chromium may lack network access on
    # some machines). Set "" to use Playwright's bundled Chromium.
    channel: str = "chrome"
    login_url: str = "https://my.reiblackbook.com/services/account/login"
    contacts_url: str = "https://my.reiblackbook.com/contacts"
    properties_inbox_url: str = "https://my.reiblackbook.com/properties/inbox"
    nav_timeout_ms: int = 30000
    slow_mo_ms: int = 0
    # Optional: point at a pre-installed Chromium (leave empty to use the one
    # Playwright manages via `playwright install chromium`).
    chromium_executable_path: str = ""
    # If the automated browser has no network but normal Chrome does, your
    # network likely uses a proxy. Set true to auto-detect it (WPAD/PAC), or put
    # an explicit "host:port" in proxy_server.
    proxy_auto_detect: bool = False
    proxy_server: str = ""
    # Extra Chrome flags if you need them (advanced).
    extra_args: list = field(default_factory=list)


@dataclass
class ChatConfig:
    """Read the 'Incoming Property Leads' space via a real logged-in browser."""
    profile_dir: str = ".chat_profile"
    headless: bool = False
    # Use your real installed Chrome — Google often blocks sign-in on the
    # bundled Chromium ("this browser may not be secure"). Set "" to fall back
    # to Playwright's Chromium.
    channel: str = "chrome"
    space_url: str = ""                          # e.g. https://chat.google.com/app/chat/<SPACE_ID>
    nav_timeout_ms: int = 60000
    max_scrolls: int = 400                        # how hard to scroll to load full history
    scroll_pause_ms: int = 700                    # give lazy-loaded older messages time to render
    stable_rounds: int = 12                       # stop only after this many scrolls with nothing new
    slow_mo_ms: int = 0


@dataclass
class Config:
    sheet_id: str = ""
    worksheet_name: str = "Raw Lead Data"
    auth_mode: str = "oauth"                      # "oauth" | "service_account"
    google_credentials_path: str = "credentials.json"
    google_token_path: str = "token.json"
    leads_input_path: str = "leads.txt"
    leads_format: str = "auto_text"              # "auto_text" | "jsonl"
    leads_source: str = "file"                   # "file" | "chat"
    source_timezone: str = "America/Los_Angeles"
    entered_by: str = "Jonathan"
    original_source_location: str = "Google Chat"
    lead_id_prefix: str = "PPL-"                 # used when a lead has no id of its own
    # Skip writing leads already present in the sheet (Duplicate? == "Yes").
    # Essential for recurring runs that re-read the whole Chat history each time.
    # "Possible" duplicates are still written and flagged for review.
    skip_confirmed_duplicates: bool = True
    # Fill County from ZIP code (deterministic lookup via pgeocode) when the
    # message doesn't include it. Set false to leave county blank + flagged.
    fill_county_from_zip: bool = True
    rei: ReiConfig = field(default_factory=ReiConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rei_data = data.pop("rei", {}) or {}
        chat_data = data.pop("chat", {}) or {}
        cfg = cls(**data)
        cfg.rei = ReiConfig(**rei_data)
        cfg.chat = ChatConfig(**chat_data)
        return cfg
