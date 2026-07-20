"""Data model for a single property lead.

One `Lead` == one row in the `Raw Lead Data` tab of the dashboard sheet.

The field order here is the canonical internal order. The mapping to actual
spreadsheet columns lives in `sheets_client.COLUMN_MAP`, so that the sheet's
formula columns (K-R, _DupFlag/_IssueFlag/_IssueText) are never touched.
"""

from dataclasses import dataclass


@dataclass
class Lead:
    # --- Manual identity / seller ---
    lead_id: str = ""
    seller_name: str = ""
    seller_phone: str = ""
    seller_email: str = ""

    # --- Property location ---
    property_address: str = ""
    city: str = ""
    county: str = ""
    zip_code: str = ""

    # --- Timing (Pacific) ---
    date_received: str = ""          # MM/DD/YYYY, real date once in the sheet
    time_received: str = ""          # h:MM AM/PM, real time once in the sheet
    day_of_week: str = ""            # computed, informational
    hour_bucket: str = ""            # computed, informational
    month: str = ""                  # computed, informational
    year: str = ""                   # computed, informational

    # --- Provenance / status ---
    source: str = ""                 # PPL / PPC / Direct Mail / SEO / Referral / Other / Unknown
    google_chat_timestamp: str = ""  # exact original timestamp text
    duplicate: str = "No"            # Yes / No / Possible
    verification_status: str = ""    # Verified / Needs Verification / Duplicate Review / Missing Information
    original_source_location: str = "Google Chat"
    notes: str = ""
    date_entered: str = ""           # MM/DD/YYYY (run date)
    entered_by: str = ""

    # --- REI BlackBook enrichment (appended columns) ---
    rei_match: str = ""              # Yes / No
    rei_contact_link: str = ""
    rei_tags: str = ""
    rei_status: str = ""

    def add_note(self, text: str) -> None:
        """Append a note without clobbering existing notes."""
        text = (text or "").strip()
        if not text:
            return
        self.notes = f"{self.notes} | {text}".strip(" |") if self.notes else text

    def needs_verification(self, missing_what: str) -> None:
        """Flag a missing/uncertain field per the 'never guess' rule."""
        self.add_note(f"Needs Verification: {missing_what}")
        # Don't downgrade a stronger status; only set if currently unset/verified.
        if self.verification_status in ("", "Verified"):
            self.verification_status = "Needs Verification"
