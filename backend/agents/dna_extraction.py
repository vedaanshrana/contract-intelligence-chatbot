"""
DNA Core — Extraction + Matching Agent
======================================

Verbatim port of `Existing Scripts/DNA Latest Extraction Matching 06.03.ipynb`
(the colleague's DNA pipeline that runs against the Fiserv Foundation API
proxy) into the chatbot's per-client agent architecture.

Why this is its own module
--------------------------
DNA's extraction prompt + dictionary structure + matching pipeline diverge
substantially from PORTICO's:

* Extraction captures an extra ``material_code`` field per item (DNA
  contracts often print the SAP code right next to the line item).
* PDF triage classifies each contract as MSA / Renewal_Amendment /
  Amendment / Other and applies a fallback rule that adds the most-recent
  qualifying pre-cutoff file when there are no qualifying post-cutoff
  ones (PORTICO has no such fallback).
* Matching uses a section-normalization table + anchor-keyword index +
  hybrid scoring (sentence-transformers + TF-IDF) restricted to the
  pertinent dictionary slice rather than the global pool, then an AI
  rerank for low-confidence items.
* Dictionary has the columns ``Material Code`` / ``Contract Description``
  / ``Section Header`` (vs PORTICO's ``Description`` + ``Material Code``).

The PORTICO module (``agents/extraction.py``) stays untouched.
``chatbot.py``'s `_run_extr` / `_run_material_match` runners dispatch to
this module when ``st.session_state.selected_core == "DNA"``.

Public surface — identical to ``agents/extraction.py`` so the existing
chatbot consumers (`frontend_agent_done`, sidecar readers, output panels)
work uniformly across both cores:

    run(client_name, ..., min_year=None)  -> dict          # Phase 1
    run_matching(client_name, ..., min_year=None) -> dict  # Phase 2
    output_path(client_name) -> Path
    matching_output_path(client_name) -> Path
    is_processed(client_name) -> bool
    matching_is_processed(client_name) -> bool
    read_extraction_meta(client_name) -> dict
    read_matching_meta(client_name) -> dict

Backend transparency: every LLM call goes through
``fiserv_client.make_client(api_key)``. On a laptop with
``OPENAI_BACKEND=openai`` (default) it uses the real OpenAI SDK against the
chosen model (default ``gpt-5.2-2025-12-11``). Inside the Fiserv VDI with
``OPENAI_BACKEND=fiserv`` the same call is routed through the keyless
Foundation proxy and the X-Purpose header maps any gpt-5-family request
to ``GPT5.1Purpose`` (only gpt-5.1 is exposed in VDI). No code change is
needed to switch — just the env var.
"""

# ── Top-level imports (mirrors the colleague's notebook cell-by-cell) ──────
from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

import fitz                                                # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)
from PIL import Image
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize                # noqa: F401  (notebook-parity import)
from rapidfuzz import fuzz, process

# Chatbot-specific (metered client + config + paths)
from config import (
    EXTRACTION_API_KEY,
    EXTRACTION_MODEL as _DEFAULT_EXTRACTION_MODEL,
    MATCHING_MODEL   as _DEFAULT_MATCHING_MODEL,
    OUTPUT_DIR       as _OUTPUT_DIR,
    INPUT_DIR        as _INPUT_DIR,
    client_input_dir,
)
from fiserv_client import make_client


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — mirrors the colleague's notebook CONFIG cell
# ═══════════════════════════════════════════════════════════════════════════
# DNA models — overridable via env var so we can experiment with different
# tiers on OpenAI direct without editing code. Default to the chatbot's
# global EXTRACTION_MODEL / MATCHING_MODEL so PORTICO and DNA stay aligned.
# On Fiserv VDI the model name is mapped to GPT5.1Purpose / GPT4.1Purpose
# by fiserv_client.FoundationClient regardless of which gpt-5/4 family
# value we pass — so the env var only matters for OpenAI direct.
EXTRACTION_MODEL = (os.environ.get("DNA_EXTRACTION_MODEL")
                    or _DEFAULT_EXTRACTION_MODEL)
MATCHING_MODEL   = (os.environ.get("DNA_MATCHING_MODEL")
                    or _DEFAULT_MATCHING_MODEL)

CHUNK_SIZE     = 8       # pages per LLM call (DNA notebook default; PORTICO
                          # uses 12 — DNA is tuned tighter because the VDI
                          # proxy has stricter payload limits).
DPI            = 600     # render resolution (notebook default).
CHUNK_WORKERS  = 2       # parallel chunks per PDF (raise on stable network).

ITEM_BATCH_SIZE             = 25
DICT_CHUNK_SIZE             = 250
MAX_PARALLEL_CALLS          = 12     # Phase 2 worker pool (DNA notebook tuning)
FUZZY_AUTO_ACCEPT_THRESHOLD = 0.90
SEMANTIC_WEIGHT             = 0.6
LEXICAL_WEIGHT              = 0.4
SECTION_NORM_THRESHOLD      = 92

# PDF year cutoff — same env var as PORTICO so the chatbot's year selector
# applies across both cores. The colleague's notebook used DATE_CUTOFF_YEAR
# = 2022; we preserve that default but let the UI override it per-run.
MIN_YEAR_CUTOFF = int(os.environ.get("EXTRACTION_MIN_YEAR", "2022"))

# Banner printed at the start of every run — confirms which module is
# actually executing (Streamlit's module cache can be stale across reruns).
_VERSION = ("DNA v2 — chat.completions.create() (notebook-verbatim API) + "
            "robust JSON parser + verbose response logging + "
            "DNA_EXTRACTION_MODEL env override")


# ═══════════════════════════════════════════════════════════════════════════
# SHARED HELPERS — date parsing, image conversion, price cleaning, etc.
# ═══════════════════════════════════════════════════════════════════════════

def extract_date_from_filename(filename: str, min_year: int = None):
    """Parse MM-DD-YYYY date from filename. Returns ``None`` when missing
    OR when the parsed year is below ``min_year`` (defaults to
    ``MIN_YEAR_CUTOFF``, which itself defaults to 2022)."""
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", filename)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        d = datetime(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    return None if d.year < min_year else d


def pdf_to_images(pdf_path: str, dpi: int = DPI) -> list:
    """Render each PDF page as a PIL Image at the given DPI."""
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(len(doc)):
        page = doc.load_page(i)
        mat  = fitz.Matrix(dpi / 72, dpi / 72)
        pix  = page.get_pixmap(matrix=mat)
        img  = Image.open(BytesIO(pix.tobytes("png")))
        pages.append({"page_number": i + 1, "image": img})
    return pages


