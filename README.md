# ECI Electoral Roll (E‑Roll) PDF Downloader

A production‑ready Python + Selenium automation for the Election Commission of
India **Download E‑Roll** page:

> https://voters.eci.gov.in/download-eroll

It downloads every available **ENGLISH** electoral‑roll PDF for a chosen
**State → Year of Revision → Roll Type → District → Assembly Constituency**,
part by part, validates each file, names them cleanly, and keeps a **resumable
CSV progress report**.

---

## ⚖️ Compliance — please read first

This tool is deliberately built to respect the ECI website:

- **The CAPTCHA is solved by you, the human — never by the script.** The browser
  opens in **visible (non‑headless)** mode and the program **pauses** so you can
  read and type the CAPTCHA yourself.
- **No** OCR, **no** CAPTCHA‑solving services, **no** browser extensions, **no**
  cookie/session/API manipulation, **no** bypass tricks of any kind.
- It works **sequentially**, with **delays between downloads**, and **never**
  uses aggressive concurrency — so it does not hammer the government server.

Please only download data you are entitled to access, and use it in line with
the ECI website’s Terms & Conditions.

---

## ✨ Features

- Cascading dropdown selection with **explicit waits** (no brittle `sleep`‑only
  logic).
- Hard **language guard** — if English is selected it will **never** download a
  regional‑language PDF.
- **Manual CAPTCHA** pause with a clear on‑screen prompt.
- Handles the **paginated** parts table (10 parts/page) and reads every page.
- Uses the **“Select All”** checkbox when present, else selects each part.
- Handles **individual PDF** downloads *and* **ZIP** archives.
- Waits for `.crdownload` / `.tmp` / `.part` files to finish before validating.
- **PDF validation**: `%PDF` header **and** PyMuPDF/pypdf opens it with ≥ 1 page.
- Treats zero‑byte / too‑small / header‑less / unparyable / zero‑page / partial /
  HTML‑error files as **corrupt** → deletes and **retries that part** (up to 3×,
  **exponential backoff**), logging every retry.
- **Duplicate‑safe & resumable**: skips parts whose valid PDF already exists;
  re‑downloads invalid ones; resumes from the CSV after a restart/crash.
- Clean **folder structure** and **meaningful filenames**.
- Full **logging** to terminal **and** `electoral_roll_downloader.log`.
- Robust handling of `StaleElementReferenceException`, `TimeoutException`,
  `ElementClickInterceptedException`, browser crashes, session expiry, etc.

---

## 📁 Output layout

```
downloads/
  Andhra_Pradesh/
    2025/
      Prakasam/
        108_Ongole/
          English/
            AP_108_Ongole_Part_001_English.pdf
            AP_108_Ongole_Part_002_English.pdf
            ...
            progress_report.csv
```

Filename pattern: `AP_108_Ongole_Part_051_English.pdf`
(State abbrev · AC number · AC name · Part number · Language)

---

## 🔧 Requirements

- **Python 3.11+**
- **Google Chrome** installed (any recent version)
- Packages in `requirements.txt` (Selenium 4, webdriver‑manager, PyMuPDF, pypdf,
  pandas)

`webdriver-manager` downloads the matching **chromedriver** automatically, so
you normally don’t need to install a driver by hand.

---

## 🚀 Setup

### Windows

```bat
:: 1) install Python 3.11+ from python.org (tick "Add to PATH")
:: 2) in the project folder:
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python electoral_roll_downloader.py
```

### macOS

```bash
# 1) install Python (e.g. via python.org or Homebrew) and Google Chrome
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 electoral_roll_downloader.py
```

> If Chrome is installed in a non‑standard location, set `CHROME_BINARY` in the
> config. If you prefer a manually‑installed driver, set `CHROMEDRIVER_PATH`.

---

## ⚙️ Configuration (edit the `CONFIG` block at the top of the script)

