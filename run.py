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
    ap.add_argument("--from-chat", action="store_true",
                    help="read leads directly from the Google Chat space")
    ap.add_argument("--no-rei", action="store_true", help="skip REI BlackBook enrichment")
    ap.add_argument("--write-duplicates", action="store_true",
                    help="write rows even for leads already in the sheet (default: skip them)")
    ap.add_argument("--dry-run", action="store_true", help="parse + print only; no sheet writes")
    ap.add_argument("--rei-test", action="store_true",
                    help="run REI lookups on parsed leads and print results; no sheet writes")
    ap.add_argument("--rei-dump", action="store_true",
                    help="open REI contacts page and print its inputs/links for selector tuning")
    ap.add_argument("--rei-search", help="debug: type this query into REI search and report results")
    ap.add_argument("--enrich-sheet", action="store_true",
                    help="backfill REI Match/Link/Tags/Status onto rows already in the sheet")
    ap.add_argument("--clear-leads", action="store_true",
                    help="clear ALL lead rows from Raw Lead Data (start fresh); asks to confirm")
    ap.add_argument("--profile", help="override the REI browser profile dir (e.g. .chat_profile)")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.profile:
        cfg.rei.profile_dir = args.profile

    if args.rei_dump:
        return _rei_dump(cfg)

    if args.rei_search:
        return _rei_search(cfg, args.rei_search)

    if args.enrich_sheet:
        return _enrich_sheet(cfg)

    if args.clear_leads:
        return _clear_leads(cfg)

    if args.from_chat or cfg.leads_source == "chat":
        raw = _read_from_chat(cfg)
    else:
        leads_path = Path(args.leads or cfg.leads_input_path)
        if not leads_path.exists():
            print(f"ERROR: leads input not found: {leads_path}", file=sys.stderr)
            return 2
        raw = leads_path.read_text(encoding="utf-8")

    leads = parse_notifications(raw, cfg.leads_format, cfg.source_timezone)
    print(f"Parsed {len(leads)} lead notification(s)")

    today = _today_pacific()
    for lead in leads:
        lead.date_entered = today
        lead.entered_by = cfg.entered_by
        lead.original_source_location = cfg.original_source_location

    if args.dry_run:
        _print_preview(leads)
        print("\n[dry-run] No changes written.")
        return 0

    if args.rei_test:
        from pipeline.rei_client import ReiClient
        print("REI test (read-only, no sheet writes)...")
        with ReiClient(cfg.rei) as rei:
            for lead in leads:
                r = rei.enrich(lead)
                print("-" * 60)
                print(f"  {lead.seller_name or '(no name)'} @ {lead.property_address or '(no addr)'}")
                print(f"  match={r.match}  link={r.contact_link or '-'}")
                print(f"  tags={r.tags or '-'}")
                print(f"  status={r.status or '-'}  notes={r.notes or '-'}")
        print("\n[rei-test] No changes written.")
        return 0

    # --- connect to sheet, seed dedupe index ---
    from pipeline.sheets_client import SheetWriter, open_worksheet
    from pipeline.dedupe import DuplicateIndex

    ws = open_worksheet(cfg)
    writer = SheetWriter(ws)
    writer.ensure_rei_headers()
    index: DuplicateIndex = writer.build_index()

    seq = writer.max_lead_seq(cfg.lead_id_prefix)   # continue after highest existing ID
    for lead in leads:
        lead.duplicate = index.classify(lead)
        if lead.duplicate in ("Yes", "Possible"):
            lead.verification_status = "Duplicate Review"
        if not lead.lead_id:
            seq += 1
            lead.lead_id = f"{cfg.lead_id_prefix}{seq:04d}"
        index.add(address=lead.property_address, phone=lead.seller_phone, email=lead.seller_email)

    skip_dupes = cfg.skip_confirmed_duplicates and not args.write_duplicates
    to_write = [l for l in leads if not (skip_dupes and l.duplicate == "Yes")]

    # --- REI enrichment (optional) — only for leads we'll actually write ---
    if cfg.rei.enabled and not args.no_rei and to_write:
        from pipeline.rei_client import ReiClient
        print(f"Enriching {len(to_write)} lead(s) via REI BlackBook (read-only)...")
        with ReiClient(cfg.rei) as rei:
            for lead in to_write:
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

    # --- write rows (batched — 3 API calls total, handles hundreds of rows) ---
    row = writer.next_row()
    writer.write_leads(to_write, row)
    written = len(to_write)
    skipped = len(leads) - written
    for i, lead in enumerate(to_write):
        print(f"  row {row + i}: {lead.lead_id}  {lead.property_address or '(no address)'}"
              f"  [{lead.duplicate}] REI={lead.rei_match or '-'}")

    print(f"\nDone. {written} lead(s) written to '{cfg.worksheet_name}', {skipped} skipped as duplicates.")
    needs = sum(1 for l in to_write if l.verification_status not in ("Verified", ""))
    print(f"Needs review: {needs}")
    return 0


