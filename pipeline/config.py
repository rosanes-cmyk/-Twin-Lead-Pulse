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
    login_url: str = "https://my.reiblackbook.com/services/account/login"
    contacts_url: str = "https://my.reiblackbook.com/contacts"
    properties_inbox_url: str = "https://my.reiblackbook.com/properties/inbox"
    nav_timeout_ms: int = 30000
    slow_mo_ms: int = 0
    # Optional: point at a pre-installed Chromium (leave empty to use the one
    # Playwright manages via `playwright install chromium`).
    chromium_executable_path: str = ""


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
    max_scrolls: int = 300                        # how hard to scroll to load full history
    scroll_pause_ms: int = 400
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
