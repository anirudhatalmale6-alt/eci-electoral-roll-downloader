#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ECI Electoral Roll (E-Roll) PDF Downloader
==========================================

Production-ready Selenium automation for the Election Commission of India
"Download E-Roll" page:

    https://voters.eci.gov.in/download-eroll

It downloads every available ENGLISH electoral-roll PDF for a selected
State / Year of Revision / Roll Type / District / Assembly Constituency,
part by part, validating each file and keeping a resumable CSV progress
report.

IMPORTANT — COMPLIANCE
----------------------
This tool does NOT bypass, solve, evade or automate the CAPTCHA in any way.
The browser runs in visible (non-headless) mode and the script PAUSES so a
human can read and type the CAPTCHA manually. No OCR, no solving services,
no cookie/session/API manipulation, no CAPTCHA-defeat techniques are used.

The script is intentionally polite to the government server: it works
sequentially, adds delays between actions and never uses aggressive
concurrency.

Tested target DOM (captured live on the real page):
    - Dropdowns are native <select> elements:
        #stateCode  #revyear  #roleType  #district  #constituency  #langCd
      (When the URL carries ?stateCode=Sxx the State <select> is disabled and
       pre-filled — the script detects and handles that.)
    - Parts appear in a paginated table:  table.contenttable-eroll
        header "Select All" checkbox -> #selectAll
        each row -> <td><input type=checkbox></td><td>1 - DODIPUTTU</td>
        pagination -> ul.pagination  (10 parts per page)
    - CAPTCHA input -> #captcha (maxlength 6), image is a base64 data-URI,
      refresh icon has aria-label "refrsh captcha".
    - Submit button -> button.submit  ("Download Selected PDFs")

Every selector lives in the SELECTORS dict below so it is trivial to update
if the ECI site changes.  Comments marked "ADJUST IF SITE CHANGES" flag the
few spots most likely to need tuning.

Author: Anirudha Talmale
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
#  Third-party imports (see requirements.txt)                                  #
# --------------------------------------------------------------------------- #
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAVE_WDM = True
except Exception:  # pragma: no cover - optional
    _HAVE_WDM = False

# PDF validation: prefer PyMuPDF (fitz); fall back to pypdf.
_PDF_BACKEND = None
try:
    import fitz  # PyMuPDF

    _PDF_BACKEND = "pymupdf"
except Exception:
    try:
        from pypdf import PdfReader  # type: ignore

        _PDF_BACKEND = "pypdf"
    except Exception:
        _PDF_BACKEND = None

# pandas is used for the CSV report (a thin wrapper; csv module is the fallback)
try:
    import pandas as pd

    _HAVE_PANDAS = True
except Exception:
    _HAVE_PANDAS = False


# =========================================================================== #
#  1. USER CONFIGURATION  — edit the values in this block                      #
# =========================================================================== #

CONFIG = {
    # ---- What to download -------------------------------------------------- #
    # State is chosen by its ECI state code (used in the page URL) AND its
    # visible name (used for folder names / verification).
    "STATE_CODE": "S01",                 # e.g. S01 = Andhra Pradesh
    "STATE_NAME": "Andhra Pradesh",      # must match the option text on the site

    # The following four are matched against the *visible text* of each
    # dropdown.  Matching is case-insensitive and accepts a partial match,
    # so "Prakasam" or "108 - Ongole" both work.
    "YEAR_OF_REVISION": "2025",          # e.g. 2026 / 2025 / 2024
    "ROLL_TYPE": "Final Roll - 2025",    # exact option text is safest
    "DISTRICT": "Prakasam",              # leave "" to skip (District is optional)
    "ASSEMBLY_CONSTITUENCY": "108 - Ongole",  # "<number> - <name>"

    "LANGUAGE": "ENGLISH",               # ENGLISH (default). Never mixes languages.

    # ---- Where / how -------------------------------------------------------- #
    "DOWNLOAD_ROOT": "downloads",        # base output folder
    "MAX_RETRIES": 3,                    # per-part retry attempts on failure
    "MIN_PDF_BYTES": 2048,               # smaller than this => treated as corrupt

    # ---- Timing (be gentle with the government server) --------------------- #
    "PAGE_LOAD_TIMEOUT": 60,             # seconds
    "ELEMENT_TIMEOUT": 30,               # explicit-wait timeout (seconds)
    "DOWNLOAD_TIMEOUT": 300,             # max wait for a download to finish
    "DELAY_BETWEEN_PARTS": 2.5,          # polite pause between part downloads
    "RETRY_BACKOFF_BASE": 3,            # exponential backoff base (seconds)

    # ---- Download strategy -------------------------------------------------- #
    # "per_part"  : select ONE part, solve CAPTCHA, download -> deterministic
    #               filenames + clean per-part retry (recommended, most robust).
    # "select_all": use the "Select All" checkbox and download everything on the
    #               current page in one shot (fewer CAPTCHAs; the server may
    #               return a ZIP).  Filename mapping is best-effort — see notes.
    "DOWNLOAD_STRATEGY": "per_part",

    # ---- Browser ------------------------------------------------------------ #
    # Non-headless is MANDATORY so a human can solve the CAPTCHA.
    "CHROME_BINARY": "",                 # optional explicit path to Chrome
    "CHROMEDRIVER_PATH": "",             # optional explicit path to chromedriver
    "BASE_URL": "https://voters.eci.gov.in/download-eroll",
}


