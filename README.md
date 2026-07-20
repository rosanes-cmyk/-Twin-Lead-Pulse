# Twin Lead Pulse — Lead Ingestion & REI BlackBook Enrichment

A small pipeline for **Equity Track Inc. / Twin Home Buyer** that turns incoming
Property Lead notifications into clean rows in the existing Google Sheet
dashboard, and enriches each lead by looking it up in REI BlackBook.

```
notifications ──▶ parse (Pacific time, never-guess) ──▶ dedupe ──▶ REI lookup (read-only) ──▶ Raw Lead Data tab
```

---

## ⚠️ Read this first: where this runs

**Run this on your own computer, not inside a Claude Code web session.**
The REI step opens a *visible* browser you log into by hand and keeps a saved
login profile between runs. A cloud/web session is headless and temporary — no
screen to log in on, and the profile is wiped when it ends. So: clone this repo
locally, install, and run it on your machine (Mac/Windows/Linux with a display).

**Security:** your REI login lives only in the local browser profile
(`.rei_profile/`, git-ignored). It is never written to the sheet, the code, or
any log. Never paste passwords into chat or commit them.

---

## What each lead row gets

Written into the `Raw Lead Data` tab — **manual columns only**; the sheet's
formula columns (`K–R` day/hour/week/month/year and `_DupFlag/_IssueFlag/_IssueText`)
are never touched. The four REI fields are **appended as new columns** so the
dashboard keeps working:

| Written by the script | Column(s) |
|---|---|
| Lead ID, Seller Name/Phone/Email, Address, City, County, ZIP | A–H |
| Date Received, Time Received (real date/time, **Pacific**) | I, J |
| Source, Google Chat Timestamp, Duplicate?, Verification Status, Original Source Location, Notes, Date Entered, Entered By | S–Z |
| **REI Match?, REI Contact Link, REI Tags, REI Status** | AD–AG (appended) |
| *(day of week, hour bucket, month, year, dup/issue flags)* | *left to the sheet's own formulas* |

Rules enforced in code: original notification timestamp only; convert to Pacific
(method noted in Notes); **never guess** — a missing field is left blank and
flagged `Needs Verification: <field>`; duplicates are **flagged, never merged**.

---

## Setup (once)

### 1. Install
```bash
git clone <this repo> && cd -Twin-Lead-Pulse
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Google Sheets access (one-time Google authorization)
The script writes to the Sheet via the Google Sheets API, so it needs credentials:

1. In [Google Cloud Console](https://console.cloud.google.com/) create (or pick) a
   project and **enable the Google Sheets API**.
2. **OAuth mode** (default): create an **OAuth client ID → Desktop app**, download
   it as `credentials.json` into this folder. On first run a browser opens once to
   approve; the token is saved to `token.json`.
   **Service-account mode** (alternative): create a service account, download its
   JSON as `credentials.json`, set `"auth_mode": "service_account"` in config, and
   **share the Sheet with the service account's email** (Editor).

### 3. Configure
```bash
cp config.example.json config.json
```
Edit `config.json` — `sheet_id` is already set to the current dashboard
(`1V_inTkuXkFDLNyDxxa5foM5Bxe2rUOfB6a_c1r_9tSw`); adjust `entered_by`,
`leads_format`, etc. as needed.

### 4. REI BlackBook login (one-time, visible browser)
```bash
python scripts/rei_login.py --config config.json
```
Log in (and finish any 2FA) in the window that opens, then press Enter in the
terminal. Your session is saved to `.rei_profile/` and reused every run.

---

## Providing the leads

Put the notifications in `leads.txt` (path set in config). Two formats:

- **`auto_text`** (default): notifications separated by blank lines; fields are
  extracted heuristically. See `leads.example.txt`. Tune the regexes in
  `pipeline/parsing.py` to match your real Chat notification wording.
- **`jsonl`** (most reliable): one JSON object per line, keys mapping to fields,
  e.g. `{"Property Address":"...","City":"...","ZIP":"...","Source":"PPL","Google Chat Timestamp":"2026-07-19 18:13 PST"}`

---

## Run

```bash
python run.py --config config.json --dry-run     # parse + preview, write nothing
python run.py --config config.json               # full run (writes rows + REI)
python run.py --config config.json --no-rei      # write rows, skip REI lookup
```

Offline logic tests (no network/credentials needed):
```bash
python tests/test_parsing.py
```

---

## Guardrails (by design)

- **REI is read-only** — the automation only navigates and reads. It never sends
  texts, applies tags, or changes anything.
- **Dashboard tabs are never edited** — only the `Raw Lead Data` tab is appended to.
- **Fail safe** — any field the code can't read confidently is left blank with a
  `Needs Verification` note rather than guessed.
- Credentials stay local (`.rei_profile/`, `credentials.json`, `token.json` are
  all git-ignored).

## Tuning notes / known limits

- **REI DOM selectors** (`pipeline/rei_client.py`, marked `# TUNE`) are best-effort;
  REI BlackBook has no public API and its markup isn't documented. If a lookup
  can't find the search box / tags / tabs on your account, capture the page HTML
  and adjust the selector lists. Everything degrades to `Needs Verification`
  instead of crashing, so a stale selector is safe, just less complete.
- **County** is never inferred from ZIP (that would be guessing). If a
  notification omits county, the row is flagged for verification.
- **REI status** is inferred from message keywords (Active/Sold/Listed/Opted-out/
  etc.); treat it as a hint and confirm the flagged ones.
