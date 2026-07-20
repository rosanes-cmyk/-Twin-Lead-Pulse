"""Duplicate detection: compare a lead against already-entered rows.

We never delete or merge (per the brief) — we only *flag*:
  - exact normalized address (or phone/email) already present -> "Yes"
  - a softer/partial signal                                   -> "Possible"
"""

from __future__ import annotations

import re

from .models import Lead

_STATE_ZIP_TAIL = re.compile(r",?\s*(?:CA|California)\b.*$", re.I)


def normalize_address(addr: str) -> str:
    """Reduce to the street segment for matching.

    Sheet rows often carry the full "street, city, ST zip, country" string
    while a fresh notification may only have "street". We key on the street
    portion (everything before the first comma), so both forms match.
    """
    if not addr:
        return ""
    a = addr.strip()
    if "," in a:
        a = a.split(",", 1)[0]                # street segment only
    a = a.lower()
    a = _STATE_ZIP_TAIL.sub("", a)            # handle no-comma "... CA 95003" tails
    a = re.sub(r"[.#]", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


class DuplicateIndex:
    """Tracks addresses/phones/emails seen so far (existing sheet rows + batch)."""

    def __init__(self) -> None:
        self.addresses: set[str] = set()
        self.phones: set[str] = set()
        self.emails: set[str] = set()

    def add(self, address: str = "", phone: str = "", email: str = "") -> None:
        if normalize_address(address):
            self.addresses.add(normalize_address(address))
        if normalize_phone(phone):
            self.phones.add(normalize_phone(phone))
        if (email or "").strip():
            self.emails.add(email.strip().lower())

    def classify(self, lead: Lead) -> str:
        """Return 'Yes' | 'Possible' | 'No' for this lead vs. what's been seen."""
        addr = normalize_address(lead.property_address)
        phone = normalize_phone(lead.seller_phone)
        email = (lead.seller_email or "").strip().lower()

        # Exact match on address, phone, or email == same lead -> confirmed.
        if addr and addr in self.addresses:
            return "Yes"
        if phone and phone in self.phones:
            return "Yes"
        if email and email in self.emails:
            return "Yes"
        return "No"
