#!/usr/bin/env python3
"""One-time REI BlackBook login to seed the persistent browser profile.

Run this ONCE on your own computer (with a screen). A visible Chromium window
opens on the REI login page; sign in by hand, complete any 2FA, then come back
to the terminal and press Enter. Your session is saved into the profile dir and
reused by every later `run.py` — you won't need to log in again unless REI logs
you out.

    python scripts/rei_login.py --config config.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import Config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed REI BlackBook persistent login")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = Config.load(args.config).rei

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: pip install -r requirements.txt "
              "&& playwright install chromium", file=sys.stderr)
        return 1

    print(f"Opening a visible browser using profile: {cfg.profile_dir}")
    print("Log in to REI BlackBook in the window, finish any 2FA, then return here.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=cfg.profile_dir,
            headless=False,          # must be visible for a manual login
            slow_mo=cfg.slow_mo_ms,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg.login_url)
        input("\n>>> Press Enter here AFTER you have fully logged in... ")
        ctx.close()
    print("Saved. You can now run: python run.py --config config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
