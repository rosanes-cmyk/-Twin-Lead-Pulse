"""Google Sheets I/O for the `Raw Lead Data` tab.

Critical rule: we write ONLY the manual columns and NEVER the formula columns
(K-R day/hour/week/month/year, and AA-AC _DupFlag/_IssueFlag/_IssueText).
REI enrichment fields are appended as new columns after the helper columns.

Existing sheet header (row 1 of the tab):
  A Lead ID | B Seller Name | C Seller Phone | D Seller Email |
  E Property Address | F City | G County | H ZIP Code |
  I Date Received | J Time Received | K-R (formulas) |
  S Source | T Google Chat Timestamp | U Duplicate? | V Verification Status |
  W Original Source Location | X Notes | Y Date Entered | Z Entered By |
  AA-AC (formulas)  ... then appended REI columns AD-AG.
"""

from __future__ import annotations

from typing import Optional

from .dedupe import DuplicateIndex
from .models import Lead

# gspread is only needed at runtime (keeps parsing testable without the dep).
try:
    import gspread  # type: ignore
except ImportError:  # pragma: no cover
    gspread = None  # type: ignore

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Contiguous write blocks: (start_col_letter, [Lead attribute per column]).
# `None` marks a formula column we must skip inside a block (there are none here
# because we split the row into three formula-free blocks).
MANUAL_BLOCK_A = ("A", [
    "lead_id", "seller_name", "seller_phone", "seller_email",
    "property_address", "city", "county", "zip_code",
    "date_received", "time_received",
])                                            # A..J
MANUAL_BLOCK_S = ("S", [
    "source", "google_chat_timestamp", "duplicate", "verification_status",
    "original_source_location", "notes", "date_entered", "entered_by",
])                                            # S..Z
REI_BLOCK_AD = ("AD", [
    "rei_match", "rei_contact_link", "rei_tags", "rei_status",
])                                            # AD..AG (appended)

REI_HEADERS = ["REI Match?", "REI Contact Link", "REI Tags", "REI Status"]


def _col_letter(idx0: int) -> str:
    """0-based column index -> A1 letter(s)."""
    idx0 += 1
    letters = ""
    while idx0:
        idx0, rem = divmod(idx0 - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def open_worksheet(cfg):
    if gspread is None:
        raise RuntimeError("gspread not installed. Run: pip install -r requirements.txt")
    if cfg.auth_mode == "service_account":
        gc = gspread.service_account(filename=cfg.google_credentials_path)
    else:
        gc = gspread.oauth(
            credentials_filename=cfg.google_credentials_path,
            authorized_user_filename=cfg.google_token_path,
        )
    return gc.open_by_key(cfg.sheet_id).worksheet(cfg.worksheet_name)


class SheetWriter:
    def __init__(self, worksheet, header_row: int = 1):
        self.ws = worksheet
        self.header_row = header_row
        self._values = worksheet.get_all_values()

    # --- header / layout -------------------------------------------------
    def find_header_row(self) -> int:
        for i, row in enumerate(self._values, start=1):
            if any(c.strip() == "Lead ID" for c in row):
                return i
        return self.header_row

    def ensure_rei_headers(self) -> None:
        """Make sure the appended REI columns have headers (idempotent)."""
        hr = self.find_header_row()
        header = self._values[hr - 1] if hr - 1 < len(self._values) else []
        have = set(h.strip() for h in header)
        if all(h in have for h in REI_HEADERS):
            return
        start = _col_letter(_a1_index(REI_BLOCK_AD[0]))
        end = _col_letter(_a1_index(REI_BLOCK_AD[0]) + len(REI_HEADERS) - 1)
        self.ws.update(f"{start}{hr}:{end}{hr}", [REI_HEADERS],
                       value_input_option="USER_ENTERED")

    # --- dedup seed ------------------------------------------------------
    def build_index(self) -> DuplicateIndex:
        idx = DuplicateIndex()
        hr = self.find_header_row()
        for row in self._values[hr:]:
            def cell(letter):
                i = _a1_index(letter)
                return row[i] if i < len(row) else ""
            if cell("E") or cell("A"):
                idx.add(address=cell("E"), phone=cell("C"), email=cell("D"))
        return idx

    def next_row(self) -> int:
        hr = self.find_header_row()
        last_data = hr
        for i, row in enumerate(self._values[hr:], start=hr + 1):
            def cell(letter):
                j = _a1_index(letter)
                return (row[j] if j < len(row) else "").strip()
            if cell("A") or cell("E"):
                last_data = i
        return last_data + 1

    # --- writing ---------------------------------------------------------
    def write_lead(self, lead: Lead, row: int) -> None:
        for start_letter, attrs in (MANUAL_BLOCK_A, MANUAL_BLOCK_S, REI_BLOCK_AD):
            values = [getattr(lead, a, "") for a in attrs]
            start = _a1_index(start_letter)
            end_letter = _col_letter(start + len(values) - 1)
            rng = f"{start_letter}{row}:{end_letter}{row}"
            self.ws.update(rng, [values], value_input_option="USER_ENTERED")


def _a1_index(letter: str) -> int:
    """A1 column letters -> 0-based index."""
    idx = 0
    for ch in letter.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1