# =========================================================================== #
#  2. SELECTORS  — one place to update if the ECI site markup changes          #
# =========================================================================== #

SELECTORS = {
    # Cascading dropdowns (native <select> elements)
    "state":         (By.ID, "stateCode"),
    "year":          (By.ID, "revyear"),
    "roll_type":     (By.ID, "roleType"),
    "district":      (By.ID, "district"),
    "constituency":  (By.ID, "constituency"),
    "language":      (By.ID, "langCd"),

    # CAPTCHA
    "captcha_input":   (By.ID, "captcha"),
    # ADJUST IF SITE CHANGES: the captcha image is a base64 data-URI <img>.
    "captcha_image":   (By.CSS_SELECTOR, "img[aria-label*='captcha' i], img[aria-label*='refrsh' i]"),
    "captcha_refresh": (By.CSS_SELECTOR, "img[aria-label*='refrsh' i], img[aria-label*='refresh' i]"),

    # Parts table
    "parts_table":  (By.CSS_SELECTOR, "table.contenttable-eroll"),
    "select_all":   (By.ID, "selectAll"),
    # rows that actually contain a part (skip the header row)
    "part_rows":    (By.CSS_SELECTOR, "table.contenttable-eroll tbody tr"),
    # relative selectors used *inside* a row element:
    "row_checkbox": (By.CSS_SELECTOR, "input[type='checkbox']"),
    "row_text":     (By.CSS_SELECTOR, "td:nth-child(2)"),

    # Pagination (ul.pagination with clickable page items / next-prev arrows)
    "pagination":       (By.CSS_SELECTOR, "ul.pagination, .pagination"),
    "pagination_items": (By.CSS_SELECTOR, "ul.pagination li, .pagination li"),

    # Download button
    "download_button": (By.CSS_SELECTOR, "button.submit[type='submit'], button.submit"),
}


# =========================================================================== #
#  3. Logging                                                                  #
# =========================================================================== #

LOG_FILE = "electoral_roll_downloader.log"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("eroll")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()


# =========================================================================== #
#  4. Data model                                                              #
# =========================================================================== #

@dataclass
class Part:
    number: str
    name: str
    page_index: int = 0          # which pagination page the part lives on
    row_index: int = 0           # row position within that page


@dataclass
class PartResult:
    state: str
    year: str
    district: str
    constituency: str
    part_number: str
    part_name: str
    language: str
    filename: str = ""
    file_size: int = 0
    page_count: int = 0
    status: str = "PENDING"      # PENDING / SUCCESS / FAILED / SKIPPED
    retry_count: int = 0
    error_message: str = ""
    timestamp: str = ""


REPORT_COLUMNS = [
    "State", "Year", "District", "Assembly Constituency", "Part Number",
    "Part Name", "Language", "Filename", "File Size", "Page Count",
    "Download Status", "Retry Count", "Error Message", "Download Timestamp",
]


# =========================================================================== #
#  5. Helpers: paths & filenames                                              #
# =========================================================================== #

def slugify(value: str) -> str:
    """Filesystem-safe token (keeps letters, digits, dashes, underscores)."""
    value = value.strip().replace("&", "and")
    value = re.sub(r"[^\w\-]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_")


def state_abbrev(state_name: str) -> str:
    """Short code for filenames, e.g. 'Andhra Pradesh' -> 'AP'."""
    words = re.findall(r"[A-Za-z]+", state_name)
    if not words:
        return "XX"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(w[0] for w in words).upper()[:4]


def parse_ac(ac_text: str) -> Tuple[str, str]:
    """'108 - Ongole' -> ('108', 'Ongole'). Falls back gracefully."""
    m = re.match(r"\s*(\d+)\s*[-–]\s*(.+)", ac_text)
    if m:
        return m.group(1), m.group(2).strip()
    return "", ac_text.strip()


