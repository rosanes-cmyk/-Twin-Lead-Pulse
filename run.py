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
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt (for --clear-leads)")
    ap.add_argument("--relabel-heatmap", action="store_true",
                    help="relabel the Dashboard heatmap hour headers 0-23 as 12 AM..11 PM")
    ap.add_argument("--revert", action="store_true", help="with --relabel-heatmap: restore 0-23")
    ap.add_argument("--tab", default="Dashboard", help="worksheet tab for --relabel-heatmap / --heatmap-legend")
    ap.add_argument("--heatmap-legend", action="store_true",
                    help="add a plain-English hour legend to the heatmap title (keeps counts working)")
    ap.add_argument("--heatmap-ampm", action="store_true",
                    help="show heatmap hours as 12 AM..11 PM AND keep counts (rewrites formulas by column position)")
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
        return _clear_leads(cfg, skip_confirm=args.yes)

    if args.relabel_heatmap:
        return _relabel_heatmap(cfg, tab=args.tab, revert=args.revert)

    if args.heatmap_legend:
        return _heatmap_legend(cfg, tab=args.tab)

    if args.heatmap_ampm:
        return _heatmap_ampm(cfg, tab=args.tab)

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

    # Drop empty/junk leads (no address, name, or phone) so blank rows aren't written.
    before = len(leads)
    leads = [l for l in leads if (l.property_address or l.seller_name or l.seller_phone)]
    if before != len(leads):
        print(f"Dropped {before - len(leads)} empty lead fragment(s)")

    # Fill City and County from ZIP (deterministic lookup) when missing.
    if getattr(cfg, "fill_county_from_zip", True):
        from pipeline.geo import zip_to_county, zip_to_city
        fc = fct = 0
        for lead in leads:
            if not lead.zip_code:
                continue
            if not lead.county:
                c = zip_to_county(lead.zip_code)
                if c:
                    lead.county = c
                    lead.notes = " | ".join(p for p in lead.notes.split(" | ") if "county" not in p.lower())
                    fc += 1
            if not lead.city:
                ct = zip_to_city(lead.zip_code)
                if ct:
                    lead.city = ct
                    lead.notes = " | ".join(p for p in lead.notes.split(" | ") if "city" not in p.lower())
                    fct += 1
        if fc or fct:
            print(f"Filled from ZIP — county: {fc}, city: {fct}")

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
        with ReiClient(cfg.rei) as rei:
            if not rei.reachable():
                print("REI is not reachable / not logged in on this machine — SKIPPING REI.\n"
                      "  (Run  --enrich-sheet  on a PC where REI loads to fill REI columns.)")
                to_enrich = []
            else:
                to_enrich = to_write
                print(f"Enriching {len(to_enrich)} lead(s) via REI BlackBook (read-only)...")
            for lead in to_enrich:
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


def _heatmap_legend(cfg, tab: str = "Dashboard") -> int:
    """Append a plain-English hour legend to the heatmap title cell (safe — it's
    a label, so the numeric header and its count formulas keep working)."""
    from pipeline.sheets_client import open_named_worksheet
    from gspread.utils import rowcol_to_a1

    ws = open_named_worksheet(cfg, tab)
    values = ws.get_all_values()
    legend = (" — Hours are 24-hour time: 0 = 12 AM (midnight), 6 = 6 AM, "
              "12 = 12 PM (noon), 18 = 6 PM, 23 = 11 PM")
    for r, row in enumerate(values, start=1):
        for c, val in enumerate(row, start=1):
            if "day of week x hour" in val.strip().lower():
                base = val.split(" — Hours are 24-hour")[0].rstrip()
                ws.update(rowcol_to_a1(r, c), [[base + legend]], value_input_option="RAW")
                print(f"Added hour legend to the heatmap title on '{tab}'.")
                return 0
    print(f"Heatmap title not found on tab '{tab}'.")
    return 1