```python
CONFIG = {
    "STATE_CODE": "S01",                     # ECI code used in the URL
    "STATE_NAME": "Andhra Pradesh",          # must match the option text
    "YEAR_OF_REVISION": "2025",
    "ROLL_TYPE": "Final Roll - 2025",        # exact option text is safest
    "DISTRICT": "Prakasam",                  # "" to skip (optional field)
    "ASSEMBLY_CONSTITUENCY": "108 - Ongole", # "<number> - <name>"
    "LANGUAGE": "ENGLISH",                   # default; never mixes languages

    "DOWNLOAD_ROOT": "downloads",
    "MAX_RETRIES": 3,
    "DOWNLOAD_STRATEGY": "per_part",         # "per_part" or "select_all"
    ...
}
```

**Tip — finding the exact option text:** open the page in Chrome, click a
dropdown, and copy the wording exactly (e.g. `Final Roll - 2025`). Matching is
case‑insensitive and also accepts a partial match, so `Ongole` works as well as
`108 - Ongole`.

**State codes:** the code in the URL (`?stateCode=S01`) selects the State and the
State dropdown is then locked on the page — that’s expected; the script detects
it and verifies the name. Change both `STATE_CODE` and `STATE_NAME` together.

---

## 🧩 How manual CAPTCHA handling works

1. The script fills in all dropdowns and loads the parts list automatically.
2. When it’s ready to download it prints:

   ```
   ====================================================================
     Please solve the CAPTCHA manually in the browser.
     1) Type the CAPTCHA characters into the 'Enter Captcha' box.
     2) Come back here and press Enter to continue.
   ====================================================================
   ```

3. You type the CAPTCHA **in the Chrome window**, then press **Enter** in the
   terminal. The script then clicks **Download Selected PDFs** and waits for the
   file(s).
4. In `per_part` mode you’ll solve one CAPTCHA per part. This is the most
   reliable and gives deterministic filenames. If you’d rather solve fewer
   CAPTCHAs, set `DOWNLOAD_STRATEGY = "select_all"` (see notes below).

The script **never** reads or guesses the CAPTCHA — it only waits for you.

---

## 🔁 Resume after a restart

Just run the script again with the **same** config. It reads
`progress_report.csv`, re‑checks the files on disk, **skips** parts that are
already valid, and continues with the pending/failed ones.

---

## 🛠️ Troubleshooting

| Symptom | Fix |
|---|---|
| `chromedriver`/Chrome version mismatch | Update Chrome; `webdriver-manager` will fetch the matching driver. Or set `CHROMEDRIVER_PATH`. |
| “Option ‘…’ not found in ‘roll_type’” | Copy the **exact** option text from the dropdown into the config. |
| Parts table never appears | Make sure **all** dropdowns (incl. Language) are set; some selections legitimately have no roll published yet. |
| Download doesn’t start after CAPTCHA | The CAPTCHA was likely wrong/expired — the script will retry; type carefully. |
| PDF flagged corrupt every time | The server may be returning an error page; open the page manually to confirm the roll exists for that selection. |
| Files open in a PDF viewer instead of downloading | Already handled (`plugins.always_open_pdf_externally`), but check Chrome isn’t overriding it via profile settings. |
| Browser closes instantly | Don’t run headless; keep it visible so you can solve the CAPTCHA. |

---

## 🧭 Where selectors live (if the ECI site changes)

All selectors are in the **`SELECTORS`** dictionary near the top of the script,
captured from the live DOM:

- Dropdowns: `#stateCode`, `#revyear`, `#roleType`, `#district`,
  `#constituency`, `#langCd`
- Parts table: `table.contenttable-eroll`; header `#selectAll`; rows
  `tbody tr` → `<td><input type=checkbox></td><td>1 - NAME</td>`
- Pagination: `ul.pagination`
- CAPTCHA: input `#captcha`; refresh icon `img[aria-label*='refrsh']`
- Download button: `button.submit`

Lines commented **“ADJUST IF SITE CHANGES”** flag the spots most likely to need
tuning after a site redesign.

### Note on `select_all` strategy & ZIP mapping
The `per_part` strategy (default) downloads one part at a time, so each file maps
cleanly to its part number and per‑part retry is exact. The `select_all`
strategy is faster (fewer CAPTCHAs) but, because the exact ZIP/file naming the
server returns can only be confirmed on a live run, its file→part mapping is
best‑effort. On your first `select_all` run, check the log for the raw
downloaded filenames and adjust the mapping in `_finalise_single_file` if
needed. When in doubt, use `per_part`.