def target_dir(cfg: dict) -> Path:
    """downloads/<State>/<Year>/<District>/<ACnum_ACname>/<Language>/"""
    ac_num, ac_name = parse_ac(cfg["ASSEMBLY_CONSTITUENCY"])
    ac_folder = slugify(f"{ac_num}_{ac_name}") if ac_num else slugify(ac_name)
    d = (
        Path(cfg["DOWNLOAD_ROOT"])
        / slugify(cfg["STATE_NAME"])
        / slugify(str(cfg["YEAR_OF_REVISION"]))
        / (slugify(cfg["DISTRICT"]) or "AllDistricts")
        / ac_folder
        / slugify(cfg["LANGUAGE"])
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def part_filename(cfg: dict, part_number: str) -> str:
    """AP_108_Ongole_Part_051_English.pdf"""
    ac_num, ac_name = parse_ac(cfg["ASSEMBLY_CONSTITUENCY"])
    return (
        f"{state_abbrev(cfg['STATE_NAME'])}_"
        f"{ac_num or 'AC'}_{slugify(ac_name)}_"
        f"Part_{str(part_number).zfill(3)}_"
        f"{cfg['LANGUAGE'].capitalize()}.pdf"
    )


# =========================================================================== #
#  6. PDF validation                                                          #
# =========================================================================== #

def validate_pdf(path: Path, min_bytes: int) -> Tuple[bool, int, str]:
    """
    Returns (is_valid, page_count, error_message).

    A PDF is valid only when:
        * file exists and size >= min_bytes
        * it is not a partial download (.crdownload/.tmp handled by caller)
        * first bytes are the '%PDF' signature (not HTML/error content)
        * PyMuPDF / pypdf can open it and it has >= 1 page
    """
    try:
        if not path.exists():
            return False, 0, "file missing"
        size = path.stat().st_size
        if size < min_bytes:
            return False, 0, f"file too small ({size} bytes)"

        with open(path, "rb") as fh:
            head = fh.read(5)
        if not head.startswith(b"%PDF"):
            return False, 0, "missing %PDF header (likely HTML/error content)"

        if _PDF_BACKEND == "pymupdf":
            with fitz.open(path) as doc:
                pages = doc.page_count
        elif _PDF_BACKEND == "pypdf":
            reader = PdfReader(str(path))
            pages = len(reader.pages)
        else:
            # No PDF library available: header + size check only.
            return True, -1, "warning: no PDF library, header-check only"

        if pages < 1:
            return False, 0, "PDF has zero pages"
        return True, pages, ""
    except Exception as exc:  # parser raised -> corrupt
        return False, 0, f"PDF parser error: {exc}"


# =========================================================================== #
#  7. WebDriver                                                               #
# =========================================================================== #

def create_driver(cfg: dict, download_dir: Path) -> webdriver.Chrome:
    """Create a VISIBLE (non-headless) Chrome that auto-saves PDFs/ZIPs."""
    opts = ChromeOptions()
    # Never headless — a human must see and solve the CAPTCHA.
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if cfg.get("CHROME_BINARY"):
        opts.binary_location = cfg["CHROME_BINARY"]

    prefs = {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,  # download PDFs, don't preview
        "profile.default_content_setting_values.automatic_downloads": 1,
        "safebrowsing.enabled": True,
    }
    opts.add_experimental_option("prefs", prefs)

    if cfg.get("CHROMEDRIVER_PATH"):
        service = ChromeService(executable_path=cfg["CHROMEDRIVER_PATH"])
    elif _HAVE_WDM:
        service = ChromeService(ChromeDriverManager().install())
    else:
        service = ChromeService()  # rely on chromedriver being on PATH

    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(cfg["PAGE_LOAD_TIMEOUT"])

    # Some Chrome builds need this to allow downloads while "automated".
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir.resolve())},
        )
    except Exception:
        pass
    return driver


def waiter(driver, cfg) -> WebDriverWait:
    return WebDriverWait(driver, cfg["ELEMENT_TIMEOUT"])


# =========================================================================== #
#  8. Dropdown handling (explicit waits, not fixed sleeps)                     #
# =========================================================================== #

def _retry_stale(fn, attempts: int = 4, pause: float = 0.6):
    """Run fn(), retrying on StaleElementReferenceException."""
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except StaleElementReferenceException as exc:
            last = exc
            time.sleep(pause)
    if last:
        raise last


def wait_options_loaded(driver, cfg, key: str, min_options: int = 2) -> None:
    """Wait until a <select> has been populated (cascading load finished)."""
    by, sel = SELECTORS[key]

    def _ready(d):
        try:
            el = d.find_element(by, sel)
            return len(Select(el).options) >= min_options
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    waiter(driver, cfg).until(_ready)


