"""ZIP-code -> county lookup (deterministic, not a guess).

Uses the `pgeocode` package (US postal data, cached locally after a one-time
download). If pgeocode isn't installed or the data can't be fetched, returns ""
so the caller keeps the existing "flag it" behaviour rather than guessing.
"""

from __future__ import annotations

_nomi = None
_failed = False


def zip_to_county(zip_code: str) -> str:
    """'95003' -> 'Santa Cruz County' (matches the sheet's convention). '' if unknown."""
    global _nomi, _failed
    z = "".join(c for c in (zip_code or "") if c.isdigit())[:5]
    if len(z) != 5 or _failed:
        return ""
    try:
        if _nomi is None:
            import pgeocode
            _nomi = pgeocode.Nominatim("us")
        rec = _nomi.query_postal_code(z)
        county = getattr(rec, "county_name", None)
        # pandas returns NaN (a float) for unknown fields.
        if isinstance(county, str) and county.strip() and county.strip().lower() != "nan":
            c = county.strip()
            return c if c.lower().endswith("county") else f"{c} County"
    except Exception:
        _failed = True   # pgeocode missing or data unreachable -> stop trying
    return ""