def image_to_base64(image) -> str:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def clean_price(val):
    """Normalise a price string. Preserves percentage values and the
    Included / Waived / No Charge / $0 / TBD textual labels."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if not s:
        return ""
    s_low = s.lower()
    if any(lbl in s_low for lbl in
           ("included", "waived", "no charge", "n/a", "free",
            "complimentary", "tbd")):
        return s
    if "%" in s:
        return s
    if re.match(r"^over\s*\$", s, re.IGNORECASE):
        return s
    if re.match(r"^\$?\d+(\.\d+)?\s*[MBmb]$", s.strip()):
        return s
    negative = bool(re.match(r"^\(", s.replace("$", "").strip()))
    cleaned  = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    m = re.search(r"\d+\.?\d*", cleaned)
    if not m:
        return s
    value = float(m.group())
    return f"-${value:,.2f}" if negative else f"${value:,.2f}"


def detect_pricing_type(val) -> str:
    """Classify a price value as flat / percentage / waived / included /
    no charge / tbd / unknown."""
    if pd.isna(val):
        return ""
    s = str(val).strip().lower()
    if not s:
        return ""
    if "%" in s:
        return "percentage"
    if "waived" in s:
        return "waived"
    if "included" in s:
        return "included"
    if any(k in s for k in ("no charge", "free", "complimentary", "n/a")):
        return "no charge"
    if "tbd" in s or "to be determined" in s:
        return "tbd"
    if re.search(r"\d", s):
        return "flat"
    return "unknown"


_TIER_CONTEXT_KEYWORDS = (
    "account", "accounts", "dda", "ddas", "share draft", "share drafts",
    "transaction", "transactions", "user", "users", "seat", "seats",
    "member", "members", "item", "items", "ach", "card", "cards",
    "deposit", "deposits", "loan", "loans", "statement", "statements",
    "asset", "assets", "tier", "tiers", "subscriber", "subscribers",
    "device", "devices", "branch", "branches", "core", "cores",
    "customer", "customers", "client", "clients", "call", "calls",
    "minute", "minutes", "month", "months", "page", "pages",
    "record", "records", "report", "reports", "license", "licenses",
)


def extract_tier_range(item_text) -> str:
    """Capture tier-range phrases like 'Up to 500 accounts' / '1,000 to
    2,500 transactions' — only when one of the tier-context keywords is
    present in the item text, to avoid false positives on plain numbers."""
    if pd.isna(item_text):
        return ""
    s = str(item_text).strip()
    if not s:
        return ""
    s_low = s.lower()
    if not any(kw in s_low for kw in _TIER_CONTEXT_KEYWORDS):
        return ""
    m = re.search(
        r"(up to\s*[\d,]+(?:\s*(?:%s))?)" % "|".join(_TIER_CONTEXT_KEYWORDS),
        s, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d{1,3}(?:,\d{3})+\s*(?:to|[-–])\s*\d{1,3}(?:,\d{3})+)", s)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d{3,}\s*(?:to|[-–])\s*\d{3,})", s)
    if m:
        return m.group(1).strip()
    m = re.search(r"(over\s*[\d,]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# Retry wrapper for any callable that may hit a 429 / rate limit.
def call_api_with_retry(fn, max_retries: int = 5):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            if ("429" in str(e) or "rate limit" in err
                    or "too many requests" in err):
                wait = 20 * (2 ** attempt)
                _log_hook(f"  Rate limit hit (attempt {attempt + 1}/"
                          f"{max_retries}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"API call failed after {max_retries} retries")


# Module-level log hook so deep helpers can route to the Streamlit progress
# bar instead of stdout. Wrapped in try/except because some helpers (e.g.
# call_api_with_retry) may run inside ThreadPoolExecutor workers where the
# Streamlit callback can raise NoSessionContext.
_LOG_HOOK: Optional[Callable[[str], None]] = None
def _log_hook(msg: str):
    if _LOG_HOOK is not None:
        try:
            _LOG_HOOK(msg)
            return
        except Exception:
            pass
    try:
        print(msg)
    except Exception:
        pass


_FREQUENCY_KEYWORDS = {
    "One-Time": ["one time", "one-time", "onetime", "set up", "set-up",
                 "setup", "implementation", "implement"],
    "Monthly":  ["monthly", "month"],
    "Annual":   ["annual", "annually", "yearly", "year"],
}


def _apply_frequency_inference(df: pd.DataFrame) -> pd.DataFrame:
    """Fill blank ``Frequency`` cells by looking for keywords in ``Item``."""
    def _infer(row):
        freq = row.get("Frequency")
        if isinstance(freq, str) and freq.strip():
            return freq
        desc = str(row.get("Item") or "").lower()
        for label, keywords in _FREQUENCY_KEYWORDS.items():
            if any(kw in desc for kw in keywords):
                return label
        return freq
    df = df.copy()
    df["Frequency"] = df.apply(_infer, axis=1)
    return df


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — PDF CLASSIFICATION + SELECTION
# ═══════════════════════════════════════════════════════════════════════════

_MSA_NAME_KWS     = ("msa", "master agreement", "master-agreement",
                     "master service", "master services",
                     "master-services-agreement")
_AMEND_NAME_KWS   = ("amendment", "addendum", "amend")
_RENEWAL_TEXT_KWS = ("renewal amendment", "renewal and amendment",
                     "renewal & amendment")
_QUALIFYING_TYPES = {"MSA", "Renewal_Amendment"}


def classify_contract(full_path: str) -> str:
    """Classify a PDF as MSA / Renewal_Amendment / Amendment / Other based
    on filename keywords + (for amendments) a peek at the first three
    pages of text."""
    name_lower = os.path.basename(full_path).lower()
    if any(kw in name_lower for kw in _MSA_NAME_KWS):
        return "MSA"
    if any(kw in name_lower for kw in _AMEND_NAME_KWS):
        first_text = ""
        page_count = 0
        try:
            doc = fitz.open(full_path)
            try:
                page_count = len(doc)
                first_text = " ".join(
                    doc[i].get_text() for i in range(min(3, page_count))
                ).lower()
            finally:
                doc.close()
        except Exception:
            pass
        if (any(kw in first_text for kw in _RENEWAL_TEXT_KWS)
                or page_count > 30):
            return "Renewal_Amendment"
        return "Amendment"
    return "Other"


def select_files_for_client(folder_path: Path, files: list,
                            min_year: int, log) -> list:
    """Apply the DNA notebook's PDF-selection logic.

    Rules:
      1. Classify every file (fast: filename keywords + 3-page text peek).
      2. Deduplicate same-date files: keep only the largest when any > 1 MB.
      3. Always include all post-cutoff files (date.year >= ``min_year``).
      4. If post-cutoff has no MSA/Renewal_Amendment, fall back to the most
         recent qualifying pre-cutoff file (2019+).

    Returns the filenames in the same folder that should be extracted."""

    def _classify_by_filename(f):
        name_lower = f.lower()
        if any(kw in name_lower for kw in _MSA_NAME_KWS):
            return "MSA"
        if any(kw in name_lower for kw in _RENEWAL_TEXT_KWS):
            return "Renewal_Amendment"
        if any(kw in name_lower for kw in _AMEND_NAME_KWS):
            return "Amendment_maybe"
        return "Other"

    all_meta = []
    for f in files:
        # NB: we use the unconstrained date (no year filter here) because we
        # need to bucket pre- vs post-cutoff explicitly below.
        m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", f)
        if m:
            try:
                d = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                d = datetime(1900, 1, 1)
        else:
            d = datetime(1900, 1, 1)
        size_mb = os.path.getsize(folder_path / f) / (1024 * 1024)
        all_meta.append({
            "filename":  f,
            "date":      d,
            "size_mb":   size_mb,
            "post":      d.year >= min_year,
            "type_hint": _classify_by_filename(f),
        })

    post_files = [m for m in all_meta if m["post"]]
    pre_files  = [m for m in all_meta if not m["post"] and m["date"].year >= 2019]
    to_classify = post_files if post_files else pre_files
    log(f"  {len(all_meta)} total files — {len(to_classify)} in-range, "
        f"{len(all_meta) - len(to_classify)} skipped (out of date range "
        f">= {min_year} or pre-2019).")

    has_qualifying_by_name = any(
        m["type_hint"] in _QUALIFYING_TYPES for m in to_classify
    )

    meta = []
    for m in to_classify:
        if m["type_hint"] in ("MSA", "Renewal_Amendment", "Other"):
            ctype = m["type_hint"]
        elif has_qualifying_by_name:
            ctype = "Amendment"
        else:
            ctype = classify_contract(str(folder_path / m["filename"]))
        meta.append({**m, "type": ctype})

    # Dedupe same-date files: keep the largest when any one of the group is
    # over 1 MB.
    date_groups = defaultdict(list)
    for m in meta:
        date_groups[m["date"]].append(m)
    deduped = []
    for d, group in date_groups.items():
        if len(group) == 1 or d.year <= 1900:
            deduped.extend(group)
        elif any(m["size_mb"] > 1 for m in group):
            largest = max(group, key=lambda m: m["size_mb"])
            dropped = [m["filename"] for m in group if m is not largest]
            log(f"  [Dedupe] {d.strftime('%Y-%m-%d')}: keeping "
                f"{largest['filename']} ({largest['size_mb']:.1f} MB), "
                f"dropping {dropped}")
            deduped.append(largest)
        else:
            deduped.extend(group)
    meta = deduped

    post = [m for m in meta if m["post"]]
    pre  = [m for m in meta if not m["post"]]
    selected_set = {m["filename"] for m in post}

    # Fallback: if post-cutoff has no qualifying MSA / Renewal_Amendment,
    # promote the most recent qualifying pre-cutoff (or any amendment) one.
    if not any(m["type"] in _QUALIFYING_TYPES for m in post):
        reason = (f"no contracts dated after {min_year}"
                  if not post else
                  f"no MSA/Renewal_Amendment among {len(post)} post-"
                  f"{min_year} file(s)")
        pool = [m for m in pre
                if m["type"] in _QUALIFYING_TYPES and m["date"].year >= 2019]
        if not pool:
            pool = [m for m in pre
                    if m["type"] == "Amendment" and m["date"].year >= 2019]
        if pool:
            best = max(pool, key=lambda m: m["date"])
            selected_set.add(best["filename"])
            d_label = (best["date"].strftime("%Y-%m-%d")
                       if best["date"].year > 1900 else "no-date")
            log(f"  [Fallback] {reason} → adding {best['filename']} "
                f"({best['type']}, {d_label})")
        else:
            log(f"  [Warning] {reason} and no qualifying pre-cutoff "
                "fallback found.")

    selected = [m for m in meta if m["filename"] in selected_set]
    selected.sort(key=lambda m: m["date"], reverse=True)
    log(f"  Selected {len(selected)}/{len(meta)} file(s) for extraction.")
    for m in selected:
        d_str = (m["date"].strftime("%Y-%m-%d")
                 if m["date"].year > 1900 else "no-date")
        log(f"    • [{m['type']:<18}] {d_str}  "
            f"{m['size_mb']:5.1f} MB  {m['filename'][:70]}")
    return [(m["filename"], m["date"]) for m in selected]


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — EXTRACTION PROMPT (verbatim from notebook)
# ═══════════════════════════════════════════════════════════════════════════

EXTRACTION_SYSTEM_PROMPT = """
You are a forensic-level contract analysis expert.

Your task is to extract ALL items that have an associated price/fee field — INCLUDING items priced at $0, "Included", "Waived", "No Charge", "N/A", "Free", "Complimentary" — AND for each item ALSO capture the Material Code if it is visible on the contract page.

PRICE FIELD — WHAT TO EXTRACT:
You MUST extract every item that has any value in a price/fee column or position, including:
- Dollar amounts: "$100", "$0", "$1,500.00", "USD 250"
- Free / no-cost labels: "Included", "Waived", "No Charge", "N/A", "Free", "Complimentary", "$0"
- TBD / pending: "TBD", "To Be Determined" (capture as the price string, do not skip)
For non-dollar labels (Included, Waived, etc.), set the "price" field to the EXACT label as written on the page.
Items priced at $0 or labeled "Included"/"Waived" still require a material code mapping — do NOT skip them.

CRITICAL INSTRUCTIONS FOR CHECKBOX DETECTION:

1. CHECKBOX PLACEMENT: Checkboxes can appear BEFORE or AFTER the fee description. Look carefully at the LEFT side of each line item for empty or filled boxes.
2. CHECKBOX TYPES:
   - CHECKED: ☑, ☒, ✓, ✗, [X], [x], filled box, box with any mark inside
   - UNCHECKED: ☐, □, [ ], empty box
3. EMPTY BOX before a fee → checkbox_checked: false. MARKED box → true. No checkbox at all → null.
4. HIERARCHICAL: If a parent checkbox is unchecked, all child items are unchecked unless they have their own marked box.

CHECKBOX LOOK-BACK / VISUAL BLOCK / SECTION BOUNDARY RULES:
Same rules as standard Portico extraction — search upward for the closest controlling checkbox in the same visual block; do not cross section boundaries; tables inherit the checkbox above them unless a row has its own.

