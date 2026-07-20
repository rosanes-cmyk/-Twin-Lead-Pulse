#!/usr/bin/env python3
"""One-time Google sign-in to seed the Chat browser profile.

Run once on your own computer. A visible Chrome window opens on Google Chat;
sign in by hand (finish any 2FA), open the "Incoming Property Leads" space so
it's loaded, then return to the terminal and press Enter. The session is saved
to the profile dir and reused by every later run.

    python scripts/chat_login.py --config config.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import Config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed Google Chat persistent login")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = Config.load(args.config).chat

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    start_url = cfg.space_url or "https://chat.google.com/"
    print(f"Opening a visible browser (profile: {cfg.profile_dir}).")
    print("Sign in to Google, open the 'Incoming Property Leads' space, then return here.")
    with sync_playwright() as pw:
        kwargs = dict(user_data_dir=cfg.profile_dir, headless=False, slow_mo=cfg.slow_mo_ms)
        if cfg.channel:
            kwargs["channel"] = cfg.channel
        try:
            ctx = pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            print(f"\nCould not launch Chrome channel '{cfg.channel}': {e}")
            print("Retrying with Playwright's bundled Chromium...")
            kwargs.pop("channel", None)
            ctx = pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(start_url)
        input("\n>>> Press Enter AFTER you're signed in and the space is open... ")
        ctx.close()
    print("Saved. You can now run: python run.py --config config.json --from-chat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