def select_dropdown_value(driver, cfg, key: str, wanted: str,
                          required: bool = True) -> bool:
    """
    Select an option by visible text.  Tries, in order:
        1. exact (case-insensitive) match
        2. 'contains' match
    Uses explicit waits for the element to be present and enabled.
    """
    if not wanted:
        if required:
            raise ValueError(f"No value configured for '{key}'")
        log.info("Skipping optional dropdown '%s' (no value configured)", key)
        return False

    by, sel = SELECTORS[key]

    def _do():
        el = waiter(driver, cfg).until(EC.presence_of_element_located((by, sel)))

        # Handle the locked/disabled State select (set via URL ?stateCode=).
        if not el.is_enabled():
            current = ""
            try:
                current = Select(el).first_selected_option.text.strip()
            except Exception:
                pass
            if wanted.lower() in current.lower() or current.lower() in wanted.lower():
                log.info("Dropdown '%s' is locked and already set to '%s' — OK",
                         key, current)
                return True
            raise WebDriverException(
                f"Dropdown '{key}' is disabled and shows '{current}', "
                f"expected '{wanted}'."
            )

        select = Select(el)
        options = [(o.text.strip(), o) for o in select.options]

        # 1) exact case-insensitive
        for text, opt in options:
            if text.lower() == wanted.lower():
                select.select_by_visible_text(opt.text)
                return True
        # 2) contains
        for text, opt in options:
            if wanted.lower() in text.lower() and text.lower() not in (
                "select state", "select roll", "select district",
                "select ac", "select language",
            ):
                log.info("Dropdown '%s': partial match '%s' for wanted '%s'",
                         key, text, wanted)
                select.select_by_visible_text(opt.text)
                return True

        available = ", ".join(t for t, _ in options[:12])
        raise NoSuchElementException(
            f"Option '{wanted}' not found in '{key}'. Available: {available} ..."
        )

    ok = _retry_stale(_do)
    if ok:
        log.info("Selected %-13s = %s", key, wanted)
        # small settle so the dependent dropdown/API can start loading
        time.sleep(1.0)
    return bool(ok)


def configure_all_dropdowns(driver, cfg) -> None:
    """Select every dropdown in the correct cascading order, with waits."""
    log.info("Configuring dropdowns ...")

    # State: usually locked by the URL param; select if enabled, else verify.
    wait_options_loaded(driver, cfg, "state", min_options=2)
    select_dropdown_value(driver, cfg, "state", cfg["STATE_NAME"], required=True)

    wait_options_loaded(driver, cfg, "year", min_options=2)
    select_dropdown_value(driver, cfg, "year", str(cfg["YEAR_OF_REVISION"]))

    wait_options_loaded(driver, cfg, "roll_type", min_options=2)
    select_dropdown_value(driver, cfg, "roll_type", cfg["ROLL_TYPE"])

    if cfg["DISTRICT"]:
        wait_options_loaded(driver, cfg, "district", min_options=2)
        select_dropdown_value(driver, cfg, "district", cfg["DISTRICT"], required=False)

    wait_options_loaded(driver, cfg, "constituency", min_options=2)
    select_dropdown_value(driver, cfg, "constituency", cfg["ASSEMBLY_CONSTITUENCY"])

    # Language MUST be English so regional PDFs are never downloaded.
    wait_options_loaded(driver, cfg, "language", min_options=2)
    select_dropdown_value(driver, cfg, "language", cfg["LANGUAGE"])

    # Guard: verify the language really is what we asked for.
    by, sel = SELECTORS["language"]
    chosen = Select(driver.find_element(by, sel)).first_selected_option.text.strip()
    if cfg["LANGUAGE"].lower() not in chosen.lower():
        raise RuntimeError(
            f"Language guard failed: page shows '{chosen}', expected "
            f"'{cfg['LANGUAGE']}'. Aborting to avoid wrong-language downloads."
        )
    log.info("Language confirmed as '%s'", chosen)


# =========================================================================== #
#  9. CAPTCHA (manual — never solved automatically)                           #
# =========================================================================== #

