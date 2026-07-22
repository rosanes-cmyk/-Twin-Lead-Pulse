"""Turn a captured Google Chat conversation into dated lead blocks.

Google Chat shows each message's time as a *relative* label next to it
(`Thu 8:32 AM`, `Yesterday 6:13 PM`, or a bare `8:49 AM` under a `Today`
divider), with day-divider lines like `Saturday, Jul 18` / `Yesterday` /
`Today` separating days. The lead text itself has no timestamp.

`leads_from_conversation` walks the captured text, tracks the date context, and
for each `NEW LEAD - PROPERTY LEADS` message resolves the real Pacific date/time
from the time label that immediately precedes it. It returns enriched blocks
(the original field lines with a `Google Chat Timestamp:` line inserted) that
the normal labeled parser then consumes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# A time label, optionally prefixed by a relative day word/weekday.
_TIME_TOKEN_RE = re.compile(
    r"^(?:(Today|Yesterday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?(\d{1,2}):(\d{2})\s*([AaPp][Mm])$"
)
# Day dividers, several shapes:
#   "Friday, Jul 17"      -> weekday, mon dd
#   "Jul 16" / "July 16"  -> mon dd (current year assumed)
#   "Jul 16, 2026"        -> mon dd, yyyy
_DIVIDER_FULL_RE = re.compile(r"^[A-Z][a-z]+day,\s+([A-Z][a-z]{2,8})\s+(\d{1,2})(?:,\s*(\d{4}))?$")
_DIVIDER_MONTH_RE = re.compile(r"^([A-Z][a-z]{2,8})\s+(\d{1,2})(?:,\s*(\d{4}))?$")
_LEAD_MARKER = "NEW LEAD"
_END_MARKERS = ("ACTION NEEDED", "\U0001F525")   # "ACTION NEEDED" or 🔥


def _most_recent_weekday(now: date, wd: int) -> date:
    delta = (now.weekday() - wd) % 7
    return now - timedelta(days=delta)


def _resolve_date(day_word: str | None, ctx_date, now: date):
    if not day_word:
        return ctx_date            # may be None -> caller won't guess a date
    w = day_word.lower()
    if w == "today":
        return now
    if w == "yesterday":
        return now - timedelta(days=1)
    if w in _WEEKDAYS:
        return _most_recent_weekday(now, _WEEKDAYS[w])
    return ctx_date or now


def _resolve_divider(line: str, now: date) -> date | None:
    s = line.strip()
    low = s.lower()
    if low == "today":
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    m = _DIVIDER_FULL_RE.match(s) or _DIVIDER_MONTH_RE.match(s)
    if m:
        mon = m.group(1)[:3].lower()
        if mon in _MONTHS:
            month, day = _MONTHS[mon], int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                d = date(year, month, day)
            except ValueError:
                return None
            if not m.group(3) and d > now:      # no explicit year & in the future -> last year
                d = date(now.year - 1, month, day)
            return d
    return None


def _iso(d: date, hour: int, minute: int) -> str:
    return f"{d.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}"


def leads_from_conversation(text: str, now: datetime) -> list[str]:
    """Return enriched 'NEW LEAD' blocks (oldest -> newest) with a resolved
    `Google Chat Timestamp:` line inserted after the header."""
    now_date = now.date()
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and ln != ","]

    ctx_date: date | None = None
    pending_iso: str | None = None
    blocks: list[str] = []
    seen: set[str] = set()

    i = 0
    while i < len(lines):
        line = lines[i]

        div = _resolve_divider(line, now_date)
        if div is not None:
            ctx_date = div
            i += 1
            continue

        tok = _TIME_TOKEN_RE.match(line)
        if tok:
            day_word, hh, mm, ap = tok.groups()
            hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
            d = _resolve_date(day_word, ctx_date, now_date)
            pending_iso = _iso(d, hour, int(mm)) if d is not None else None
            i += 1
            continue

        if _LEAD_MARKER in line.upper():
            # Collect the lead's field lines until an end marker / next section.
            body = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if (any(nxt.upper().startswith(m) or m in nxt for m in _END_MARKERS)
                        or _LEAD_MARKER in nxt.upper()
                        or _TIME_TOKEN_RE.match(nxt)
                        or _resolve_divider(nxt, now_date) is not None
                        or nxt in ("Zapier", "App")):
                    break
                body.append(nxt)
                j += 1

            ts_line = f"Google Chat Timestamp: {pending_iso}" if pending_iso else ""
            block = "\n".join([line, ts_line, *body]).strip()
            key = " ".join(block.split()).lower()[:140]
            if key not in seen:
                seen.add(key)
                blocks.append(block)
            i = j
            continue

        i += 1

    return blocks
