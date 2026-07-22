"""ZIP-code -> county lookup (deterministic, not a guess).

Uses the `pgeocode` package (US postal data, cached locally after a one-time
download). If pgeocode isn't installed or the data can't be fetched, returns ""
so the caller keeps the existing "flag it" behaviour rather than guessing.
"""

from __future__ import annotations

_nomi = None
_failed = False


def _field(zip_code: str, attr: str) -> str:
    """Return a string field (county_name/place_name) for a ZIP, or ''."""
    global _nomi, _failed
    z = "".join(c for c in (zip_code or "") if c.isdigit())[:5]
    if len(z) != 5 or _failed:
        return ""
    try:
        if _nomi is None:
            import pgeocode
            _nomi = pgeocode.Nominatim("us")
        val = getattr(_nomi.query_postal_code(z), attr, None)
        if isinstance(val, str) and val.strip() and val.strip().lower() != "nan":
            return val.strip()
    except Exception:
        _failed = True   # pgeocode missing or data unreachable -> stop trying
    return ""


def zip_to_county(zip_code: str) -> str:
    """'95003' -> 'Santa Cruz County' (matches the sheet's convention). '' if unknown."""
    c = _field(zip_code, "county_name")
    if not c:
        return ""
    return c if c.lower().endswith("county") else f"{c} County"


def zip_to_city(zip_code: str) -> str:
    """'95003' -> 'Aptos' (postal place name). '' if unknown."""
    return _field(zip_code, "place_name")