def _heatmap_ampm(cfg, tab: str = "Dashboard") -> int:
    """Show the heatmap hour headers as readable clock hours (12 AM..11 PM)
    WITHOUT breaking the counts.

    The count formulas read the numeric hour out of each column's header cell, so
    just relabeling the header to "12 AM" breaks them (that's what happened with
    --relabel-heatmap). Instead we first rewrite every count formula to use the
    *literal* hour number for its column (derived purely from the column's
    position), so no formula depends on the header text any more. Only once every
    formula is header-independent do we relabel the headers. A safety guard aborts
    the relabel if any formula still points at the header row — so the counts can
    never silently break.
    """
    import re
    from pipeline.sheets_client import open_named_worksheet
    from gspread.utils import rowcol_to_a1

    ws = open_named_worksheet(cfg, tab)
    values = ws.get_all_values()

    # Locate the 'Day / Hr' corner cell (top-left of the heatmap grid).
    target = None
    for rr, row in enumerate(values, start=1):
        for cc, val in enumerate(row, start=1):
            if val.strip().lower().replace(" ", "") in ("day/hr", "day/hour"):
                target = (rr, cc)
                break
        if target:
            break
    if not target:
        print(f"Couldn't find the heatmap 'Day / Hr' header on tab '{tab}'.")
        return 1
    r, c = target

    def col_letter(col1: int) -> str:
        s = ""
        while col1:
            col1, rem = divmod(col1 - 1, 26)
            s = chr(65 + rem) + s
        return s

    hour_cols = [c + 1 + k for k in range(24)]        # 1-based sheet columns
    first_data_row, last_data_row = r + 1, r + 7      # 7 weekday rows
    top_left = rowcol_to_a1(first_data_row, hour_cols[0])
    bot_right = rowcol_to_a1(last_data_row, hour_cols[-1])

    grid = ws.get(f"{top_left}:{bot_right}", value_render_option="FORMULA")

    # In each column, replace the reference to *that column's* header cell (row r)
    # with the literal hour index for the column's position.
    new_grid = []
    changed = 0
    for gridrow in grid:
        out_row = []
        for ci in range(24):
            val = gridrow[ci] if ci < len(gridrow) else ""
            letter = col_letter(hour_cols[ci])
            if isinstance(val, str) and val.startswith("="):
                pat = re.compile(rf"(?<![A-Z])\$?{letter}\$?{r}(?![0-9])")
                new_val, n = pat.subn(str(ci), val)      # ci == hour 0..23
                changed += n
                out_row.append(new_val)
            else:
                out_row.append(val)
        new_grid.append(out_row)

    if changed == 0:
        print("No header-referencing formulas found — the heatmap may already be\n"
              "position-based, or its layout differs. Nothing changed.")
        return 1

    ws.update(f"{top_left}:{bot_right}", new_grid, value_input_option="USER_ENTERED")

    # SAFETY GUARD: re-read and confirm no formula still references the header row.
    check = ws.get(f"{top_left}:{bot_right}", value_render_option="FORMULA")
    stale = re.compile(rf"(?<![A-Z])\$?[A-Z]+\$?{r}(?![0-9])")
    for gridrow in check:
        for val in gridrow:
            if isinstance(val, str) and val.startswith("=") and stale.search(val):
                print("Safety guard: some formulas still reference the header row —\n"
                      "NOT relabeling the headers, so the counts stay intact.")
                return 1

    # Safe now: relabel the 24 hour headers to readable clock hours.
    labels = [f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}" for h in range(24)]
    rng = f"{rowcol_to_a1(r, hour_cols[0])}:{rowcol_to_a1(r, hour_cols[-1])}"
    ws.update(rng, [labels], value_input_option="RAW")
    print(f"Done — heatmap hours now read 12 AM..11 PM on '{tab}', and the counts\n"
          f"still work (rewrote {changed} formula reference(s) to the hour position).")
    return 0


def _relabel_heatmap(cfg, tab: str = "Dashboard", revert: bool = False) -> int:
    """Relabel the heatmap hour headers (0-23) as readable clock hours.

    Finds the 'Day / Hr' header cell and rewrites the 24 cells to its right.
    Reversible with --revert (restores numeric 0-23).
    """
    from pipeline.sheets_client import open_named_worksheet
    from gspread.utils import rowcol_to_a1

    ws = open_named_worksheet(cfg, tab)
    values = ws.get_all_values()
    target = None
    for r, row in enumerate(values, start=1):
        for c, val in enumerate(row, start=1):
            if val.strip().lower().replace(" ", "") in ("day/hr", "day/hour"):
                target = (r, c)
                break
        if target:
            break
    if not target:
        print(f"Couldn't find the heatmap 'Day / Hr' header on tab '{tab}'.")
        return 1

    r, c = target
    if revert:
        labels = [str(h) for h in range(24)]
        opt = "USER_ENTERED"     # restore numeric 0-23
    else:
        labels = [f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}" for h in range(24)]
        opt = "RAW"              # keep as literal text, not parsed as times
    rng = f"{rowcol_to_a1(r, c + 1)}:{rowcol_to_a1(r, c + 24)}"
    ws.update(rng, [labels], value_input_option=opt)
    print(f"Updated heatmap hour labels on '{tab}' ({'0-23' if revert else '12 AM..11 PM'}).")
    if not revert:
        print("IMPORTANT: check the heatmap still shows counts. If the numbers went\n"
              "blank, the formulas key off the numeric header — run again with --revert.")
    return 0


def _clear_leads(cfg, skip_confirm: bool = False) -> int:
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
    if not skip_confirm:
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
        if not rei.reachable():
            print("REI is not reachable / not logged in on this machine — nothing written.\n"
                  "  Run this on a PC where REI loads (log in with scripts\\rei_login.py first).")
            return 0
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
