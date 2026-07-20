"""Parse raw lead notifications into `Lead` objects.

Design goals:
- **Never guess.** If a field is not confidently present, leave it blank and
  record "Needs Verification: <field>" in Notes (see Lead.needs_verification).
- All arrival times are converted to **Pacific time**. Naive timestamps are
  assumed to be in `source_timezone` (config) and the conversion method is
  recorded in Notes.
- Stdlib-only (no third-party deps) so the parsing/timezone logic is testable
  offline.

Two input shapes are supported (see `parse_notifications`):
  1. "jsonl" — one JSON object per line with already-structured fields
     (most reliable; recommended once you know your notification format).
  2. "auto_text" — free-text notifications separated by blank lines; fields are
     extracted heuristically. Tune the regexes to your real Chat format.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

from .models import Lead

# ---------------------------------------------------------------------------
# Timezone / calendar helpers
# ---------------------------------------------------------------------------

_TZ_ABBREV_HINTS = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "UTC": "UTC",
    "GMT": "UTC",
}

# strptime formats we try, in order, after stripping a trailing tz abbreviation.
_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%a, %b %d, %Y %I:%M %p",
    "%m/%d/%y %I:%M %p",
    "%m/%d/%y %H:%M",
]


def _zone(name: str):
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo unavailable; use Python 3.9+ (and `pip install tzdata`).")
    return ZoneInfo(name)


def parse_timestamp(raw: str, source_timezone: str) -> tuple[datetime | None, str]:
    """Return (aware_datetime_in_pacific, method_note).

    `raw` is the exact timestamp text from the notification. If a tz abbreviation
    is present (e.g. "PST", "EST") it wins; otherwise `source_timezone` is assumed.
    Returns (None, reason) if it cannot be parsed confidently — we do NOT guess.
    """
    if not raw or not raw.strip():
        return None, "no timestamp in message"

    text = raw.strip()
    assumed_zone = source_timezone
    method = f"assumed {source_timezone}"

    # Detect a trailing/inline tz abbreviation and strip it before strptime.
    tokens = text.replace(",", " ").split()
    for tok in tokens:
        up = tok.upper()
        if up in _TZ_ABBREV_HINTS:
            assumed_zone = _TZ_ABBREV_HINTS[up]
            method = f"source tz {up} -> {assumed_zone}"
            text = re.sub(rf"\b{re.escape(tok)}\b", "", text).strip()
            break

    text = re.sub(r"\s+", " ", text).strip(" ,")

    parsed = None
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        return None, f"unparseable timestamp: {raw!r}"

    aware = parsed.replace(tzinfo=_zone(assumed_zone))
    pacific = aware.astimezone(_zone("America/Los_Angeles"))
    # Only note an actual cross-zone conversion; if the source is already
    # Pacific there is nothing to record.
    note = "" if assumed_zone == "America/Los_Angeles" \
        else f"TZ conversion ({method})"
    return pacific, note


def hour_bucket(hour24: int) -> str:
    """'8:00-8:59 AM', '13:00-1:59 PM', '0:00-12:59 AM' (matches the sheet)."""
    ampm = "AM" if hour24 < 12 else "PM"
    end12 = hour24 % 12 or 12
    return f"{hour24}:00-{end12}:59 {ampm}"


def fill_calendar_fields(lead: Lead, dt: datetime) -> None:
    """Populate Pacific date/time + informational calendar fields from `dt`."""
    lead.date_received = dt.strftime("%m/%d/%Y")
    # %-I is platform-specific; build 12h hour manually for portability.
    hour12 = dt.hour % 12 or 12
    lead.time_received = f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
    lead.day_of_week = dt.strftime("%A")
    lead.hour_bucket = hour_bucket(dt.hour)
    lead.month = dt.strftime("%B")
    lead.year = str(dt.year)


# ---------------------------------------------------------------------------
# Field extraction (heuristic, for the "auto_text" input mode)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(\d{3})[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)")
_ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_STATE_CITY_RE = re.compile(r",\s*([A-Za-z .'-]+),\s*(?:CA|California)\b", re.I)
_STREET_RE = re.compile(
    r"\d+\s+[\w .'-]+?\b(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|"
    r"Ct|Court|Way|Pl|Place|Ter|Terrace|Cir|Circle|Hwy|Highway|Pkwy|Parkway|Trl|Trail|Loop|Sq|Square)\b",
    re.I,
)
# ISO "2026-07-20 08:57 PST" or US "07/20/2026 8:57 AM"; trailing tz limited to
# real abbreviations so it can't swallow following words across a newline.
_TZ_SUFFIX = r"(?: ?(?:PST|PDT|PT|MST|MDT|CST|CDT|EST|EDT|UTC|GMT))?"
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?" + _TZ_SUFFIX
    + r"|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2} ?(?:[APap]\.?[Mm]\.?)?" + _TZ_SUFFIX
)
_SOURCE_KEYWORDS = {
    "property leads": "PPL",
    "propertyleads": "PPL",
    "pay per lead": "PPL",
    "ppl": "PPL",
    "pay per click": "PPC",
    "ppc": "PPC",
    "direct mail": "Direct Mail",
    "seo": "SEO",
    "referral": "Referral",
}

# Street-type words used to split "<street> <city>" when there's no comma
# between them (e.g. "302 Seascape Resort Dr Aptos").
_SUFFIX_SPLIT_RE = re.compile(
    r"\b(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|"
    r"Way|Pl|Place|Ter|Terrace|Cir|Circle|Hwy|Highway|Pkwy|Parkway|Trl|Trail|Loop|Sq|Square)\b\.?",
    re.I,
)
_LABEL_RE = {
    # No leading \b: the Zapier header glues "...PROPERTY LEADSName:" together.
    "seller_name": re.compile(r"Name\s*:\s*(.+)", re.I),
    "seller_phone": re.compile(r"\bPhone\s*:\s*(.+)", re.I),
    "seller_email": re.compile(r"\bEmail\s*:\s*(\S+@\S+)", re.I),
    "source_raw": re.compile(r"\bLead Source\s*:\s*(.+)", re.I),
    "address_raw": re.compile(r"\bProperty Address\s*:\s*(.+)", re.I),
}


def split_address(value: str) -> tuple[str, str, str]:
    """'302 Seascape Resort Dr Aptos, 95003' -> (street, city, zip).

    Splits city off after the street-type word; ZIP is the trailing 5 digits.
    City is left blank (to be flagged, not guessed) if we can't isolate it.
    """
    v = " ".join(value.split()).strip()
    zip_code = ""
    left = v
    if "," in v:
        left, right = v.split(",", 1)
        m = _ZIP_RE.search(right)
        if m:
            zip_code = m.group(1)
    if not zip_code:
        zips = _ZIP_RE.findall(v)
        if zips:
            zip_code = zips[-1]
            left = re.sub(rf"\b{zip_code}\b.*$", "", left).strip(" ,")
    left = left.strip(" ,")

    matches = list(_SUFFIX_SPLIT_RE.finditer(left))
    if matches:
        end = matches[-1].end()
        street = left[:end].strip(" ,")
        city = left[end:].strip(" ,")
    else:
        street, city = left, ""
    return street, city, zip_code
_ALLOWED_SOURCES = {"PPL", "PPC", "Direct Mail", "SEO", "Referral", "Other", "Unknown"}


def _extract_source(text: str) -> str:
    low = text.lower()
    for kw, val in _SOURCE_KEYWORDS.items():
        if kw in low:
            return val
    return ""


def _looks_labeled(text: str) -> bool:
    return bool(_LABEL_RE["address_raw"].search(text) or _LABEL_RE["seller_name"].search(text))


def parse_labeled_block(block: str) -> Lead:
    """Parse the 'NEW LEAD - PROPERTY LEADS' labeled format (Zapier posts)."""
    lead = Lead()
    text = block

    m = _LABEL_RE["seller_name"].search(text)
    if m:
        lead.seller_name = m.group(1).strip()
    m = _LABEL_RE["seller_phone"].search(text)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            lead.seller_phone = digits
    m = _LABEL_RE["seller_email"].search(text)
    if m:
        lead.seller_email = m.group(1).strip()
    m = _LABEL_RE["source_raw"].search(text)
    if m:
        lead.source = _extract_source(m.group(1)) or ""
    m = _LABEL_RE["address_raw"].search(text)
    if m:
        street, city, zip_code = split_address(m.group(1))
        lead.property_address = street
        lead.city = city
        lead.zip_code = zip_code

    # A timestamp may have been prepended by the Chat reader (sender/time line).
    mt = _TIMESTAMP_RE.search(text)
    if mt:
        lead.google_chat_timestamp = mt.group(0).strip()
    return lead


def parse_text_block(block: str) -> Lead:
    """Best-effort parse of one free-text notification block into a Lead.

    Confidently-found fields are filled; anything missing is flagged for
    verification rather than guessed.
    """
    if _looks_labeled(block):
        return parse_labeled_block(block)

    lead = Lead()
    text = block.strip()

    m = _EMAIL_RE.search(text)
    if m:
        lead.seller_email = m.group(0)

    m = _PHONE_RE.search(text)
    if m:
        lead.seller_phone = "".join(m.groups())

    m = _STREET_RE.search(text)
    if m:
        lead.property_address = m.group(0).strip()

    m = _STATE_CITY_RE.search(text)
    if m:
        lead.city = m.group(1).strip()

    # ZIP: prefer one in a "CA <zip>" context; else the LAST 5-digit group
    # (avoids mistaking a 5-digit street number like "15510" for a ZIP).
    zip_ca = re.search(r"\b(?:CA|California)[ ,]+(\d{5})\b", text, re.I)
    if zip_ca:
        lead.zip_code = zip_ca.group(1)
    else:
        zips = _ZIP_RE.findall(text)
        if zips:
            lead.zip_code = zips[-1]

    lead.source = _extract_source(text) or ""

    m = _TIMESTAMP_RE.search(text)
    if m:
        lead.google_chat_timestamp = m.group(0).strip()

    # We deliberately do NOT infer county from ZIP (that would be guessing).
    return lead


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------

def _lead_from_json(obj: dict) -> Lead:
    """Build a Lead from a structured JSON object (keys map to Lead fields)."""
    lead = Lead()
    for key, value in obj.items():
        field = key.strip().lower().replace(" ", "_").replace("?", "")
        aliases = {
            "address": "property_address",
            "propertyaddress": "property_address",
            "zip": "zip_code",
            "zipcode": "zip_code",
            "name": "seller_name",
            "phone": "seller_phone",
            "email": "seller_email",
            "timestamp": "google_chat_timestamp",
            "chat_timestamp": "google_chat_timestamp",
        }
        field = aliases.get(field, field)
        if hasattr(lead, field):
            setattr(lead, field, str(value).strip())
    return lead


def parse_notifications(raw_text: str, fmt: str, source_timezone: str) -> list[Lead]:
    """Parse the whole input into finalized (timezone-normalized) Leads."""
    leads: list[Lead] = []

    if fmt == "jsonl":
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            leads.append(_lead_from_json(json.loads(line)))
    elif fmt == "auto_text":
        # If the "NEW LEAD" marker is present, split on it so each lead's whole
        # block (including trailing action items) stays together. Otherwise fall
        # back to blank-line separation.
        if re.search(r"NEW LEAD", raw_text, re.I):
            parts = re.split(r"(?i)(?=NEW LEAD\b)", raw_text)
        else:
            parts = re.split(r"\n\s*\n", raw_text.strip())
        for block in parts:
            if block.strip():
                leads.append(parse_text_block(block))
    else:
        raise ValueError(f"Unknown leads format: {fmt!r} (use 'jsonl' or 'auto_text')")

    _finalize(leads, source_timezone)
    return leads


def _finalize(leads: Iterable[Lead], source_timezone: str) -> None:
    """Normalize timestamps to Pacific and flag missing required fields."""
    for lead in leads:
        ts_source = lead.google_chat_timestamp or lead.date_received
        dt, note = parse_timestamp(ts_source, source_timezone)
        if dt is not None:
            fill_calendar_fields(lead, dt)
            if note:
                lead.add_note(note)
        else:
            lead.needs_verification(f"timestamp ({note})")
            if not lead.verification_status or lead.verification_status == "Needs Verification":
                # A missing arrival date is a core field -> Missing Information.
                lead.verification_status = "Missing Information"

        # Core-field checks (never guess; flag instead).
        if not lead.property_address:
            lead.needs_verification("property address")
            lead.verification_status = "Missing Information"
        for label, value in (("city", lead.city), ("county", lead.county),
                             ("source", lead.source), ("ZIP", lead.zip_code)):
            if not value:
                lead.needs_verification(label)

        if lead.source and lead.source not in _ALLOWED_SOURCES:
            lead.add_note(f"non-standard source '{lead.source}' -> set to Unknown")
            lead.source = "Unknown"

        if not lead.verification_status:
            lead.verification_status = "Needs Verification"
