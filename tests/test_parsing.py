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


def test_labeled_zapier_format():
    raw = (
        "NEW LEAD - PROPERTY LEADSName: Ganesh Iyer\n"
        "Phone: 2812218511\n"
        "Email:  gaiyer777@yahoo.com\n"
        "Lead Source: Property Leads\n"
        "Property Address: 302 Seascape Resort Dr Aptos, 95003\n\n"
        "ACTION NEEDED:\n1. Call this lead\n\n"
        "NEW LEAD - PROPERTY LEADSName: William Santora\n"
        "Phone: 5103013126\n"
        "Lead Source: Property Leads\n"
        "Property Address: 15510 Montreal St San Leandro, 94579\n"
    )
    leads = parse_notifications(raw, "auto_text", "America/Los_Angeles")
    assert len(leads) == 2, f"expected 2 leads, got {len(leads)}"
    a, b = leads
    assert a.seller_name == "Ganesh Iyer" and a.seller_phone == "2812218511"
    assert a.property_address == "302 Seascape Resort Dr" and a.city == "Aptos" and a.zip_code == "95003"
    assert a.source == "PPL"
    assert b.seller_name == "William Santora"
    assert b.city == "San Leandro" and b.zip_code == "94579"   # multi-word city


def test_split_address():
    from pipeline.parsing import split_address
    assert split_address("302 Seascape Resort Dr Aptos, 95003") == ("302 Seascape Resort Dr", "Aptos", "95003")
    assert split_address("15510 Montreal St San Leandro, 94579") == ("15510 Montreal St", "San Leandro", "94579")


def test_chat_conversation_timestamps():
    from datetime import datetime
    from pipeline.chat_parse import leads_from_conversation
    convo = (
        "Zapier\n,\nApp\n,\nThu 8:32 AM\n,\nZap sent by @Bryan\n"
        "NEW LEAD - PROPERTY LEADS\nName: Varun Bhat\nPhone: 9598882646\n"
        "Lead Source: Property Leads\nProperty Address: 7323 Bower Ln Dublin, 94568\n"
        "ACTION NEEDED:\n,\nThu 8:32 AM\n,\n"
        "Yesterday\nZapier\n,\nApp\n,\nYesterday 6:13 PM\n,\n"
        "NEW LEAD - PROPERTY LEADS\nName: Elias Hernandez\nPhone: 7073642931\n"
        "Lead Source: Property Leads\nProperty Address: 420 Henry Cowell Dr Santa Cruz, 95060\n"
        "ACTION NEEDED:\n,\nYesterday 6:13 PM\n,\n"
        "Today\nZapier\n,\nApp\n,\n8:49 AM\n,\n"
        "NEW LEAD - PROPERTY LEADS\nName: Ganesh Iyer\nPhone: 2812218511\n"
        "Lead Source: Property Leads\nProperty Address: 302 Seascape Resort Dr Aptos, 95003\n"
        "ACTION NEEDED:\n,\n8:49 AM\n,\n"
    )
    now = datetime(2026, 7, 20, 13, 30)          # Monday
    blocks = leads_from_conversation(convo, now)
    leads = parse_notifications("\n\n".join(blocks), "auto_text", "America/Los_Angeles")
    got = {l.seller_name: (l.date_received, l.time_received, l.day_of_week) for l in leads}
    assert got["Varun Bhat"] == ("07/16/2026", "8:32 AM", "Thursday"), got
    assert got["Elias Hernandez"] == ("07/19/2026", "6:13 PM", "Sunday"), got
    assert got["Ganesh Iyer"] == ("07/20/2026", "8:49 AM", "Monday"), got


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