MATERIAL CODE EXTRACTION:
Material Codes are short alphanumeric identifiers (e.g. GPS022, ACHALERT0001, MS3001) that often appear in contracts directly next to a line item, in a dedicated column, or in parentheses after the description.
- For EACH extracted item, look for a Material Code adjacent to that item (same row, same line, or in a "Material Code" / "Item Code" column).
- If a code is present, capture it EXACTLY as written into the "material_code" field.
- If no code is visible for that item, set material_code to null.
- Material codes are typically 5-12 characters, alphanumeric, often uppercase.

SECTION HEADER IDENTIFICATION AND INHERITANCE

You must determine the SECTION HEADER corresponding to each extracted item.
A Section Header is a high-level title that describes the contract section containing the items. These headers typically appear:
- At the TOP of a page
- CENTERED horizontally — this is the most important visual indicator
- In larger or bold text
- Above paragraphs, lists, or tables
- Often without a dollar value

CRITICAL: Central horizontal alignment is the PRIMARY visual indicator of a Section Header. Text that is left-aligned, indented, or inline with paragraph content is NEVER a Section Header, regardless of its size or formatting.

MULTI-LINE HEADER RULE:
Some section headers span multiple lines or consist of a title and a subtitle directly below it.
If a title line is immediately followed by another descriptive line with no other content between them, treat the COMBINED text as the full section header.
Both lines must be centrally aligned for this rule to apply.

SECTION HEADER ASSOCIATION PROCESS:
STEP 1 — Upward Visual Scan: When extracting an item with a dollar value, scan upward visually from the item's position on the page. Identify the nearest CENTRALLY ALIGNED text block above the item.
STEP 2 — Header Selection: The FIRST qualifying section header encountered while scanning upward is the correct Section Header for that item.
STEP 3 — Page-Level Inheritance: If the current page does NOT contain a section header above the item, inherit the most recent section header that appeared on previous pages.
STEP 4 — Section Scope: A section header governs all paragraphs, bullet lists, numbered clauses, lettered clauses, tables, and fee lines that appear beneath it until a new section header appears.
STEP 5 — Table Handling: If a table appears under a section header, ALL rows in the table inherit the same section header unless another header appears inside the table block.

SECTION HEADER GENERICITY CHECK:
STEP 6 — Genericity Check: A section header is GENERIC (insufficient) if it contains ONLY structural/positional words such as: "Attachment", "Appendix", "Schedule", "Exhibit", "Section", "Fees", "Notes", "Table" — alone or combined with a number or letter, and does NOT contain any specific service name or descriptive topic.
STEP 7 — Predecessor Fallback: If the header is GENERIC, scan upward for the nearest NON-GENERIC centrally aligned header and combine: "[Non-Generic] — [Generic]". If no non-generic predecessor exists, return the generic header as-is.

FAILURE RULES:
- Every extracted item MUST have a Section Header returned. No exceptions.
- Returning null for any item when any header exists anywhere above it in the document is an ERROR.

STRIKETHROUGH HANDLING:
- IGNORE struck values. If a struck price is followed by a new non-struck price, capture only the new one.
- If a non-struck label says "Included" / "Waived" / "$0" / "No Charge" — DO extract the item with that label as the price.

WAIVED-LABEL OVERRIDE RULE:
If the word "waived" (case-insensitive, including "(waived)", "[waived]", or "Fee waived") appears anywhere in or directly adjacent to the price/description for an item, you MUST treat that item as waived.
- Set "price" to "Waived" verbatim (do NOT capture any visible dollar amount alongside it as the price).

PERCENTAGE PRICING:
If an item is priced as a percentage (e.g. "2.9%", "0.05% of transaction amount"), capture the FULL price string as written.

EXCLUSION RULES:
- EXCLUDE totals, subtotals, grand totals.
- EXCLUDE asset-size thresholds, tier triggers (e.g. "over $100M"), prose conditions.

TABLE COLUMN PRIORITY:
- If QTY + UNIT PRICE + AMOUNT all exist: use AMOUNT for price, QTY for quantity, UNITS for frequency. Ignore UNIT PRICE.
- If AMOUNT is TBD/blank: fall back to UNIT PRICE.

TIER ROW SELECTION:
1. ROW-LEVEL CHECKBOXES PRESENT: Only extract rows whose box is MARKED.
2. NO ROW-LEVEL CHECKBOXES: EXTRACT EVERY TIER ROW IN THE TABLE. Do NOT pick only one tier.
3. ABSENCE OF VISIBLE TIER CHECKBOXES IS NOT A SIGNAL TO PICK ONE TIER.

Return STRICT JSON:

{
  "items": [
    {
      "item": "<clear description>",
      "checkbox_checked": true | false | null,
      "price": "<dollar value exactly as written>",
      "quantity": "<qty if present, else null>",
      "frequency": "<frequency / units if present, else null>",
      "page": <page number>,
      "explanation": "brief explanation",
      "section_header": "<section title or null>",
      "material_code": "<material code from contract if visible, else null>"
    }
  ]
}

IMPORTANT:
- Use ONLY visible text. Do NOT infer.
- Be exhaustive. Missing a dollar value is an error.
- Do not include markdown.
""".strip()


# ── Optional marked-checkbox reference image ────────────────────────────────
# The DNA notebook crashes hard when the image is missing; we make it
# optional with a clear warning because the prompt's textual rules still
# carry most of the checkbox classification weight.
_REF_IMAGE_PATHS = [
    Path(__file__).resolve().parent.parent / "marked_checkbox_example.png",
    Path(__file__).resolve().parent / "marked_checkbox_example.png",
    Path.cwd() / "marked_checkbox_example.png",
]


def _load_checkbox_reference_b64(log) -> Optional[str]:
    for p in _REF_IMAGE_PATHS:
        if p.exists():
            try:
                return base64.b64encode(p.read_bytes()).decode("utf-8")
            except Exception as e:
                log(f"  (could not read checkbox reference {p}: {e})")
    log("  (marked_checkbox_example.png not found — proceeding without "
        "visual reference; the prompt's textual checkbox rules still apply)")
    return None


def _parse_json_text(raw: str, chunk_index: int, log) -> Optional[dict]:
    """Robust JSON-from-text parser. Three strategies in order:
      1. Strip ``` fences then ``json.loads`` the result
      2. Try the raw text as-is via ``json.loads``
      3. Slice from first ``{`` to last ``}`` and try parse — handles
         models that wrap their JSON in conversational prose

    Returns the parsed dict if any strategy succeeds and the dict has
    an ``items`` key; ``None`` otherwise. On failure, logs a generous
    500-char preview of the raw text so the user can diagnose."""
    if not raw or not raw.strip():
        log(f"  ⚠ chunk {chunk_index}: response message.content is EMPTY")
        return None

    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"```$", "", s).strip()

    for attempt_text in (s, raw.strip()):
        try:
            parsed = json.loads(attempt_text)
            if isinstance(parsed, dict) and "items" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    first = s.find("{")
    last  = s.rfind("}")
    if first >= 0 and last > first:
        try:
            parsed = json.loads(s[first:last + 1])
            if isinstance(parsed, dict) and "items" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    log(f"  ⚠ chunk {chunk_index}: could not parse JSON from response. "
        f"Raw preview (500 chars):")
    log(f"      {raw[:500]!r}")
    return None


def _extract_response_json(resp, chunk_index: int, log) -> Optional[dict]:
    """Pull JSON out of a Responses-API response.

    Handles the gpt-5.x quirk where ``resp.output_text`` is empty but the
    actual JSON lives in ``resp.output[*].content[*].text`` (reasoning
    items vs message items). Collects every candidate text payload, tries
    each one through ``_parse_json_text``'s fence/slice strategies, and
    returns the first valid ``{"items": [...]}`` payload it finds."""
    candidates = []

    raw_text = getattr(resp, "output_text", None) or ""
    if raw_text.strip():
        candidates.append(raw_text.strip())

    out_items = getattr(resp, "output", None) or []
    item_types = []
    try:
        for item in out_items:
            t = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            item_types.append(t)
            if t and t != "message":
                continue
            content = getattr(item, "content", None) or (
                item.get("content") if isinstance(item, dict) else []
            ) or []
            for c in content:
                text = (getattr(c, "text", None)
                        or (c.get("text") if isinstance(c, dict) else None))
                if text and str(text).strip():
                    s = str(text).strip()
                    if s not in candidates:
                        candidates.append(s)
    except Exception:
        pass

    for raw in candidates:
        parsed = _parse_json_text(raw, chunk_index, log)
        if parsed is not None:
            return parsed

    if not candidates:
        log(f"  ⚠ chunk {chunk_index}: response has no usable text — "
            f"output_text empty AND output[*] item types={item_types}")
    return None


