# Twin Lead Pulse — Property Lead Pipeline

Automates the "Incoming Property Leads" workflow for **Equity Track Inc. / Twin
Home Buyer**:

```
Google Chat space ─▶ parse each lead ─▶ dedupe ─▶ write to the dashboard Sheet ─▶ enrich from REI BlackBook
```

For every `NEW LEAD - PROPERTY LEADS` message posted in the Chat space, it writes
one clean row into the **Raw Lead Data** tab (Pacific-time date/time, deduped,
never guessing missing fields) and fills in the REI **contact link, tags, and
status** by looking the lead up in REI BlackBook by phone.

---

## What you run day to day

From inside the `twinleadpulse` folder:

| Goal | Command |
|---|---|
| Pull new leads from Chat into the Sheet (with REI enrichment) | `python run.py --config config.json --from-chat` |
| Backfill / refresh REI link+tags+status on rows already in the Sheet | `python run.py --config config.json --enrich-sheet` |
| Preview only, write nothing | add `--dry-run` |
| Skip the REI step | add `--no-rei` |

New leads that are already in the Sheet are skipped automatically (matched by
address, phone, or email), so you can run it as often as you like.

---

## ⚠️ Network requirement (read this)

The automated browser must be able to load **both** `chat.google.com` **and**
`my.reiblackbook.com`. Some office networks / endpoint-security software block
*automated* browsers from reaching REI (even though your normal Chrome works).

**If REI pages won't load** (Chat works but REI hangs):
- Run on a network that doesn't block it — a **phone hotspot** is the quickest test, or
- Have IT **whitelist `my.reiblackbook.com`** for the automation on the office network.

Chat and the Sheet worked on the office network in testing; REI needed an
unrestricted network. Everything else (dates, dedup, Sheet writes) is unaffected.

---

## One-time setup (per computer)

1. **Python** — install from [python.org](https://www.python.org/downloads/); on the
   first installer screen check **"Add python.exe to PATH"**. Reopen the terminal.
2. **Get the code:**
   ```
   cd %USERPROFILE%
   git clone https://github.com/rosanes-cmyk/-twin-lead-pulse.git twinleadpulse
   cd twinleadpulse
   ```
3. **Dependencies:**
   ```
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```
4. **Config:**
   ```
   copy config.example.json config.json
   ```
   Defaults are correct (service-account auth, real Chrome). `sheet_id` and the
   Chat `space_url` are already filled in.
5. **Google key** — put your service-account **`credentials.json`** in this folder,
   and share the Sheet with the service account's email as **Editor**
   (see *Google setup* below). Needed for any command that writes to the Sheet.
6. **Log in once** (visible browser windows; sessions are saved and reused):
   ```
   python scripts\chat_login.py --config config.json
   ```
   → sign into Google, open the "Incoming Property Leads" space, press Enter.
   ```
   python scripts\rei_login.py --config config.json
   ```
   → sign into REI BlackBook. REI emails a one-time verification link the first
   time — open it in that same window, then press Enter after you reach the
   dashboard.

---

## Google setup (service account, one-time)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project
   (e.g. *Twin Lead Pulse*) and **enable the Google Sheets API**.
2. Create a **Service Account**, then **Add Key → JSON**; save the downloaded file
   as **`credentials.json`** in the `twinleadpulse` folder.
3. Open the dashboard Sheet → **Share** → add the service account's email
   (`…@….iam.gserviceaccount.com`) as **Editor**.

No billing or credit card is required — the Sheets API is free at this volume.

---

## How REI matching works

- REI's search is **"Search By Name, Phone"**, so the tool searches by **phone
  first** (most reliable), then seller name. Property address is not used for
  search (REI doesn't index it).
- On a match it opens `https://my.reiblackbook.com/contacts/<id>` and reads:
  - **REI Contact Link** = that URL
  - **REI Tags** = every tag on the contact
  - **REI Status** = inferred from the contact's messages (Active / Sold / Listed /
    Opted-out / Not Interested / Wrong Number / No Contact Yet)
- If no contact matches, the row is left with `REI Match? = No` and flagged for
  manual review — never guessed.

---

## What gets written (and what's protected)

Only the **Raw Lead Data** tab is touched. Manual columns written:

`A–H` Lead ID · Seller Name/Phone/Email · Property Address · City · County · ZIP
`I–J` Date Received · Time Received (real date/time, **Pacific**)
`S–Z` Source · Google Chat Timestamp · Duplicate? · Verification Status · Original Source Location · Notes · Date Entered · Entered By
`AD–AG` **REI Match? · REI Contact Link · REI Tags · REI Status** (appended)

**Never touched:** the formula columns `K–R` (Day of Week, Hour, Week, Month,
Year) and `AA–AC` (`_DupFlag/_IssueFlag/_IssueText`), and every other tab
(Dashboard, Duplicate Review, Data Summary, Executive Summary, Issues).

---

## Guardrails

- **Read-only in REI** — the tool only navigates and reads; it never sends texts,
  applies tags, or changes anything.
- **Never guesses** — a missing/unreadable field is left blank and flagged
  `Needs Verification`, never invented (e.g. county, which isn't in the messages).
- **Deduped** — leads already in the Sheet are skipped, so re-runs don't duplicate.
- **Credentials stay local** — `credentials.json` and the browser profiles
  (`.chat_profile`, `.rei_profile`) are git-ignored and never leave the machine.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Python was not found` | Install Python and re-open the terminal (check "Add to PATH"). |
| `not a git repository` / `can't open run.py` | You're not in the folder — run `cd twinleadpulse` first. |
| `Client secrets must be for a web or installed app` | `config.json` has `auth_mode` wrong, or `credentials.json` is the wrong key type — use the **service-account** JSON with `auth_mode: service_account`. |
| REI keeps asking to verify email | Log in once fully in the script's own window (open the emailed link there), then press Enter. |
| REI page won't load (Chat does) | Network is blocking the automated browser — use a phone hotspot or whitelist `my.reiblackbook.com`. |
| REI shows `match=No` for everyone | Make sure you're logged into REI in the script's profile; the window must be desktop-width (handled automatically). |
| Only new leads matter | The tool skips anything already in the Sheet; that's expected. |

Debug helpers: `--rei-dump` (prints REI page structure) and
`--rei-search "<query>"` (types a query into REI search and reports the result).

---

## Tests

Offline logic (parsing, timezone, dedup, timestamp resolution) — no network/keys:
```
python tests\test_parsing.py
```