def _rei_dump(cfg) -> int:
    """Print the REI contacts page's inputs and contact links for selector tuning."""
    from pipeline.rei_client import ReiClient
    with ReiClient(cfg.rei) as rei:
        try:
            rei.page.goto(cfg.rei.contacts_url, wait_until="domcontentloaded")
            rei.page.wait_for_timeout(4000)
        except Exception as e:
            print(f"nav error: {e}")
        inputs = rei.page.eval_on_selector_all(
            "input,textarea",
            "els => els.map(e => ({tag:e.tagName, type:e.type||'', name:e.name||'', "
            "placeholder:e.placeholder||'', id:e.id||'', aria:e.getAttribute('aria-label')||''}))",
        )
        print("URL:", rei.page.url)
        print("INPUT FIELDS:")
        for i in inputs:
            print("  ", i)
        links = rei.page.eval_on_selector_all(
            "a[href*='/contacts/']", "els => els.slice(0,8).map(e => e.getAttribute('href'))"
        )
        print("CONTACT LINKS (sample):", links)
        try:
            from pathlib import Path
            Path("rei_contacts.html").write_text(rei.page.content(), encoding="utf-8")
            print("Full HTML saved to rei_contacts.html")
        except Exception:
            pass
    return 0


def _clear_leads(cfg) -> int:
    """Clear ALL lead rows from Raw Lead Data so you can start fresh.

    Clears only the manual + REI columns (A-J, S-Z, AD-AG); the formula columns
    (K-R, AA-AC) keep their formulas. Asks for confirmation first.
    """
    from pipeline.sheets_client import open_worksheet, SheetWriter

    ws = open_worksheet(cfg)
    writer = SheetWriter(ws)
    hr = writer.find_header_row()
    values = ws.get_all_values()
    last = len(values)
    if last <= hr:
        print("No lead rows to clear.")
        return 0

    print(f"This will CLEAR all lead data in rows {hr + 1}-{last} of "
          f"'{cfg.worksheet_name}' (dashboard formulas are kept).")
    if input("Type YES to confirm: ").strip() != "YES":
        print("Cancelled — nothing changed.")
        return 0

    ranges = [f"A{hr + 1}:J{last}", f"S{hr + 1}:Z{last}", f"AD{hr + 1}:AG{last}"]
    ws.batch_clear(ranges)
    print(f"Cleared {last - hr} row(s). Now run  --from-chat  to load current leads.")
    return 0