def extract_chunk(ai_client, page_chunk: list, chunk_index: int,
                  ref_b64: Optional[str], log) -> dict:
    """Send one chunk of rendered PDF pages to the LLM and parse the JSON.

    Uses the Responses API (``responses.create``) — same call shape that
    PORTICO's extraction.py uses successfully against gpt-5.2-2025-12-11.
    DNA was previously using ``chat.completions.create`` with the
    notebook-verbatim API, but on OpenAI-direct the gpt-5.x family does
    NOT process ``image_url`` content blocks reliably from
    chat.completions (the symptom was 19k input tokens billed for the
    images but 10 output tokens emitted — the model effectively replied
    "no items" because it couldn't see the pages). The Responses API,
    with ``input_image`` content blocks, is gpt-5's native multimodal
    path.

    On both backends:
      * OpenAI direct (``OPENAI_BACKEND=openai``) — hits OpenAI's
        responses endpoint natively.
      * Fiserv VDI (``OPENAI_BACKEND=fiserv``) — fiserv_client's
        ``FoundationClient.responses.create`` translates
        ``input_text`` / ``input_image`` blocks into the proxy's
        chat-completions content blocks transparently.

    Returns a dict with shape ``{"items": [...], "_failure": str}``. When
    parsing fails after all retries, ``items`` is ``[]`` and ``_failure``
    carries a one-line summary (e.g. ``"empty_response"`` /
    ``"parse_error: <preview>"`` / ``"api_error: <type>: <msg>"``) so the
    caller can surface a real reason instead of the generic "0 items"
    fallback."""
    input_content: list = []

    if ref_b64:
        input_content.append({
            "type": "input_text",
            "text": ("The first image is a REFERENCE EXAMPLE of a MARKED "
                     "checkbox. Any square containing visible crossing "
                     "diagonal lines is ALWAYS classified as MARKED."),
        })
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{ref_b64}",
            "detail": "high",
        })
        input_content.append({
            "type": "input_text",
            "text": ("The following images are sequential pages from a "
                     "contract. Use the checkbox reference example above "
                     "to correctly classify checkboxes."),
        })
    else:
        input_content.append({
            "type": "input_text",
            "text": ("The following images are sequential pages from a "
                     "contract."),
        })

    for page in page_chunk:
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{image_to_base64(page['image'])}",
            "detail": "high",
        })

    max_attempts = 3
    last_err = None
    # gpt-5 reasoning models on responses.create reject `temperature` overrides
    # but accept `max_output_tokens`. gpt-4-class models accept both.
    is_reasoning = bool(re.search(r"gpt-?5", str(EXTRACTION_MODEL), re.I))
    for attempt in range(1, max_attempts + 1):
        try:
            kwargs = dict(
                model=EXTRACTION_MODEL,
                max_output_tokens=32768,
                input=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user",   "content": input_content},
                ],
            )
            if not is_reasoning:
                kwargs["temperature"] = 0
            try:
                resp = ai_client.responses.create(**kwargs)
            except Exception as inner_e:
                emsg = str(inner_e).lower()
                if "temperature" in emsg and "temperature" in kwargs:
                    log(f"    (chunk {chunk_index}: model rejected "
                        f"temperature; retrying without it)")
                    kwargs.pop("temperature", None)
                    resp = ai_client.responses.create(**kwargs)
                else:
                    raise

            parsed = _extract_response_json(resp, chunk_index, log)
            if parsed is not None:
                return parsed
            last_err = "empty_response"
        except Exception as e:
            err_summary = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"  ⚠ chunk {chunk_index} attempt {attempt}: API call "
                f"failed: {err_summary}")
            last_err = f"api_error: {err_summary}"
        if attempt < max_attempts:
            wait = 2 ** attempt
            log(f"    retrying in {wait}s…")
            time.sleep(wait)
    log(f"  ⚠ chunk {chunk_index} EXHAUSTED {max_attempts} attempts "
        f"(last error: {last_err}) — DROPPING this chunk's items.")
    return {"items": [], "_failure": last_err or "unknown"}


def extract_items_with_dollar_values(pdf_path: str, ai_client,
                                     ref_b64: Optional[str], log) -> tuple:
    """Render the PDF, chunk it, and dispatch chunks across a thread pool.

    Returns ``(df, failure_summary)`` where ``failure_summary`` is a list
    of strings — one per failed chunk — so the caller can surface a real
    reason in the sidecar metadata when extraction produced zero items
    (e.g. ``"chunk 1: empty_response"``,
    ``"chunk 2: api_error: RateLimitError: ..."``). Empty list means
    every chunk succeeded (or the PDF really has no fees)."""
    log(f"  Converting {os.path.basename(pdf_path)} to images @ DPI {DPI}…")
    pages        = pdf_to_images(pdf_path)
    total_pages  = len(pages)
    total_chunks = math.ceil(total_pages / CHUNK_SIZE)
    n_workers    = max(1, min(CHUNK_WORKERS, total_chunks))
    log(f"  Total pages: {total_pages} | Chunks: {total_chunks} | "
        f"Workers: {n_workers}")

    chunk_results = [None] * total_chunks
    done_count    = [0]
    lock          = threading.Lock()

    def _process_chunk(ci: int):
        start = ci * CHUNK_SIZE
        chunk = pages[start:start + CHUNK_SIZE]
        try:
            result = extract_chunk(ai_client, chunk, ci + 1, ref_b64, log)
        except Exception as e:
            log(f"  ⚠ chunk {ci + 1} threw uncaught exception: "
                f"{type(e).__name__}: {str(e)[:200]}")
            return ci, {"items": [],
                        "_failure": f"uncaught: {type(e).__name__}: "
                                    f"{str(e)[:200]}"}
        with lock:
            done_count[0] += 1
            n_items = len(result.get("items", []) or [])
            log(f"    chunk {ci + 1}/{total_chunks}: {n_items} item(s)  "
                f"({done_count[0]}/{total_chunks} done)")
        return ci, result

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(_process_chunk, i) for i in range(total_chunks)]
        for fut in as_completed(futures):
            ci, parsed = fut.result()
            chunk_results[ci] = parsed

    all_items = []
    failed_chunks = 0
    failures: list = []
    for ci, parsed in enumerate(chunk_results):
        if parsed is None:
            failed_chunks += 1
            failures.append(f"chunk {ci + 1}: no result")
            continue
        chunk_items = parsed.get("items", []) or []
        if not chunk_items:
            failed_chunks += 1
            # Only surface explicit failures (API/parse errors). A
            # genuinely-empty chunk on a fee-less page shouldn't generate
            # a scary error string.
            reason = parsed.get("_failure")
            if reason:
                failures.append(f"chunk {ci + 1}: {reason}")
        for item in chunk_items:
            all_items.append({
                "Item":                    item.get("item"),
                "Price":                   item.get("price"),
                "Quantity":                item.get("quantity"),
                "Frequency":               item.get("frequency"),
                "Page":                    item.get("page"),
                "Checkbox_Checked":        item.get("checkbox_checked"),
                "Explanation":             item.get("explanation"),
                "Section_Header":          item.get("section_header"),
                "Extracted_Material_Code": item.get("material_code"),
            })
    df = pd.DataFrame(all_items)
    if not df.empty:
        df = _apply_frequency_inference(df)
    log(f"  ── PDF summary: {total_pages} pages → {len(df)} items "
        f"across {total_chunks} chunks ({failed_chunks} failed/empty)")
    return df, failures


# ═══════════════════════════════════════════════════════════════════════════
# EMPTY-OUTPUT WRITERS + SIDECAR METADATA (mirrors PORTICO)
# ═══════════════════════════════════════════════════════════════════════════

_EXTRACTION_COLUMNS = [
    "Item", "Price", "Quantity", "Frequency", "Page", "Checkbox_Checked",
    "Explanation", "Section_Header", "Extracted_Material_Code",
    "Source Contract", "Date",
]

_MATCHING_COLUMNS = [
    "Item", "Price", "Cleaned Price", "Pricing_Type", "Tier_Range",
    "Quantity", "Frequency", "Page", "Checkbox_Checked", "Explanation",
    "Section_Header", "Normalized_Section", "Extracted_Material_Code",
    "Source Contract", "Date",
    "Final Material Code", "Material Code",      # canonical alias
    "Matched Description", "Match Source", "Confidence Percentage",
    "Match Confidence",
]


def _extraction_meta_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "extraction_meta.json"


def _matching_meta_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "material_match_meta.json"


