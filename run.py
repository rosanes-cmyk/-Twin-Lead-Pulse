#!/usr/bin/env python3
"""Twin Lead Pulse — orchestrator.

Pipeline:
  1. Parse lead notifications -> Leads (Pacific time, never-guess flags).
  2. Dedupe against existing sheet rows + within the batch.
  3. (optional) Enrich each lead from REI BlackBook (read-only browser).
  4. Write one row per lead into the `Raw Lead Data` tab (manual columns +
     appended REI columns only; formula columns untouched).

Usage:
  python run.py --config config.json                 # full run
  python run.py --config config.json --no-rei        # skip REI step
  python run.py --config config.json --dry-run       # parse only, write nothing
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from pipeline.config import Config
from pipeline.parsing import parse_notifications


def _today_pacific() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%m/%d/%Y")
    except Exception:
        return datetime.now().strftime("%m/%d/%Y")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Twin Lead Pulse lead pipeline")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--leads", help="override leads input path from config")
    ap.add_argument("--no-rei", action="store_true", help="skip REI BlackBook enrichment")
    ap.add_argument("--dry-run", action="store_true", help="parse + print only; no sheet writes")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    leads_path = Path(args.leads or cfg.leads_input_path)
    if not leads_path.exists():
        print(f"ERROR: leads input not found: {leads_path}", file=sys.stderr)
        return 2

    raw = leads_path.read_text(encoding="utf-8")
    leads = parse_notifications(raw, cfg.leads_format, cfg.source_timezone)
    print(f"Parsed {len(leads)} lead notification(s) from {leads_path}")

    today = _today_pacific()
    for lead in leads:
        lead.date_entered = today
        lead.entered_by = cfg.entered_by
        lead.original_source_location = cfg.original_source_location

    if args.dry_run:
        _print_preview(leads)
        print("\n[dry-run] No changes written.")
        return 0

    # --- connect to sheet, seed dedupe index ---
    from pipeline.sheets_client import SheetWriter, open_worksheet
    from pipeline.dedupe import DuplicateIndex

    ws = open_worksheet(cfg)
    writer = SheetWriter(ws)
    writer.ensure_rei_headers()
    index: DuplicateIndex = writer.build_index()

    for i, lead in enumerate(leads, start=1):
        lead.duplicate = index.classify(lead)
        if lead.duplicate in ("Yes", "Possible"):
            lead.verification_status = "Duplicate Review"
        if not lead.lead_id:
            lead.lead_id = f"{cfg.lead_id_prefix}{i:04d}"
        index.add(address=lead.property_address, phone=lead.seller_phone, email=lead.seller_email)

    # --- REI enrichment (optional) ---
    if cfg.rei.enabled and not args.no_rei:
        from pipeline.rei_client import ReiClient
        print("Enriching via REI BlackBook (read-only)...")
        with ReiClient(cfg.rei) as rei:
            for lead in leads:
                result = rei.enrich(lead)
                lead.rei_match = result.match
                lead.rei_contact_link = result.contact_link
                lead.rei_tags = result.tags
                lead.rei_status = result.status
                if result.notes:
                    lead.add_note(f"REI: {result.notes}")
                if result.match == "No":
                    lead.add_note("Needs Verification: no REI match")
    else:
        print("Skipping REI enrichment.")

    # --- write rows ---
    row = writer.next_row()
    for lead in leads:
        writer.write_lead(lead, row)
        print(f"  wrote row {row}: {lead.lead_id}  {lead.property_address or '(no address)'}"
              f"  [{lead.duplicate}] REI={lead.rei_match or '-'}")
        row += 1

    print(f"\nDone. {len(leads)} lead(s) written to '{cfg.worksheet_name}'.")
    dups = sum(1 for l in leads if l.duplicate != "No")
    needs = sum(1 for l in leads if l.verification_status not in ("Verified", ""))
    print(f"Duplicates flagged: {dups} | Needs review: {needs}")
    return 0


def _print_preview(leads) -> None:
    for l in leads:
        print("-" * 60)
        print(f"  Address : {l.property_address or '(blank)'}")
        print(f"  City/ZIP: {l.city or '(blank)'} / {l.zip_code or '(blank)'}")
        print(f"  Received: {l.date_received} {l.time_received}  ({l.day_of_week}, {l.hour_bucket})")
        print(f"  Source  : {l.source or '(blank)'}   Chat TS: {l.google_chat_timestamp or '(blank)'}")
        print(f"  Status  : {l.verification_status}")
        print(f"  Notes   : {l.notes or '-'}")


if __name__ == "__main__":
    raise SystemExit(main())