def _enrich_sheet(cfg) -> int:
    """Backfill REI Match/Link/Tags/Status onto rows already in the sheet.

    For each existing row, search REI by its phone/name and write the four REI
    columns (AD-AG). Existing manual columns are never touched. Safe to re-run
    (e.g. to refresh status).
    """
    from pipeline.sheets_client import open_worksheet, SheetWriter, _a1_index
    from pipeline.rei_client import ReiClient
    from pipeline.models import Lead

    ws = open_worksheet(cfg)
    writer = SheetWriter(ws)
    writer.ensure_rei_headers()
    values = ws.get_all_values()
    hr = writer.find_header_row()

    def cell(row, letter):
        i = _a1_index(letter)
        return (row[i] if i < len(row) else "").strip()

    targets = []
    for idx in range(hr, len(values)):      # rows after header (0-based -> row idx+1)
        row = values[idx]
        if cell(row, "C") or cell(row, "B") or cell(row, "E"):
            targets.append((idx + 1, row))

    if not targets:
        print("No data rows found to enrich.")
        return 0

    print(f"Enriching {len(targets)} existing row(s) via REI (read-only)...")
    updated = 0
    buf: list = []
    buf_start = None

    def flush():
        nonlocal buf, buf_start
        if not buf:
            return
        end = buf_start + len(buf) - 1
        ws.update(f"AD{buf_start}:AG{end}", buf, value_input_option="USER_ENTERED")
        buf = []
        buf_start = None

    with ReiClient(cfg.rei) as rei:
        for rownum, row in targets:
            lead = Lead(
                seller_name=cell(row, "B"),
                seller_phone=cell(row, "C"),
                property_address=cell(row, "E"),
            )
            r = rei.enrich(lead)
            if buf and rownum != buf_start + len(buf):   # non-contiguous -> flush first
                flush()
            if buf_start is None:
                buf_start = rownum
            buf.append([r.match, r.contact_link, r.tags, r.status])
            updated += 1
            print(f"  row {rownum}: {lead.seller_name or lead.property_address} -> "
                  f"{r.match} {r.contact_link or ''} [{r.status or '-'}]")
            if len(buf) >= 30:                           # periodic flush (resilience + rate limits)
                flush()
    flush()
    print(f"\nDone. REI data written to {updated} row(s).")
    return 0


def _rei_search(cfg, query: str) -> int:
    """Debug: type a query into REI's search and report what happens."""
    from pipeline.rei_client import ReiClient, _SEARCH_INPUT_SELECTORS
    with ReiClient(cfg.rei) as rei:
        try:
            rei.page.goto(cfg.rei.contacts_url, wait_until="domcontentloaded")
            rei.page.wait_for_timeout(2500)
        except Exception as e:
            print(f"nav error: {e}")
        print("URL:", rei.page.url)
        for sel in _SEARCH_INPUT_SELECTORS:
            try:
                loc = rei.page.locator(sel).first
                if loc.count() > 0:
                    print(f"  search selector {sel!r}: found, visible={loc.is_visible()}")
            except Exception:
                pass
        before = rei._numeric_contact_ids()
        print("contacts before search:", len(before))
        box = rei._first_visible(_SEARCH_INPUT_SELECTORS)
        if box is None:
            print("!! no visible search box found")
        else:
            try:
                box.click()
                box.fill("")
                box.type(query, delay=40)
                rei.page.wait_for_timeout(3000)
                print(f"typed query: {query!r}")
            except Exception as e:
                print(f"type error: {e}")
        after = rei._numeric_contact_ids()
        print("contacts after search:", len(after), "->", after[:10])
        try:
            from pathlib import Path
            Path("rei_search.html").write_text(rei.page.content(), encoding="utf-8")
            print("saved rei_search.html")
        except Exception:
            pass
    return 0


def _read_from_chat(cfg) -> str:
    """Scrape the Google Chat space and return raw text (blank-line separated).

    Also writes everything captured to chat_dump.txt so you can verify what was
    read (and share it if the parser needs tuning to your format).
    """
    from pipeline.chat_client import ChatClient
    print(f"Reading Google Chat space (channel={cfg.chat.channel or 'bundled chromium'})...")
    with ChatClient(cfg.chat) as chat:
        messages = chat.fetch_space_messages(cfg.chat.space_url)
    dump = "\n\n".join(messages)
    Path("chat_dump.txt").write_text(dump, encoding="utf-8")
    print(f"Captured {len(messages)} message block(s) -> chat_dump.txt")
    return dump


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