def read_extraction_meta(client_name: str) -> dict:
    p = _extraction_meta_path(client_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_matching_meta(client_name: str) -> dict:
    p = _matching_meta_path(client_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _summarize_failures(failures: list) -> str:
    """Distill a list of per-chunk failure strings into one human-readable
    line for the sidecar meta + frontend explanation. Grouped by reason
    so 'chunk 1: empty_response | chunk 2: empty_response | chunk 3:
    api_error: RateLimitError: ...' becomes
    '2 chunks: empty_response; 1 chunk: RateLimitError: ...'."""
    if not failures:
        return ""
    buckets: dict = {}
    for f in failures:
        # Strip the "<filename>: " and/or "chunk N: " prefixes so
        # 'foo.pdf: chunk 1: empty_response' and
        # 'bar.pdf: chunk 2: empty_response' bucket together.
        reason = str(f)
        reason = re.sub(r"^[^:]+\.pdf:\s*", "", reason, flags=re.IGNORECASE)
        reason = re.sub(r"^chunk\s+\d+:\s*", "", reason)
        buckets[reason] = buckets.get(reason, 0) + 1
    parts = []
    for reason, count in sorted(buckets.items(),
                                key=lambda x: -x[1]):
        # Trim very long reasons (e.g. full exception messages) so the
        # frontend pill stays readable.
        if len(reason) > 200:
            reason = reason[:200] + "…"
        parts.append(f"{count} chunk{'s' if count > 1 else ''}: {reason}")
    return "; ".join(parts)


def _write_empty_extraction(client_name: str, status: str, min_year: int,
                            folder, log, *,
                            files_in_folder: int = 0,
                            selected_files: int = 0,
                            failures: Optional[list] = None,
                            extra_note: str = "") -> None:
    out_dir = _OUTPUT_DIR / client_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "extraction_output.xlsx"
    try:
        with pd.ExcelWriter(str(out_path), engine="xlsxwriter") as writer:
            pd.DataFrame(columns=_EXTRACTION_COLUMNS).to_excel(
                writer, index=False, sheet_name="Dollar_Items"
            )
    except Exception as e:
        log(f"  ⚠ Could not write empty extraction_output.xlsx: {e}")
        return

    failure_summary = _summarize_failures(failures or [])

    # Pick a note appropriate to the actual failure mode. ``no_pdfs`` means
    # the year filter excluded everything; ``no_items`` with chunk failures
    # means the API/parse misbehaved; ``no_items`` with no failures means
    # the model genuinely saw no fees on the page(s).
    if status == "no_pdfs":
        note = (
            f"Fee Description found {files_in_folder} PDF(s) in the input "
            f"folder but none passed the year filter (≥ {min_year}). "
            f"Lower the PDF year cutoff in the chatbot UI and re-run."
        )
    elif failure_summary:
        note = (
            f"Fee Description processed {selected_files} PDF(s) dated ≥ "
            f"{min_year} but the model API failed for every chunk. "
            f"Reasons: {failure_summary}. The PDFs are NOT pre-cutoff — "
            f"this is an API / model response problem, not a year-filter "
            f"problem."
        )
    else:
        note = (
            f"Fee Description processed {selected_files} PDF(s) dated ≥ "
            f"{min_year} but the model reported zero fee items in any of "
            f"them. The PDFs are NOT pre-cutoff. Verify the contracts "
            f"actually contain a fee schedule (not just legal/body text)."
        )
    if extra_note:
        note = f"{note} {extra_note}"

    payload = {
        "status":   status, "core": "DNA",
        "min_year": int(min_year), "folder": str(folder),
        "ran_at":   datetime.now().isoformat(timespec="seconds"),
        "rows":     0,
        "files_in_folder": int(files_in_folder),
        "selected_files":  int(selected_files),
        "failure_summary": failure_summary,
        "note": note,
    }
    _write_meta(_extraction_meta_path(client_name), payload)
    log(f"  Wrote empty {out_path.name} + sidecar meta ({status}, "
        f"min_year={min_year})")


def _write_empty_matching(client_name: str, status: str, min_year: int,
                          log) -> None:
    out_dir = _OUTPUT_DIR / client_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "material_match_output.xlsx"
    try:
        pd.DataFrame(columns=_MATCHING_COLUMNS).to_excel(
            str(out_path), index=False
        )
    except Exception as e:
        log(f"  ⚠ Could not write empty material_match_output.xlsx: {e}")
        return
    _write_meta(_matching_meta_path(client_name), {
        "status":   status, "core": "DNA",
        "min_year": int(min_year),
        "ran_at":   datetime.now().isoformat(timespec="seconds"),
        "rows":     0,
        "note": (
            "DNA Material Code Matching ran but produced 0 rows. Most "
            "common cause: Fee Description produced 0 items (all PDFs "
            f"pre-{min_year}, or the DNA dictionary is missing). Check "
            "Input/DNA/ for DNA Dictionary US 03-26.xlsx and DNA "
            "Section Headers Normalization.xlsx."
        ),
    })
    log(f"  Wrote empty {out_path.name} + sidecar meta ({status}, "
        f"min_year={min_year})")


def export_to_excel(df: pd.DataFrame, output_path: Path, log) -> None:
    if df.empty:
        log("  No items extracted — skipping export.")
        return
    with pd.ExcelWriter(str(output_path), engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Dollar_Items")
    log(f"  Extraction saved: {output_path}")


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 PUBLIC ENTRY
# ═══════════════════════════════════════════════════════════════════════════

def run(client_name: str,
        api_key: str = "",
        model: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
        contracts: Optional[list] = None,    # accepted for API parity; unused
        core: str = "",
        min_year: Optional[int] = None,
        **_legacy_kwargs) -> dict:
    """DNA Phase 1 — extraction.

    Writes ``Output/<Client>/extraction_output.xlsx`` (sheet
    ``Dollar_Items``) plus an ``extraction_meta.json`` sidecar describing
    the run (status, year cutoff, row count). Returns a dict with the
    same shape as ``agents.extraction.run`` so the chatbot's runner code
    is uniform across cores."""
    global _LOG_HOOK
    # `log` is called from ThreadPoolExecutor workers (one per chunk).
    # The chatbot's progress callback is a Streamlit widget update
    # (pb.info), and Streamlit raises NoSessionContext when a widget is
    # touched from a non-main thread. Wrap so worker-thread logging can
    # never propagate an exception that the chunk loop would mis-attribute
    # as an API failure (which is what produced the misleading
    # "uncaught: NoSessionContext" failure_summary).
    _raw_log = progress_callback or (lambda m: None)
    _log_lock = threading.Lock()

    def log(msg: str) -> None:
        try:
            with _log_lock:
                _raw_log(msg)
        except Exception:
            # Worker-thread NoSessionContext / any other surface-side
            # failure — print to stdout as a fallback so the message
            # isn't lost, but never propagate.
            try:
                print(msg)
            except Exception:
                pass

    _LOG_HOOK = log
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    try:
        api_key = api_key or EXTRACTION_API_KEY
        global EXTRACTION_MODEL
        if model:
            EXTRACTION_MODEL = model

        log(f"━━━ Fee Description (DNA) {_VERSION} ━━━")
        log(f"Extraction model: {EXTRACTION_MODEL}")
        log(f"Year filter: contracts dated >= {min_year} are processed.")

        folder = client_input_dir(client_name, core or "DNA")
        if not folder.exists():
            log(f"  ⚠ Input folder does not exist: {folder}")
            _write_empty_extraction(client_name, "no_pdfs", min_year,
                                    folder, log)
            return {"status": "no_pdfs", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        files = [f for f in os.listdir(folder)
                 if f.lower().endswith(".pdf")]
        if not files:
            log(f"  ⚠ No PDFs in {folder}")
            _write_empty_extraction(client_name, "no_pdfs", min_year,
                                    folder, log, files_in_folder=0)
            return {"status": "no_pdfs", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        pairs = select_files_for_client(folder, files, min_year, log)
        if not pairs:
            _write_empty_extraction(client_name, "no_pdfs", min_year,
                                    folder, log,
                                    files_in_folder=len(files))
            return {"status": "no_pdfs", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        ai_client = make_client(api_key)
        ref_b64   = _load_checkbox_reference_b64(log)

        frames = []
        all_failures: list = []
        per_file_failures = {}
        for fname, date_obj in pairs:
            log(f"\n  Extracting: {fname}")
            try:
                df, fail = extract_items_with_dollar_values(
                    str(folder / fname), ai_client, ref_b64, log
                )
            except Exception as e:
                log(f"  ⚠ Failed on {fname}: {e} — skipping")
                err = f"{type(e).__name__}: {str(e)[:200]}"
                per_file_failures[fname] = [f"uncaught: {err}"]
                all_failures.append(f"{fname}: uncaught: {err}")
                continue
            if fail:
                per_file_failures[fname] = fail
                for f in fail:
                    all_failures.append(f"{fname}: {f}")
            if df.empty:
                log("  No items extracted from this file.")
                continue
            df["Source Contract"] = fname
            df["Date"] = (date_obj.strftime("%Y-%m-%d")
                          if date_obj and date_obj.year > 1900 else "")
            frames.append(df)

        if not frames:
            log("  No items extracted from any qualifying PDF.")
            _write_empty_extraction(
                client_name, "no_items", min_year, folder, log,
                files_in_folder=len(files),
                selected_files=len(pairs),
                failures=all_failures,
            )
            return {"status": "no_items", "client": client_name,
                    "rows": 0, "min_year": int(min_year),
                    "failure_summary": _summarize_failures(all_failures)}

        combined = pd.concat(frames, ignore_index=True)
        out_path = output_path(client_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        export_to_excel(combined, out_path, log)
        meta_payload = {
            "status":   "complete", "core": "DNA",
            "min_year": int(min_year),
            "folder":   str(folder),
            "ran_at":   datetime.now().isoformat(timespec="seconds"),
            "rows":     int(len(combined)),
            "files_in_folder": len(files),
            "selected_files":  len(pairs),
        }
        if all_failures:
            # Partial success — surface so the UI can warn that some pages
            # were dropped even though overall the run produced rows.
            meta_payload["failure_summary"] = _summarize_failures(all_failures)
        _write_meta(_extraction_meta_path(client_name), meta_payload)
        log(f"\nWrote: {out_path.name} ({len(combined)} items)")
        return {"status": "complete", "client": client_name,
                "rows": int(len(combined)), "output": str(out_path),
                "min_year": int(min_year)}
    finally:
        _LOG_HOOK = None


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — MATCHING (section-aware hybrid + AI rerank)
# ═══════════════════════════════════════════════════════════════════════════

_LEGAL_NOISE_PATTERNS = [
    r"pursuant to[^,.]*",
    r"as defined in[^,.]*",
    r"in accordance with[^,.]*",
    r"as set forth[^,.]*",
    r"as described in[^,.]*",
    r"under section [^,.]*",
    r"hereunder",
    r"thereunder",
    r"as applicable",
    r"if applicable",
]


def strip_legal_noise(text) -> str:
    if not isinstance(text, str):
        return ""
    s = text
    for p in _LEGAL_NOISE_PATTERNS:
        s = re.sub(p, "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def normalize_section(raw_header, norm_lookup: dict,
                      norm_choices: list) -> tuple:
    """Map a raw section header to its canonical DNA section name."""
    if not isinstance(raw_header, str) or not raw_header.strip():
        return None, 0
    key = raw_header.strip().lower()
    if key in norm_lookup:
        return norm_lookup[key], 100
    best = process.extractOne(key, norm_choices, scorer=fuzz.WRatio)
    if best and best[1] >= SECTION_NORM_THRESHOLD:
        return norm_lookup[best[0]], best[1]
    return raw_header.strip(), best[1] if best else 0


MATCHING_SYSTEM_PROMPT = """
You are an expert at matching contract line items to a Material Code dictionary.

For each contract item, return the BEST matching dictionary entry's Contract Description (verbatim) and its Material Code.

RULES:
1. Match on MEANING, not surface form. "Mthly" == "Monthly". "Setup" == "Implementation".
2. Use ONLY descriptions that appear in the dictionary block below. Do NOT invent.
3. Monthly fees and one-time/setup fees are not interchangeable.
4. Product specificity beats generic. Prefer the specific product/service over a generic category.
5. If a contract item clearly matches none of the descriptions, return null.

Return STRICT JSON:

{
  "matches": [
    {
      "item_index": <integer index from the input list>,
      "matched_description": "<exact dictionary description or null>",
      "material_code": "<material code from dictionary or null>",
      "confidence": <integer 0-100>
    }
  ]
}

No markdown. No explanations.
""".strip()


def ai_match_batch(ai_client, item_batch: list,
                   dict_chunk: pd.DataFrame, log) -> list:
    """Send one batch of items + one chunk of the dictionary to the model
    and return the parsed ``matches`` list."""
    items_text = "\n".join(
        f"[{i}] Item: {it['Item']} | Section: "
        f"{it.get('Normalized_Section') or it.get('Section_Header') or 'N/A'}"
        for i, it in enumerate(item_batch)
    )
    dict_text = "\n".join(
        f"- [{r['Material Code']}] {r['Contract Description']} "
        f"(Section: {r['Section Header']})"
        for _, r in dict_chunk.iterrows()
    )
    user_prompt = (
        f"DICTIONARY ENTRIES:\n{dict_text}\n\n"
        f"CONTRACT ITEMS TO MATCH:\n{items_text}\n\n"
        "Return matches for each item by item_index. Use null when no "
        "good match exists."
    )
    try:
        # chat.completions.create — same API the colleague's notebook uses
        # for matching. Avoids the gpt-5.x Responses API quirks that
        # caused empty extraction output. Same param-routing rules as
        # extract_chunk: gpt-5 family wants max_completion_tokens and no
        # temperature override.
        is_reasoning = bool(re.search(r"gpt-?5", str(MATCHING_MODEL), re.I))
        kwargs = dict(
            model=MATCHING_MODEL,
            messages=[
                {"role": "system", "content": MATCHING_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
        )
        if not is_reasoning:
            kwargs["temperature"] = 0
        try:
            resp = call_api_with_retry(
                lambda: ai_client.chat.completions.create(**kwargs))
        except Exception as inner_e:
            emsg = str(inner_e).lower()
            if "temperature" in emsg and "temperature" in kwargs:
                kwargs.pop("temperature", None)
                resp = call_api_with_retry(
                    lambda: ai_client.chat.completions.create(**kwargs))
            else:
                raise
        raw = ""
        if getattr(resp, "choices", None):
            msg = getattr(resp.choices[0], "message", None)
            raw = (getattr(msg, "content", None) or "").strip() if msg else ""
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"```$", "", raw).strip()
        # Robust parse — same fallback strategies as the extraction parser.
        try:
            return json.loads(raw).get("matches", []) or []
        except json.JSONDecodeError:
            first = raw.find("{")
            last  = raw.rfind("}")
            if first >= 0 and last > first:
                try:
                    return json.loads(raw[first:last + 1]).get("matches", []) or []
                except json.JSONDecodeError:
                    pass
        log(f"  ⚠ AI match: raw preview: {raw[:300]!r}")
        return []
    except Exception as e:
        log(f"  ⚠ AI match call failed: {e}")
        return []


def hybrid_score_item(item_text: str, candidate_idxs: list,
                      item_emb: np.ndarray, all_desc_emb: np.ndarray,
                      all_desc_texts: list):
    """Combined semantic + lexical score against a restricted candidate
    pool. Returns ``(best_idx, score)``."""
    if not candidate_idxs:
        return None, 0.0
    cand_emb   = all_desc_emb[candidate_idxs]
    sem_scores = (item_emb @ cand_emb.T).flatten()
    cand_texts = [all_desc_texts[i] for i in candidate_idxs]
    try:
        vec   = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        tfidf = vec.fit_transform([item_text] + cand_texts)
        lex   = (tfidf[0] @ tfidf[1:].T).toarray().flatten()
        if lex.max() > 0:
            lex = lex / lex.max()
    except Exception:
        lex = np.zeros_like(sem_scores)
    combined   = SEMANTIC_WEIGHT * sem_scores + LEXICAL_WEIGHT * lex
    best_local = int(np.argmax(combined))
    return candidate_idxs[best_local], float(combined[best_local])


# Cached sentence-transformer (lazy load; reused across run_matching calls
# within the same Streamlit session so the 420 MB model isn't reloaded
# every click).
_ST_MODEL: Optional[SentenceTransformer] = None


def _get_st_model(log):
    global _ST_MODEL
    if _ST_MODEL is None:
        log("  Loading sentence transformer 'all-mpnet-base-v2'…")
        _ST_MODEL = SentenceTransformer("all-mpnet-base-v2")
        log("  Model loaded.")
    return _ST_MODEL


def _load_dna_dictionary(dict_path: Path, log) -> Optional[pd.DataFrame]:
    """Load the DNA dictionary. Tries the same sheet candidates PORTICO does
    but prefers the colleague's default ``Sheet1``."""
    try:
        sheets = pd.ExcelFile(str(dict_path)).sheet_names
    except Exception as e:
        log(f"  ⚠ Could not open DNA dictionary {dict_path.name}: {e}")
        return None
    for sheet in ("Sheet1", "Combined Dictionary", "Final", "Dictionary"):
        if sheet in sheets:
            df = pd.read_excel(str(dict_path), sheet_name=sheet)
            if ({"Material Code", "Contract Description"}.issubset(df.columns)):
                log(f"  Using DNA dictionary sheet '{sheet}' "
                    f"({len(df)} rows)")
                return df.dropna(subset=["Material Code",
                                         "Contract Description"]).reset_index(drop=True)
    for sheet in sheets:
        df = pd.read_excel(str(dict_path), sheet_name=sheet)
        if ({"Material Code", "Contract Description"}.issubset(df.columns)):
            log(f"  Using auto-detected DNA sheet '{sheet}' "
                f"({len(df)} rows)")
            return df.dropna(subset=["Material Code",
                                     "Contract Description"]).reset_index(drop=True)
    log(f"  ⚠ No sheet in {dict_path.name} has both 'Material Code' AND "
        f"'Contract Description' columns.")
    return None


def _load_dna_normalization(norm_path: Path, log) -> pd.DataFrame:
    """Load the DNA section-header normalization table. Returns an empty
    DataFrame if the file isn't present — normalization just degrades to
    'no canonicalisation' and the section-aware matching still uses the
    raw header."""
    if not norm_path or not norm_path.exists():
        log(f"  ⚠ Normalization file not found at {norm_path}; "
            "matching will skip section canonicalisation.")
        return pd.DataFrame(columns=["Section Headers", "Section"])
    try:
        for sheet in ("Sheet2", "Sheet1", "Normalization"):
            try:
                df = pd.read_excel(str(norm_path), sheet_name=sheet)
            except Exception:
                continue
            if "Section Headers" in df.columns and "Section" in df.columns:
                log(f"  Using normalization sheet '{sheet}' "
                    f"({len(df)} rows)")
                return df
        log(f"  ⚠ No normalization sheet found with the expected "
            "'Section Headers' + 'Section' columns.")
        return pd.DataFrame(columns=["Section Headers", "Section"])
    except Exception as e:
        log(f"  ⚠ Could not read normalization file: {e}")
        return pd.DataFrame(columns=["Section Headers", "Section"])


def _normalize_client_name(name: str) -> str:
    """Lower-case + collapse whitespace + drop punctuation so two spellings
    of the same client (e.g. 'KAWARTHA CREDIT UNION LTD' vs 'Kawartha
    Credit Union, Ltd.') compare equal."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip()


_CA_NAME_NOISE_TOKENS = {
    "credit", "union", "cu", "ltd", "limited", "inc", "incorporated",
    "the", "of", "and", "a", "an", "co", "company", "corp", "corporation",
    "fcu", "federal", "financial", "services", "service",
}


def _client_name_signature(name: str) -> set:
    """Tokenise a normalised client name and drop generic noise words
    ('credit', 'union', 'cu', 'ltd', etc.) so two clients only register as
    matching when they share a distinctive, non-generic token (e.g.
    'kawartha', 'concentra'). Prevents bogus matches like every credit
    union in the world routing to Canada."""
    return {
        t for t in _normalize_client_name(name).split()
        if t and t not in _CA_NAME_NOISE_TOKENS and not t.isdigit()
    }


def _load_ca_client_names(ca_list_path: Path, log) -> set:
    """Load the Canadian-client roster from ``DNA CA Client Names.xlsx`` and
    return a set of normalised names.

    The workbook ships with two sheets:
      * ``Export``  — a full SAP-style data export with 15 columns
        (SoldTo, BillTo, sales org, postal codes, …). NOT the source of
        truth — picking it up would pollute the set with 'sales org',
        country codes, GUD numbers, etc.
      * ``Sheet1``  — a single-column curated list of client names with
        the first client as the (un-labelled) header row.
    We prefer ``Sheet1``. If the curated sheet is missing, we fall back
    to the ``SoldTo`` column of ``Export`` (the only column that holds
    client names) so the matcher still works on a renamed workbook."""
    if not ca_list_path or not ca_list_path.exists():
        log(f"  (no CA-client list at {ca_list_path}; all DNA clients "
            "default to the US dictionary)")
        return set()
    try:
        xls = pd.ExcelFile(str(ca_list_path))
    except Exception as e:
        log(f"  ⚠ Could not open CA-client list {ca_list_path.name}: {e}")
        return set()

    raw_values: list = []
    if "Sheet1" in xls.sheet_names:
        # header=None — the first row IS a client name on this sheet, not a
        # header label.
        df_ca = pd.read_excel(str(ca_list_path), sheet_name="Sheet1",
                              header=None)
        # Single-column curated list — take the first column verbatim.
        if len(df_ca.columns) >= 1:
            raw_values = df_ca.iloc[:, 0].dropna().astype(str).tolist()
    if not raw_values:
        for s in xls.sheet_names:
            df_ca = pd.read_excel(str(ca_list_path), sheet_name=s)
            for cand_col in ("SoldTo", "Client", "Client Name", "Name"):
                if cand_col in df_ca.columns:
                    raw_values = df_ca[cand_col].dropna().astype(str).tolist()
                    break
            if raw_values:
                break

    names: set = set()
    for v in raw_values:
        key = _normalize_client_name(v)
        # Reject anything that's all-digits or shorter than 3 chars — those
        # are SAP IDs, postal codes, or noise rows.
        if key and len(key) >= 3 and not key.replace(" ", "").isdigit():
            names.add(key)
    log(f"  Loaded {len(names)} Canadian DNA client name(s) from "
        f"{ca_list_path.name}")
    return names


def _is_ca_client(client_name: str, ca_names: set) -> bool:
    """Match the current client against the CA roster.

    Strategy (in order):
      1. Exact normalised equality (handles the common case where the
         client folder name is verbatim the SAP SoldTo name).
      2. Distinctive-token overlap — the client's non-generic tokens
         (everything except 'credit', 'union', 'ltd', etc.) must form a
         subset OR superset of some CA roster entry's distinctive
         tokens. This catches legal-suffix variants ('KAWARTHA CREDIT
         UNION LTD' vs 'KAWARTHA CREDIT UNION') without false-matching
         every credit union just because the words overlap."""
    if not client_name or not ca_names:
        return False
    key = _normalize_client_name(client_name)
    if not key:
        return False
    if key in ca_names:
        return True
    client_sig = _client_name_signature(client_name)
    if not client_sig:
        return False
    for n in ca_names:
        n_sig = _client_name_signature(n)
        if not n_sig:
            continue
        if client_sig == n_sig:
            return True
        # Subset/superset match on distinctive tokens — handles
        # abbreviations like 'BULKLEY VALLEY CU' vs 'BULKLEY VALLEY
        # CREDIT UNION'. Requires both signatures to be non-trivial.
        if (client_sig <= n_sig or n_sig <= client_sig) \
                and min(len(client_sig), len(n_sig)) >= 1:
            return True
    return False


def _resolve_dna_inputs(core: str, log, client_name: str = "") -> tuple:
    """Return (dictionary_path, normalization_path). Both default to
    Input/<core>/<filename> with the canonical DNA names.

    The dictionary is routed by client geography:
      * Canadian clients (any name listed in ``DNA CA Client Names.xlsx``)
        → ``DNA Dictionary Canada.xlsx``
      * Everyone else → ``DNA Dictionary US 03-26.xlsx``
    Falls back to any ``*dict*.xlsx`` in the folder if the canonical
    filename is missing, so renamed releases keep working."""
    base = _INPUT_DIR / (core or "DNA")
    norm_path = base / "DNA Section Headers Normalization.xlsx"
    ca_list_path = base / "DNA CA Client Names.xlsx"

    ca_names = _load_ca_client_names(ca_list_path, log)
    is_canadian = _is_ca_client(client_name, ca_names)
    if is_canadian:
        log(f"  Client {client_name!r} is on the Canadian roster — "
            "routing to DNA Dictionary Canada.")
        dict_path = base / "DNA Dictionary Canada.xlsx"
        dict_keyword = "canada"
    else:
        log(f"  Client {client_name!r} not on the Canadian roster — "
            "using DNA Dictionary US.")
        dict_path = base / "DNA Dictionary US 03-26.xlsx"
        dict_keyword = "us"

    if not dict_path.exists():
        # Fall back to a matching *Dictionary*.xlsx (geography-aware) so a
        # renamed release still works.
        candidates = [p for p in base.glob("*.xlsx")
                      if "dict" in p.name.lower()
                      and dict_keyword in p.name.lower()]
        if not candidates:
            candidates = [p for p in base.glob("*.xlsx")
                          if "dict" in p.name.lower()]
        if candidates:
            dict_path = max(candidates, key=lambda p: p.stat().st_size)
            log(f"  (using {dict_path.name} as the DNA dictionary)")
    if not norm_path.exists():
        candidates = [p for p in base.glob("*.xlsx")
                      if "norm" in p.name.lower()]
        if candidates:
            norm_path = max(candidates, key=lambda p: p.stat().st_size)
            log(f"  (using {norm_path.name} as the DNA normalization table)")
    return dict_path, norm_path


def _run_matching_impl(extraction_df: pd.DataFrame,
                       df_dict: pd.DataFrame,
                       df_norm: pd.DataFrame,
                       ai_client, log) -> pd.DataFrame:
    """Verbatim port of the notebook's ``run_matching`` function."""
    st_model = _get_st_model(log)

    df_norm_clean = df_norm[["Section Headers", "Section"]].dropna()
    norm_lookup   = {
        str(r["Section Headers"]).strip().lower(): str(r["Section"]).strip()
        for _, r in df_norm_clean.iterrows()
    }
    norm_choices = list(norm_lookup.keys())
    log(f"  Normalization table: {len(norm_lookup)} mapping(s)")
    log(f"  Dictionary: {len(df_dict)} row(s), "
        f"{df_dict['Section Header'].nunique() if 'Section Header' in df_dict.columns else 0} "
        f"unique section(s)")

    df = extraction_df.copy()
    # Keep checked or unchecked-NaN; drop explicit unchecked (Checkbox == False).
    df = df[df["Checkbox_Checked"].isin([True, None]) | df["Checkbox_Checked"].isna()]
    df = df[df["Item"].notna() & (df["Item"].astype(str).str.strip() != "")]
    df = df.reset_index(drop=True)

    log(f"  Normalizing section headers for {len(df)} items…")
    norm_results = df["Section_Header"].apply(
        lambda h: normalize_section(h, norm_lookup, norm_choices)
    )
    df["Normalized_Section"] = [r[0] for r in norm_results]
    df["Section_Norm_Score"] = [r[1] for r in norm_results]

    log(f"  Encoding {len(df_dict)} dictionary descriptions…")
    dict_descs = df_dict["Contract Description"].astype(str).tolist()
    dict_emb   = st_model.encode(dict_descs, convert_to_numpy=True,
                                 normalize_embeddings=True, batch_size=64,
                                 show_progress_bar=False)

    log(f"  Encoding {len(df)} contract items…")
    item_texts = [strip_legal_noise(t or "") for t in df["Item"].astype(str).tolist()]
    item_embs  = st_model.encode(item_texts, convert_to_numpy=True,
                                 normalize_embeddings=True, batch_size=64,
                                 show_progress_bar=False)

    sect_lower_series = (df_dict["Section Header"].astype(str).str.lower()
                         if "Section Header" in df_dict.columns
                         else pd.Series([""] * len(df_dict)))
    section_index_cache: dict = {}

    def get_section_indices(normalized_section):
        if not normalized_section or not isinstance(normalized_section, str):
            return None
        key = normalized_section.lower()
        if key in section_index_cache:
            return section_index_cache[key]
        mask = sect_lower_series.str.contains(re.escape(key), na=False)
        idxs = df_dict.index[mask].tolist()
        if not idxs:
            unique_secs = sect_lower_series.unique().tolist()
            matched_secs = [s for s in unique_secs
                            if fuzz.partial_ratio(key, s) >= 75]
            if matched_secs:
                idxs = df_dict.index[sect_lower_series.isin(matched_secs)].tolist()
        section_index_cache[key] = idxs
        return idxs

    log("  Building product-keyword anchor index…")
    raw_secs = (df_dict["Section Header"].dropna().astype(str).unique().tolist()
                if "Section Header" in df_dict.columns else [])
    anchor_keywords: set = set()
    for s in raw_secs:
        s_clean = s.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9 \-]{2,20}", s_clean) and len(s_clean.split()) <= 3:
            for w in s_clean.split():
                if len(w) >= 4 and w.isalpha():
                    anchor_keywords.add(w.lower())
        m = re.match(r"^([A-Z][a-z]{4,15})(\s|$|®|™)", s_clean)
        if m:
            anchor_keywords.add(m.group(1).lower())
    anchor_keywords.update({
        "wisdom", "mobiliti", "zelle", "alldata", "nautilus", "cardhub",
        "originate", "notifi", "weiland", "checkfree", "convergeit",
        "loancierge", "transfernow", "fundnow", "securenow", "premier",
        "signature", "directors", "data compass", "data safe",
        "card valet", "instant open", "tmagic", "openchecking",
        "card expert", "card risk office", "configure digital",
        "kinective", "intelligent workplace",
    })
    log(f"    Anchor keywords: {len(anchor_keywords)}")

    keyword_index_cache: dict = {}
    sect_low = sect_lower_series.values
    desc_low = df_dict["Contract Description"].astype(str).str.lower().values
    for kw in anchor_keywords:
        mask = (
            pd.Series(sect_low).str.contains(re.escape(kw), na=False)
            | pd.Series(desc_low).str.contains(re.escape(kw), na=False)
        )
        keyword_index_cache[kw] = df_dict.index[mask].tolist()

    def get_keyword_indices(item_text: str) -> list:
        if not item_text:
            return []
        t = item_text.lower()
        idxs: set = set()
        for kw, kw_idxs in keyword_index_cache.items():
            if kw in t:
                idxs.update(kw_idxs)
        return list(idxs)

    log(f"  Scoring {len(df)} items via hybrid (sentence-transformers + TF-IDF)…")
    results    = [None] * len(df)
    ai_pending = []
    full_idxs  = df_dict.index.tolist()

    for i, row in df.iterrows():
        section        = row.get("Normalized_Section")
        extracted_code = row.get("Extracted_Material_Code")

        # 1) Contract-extracted material code validation (high-confidence path)
        if isinstance(extracted_code, str) and extracted_code.strip():
            code_clean = extracted_code.strip().upper()
            cand_idxs  = get_section_indices(section) or full_idxs
            sub        = df_dict.loc[cand_idxs]
            hit        = sub[sub["Material Code"].astype(str).str.upper() == code_clean]
            label      = "Contract+Dict (section)"
            if hit.empty:
                hit   = df_dict[df_dict["Material Code"].astype(str).str.upper() == code_clean]
                label = "Contract+Dict (full)"
            if not hit.empty:
                r = hit.iloc[0]
                results[i] = {
                    "Final Material Code": r["Material Code"],
                    "Matched Description": r["Contract Description"],
                    "Match Source":        label,
                    "Confidence Percentage": 100,
                }
            else:
                results[i] = {
                    "Final Material Code": extracted_code,
                    "Matched Description": "",
                    "Match Source":        "Contract-only (unvalidated)",
                    "Confidence Percentage": 90,
                }
            continue

        section_idxs = get_section_indices(section) or []
        keyword_idxs = get_keyword_indices(item_texts[i])
        pool_idxs    = list(set(section_idxs) | set(keyword_idxs))

        if pool_idxs:
            using_full = False
        else:
            using_full = True
            pool_idxs  = full_idxs

        if not item_texts[i]:
            results[i] = {"Final Material Code": "", "Matched Description": "",
                          "Match Source": "Empty item",
                          "Confidence Percentage": 0}
            continue

        best_idx, score = hybrid_score_item(
            item_texts[i], pool_idxs, item_embs[i], dict_emb, dict_descs
        )

        if score >= FUZZY_AUTO_ACCEPT_THRESHOLD and best_idx is not None:
            r = df_dict.iloc[best_idx]
            results[i] = {
                "Final Material Code": r["Material Code"],
                "Matched Description": r["Contract Description"],
                "Match Source":        "Hybrid (auto)" + (" - full" if using_full else ""),
                "Confidence Percentage": int(round(score * 100)),
            }
        else:
            ai_pending.append((i, pool_idxs, using_full, score, best_idx))

    auto_n = sum(1 for r in results if r and r["Match Source"].startswith("Hybrid"))
    ccode_n = sum(1 for r in results if r and r["Match Source"].startswith("Contract"))
    log(f"  Hybrid auto-accepted: {auto_n}")
    log(f"  Contract-validated:   {ccode_n}")
    log(f"  Pending AI rerank:    {len(ai_pending)}")

    # 2) AI rerank for the long tail (sub-threshold or empty pool).
    if ai_pending:
        def _run_ai_against(cand_idxs_, row_idx_):
            cand_df = df_dict.loc[cand_idxs_].head(DICT_CHUNK_SIZE)
            try:
                ai_results = ai_match_batch(
                    ai_client, [df.iloc[row_idx_].to_dict()], cand_df, log
                )
                if ai_results:
                    m = ai_results[0]
                    return {"material_code":       m.get("material_code") or "",
                            "matched_description": m.get("matched_description") or "",
                            "confidence":          m.get("confidence") or 0}
            except Exception as e:
                log(f"  ⚠ AI rerank failed for row {row_idx_}: {e}")
            return None

        def _ai_one(row_idx, cand_idxs, using_full, fallback_score, fallback_idx):
            primary = _run_ai_against(cand_idxs, row_idx)
            best  = primary
            label = "AI" + (" - full" if using_full else "")
            if not using_full and (not primary or primary.get("confidence", 0) < 70):
                full_top_idxs = list(np.argsort(
                    SEMANTIC_WEIGHT * (item_embs[row_idx] @ dict_emb.T)
                )[::-1][:DICT_CHUNK_SIZE])
                full_ai = _run_ai_against(full_top_idxs, row_idx)
                if full_ai and full_ai.get("confidence", 0) > (
                        primary.get("confidence", 0) if primary else 0):
                    best  = full_ai
                    label = "AI - full (fallback)"
            if best:
                return row_idx, {
                    "Final Material Code": best.get("material_code", ""),
                    "Matched Description": best.get("matched_description", ""),
                    "Match Source":        label,
                    "Confidence Percentage": best.get("confidence", 0),
                }
            if fallback_idx is not None:
                r = df_dict.iloc[fallback_idx]
                return row_idx, {
                    "Final Material Code": r["Material Code"],
                    "Matched Description": r["Contract Description"],
                    "Match Source":        "Hybrid (fallback)" + (" - full" if using_full else ""),
                    "Confidence Percentage": int(round(fallback_score * 100)),
                }
            return row_idx, {"Final Material Code": "", "Matched Description": "",
                             "Match Source": "No match",
                             "Confidence Percentage": 0}

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CALLS) as ex:
            futures = [ex.submit(_ai_one, *p) for p in ai_pending]
            done = 0
            for fut in as_completed(futures):
                row_idx, res = fut.result()
                results[row_idx] = res
                done += 1
                if done % 25 == 0:
                    log(f"    AI rerank progress: {done}/{len(ai_pending)}")

    df_match = pd.DataFrame(results)
    df_out   = pd.concat(
        [df.reset_index(drop=True), df_match.reset_index(drop=True)],
        axis=1,
    )
    df_out["Cleaned Price"] = df_out["Price"].apply(clean_price)
    df_out["Pricing_Type"]  = df_out["Price"].apply(detect_pricing_type)
    df_out["Tier_Range"]    = df_out["Item"].apply(extract_tier_range)
    if "Section_Norm_Score" in df_out.columns:
        df_out = df_out.drop(columns=["Section_Norm_Score"])
    # Canonical alias so downstream consumers (chatbot context_builder,
    # snowflake_invoice) that expect 'Material Code' keep working.
    df_out["Material Code"] = df_out["Final Material Code"]
    # Compat with the chatbot's 'Match Confidence' (0-1 scale).
    df_out["Match Confidence"] = (
        pd.to_numeric(df_out["Confidence Percentage"], errors="coerce")
        .fillna(0) / 100.0
    )
    return df_out


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 PUBLIC ENTRY
# ═══════════════════════════════════════════════════════════════════════════

def run_matching(client_name: str,
                 api_key: str = "",
                 matching_model: str = "",
                 dictionary_path: Optional[Path] = None,   # accepted for parity
                 progress_callback: Optional[Callable[[str], None]] = None,
                 contracts: Optional[list] = None,
                 core: str = "",
                 min_year: Optional[int] = None,
                 **_legacy_kwargs) -> dict:
    """DNA Phase 2 — match extracted items to the DNA dictionary.

    Writes ``Output/<Client>/material_match_output.xlsx`` plus a sidecar
    ``material_match_meta.json``. Returns the same dict shape as
    ``agents.extraction.run_matching``.

    Note: the ``dictionary_path`` parameter is accepted for API parity
    with PORTICO but DNA resolves its dictionary from
    ``Input/<core>/DNA Dictionary US 03-26.xlsx`` (or any *Dictionary*.xlsx
    in that folder) so the user can drop in a new dictionary release
    without touching code or config."""
    global _LOG_HOOK
    # Same thread-safe log wrapper as run() — Phase 2's AI rerank also
    # fans out via ThreadPoolExecutor and would otherwise raise
    # NoSessionContext when a worker logs through the Streamlit callback.
    _raw_log = progress_callback or (lambda m: None)
    _log_lock = threading.Lock()

    def log(msg: str) -> None:
        try:
            with _log_lock:
                _raw_log(msg)
        except Exception:
            try:
                print(msg)
            except Exception:
                pass

    _LOG_HOOK = log
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    try:
        api_key = api_key or EXTRACTION_API_KEY
        global MATCHING_MODEL
        if matching_model:
            MATCHING_MODEL = matching_model

        log(f"━━━ Material Match (DNA) {_VERSION} ━━━")
        log(f"Matching model: {MATCHING_MODEL}")

        ext_path = output_path(client_name)
        if not ext_path.exists():
            log(f"  ⚠ No extraction_output.xlsx for {client_name!r} — "
                "run the DNA Fee Description Agent first.")
            return {"status": "no_extraction", "client": client_name}

        try:
            try:
                items_df = pd.read_excel(str(ext_path), sheet_name="Dollar_Items")
            except Exception:
                items_df = pd.read_excel(str(ext_path))
        except Exception as e:
            log(f"  ⚠ Failed to read extraction output: {e}")
            return {"status": "bad_extraction", "client": client_name}

        if items_df.empty:
            log(f"  Extraction output is empty (0 items) for "
                f"{client_name!r} — nothing to match. Writing empty "
                f"matching output so the agent is marked complete.")
            _write_empty_matching(client_name, "no_extraction_items",
                                  min_year, log)
            return {"status": "no_items", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        dict_path, norm_path = _resolve_dna_inputs(
            core or "DNA", log, client_name=client_name
        )
        if not dict_path.exists():
            log(f"  ⚠ DNA dictionary not found at "
                f"{_INPUT_DIR / (core or 'DNA')}. Expected "
                "'DNA Dictionary Canada.xlsx' (for Canadian clients) or "
                "'DNA Dictionary US 03-26.xlsx' (for everyone else), or "
                "any *.xlsx with 'dict' in the name.")
            return {"status": "no_dictionary", "client": client_name}

        log(f"  Loading DNA dictionary: {dict_path.name}")
        df_dict = _load_dna_dictionary(dict_path, log)
        if df_dict is None:
            return {"status": "bad_dictionary", "client": client_name}

        log(f"  Loading DNA normalization: {norm_path.name}")
        df_norm = _load_dna_normalization(norm_path, log)

        ai_client = make_client(api_key)
        df_out = _run_matching_impl(items_df, df_dict, df_norm,
                                    ai_client, log)

        out_path = matching_output_path(client_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df_out.to_excel(str(out_path), index=False)

        _write_meta(_matching_meta_path(client_name), {
            "status":   "complete", "core": "DNA",
            "min_year": int(min_year),
            "ran_at":   datetime.now().isoformat(timespec="seconds"),
            "rows":     int(len(df_out)),
            "dictionary": dict_path.name,
            "normalization": norm_path.name,
        })
        log(f"\nWrote: {out_path.name} ({len(df_out)} rows)")
        return {"status": "complete", "client": client_name,
                "rows": int(len(df_out)), "output": str(out_path),
                "min_year": int(min_year)}
    finally:
        _LOG_HOOK = None


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC SURFACE — chatbot calls these from `_run_extr` / `_run_material_match`
# ═══════════════════════════════════════════════════════════════════════════

def output_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "extraction_output.xlsx"


def matching_output_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "material_match_output.xlsx"


def is_processed(client_name: str) -> bool:
    """Fee Description considered "done" whenever the extraction Excel
    exists for the client, regardless of row count. Empty Excels +
    sidecar JSON are written on early-exit paths so the Load button
    doesn't keep re-triggering."""
    return output_path(client_name).exists()


def matching_is_processed(client_name: str) -> bool:
    return matching_output_path(client_name).exists()
