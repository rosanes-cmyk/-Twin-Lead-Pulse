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


@dataclass
class Config:
    sheet_id: str = ""
    worksheet_name: str = "Raw Lead Data"
    auth_mode: str = "oauth"                      # "oauth" | "service_account"
    google_credentials_path: str = "credentials.json"
    google_token_path: str = "token.json"
    leads_input_path: str = "leads.txt"
    leads_format: str = "auto_text"              # "auto_text" | "jsonl"
    source_timezone: str = "America/Los_Angeles"
    entered_by: str = "Jonathan"
    original_source_location: str = "Google Chat"
    lead_id_prefix: str = "PPL-"                 # used when a lead has no id of its own
    rei: ReiConfig = field(default_factory=ReiConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rei_data = data.pop("rei", {}) or {}
        cfg = cls(**data)
        cfg.rei = ReiConfig(**rei_data)
        return cfg
