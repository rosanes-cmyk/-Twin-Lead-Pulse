"""Offline tests for parsing/timezone/dedupe (stdlib only — no network, no deps).

Run:  python -m pytest -q     (or)     python tests/test_parsing.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.parsing import hour_bucket, parse_timestamp, parse_notifications
from pipeline.dedupe import DuplicateIndex, normalize_address


def test_hour_bucket():
    assert hour_bucket(8) == "8:00-8:59 AM"
    assert hour_bucket(0) == "0:00-12:59 AM"
    assert hour_bucket(12) == "12:00-12:59 PM"
    assert hour_bucket(13) == "13:00-1:59 PM"
    assert hour_bucket(23) == "23:00-11:59 PM"


def test_timestamp_pacific_passthrough():
    dt, note = parse_timestamp("2025-01-06 08:15", "America/Los_Angeles")
    assert dt is not None
    assert dt.hour == 8 and dt.minute == 15
    assert note == ""  # already Pacific, assumed


def test_timestamp_est_to_pacific():
    # 11:15 EST -> 08:15 PST (3 hours behind), same date.
    dt, note = parse_timestamp("01/06/2025 11:15 AM EST", "America/Los_Angeles")
    assert dt is not None
    assert dt.hour == 8 and dt.minute == 15
    assert "TZ conversion" in note


def test_timestamp_unparseable_is_flagged_not_guessed():
    dt, note = parse_timestamp("sometime last week", "America/Los_Angeles")
    assert dt is None
    assert "unparseable" in note


def test_normalize_address():
    a = normalize_address("302 Seascape Resort Dr, Aptos, CA 95003, USA")
    b = normalize_address("302 Seascape Resort Dr")
    assert a == b == "302 seascape resort dr"


def test_dedupe_flags_exact_address():
    idx = DuplicateIndex()
    idx.add(address="15510 Montreal St, San Leandro, CA 94579")

    class L:  # minimal stand-in
        property_address = "15510 Montreal St"
        seller_phone = ""
        seller_email = ""

    assert idx.classify(L()) == "Yes"


def test_jsonl_parse_and_finalize():
    line = ('{"Property Address":"420 Henry Cowell Dr","City":"Santa Cruz",'
            '"ZIP":"95060","Source":"PPL","Google Chat Timestamp":"2026-07-19 18:13"}')
    leads = parse_notifications(line, "jsonl", "America/Los_Angeles")
    assert len(leads) == 1
    lead = leads[0]
    assert lead.property_address == "420 Henry Cowell Dr"
    assert lead.date_received == "07/19/2026"
    assert lead.time_received == "6:13 PM"
    assert lead.day_of_week == "Sunday"
    assert lead.source == "PPL"
    # County missing -> flagged, not guessed.
    assert "county" in lead.notes.lower()


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed.")


if __name__ == "__main__":
    _run()