def wait_for_manual_captcha(driver, cfg) -> None:
    """
    Pause and let the human solve the CAPTCHA in the visible browser.

    We do NOT read, OCR or solve the CAPTCHA.  We simply make sure the input
    is on screen, print an instruction, and wait for the user to:
        * type the CAPTCHA in the browser, then
        * press Enter in this terminal.

    As a convenience we also auto-continue if we detect the input already
    contains the full 6-character code (so the user can just fill it and the
    script proceeds), but Enter is always accepted.
    """
    by, sel = SELECTORS["captcha_input"]
    try:
        box = waiter(driver, cfg).until(EC.visibility_of_element_located((by, sel)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    except TimeoutException:
        log.warning("CAPTCHA input not visible yet; continuing to prompt anyway.")
        box = None

    print("\n" + "=" * 68)
    print("  Please solve the CAPTCHA manually in the browser.")
    print("  1) Type the CAPTCHA characters into the 'Enter Captcha' box.")
    print("  2) Come back here and press Enter to continue.")
    print("=" * 68)

    # Give a short window for auto-detect (input filled to full length),
    # otherwise fall back to a blocking Enter prompt.
    maxlen = 6
    try:
        if box is not None:
            for _ in range(3):  # ~1.5s glance; non-blocking best-effort
                val = box.get_attribute("value") or ""
                if len(val.strip()) >= maxlen:
                    log.info("CAPTCHA field already filled — continuing.")
                    return
                time.sleep(0.5)
    except StaleElementReferenceException:
        pass

    input("   >> Press Enter AFTER you have typed the CAPTCHA in the browser ... ")
    # Re-read to give the user a gentle nudge if it still looks empty.
    try:
        if box is not None:
            val = (box.get_attribute("value") or "").strip()
            if not val:
                print("   (The CAPTCHA box still looks empty — make sure you typed it.)")
    except StaleElementReferenceException:
        pass


def refresh_captcha(driver, cfg) -> None:
    """Click the refresh icon to get a new CAPTCHA (used before a retry)."""
    by, sel = SELECTORS["captcha_refresh"]
    try:
        icon = driver.find_element(by, sel)
        icon.click()
        time.sleep(1.0)
        log.info("Requested a fresh CAPTCHA.")
    except Exception:
        log.debug("Could not click CAPTCHA refresh (not critical).")


# =========================================================================== #
#  10. Parts: load (through pagination) & select                              #
# =========================================================================== #

def _pagination_pages(driver) -> List:
    """Return the clickable numeric page <li> elements (excluding arrows)."""
    by, sel = SELECTORS["pagination_items"]
    items = driver.find_elements(by, sel)
    numeric = []
    for it in items:
        txt = (it.text or "").strip()
        if txt.isdigit():
            numeric.append(it)
    return numeric


def _read_current_page_parts(driver, cfg, page_index: int) -> List[Part]:
    parts: List[Part] = []
    rows = driver.find_elements(*SELECTORS["part_rows"])
    for r_i, row in enumerate(rows):
        try:
            txt = row.find_element(*SELECTORS["row_text"]).text.strip()
        except NoSuchElementException:
            continue
        if not txt:
            continue
        num, name = parse_ac(txt)  # reuse '<n> - <name>' parser
        parts.append(Part(number=num or txt, name=name, page_index=page_index,
                          row_index=r_i))
    return parts


def load_all_parts(driver, cfg) -> List[Part]:
    """
    Wait for the parts table, then walk every pagination page so that all
    dynamically-loaded rows are seen.  Returns a de-duplicated Part list.

    NOTE: the ECI table paginates 10 parts per page (it is NOT infinite
    scroll).  We still scroll each page into view to be safe.
    """
    waiter(driver, cfg).until(
        EC.presence_of_element_located(SELECTORS["parts_table"])
    )
    # ensure at least one data row is present
    waiter(driver, cfg).until(
        lambda d: len(d.find_elements(*SELECTORS["part_rows"])) >= 1
    )

    all_parts: Dict[str, Part] = {}
    numeric_pages = _pagination_pages(driver)
    total_pages = max(1, len(numeric_pages))
    log.info("Parts table found. Pagination pages detected: %d", total_pages)

    for page_no in range(1, total_pages + 1):
        if total_pages > 1:
            # click the page number (re-find each loop to avoid staleness)
            pages_now = _pagination_pages(driver)
            target = next((p for p in pages_now if p.text.strip() == str(page_no)),
                          None)
            if target is not None:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
                    target.click()
                    time.sleep(1.2)
                except (ElementClickInterceptedException, StaleElementReferenceException):
                    driver.execute_script("arguments[0].click();", target)
                    time.sleep(1.2)

        # scroll table bottom into view (defensive against lazy rendering)
        try:
            tbl = driver.find_element(*SELECTORS["parts_table"])
            driver.execute_script("arguments[0].scrollIntoView(false);", tbl)
        except Exception:
            pass
        time.sleep(0.4)

        for p in _read_current_page_parts(driver, cfg, page_no):
            all_parts.setdefault(f"{p.number}|{p.name}", p)
        log.info("  page %d/%d: %d parts collected so far",
                 page_no, total_pages, len(all_parts))

    parts = list(all_parts.values())
    # sort numerically where possible
    def _key(pt: Part):
        m = re.match(r"\d+", pt.number or "")
        return (0, int(m.group(0))) if m else (1, pt.number)
    parts.sort(key=_key)
    log.info("Total parts discovered: %d", len(parts))
    return parts


def collect_part_details(parts: List[Part]) -> List[Part]:
    """Hook kept for clarity/extensibility (spec function name)."""
    return parts


def _go_to_page(driver, cfg, page_no: int) -> None:
    if page_no <= 1:
        return
    pages_now = _pagination_pages(driver)
    target = next((p for p in pages_now if p.text.strip() == str(page_no)), None)
    if target is not None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            target.click()
        except Exception:
            driver.execute_script("arguments[0].click();", target)
        time.sleep(1.0)


def _row_for_part(driver, cfg, part: Part):
    """Find the <tr> whose 2nd cell text matches this part (current page)."""
    rows = driver.find_elements(*SELECTORS["part_rows"])
    for row in rows:
        try:
            txt = row.find_element(*SELECTORS["row_text"]).text.strip()
        except NoSuchElementException:
            continue
        num, name = parse_ac(txt)
        if (num or txt) == part.number and name == part.name:
            return row
    return None


def _set_checkbox(driver, checkbox, checked: bool) -> None:
    if checkbox.is_selected() != checked:
        try:
            checkbox.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            driver.execute_script("arguments[0].click();", checkbox)


def clear_all_selections(driver, cfg) -> None:
    """Uncheck 'Select All' and any checked row boxes on the current page."""
    try:
        sa = driver.find_element(*SELECTORS["select_all"])
        if sa.is_selected():
            _set_checkbox(driver, sa, False)
    except Exception:
        pass
    for row in driver.find_elements(*SELECTORS["part_rows"]):
        try:
            cb = row.find_element(*SELECTORS["row_checkbox"])
            _set_checkbox(driver, cb, False)
        except Exception:
            continue


def select_single_part(driver, cfg, part: Part) -> bool:
    """Select exactly one part's checkbox (per_part strategy)."""
    _go_to_page(driver, cfg, part.page_index)
    clear_all_selections(driver, cfg)
    row = _retry_stale(lambda: _row_for_part(driver, cfg, part))
    if row is None:
        log.error("Could not locate row for part %s - %s", part.number, part.name)
        return False
    cb = row.find_element(*SELECTORS["row_checkbox"])
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
    _set_checkbox(driver, cb, True)
    return cb.is_selected()


def select_all_parts(driver, cfg) -> int:
    """
    Select every part checkbox on the CURRENT page.  Prefers the header
    'Select All' checkbox; otherwise ticks each row individually.
    Returns the number of row checkboxes that ended up selected.
    """
    try:
        sa = driver.find_element(*SELECTORS["select_all"])
        _set_checkbox(driver, sa, True)
        time.sleep(0.4)
    except Exception:
        log.info("No 'Select All' checkbox — selecting rows individually.")

    count = 0
    for row in driver.find_elements(*SELECTORS["part_rows"]):
        try:
            cb = row.find_element(*SELECTORS["row_checkbox"])
            _set_checkbox(driver, cb, True)
            if cb.is_selected():
                count += 1
        except Exception:
            continue
    log.info("Selected %d part checkboxes on the current page.", count)
    return count


# =========================================================================== #
#  11. Downloading & waiting                                                   #
# =========================================================================== #

def _files_in(folder: Path) -> set:
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def _has_partial(folder: Path) -> bool:
    for p in folder.iterdir():
        if p.suffix.lower() in (".crdownload", ".tmp") or p.name.endswith(".part"):
            return True
    return False


def trigger_download(driver, cfg) -> None:
    """Click 'Download Selected PDFs'. Assumes CAPTCHA already solved."""
    btn = waiter(driver, cfg).until(
        EC.element_to_be_clickable(SELECTORS["download_button"])
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    try:
        btn.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", btn)
    log.info("Clicked 'Download Selected PDFs'.")


def wait_for_downloads(folder: Path, cfg, before: set,
                       timeout: Optional[int] = None) -> List[Path]:
    """
    Wait until any new download finishes (no .crdownload/.tmp/.part left).
    Returns the list of newly-created files (PDFs and/or ZIPs).
    """
    timeout = timeout or cfg["DOWNLOAD_TIMEOUT"]
    deadline = time.time() + timeout
    seen_new = False

    while time.time() < deadline:
        current = _files_in(folder)
        new = current - before
        # ignore partials when deciding what's "new & finished"
        finished_new = [
            folder / n for n in new
            if not (n.endswith(".crdownload") or n.endswith(".tmp") or n.endswith(".part"))
        ]
        if finished_new and not _has_partial(folder):
            # small settle to be sure the OS finished flushing
            time.sleep(1.0)
            if not _has_partial(folder):
                return finished_new
        if new:
            seen_new = True
        time.sleep(1.0)

    if not seen_new:
        log.warning("No new download appeared within %ss.", timeout)
    else:
        log.warning("Download did not finish cleanly within %ss (partial left).",
                    timeout)
    return []


def _extract_zip(zip_path: Path, dest: Path) -> List[Path]:
    """Extract a ZIP of PDFs, returning the extracted PDF paths."""
    out: List[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if not info.filename.lower().endswith(".pdf"):
                    continue
                raw = Path(info.filename).name
                target = dest / raw
                with zf.open(info) as src, open(target, "wb") as fh:
                    fh.write(src.read())
                out.append(target)
        log.info("Extracted %d PDF(s) from %s", len(out), zip_path.name)
    except zipfile.BadZipFile:
        log.error("Downloaded file is not a valid ZIP: %s", zip_path.name)
    return out


# =========================================================================== #
#  12. Per-part download with validation + retries                            #
# =========================================================================== #

def _finalise_single_file(src: Path, cfg: dict, dest: Path,
                          part: Part) -> Tuple[bool, int, int, str, str]:
    """
    Validate a freshly-downloaded file for a single part, and if valid rename
    it to the naming convention inside `dest`.
    Returns (ok, size, pages, final_name, error).
    """
    # If the server handed us a ZIP for a single part, pull the PDF out.
    candidates: List[Path] = []
    if src.suffix.lower() == ".zip":
        candidates = _extract_zip(src, dest)
        try:
            src.unlink()
        except Exception:
            pass
    else:
        candidates = [src]

    if not candidates:
        return False, 0, 0, "", "no PDF produced by download"

    # For a single-part download we expect exactly one PDF; take the first.
    pdf = candidates[0]
    ok, pages, err = validate_pdf(pdf, cfg["MIN_PDF_BYTES"])
    if not ok:
        try:
            pdf.unlink()
        except Exception:
            pass
        return False, 0, 0, "", err

    final_name = part_filename(cfg, part.number)
    final_path = dest / final_name
    try:
        if final_path.exists():
            final_path.unlink()
        pdf.rename(final_path)
    except Exception as exc:
        return False, 0, 0, "", f"rename failed: {exc}"

    size = final_path.stat().st_size
    return True, size, pages, final_name, ""


def download_one_part(driver, cfg, dest: Path, part: Part,
                      result: PartResult) -> PartResult:
    """
    Full per-part flow with retries + exponential backoff.
    Each attempt: select the part -> manual CAPTCHA -> download -> validate.
    """
    max_retries = cfg["MAX_RETRIES"]
    for attempt in range(1, max_retries + 1):
        result.retry_count = attempt - 1
        try:
            log.info("Part %s (%s) — attempt %d/%d",
                     part.number, part.name, attempt, max_retries)

            if not select_single_part(driver, cfg, part):
                raise RuntimeError("failed to select the part checkbox")

            # Fresh CAPTCHA for each submit; human solves it.
            if attempt > 1:
                refresh_captcha(driver, cfg)
            wait_for_manual_captcha(driver, cfg)

            before = _files_in(dest)
            trigger_download(driver, cfg)
            new_files = wait_for_downloads(dest, cfg, before)

            if not new_files:
                raise RuntimeError("no file downloaded (check CAPTCHA / network)")

            ok, size, pages, fname, err = _finalise_single_file(
                new_files[0], cfg, dest, part
            )
            if not ok:
                raise RuntimeError(err or "PDF validation failed")

            result.filename = fname
            result.file_size = size
            result.page_count = pages
            result.status = "SUCCESS"
            result.error_message = ""
            result.timestamp = datetime.now().isoformat(timespec="seconds")
            log.info("  ✓ saved %s (%d bytes, %d pages)", fname, size, pages)
            time.sleep(cfg["DELAY_BETWEEN_PARTS"])
            return result

        except Exception as exc:
            result.status = "FAILED"
            result.error_message = str(exc)
            result.timestamp = datetime.now().isoformat(timespec="seconds")
            log.error("  ✗ part %s attempt %d failed: %s",
                      part.number, attempt, exc)
            if attempt < max_retries:
                backoff = cfg["RETRY_BACKOFF_BASE"] ** attempt
                log.info("  retrying in %ds (exponential backoff) ...", backoff)
                time.sleep(backoff)

    return result


def retry_failed_download(driver, cfg, dest: Path, part: Part,
                          result: PartResult) -> PartResult:
    """Explicit spec hook — a single fresh attempt for one part."""
    return download_one_part(driver, cfg, dest, part, result)


# =========================================================================== #
#  13. Duplicate detection / resume                                           #
# =========================================================================== #

def existing_valid(cfg, dest: Path, part: Part) -> Optional[Tuple[int, int, str]]:
    """
    If a valid PDF for this part already exists on disk, return
    (size, pages, filename); otherwise None (and delete an invalid one).
    """
    fname = part_filename(cfg, part.number)
    path = dest / fname
    if not path.exists():
        return None
    ok, pages, _ = validate_pdf(path, cfg["MIN_PDF_BYTES"])
    if ok:
        return path.stat().st_size, pages, fname
    # invalid -> remove so it will be re-downloaded
    try:
        path.unlink()
        log.info("Removed invalid existing file for part %s (will re-download).",
                 part.number)
    except Exception:
        pass
    return None


def resume_previous_run(report_path: Path) -> Dict[str, dict]:
    """
    Read a prior CSV report and return {part_number: row_dict} for parts that
    were previously SUCCESS, so they can be skipped.
    """
    done: Dict[str, dict] = {}
    if not report_path.exists():
        return done
    try:
        with open(report_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if (row.get("Download Status") or "").upper() == "SUCCESS":
                    done[str(row.get("Part Number"))] = row
        if done:
            log.info("Resume: %d part(s) already completed in previous run.",
                     len(done))
    except Exception as exc:
        log.warning("Could not read previous report (%s) — starting fresh.", exc)
    return done


# =========================================================================== #
#  14. Progress report (CSV)                                                   #
# =========================================================================== #

def save_progress(report_path: Path, results: List[PartResult]) -> None:
    """Write/overwrite the CSV progress report (atomic-ish)."""
    rows = []
    for r in results:
        rows.append({
            "State": r.state, "Year": r.year, "District": r.district,
            "Assembly Constituency": r.constituency, "Part Number": r.part_number,
            "Part Name": r.part_name, "Language": r.language, "Filename": r.filename,
            "File Size": r.file_size, "Page Count": r.page_count,
            "Download Status": r.status, "Retry Count": r.retry_count,
            "Error Message": r.error_message, "Download Timestamp": r.timestamp,
        })
    tmp = report_path.with_suffix(".csv.tmp")
    if _HAVE_PANDAS:
        pd.DataFrame(rows, columns=REPORT_COLUMNS).to_csv(tmp, index=False)
    else:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
            w.writeheader()
            w.writerows(rows)
    tmp.replace(report_path)


# =========================================================================== #
#  15. Main orchestration                                                      #
# =========================================================================== #

def build_url(cfg: dict) -> str:
    base = cfg["BASE_URL"].rstrip("/")
    return f"{base}?stateCode={cfg['STATE_CODE']}"


def main() -> int:
    cfg = CONFIG
    log.info("=" * 68)
    log.info("ECI Electoral Roll Downloader starting")
    log.info("State=%s  Year=%s  Roll=%s  District=%s  AC=%s  Lang=%s",
             cfg["STATE_NAME"], cfg["YEAR_OF_REVISION"], cfg["ROLL_TYPE"],
             cfg["DISTRICT"], cfg["ASSEMBLY_CONSTITUENCY"], cfg["LANGUAGE"])
    log.info("PDF backend: %s | pandas: %s | webdriver-manager: %s",
             _PDF_BACKEND, _HAVE_PANDAS, _HAVE_WDM)
    if _PDF_BACKEND is None:
        log.warning("No PDF library found (PyMuPDF/pypdf). Validation will be "
                    "header+size only. `pip install pymupdf` is recommended.")

    dest = target_dir(cfg)
    report_path = dest / "progress_report.csv"
    log.info("Output folder: %s", dest.resolve())

    already_done = resume_previous_run(report_path)

    driver = None
    results: List[PartResult] = []
    try:
        driver = create_driver(cfg, dest)
        url = build_url(cfg)
        log.info("Opening %s", url)
        driver.get(url)

        configure_all_dropdowns(driver, cfg)

        parts = collect_part_details(load_all_parts(driver, cfg))
        if not parts:
            log.error("No parts found for this selection. Nothing to download.")
            return 2

        ac_num, ac_name = parse_ac(cfg["ASSEMBLY_CONSTITUENCY"])
        for idx, part in enumerate(parts, 1):
            res = PartResult(
                state=cfg["STATE_NAME"], year=str(cfg["YEAR_OF_REVISION"]),
                district=cfg["DISTRICT"], constituency=cfg["ASSEMBLY_CONSTITUENCY"],
                part_number=part.number, part_name=part.name,
                language=cfg["LANGUAGE"],
            )

            log.info("-" * 60)
            log.info("[%d/%d] Part %s - %s", idx, len(parts), part.number, part.name)

            # Resume / duplicate handling
            if part.number in already_done:
                prev = already_done[part.number]
                res.filename = prev.get("Filename", "")
                res.file_size = int(prev.get("File Size") or 0)
                res.page_count = int(prev.get("Page Count") or 0)
                # re-validate the file still on disk before trusting the report
                ev = existing_valid(cfg, dest, part)
                if ev:
                    res.status = "SKIPPED"
                    res.timestamp = datetime.now().isoformat(timespec="seconds")
                    log.info("  already downloaded & valid — skipping.")
                    results.append(res)
                    save_progress(report_path, results)
                    continue

            ev = existing_valid(cfg, dest, part)
            if ev:
                res.file_size, res.page_count, res.filename = ev
                res.status = "SKIPPED"
                res.timestamp = datetime.now().isoformat(timespec="seconds")
                log.info("  valid file already on disk — skipping.")
                results.append(res)
                save_progress(report_path, results)
                continue

            # Download (with retries inside)
            res = download_one_part(driver, cfg, dest, part, res)
            results.append(res)
            save_progress(report_path, results)  # persist after every part (resume-safe)

        # Summary
        ok = sum(1 for r in results if r.status in ("SUCCESS", "SKIPPED"))
        bad = sum(1 for r in results if r.status == "FAILED")
        log.info("=" * 68)
        log.info("DONE. %d/%d parts OK, %d failed. Report: %s",
                 ok, len(results), bad, report_path)
        return 0 if bad == 0 else 3

    except KeyboardInterrupt:
        log.warning("Interrupted by user. Progress saved; you can resume later.")
        save_progress(report_path, results)
        return 130
    except TimeoutException as exc:
        log.error("Timed out waiting for a page element: %s", exc)
        return 4
    except WebDriverException as exc:
        log.error("Browser/WebDriver error: %s", exc)
        return 5
    finally:
        if results:
            save_progress(report_path, results)
        if driver is not None:
            input("\nPress Enter to close the browser ... ")
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
