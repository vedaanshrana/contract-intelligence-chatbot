"""
Fee Description + Material Code Matching Agent
==============================================

SINGLE-FILE VERBATIM PORT of `Existing Scripts/Contract Extraction.ipynb`,
the colleague's reference notebook that produces 301 items + 187 matches on
EDUCATIONAL FCU.

Everything from the original notebook — both Phase 1 (extraction) and
Phase 2 (matching) — lives in this one module, with all imports at the top
exactly as in the notebook. The frontend exposes two agents:

    Fee Description Agent         → calls run(client_name, ...)
                                    writes Output/<Client>/extraction_output.xlsx
    Material Code Matching Agent  → calls run_matching(client_name, ...)
                                    writes Output/<Client>/material_match_output.xlsx

`agents/material_match.py` is now a thin re-export of `run_matching` so the
two frontend buttons still appear as separate agents in the UI, but the
backend is one cohesive script — no logic duplication, no module-cache
games, no lazy-importing the sentence-transformer.

The only adaptations from the original notebook:
  - the OpenAI client is built via `fiserv_client.make_client(api_key)` so
    token usage is metered into `run_metrics`;
  - PDF discovery runs against the chatbot's per-client Input folder
    layout (`config.client_input_dir(client_name, core)`);
  - the `marked_checkbox_example.png` reference image is OPTIONAL — used if
    present in the project root, skipped (with a log line) otherwise.

Everything else — the prompts, the DPI, the chunk sizes, the fuzzy
threshold, the parallel-LLM fan-out, the candidate aggregation, the
ranking pass, the checkbox + non-zero-price filter, the output Excel
shape — is byte-for-byte the original.
"""

# ── All imports needed for both phases ──────────────────────────
# Top-level imports, matching the colleague's notebook cell exactly. If
# `sentence-transformers` or `scikit-learn` aren't installed, this module
# will fail to import — which is the correct behavior, because Phase 2
# REQUIRES them. The notebook's intent is "have these installed before you
# run anything", and we honor that here too.
import os
import json
import base64
import math
import time
import re
import numpy as np
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
fitz.TOOLS.mupdf_display_errors(False)  # suppress non-fatal MuPDF warnings
import pandas as pd
from PIL import Image
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

# Chatbot-specific (metered OpenAI client + config paths).
from config import (
    EXTRACTION_API_KEY,
    EXTRACTION_MODEL as _DEFAULT_EXTRACTION_MODEL,
    MATCHING_MODEL as _DEFAULT_MATCHING_MODEL,
    DICTIONARY_SHEET_CANDIDATES,
    OUTPUT_DIR as _OUTPUT_DIR,
    client_input_dir,
    default_dictionary_for,
)
from fiserv_client import make_client


# ═══════════════════════════════════════════════════════════════════
# CONFIG  (mirrors the colleague's notebook CONFIG cell)
# ═══════════════════════════════════════════════════════════════════
# Phase 1 — Extraction
EXTRACTION_MODEL = _DEFAULT_EXTRACTION_MODEL    # "gpt-5.2-2025-12-11"
CHUNK_SIZE       = 12                            # PDF pages per API call
DPI              = 600                           # rendering resolution

# PDF date filter — only contracts dated >= this year are processed. Anything
# older is treated as "legacy" and skipped at filter time. This used to be
# hardcoded to 2022 in `extract_date_from_filename`; it's now the single
# source of truth and is overridable via env var OR runtime kwarg (the UI
# passes a session-level value through `run(..., min_year=...)`). Override
# default with EXTRACTION_MIN_YEAR=2020 to widen the window.
MIN_YEAR_CUTOFF = int(os.environ.get("EXTRACTION_MIN_YEAR", "2022"))

# Phase 2 — Matching
MATCHING_MODEL              = _DEFAULT_MATCHING_MODEL    # "gpt-4.1-2025-04-14"
ITEM_BATCH_SIZE             = 25      # items per AI matching batch
DICT_CHUNK_SIZE             = 250     # dictionary entries per AI chunk
MAX_PARALLEL_CALLS          = 22      # parallel API calls during matching
FUZZY_AUTO_ACCEPT_THRESHOLD = 0.90    # similarity to auto-accept w/o AI
SEMANTIC_WEIGHT             = 0.6     # sentence-transformer weight
LEXICAL_WEIGHT              = 0.4     # TF-IDF weight
USE_CHECKPOINT              = False   # resume an interrupted matching run

# Banner printed at the start of every run() / run_matching() call so the
# user can SEE which code is executing in the log. If you don't see this,
# Streamlit is holding a stale copy of the module — restart it.
_VERSION = ("v7 — verbatim port of Harshit's Colleague_dollar_value_extraction.ipynb "
            "(includes quantity/frequency, Share-Draft tier rules, "
            "Included/Waived in scope, keep-label-on-strikethrough) "
            "+ chunk retry + detail=high + per-chunk + per-PDF diagnostics")


# ═══════════════════════════════════════════════════════════════════
# SHARED HELPERS  (used by both phases, verbatim from the notebook)
# ═══════════════════════════════════════════════════════════════════

def extract_date_from_filename(filename, min_year: int = None):
    """Parse MM-DD-YYYY date from filename. Returns None if missing or older
    than `min_year` (defaults to MIN_YEAR_CUTOFF, which itself defaults to
    2022 unless overridden via env var EXTRACTION_MIN_YEAR or by the UI's
    year-selector value being passed through ``run(..., min_year=...)``)."""
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    pattern = r'(\d{1,2})-(\d{1,2})-(\d{4})'
    match = re.search(pattern, filename)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        date_obj = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return None if date_obj.year < min_year else date_obj


def pdf_to_images(pdf_path, dpi=DPI):
    """Convert each page of a PDF to a PIL Image at the specified DPI."""
    doc = fitz.open(pdf_path)
    pages = []
    for page_index in range(len(doc)):
        page = doc.load_page(page_index)
        mat  = fitz.Matrix(dpi / 72, dpi / 72)
        pix  = page.get_pixmap(matrix=mat)
        img  = Image.open(BytesIO(pix.tobytes("png")))
        pages.append({"page_number": page_index + 1, "image": img})
    return pages


def image_to_base64(image):
    """Encode a PIL Image as a base64 PNG string."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def is_zero_price(val):
    """Return True if a price value is effectively zero or non-numeric (waived, free, etc.)."""
    if pd.isna(val):
        return False
    s = str(val).strip()
    if s.lower() in ("no charge", "waived", "n/a", "none", "free"):
        return True
    if re.match(r"^over\s*\$", s, re.IGNORECASE):
        return True
    if re.match(r"^\$?\d+(\.\d+)?\s*[MBmb]$", s.strip()):
        return True
    try:
        cleaned = s.replace("$", "").replace(",", "").strip()
        m = re.search(r'-?\d+\.?\d*', cleaned)
        return float(m.group()) == 0 if m else False
    except Exception:
        return False


def clean_price(val):
    """Normalise a price string to $X,XXX.XX format."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.lower() in ("no charge",) or re.match(r"^over\s*\$", s, re.IGNORECASE):
        return ""
    if re.match(r"^\$?\d+(\.\d+)?\s*[MBmb]$", s.strip()):
        return ""
    negative = bool(re.match(r"^\(", s.replace("$", "").strip()))
    cleaned  = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    m = re.search(r"\d+\.?\d*", cleaned)
    if not m:
        return ""
    value = float(m.group())
    return f"-${value:,.2f}" if negative else f"${value:,.2f}"


def call_api_with_retry(fn, max_retries=5):
    """Call an API function with exponential backoff on rate-limit errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            if "429" in str(e) or "rate limit" in err or "too many requests" in err:
                wait = 2 ** attempt
                # Use the logger if provided via thread-local, else stdout.
                msg = f"  Rate limit hit (attempt {attempt+1}/{max_retries}), retrying in {wait}s..."
                _LOG_HOOK(msg) if _LOG_HOOK else print(msg)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"API call failed after {max_retries} retries")


# Module-level log hook — set inside run()/run_matching() so call_api_with_retry
# (and other deep helpers) can route messages to the Streamlit progress bar
# instead of stdout. Reset to None when the call returns.
_LOG_HOOK: Optional[Callable[[str], None]] = None


# Aliases kept for back-compat with anything in the chatbot that may have
# imported the old private names (`_is_zero_price`, `_clean_price`).
_is_zero_price = is_zero_price
_clean_price   = clean_price


# Legacy alias — older context_builder code may have called `_classify_fee`.
# The verbatim port no longer needs it (the schema is dollar-only), but we
# keep a stub for safety.
def _classify_fee(val) -> str:
    if val is None: return ""
    s = str(val).lower().strip()
    if not s or s == "nan": return ""
    if "included" in s and "in fee" not in s: return "included"
    if "included in fee" in s: return "included"
    if "prev paid" in s or "previously paid" in s: return "prev_paid"
    if "waived" in s: return "waived"
    if "by quote" in s: return "by_quote"
    if "no charge" in s: return "no_charge"
    if "tbd" in s: return "tbd"
    if re.search(r"\$|\d", s): return "dollar"
    return "other"


# ═══════════════════════════════════════════════════════════════════
# PHASE 1 — EXTRACTION  (verbatim from the colleague's notebook)
# ═══════════════════════════════════════════════════════════════════

EXTRACTION_SYSTEM_PROMPT = """
You are a forensic-level contract analysis expert.

Your task is to extract ALL items that have an associated dollar-denominated value, including items priced as $0, Included, Waived, No Charge, Free, N/A, or Complimentary.

CRITICAL INSTRUCTIONS FOR CHECKBOX DETECTION:

1. CHECKBOX PLACEMENT: Checkboxes can appear BEFORE or AFTER the fee description. Look carefully at the LEFT side of each line item for empty or filled boxes.

2. CHECKBOX TYPES - Learn to recognize these:
   - CHECKED (fee IS selected): ☑, ☒, ✓, ✗, [X], [x], filled box, box with any mark inside
   - UNCHECKED (fee NOT selected): ☐, □, [ ], empty box, empty square, unfilled checkbox

3. IMPORTANT: An EMPTY BOX (☐ or □) placed before a fee description means the checkbox is UNCHECKED - the fee was NOT selected by the client.

4. HIERARCHICAL STRUCTURE: Some fees have parent-child relationships:
   - Parent item: "☐ VB Website Design and Hosting"
   - Child items indented below: "☐ Up to 5 page site", "☐ 6-10 page site"
   - If the PARENT checkbox is unchecked, all child items under it are also NOT selected.

---------------------------------------------------
ADVANCED CHECKBOX ASSOCIATION LOGIC
---------------------------------------------------

CHECKBOX LOOK-BACK RULE:
For each item with a dollar value:
- If no checkbox appears directly on the same line,
  scan UPWARD visually within the same visual block or section.
- The most recent checkbox appearing immediately before the item
  (within a few lines above) should be considered associated with that item
  UNLESS it clearly belongs to a different section, indentation level, or heading.

VISUAL BLOCK DETECTION:
- Treat items that are vertically grouped together with consistent indentation
  as belonging to the same structural block.
- If a checkbox appears at the start of a block,
  all subsequent indented items under that block inherit that checkbox
  unless they contain their own explicit checkbox.

INDENTATION / HIERARCHY RULES:
- Parent items often:
    • Start at the left margin
    • Contain a checkbox
- Child items often:
    • Are indented to the right
    • Appear directly below a parent
    • May not contain their own checkbox

If a child item does NOT have its own checkbox:
    → It inherits the parent's checkbox state.

If a parent checkbox is EMPTY:
    → All child items are checkbox_checked: false
      unless a child explicitly has its own marked checkbox.

If a parent checkbox is MARKED:
    → Child items are considered true ONLY if visually marked,
      otherwise they inherit the parent's state.

SECTION BOUNDARY RULE:
Do NOT associate a checkbox with an item if:
- A clear section header appears between them
- A large visual spacing break occurs
- A numbering change indicates a new clause

RECENCY PRIORITY RULE:
When multiple checkboxes appear above an item,
associate the item with the closest preceding checkbox
within the same indentation or visual block.

IMPORTANT:
You MUST actively search upward for checkbox association
before concluding checkbox_checked: null.

Failure to associate a visible checkbox in the same block is an error.

---------------------------------------------------
TABLE-LEVEL CHECKBOX ASSOCIATION
---------------------------------------------------

TABLE CONTROL RULE:

If a checkbox appears immediately above a table,
and no intervening section header or clause break exists,
that checkbox is considered the controlling checkbox
for the ENTIRE table.

If such a checkbox exists:
- ALL line items (rows) inside that table inherit the checkbox state
  UNLESS a specific row contains its own explicit checkbox.

ROW-LEVEL OVERRIDE RULE:
If an individual table row contains its own checkbox:
    → That row's checkbox overrides the table-level checkbox.
If no row-level checkbox exists:
    → Use the table-level checkbox state.

MULTI-PAGE TABLE RULE:
If a table spans multiple pages:
    → The checkbox located above the first page of the table
       applies to all continuation pages
       unless a new checkbox explicitly appears above a continued portion.

---------------------------------------------------
CHECKBOX + LABEL + TABLE STRUCTURE RULE
---------------------------------------------------

If a checkbox appears next to a label or option name,
and a table appears directly below that label,
then that checkbox governs the entire table.

A horizontal line, divider line, or table border does NOT break checkbox association.

Only ignore the checkbox if:
- A completely new numbered section begins
- A new major heading appears
- A new clause identifier appears
- A large vertical spacing break clearly separates sections

---------------------------------------------------
NUMBERED SECTION CHECKBOX INHERITANCE RULE
---------------------------------------------------

If a checkbox appears next to a numbered section heading,
then that checkbox governs ALL subordinate content under that section
unless explicitly overridden.

Subordinate content includes:
- Lettered sub-clauses: (a), (b), (c)
- Roman numerals: (i), (ii), (iii)
- Indented fee lines
- Pricing lines that appear below the section header
- Tables that appear under the section

---------------------------------------------------
SECTION HEADER IDENTIFICATION AND INHERITANCE
---------------------------------------------------

You must determine the SECTION HEADER corresponding to each extracted item.

A Section Header is a high-level title that describes the contract section.
These headers typically appear at the TOP of a page, CENTERED, in larger or bold text.

SECTION HEADER ASSOCIATION PROCESS:
STEP 1 — Scan UPWARD from the item to find the nearest section title.
STEP 2 — The FIRST qualifying section header encountered is the correct one.
STEP 3 — If the current page has no header above the item, inherit from the previous page.
STEP 4 — A section header governs all content beneath it until a new header appears.
STEP 5 — All rows in a table under a section header inherit the same header.

Always return the header text EXACTLY as written. Do NOT summarize or rewrite it.

MULTI-LINE HEADER RULE:
Some section headers span multiple lines or consist of a title
and a subtitle directly below it.
If a title line is immediately followed by another descriptive line
with no other content between them, treat the COMBINED text as
the full section header.
Example:
"Fee Exhibit"
"to CheckFree Bill Payment and Delivery Services Schedule"
→ Full header = "Fee Exhibit to CheckFree Bill Payment and Delivery Services Schedule"

Do NOT stop at the first line if a continuation line immediately follows.
Combine all consecutive header lines into one complete section header string.

---------------------------------------------------
VISUAL CHECKBOX REFERENCE
---------------------------------------------------

MARKED checkbox: ☒ ☑ [X] — any square containing crossing diagonal lines or a checkmark.
EMPTY checkbox: ☐ □ [ ] — square outline only, nothing inside.

If a square contains two diagonal crossing lines, it is ALWAYS classified as MARKED.

---------------------------------------------------
STRIKETHROUGH HANDLING RULES
---------------------------------------------------

RULE 1 — IGNORE ALL STRIKETHROUGH VALUES:
If a dollar value appears with a visible strikethrough (the number is crossed out),
you MUST ignore that value entirely.
Do NOT extract it, do NOT include it in the price field, and do NOT reference it in the item description.

RULE 2 — CAPTURE REPLACEMENT PRICE ONLY:
If a strikethrough price is followed by a new non-struck price on the same line,
capture ONLY the new replacement price.
Example: $250 $200 → extract $200 only.

RULE 3 — STRIKETHROUGH + NON-BILLABLE LABEL:
If a dollar value has a strikethrough AND the text next to it is any non-billable label
(including but not limited to: "Included", "Waived", "N/A", "Complimentary", "No Charge", "Free"),
extract the item with the non-billable label as the price (e.g. price = "Included", price = "Waived").

RULE 4 — PARTIAL STRIKETHROUGH:
If only part of a price string is struck through, treat the entire original value as struck
and apply the same rules above.

---------------------------------------------------
EXCLUSION RULES
---------------------------------------------------

RULE — EXCLUDE TOTALS AND SUBTOTALS:
Do NOT extract rows that are labeled as totals, subtotals, or summaries.
This includes lines explicitly prefixed or suffixed with words such as:
"Total", "Subtotal", "Grand Total", "Total One Time Fees", "Total Monthly Fees", etc.
These are aggregations of other line items, not standalone fee items.

RULE — EXCLUDE NON-FEE MONETARY FIGURES:
Do NOT extract dollar values that describe a client's asset size, account balance,
financial metric, or threshold trigger — even if they contain a "$" symbol.
These are contextual figures, not fees or charges.
Examples of values to EXCLUDE:
"total assets up to $73M"
"current assets confirmed to be up to $73M"
"assets over $500M"
A valid fee item must represent a charge, cost, or billable amount payable under the contract.

RULE — EXCLUDE CONDITIONAL THRESHOLD/TRIGGER VALUES:
Do NOT extract dollar values that appear as a condition trigger or threshold label
rather than a fee amount.
Specifically, if a table column is labeled "Asset Size", "Threshold", "Tier", "Over X", or similar,
and the value in that column is a dollar or asset amount describing WHEN a fee activates
(e.g., "Over $100M"), do NOT extract that value.
Only extract the corresponding fee amount from the "Fee" or "Additional Monthly Service Fees"
column in the same row.

RULE — EXCLUDE THRESHOLD AND CONDITION REFERENCES IN PROSE:
Do NOT extract dollar values that appear inside descriptive or legal paragraph text as conditions,
eligibility thresholds, or triggers — even if they contain "$" or "USD".
These are not fees; they describe circumstances under which something applies.
Indicators that a value is a threshold/condition reference include:
"more than $X", "greater than $X", "exceeds $X", "over $X", "above $X"
"holds more than $X in assets", "threshold of $X", "asset size exceeds $X"
The sentence describes WHO qualifies or WHEN something applies, not WHAT is charged.
A valid fee item must be a charge, cost, or billable amount — not a qualifying condition.

Definition:
An item is any service, fee, charge, product, obligation, or clause
that explicitly has a "$" or "USD" amount written.

STEP 1 — Identify EVERY dollar-denominated value. Extract ALL occurrences of "$" or "USD".
STEP 2 — For EACH dollar value, determine the specific item it relates to.
         If the same item has multiple dollar values, extract each as a separate row.

---------------------------------------------------
TABLE COLUMN PRIORITY RULES
---------------------------------------------------

RULE — QTY + UNIT PRICE + AMOUNT COLUMNS:
When a table contains all three of these columns: QTY (or Quantity), UNIT PRICE, and AMOUNT:
- Extract AMOUNT as the price field
- Extract QTY value as the quantity field
- Extract the UNITS value (e.g., "Per Appliance", "Per Hour", "Per Certificate") as the frequency field
- IGNORE the UNIT PRICE column entirely
- Do NOT create separate rows for unit price and amount — extract ONE row per line item only

RULE — AMOUNT IS TBD OR MISSING:
If the AMOUNT column exists but contains "TBD", is blank, or is otherwise not a dollar value:
- Fall back to UNIT PRICE as the price field
- Still extract QTY as quantity and UNITS as frequency
- Still extract ONE row per line item only

RULE — NO AMOUNT COLUMN:
If the table does NOT have an AMOUNT column and only has a single price column
(labeled "Fee", "Price", "Unit Price", "Rate", etc.):
- Extract that column's value as the price field
- Apply normal quantity and frequency extraction rules



---------------------------------------------------
SHARE DRAFT / DDA ACCOUNT TIER TABLE RULES
---------------------------------------------------

These rules apply ONLY to tables where the row labels represent ranges of DDA or Share Draft accounts
(e.g. "Up to 500", "501 to 1,000", "1,001 to 2,500", "Up to 1,000 DDAs").

RULE 1 - CHECKED ROW ONLY:
If one or more checkboxes are marked in the table, extract ONLY the checked row(s).
Do NOT extract unchecked rows.

RULE 2 - APPEND TIER TO DESCRIPTION:
Append the tier range in parentheses to the fee description.
Example: "CheckFree Hosted UI Monthly Fee (501 to 1,000 Share Draft accounts)"
Example: "Zelle Person to Person Monthly Fee (Up to 1,000 DDAs / Share Draft accounts)"

RULE 3 - PREFIX WITH PRODUCT NAME:
Prefix the description with the product or service name from the nearest section header above the table.
Example: section header "CheckFree Hosted UI Recurring Fees" + row "Monthly Fee" + tier "501 to 1,000"
-> "CheckFree Hosted UI Monthly Fee (501 to 1,000 Share Draft accounts)"

RULE 4 - NO TIER AS STANDALONE DESCRIPTION:
Do NOT extract the tier range alone (e.g. "501 to 1,000" or "Share Draft accounts") as the fee description.

RULE 5 - NO CHECKBOX IN TABLE:
If the table has no checkboxes, extract ALL rows with their tier ranges appended per RULE 2 and RULE 3.


Return STRICT JSON:

{
  "items": [
    {
      "item": "<clear description>",
      "checkbox_checked": true or false,
      "price": "<dollar value exactly as written>",
      "quantity": "<QTY value if present, else null>",
      "frequency": "<UNITS value if present e.g. Per Account, Per Month, else null>",
      "page": <page number>,
      "explanation": "brief explanation",
      "section_header": "<section title or null>"
    }
  ]
}

IMPORTANT RULES:
- EMPTY box before the fee → checkbox_checked: false
- MARKED box before the fee → checkbox_checked: true
- No checkbox at all → checkbox_checked: null
- Use ONLY visible text in the images. Do NOT infer missing values.
- Ignore non-dollar currencies, dates, and section numbers.
- Be exhaustive. Missing even one dollar value is an error.

Do not include markdown. Do not include explanations.
""".strip()


# Locations searched for the marked-checkbox reference image. The original
# notebook required `marked_checkbox_example.png` next to the script; we
# make it optional and look in the chatbot project root first.
_REF_IMAGE_PATHS = [
    Path(__file__).resolve().parent.parent / "marked_checkbox_example.png",
    Path(__file__).resolve().parent / "marked_checkbox_example.png",
    Path.cwd() / "marked_checkbox_example.png",
]


def _load_checkbox_reference_b64(log) -> Optional[str]:
    """Return the marked-checkbox reference image as base64 PNG, or None if
    the file isn't found. The notebook required this; we tolerate its
    absence since the prompt's textual rules describe the visual cues."""
    for p in _REF_IMAGE_PATHS:
        if p.exists():
            try:
                return base64.b64encode(p.read_bytes()).decode("utf-8")
            except Exception as e:
                log(f"  (could not read checkbox reference {p}: {e})")
    log("  (marked_checkbox_example.png not found — proceeding without "
        "visual reference; the prompt's textual checkbox rules still apply)")
    return None


def _extract_response_json(resp, chunk_index, _log):
    """Robust JSON extraction from an OpenAI Responses API result.

    Same logic + comments as the DNA module's helper — fixes the silent
    failure mode where ``resp.output_text`` is empty on gpt-5.x reasoning
    runs but the JSON is actually present in ``resp.output[*].content``,
    or where the model wraps its JSON in prose / fences. Returns the
    parsed dict or ``None`` (caller's retry loop decides what to do)."""
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
            if t != "message":
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
        s = raw
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"```$", "", s).strip()
        try:
            parsed = json.loads(s)
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

    preview = candidates[0][:500] if candidates else "(empty)"
    _log(f"  ⚠ chunk {chunk_index}: could not extract JSON from response. "
         f"output_text={'empty' if not raw_text.strip() else 'non-empty'}, "
         f"output item types={item_types}, candidates collected="
         f"{len(candidates)}")
    _log(f"      raw preview: {preview!r}")
    return None


def extract_chunk(client, page_chunk, chunk_index, ref_b64=None, log=None):
    """Send one chunk of PDF pages to the extraction model and return parsed items.

    Hardened vs. the bare notebook implementation:
      - explicit image `detail: "high"` on every page image so contracts'
        small text + checkboxes remain legible (the OpenAI default is "auto"
        which usually picks high; the Fiserv backend used to hard-code "low"
        which was a major source of silent misses — that's now fixed too);
      - up to 3 attempts on transient failures (HTTP / network / parse) with
        exponential backoff, so one bad call doesn't lose a whole chunk of
        ~50 items;
      - explicit `max_output_tokens=32768` to make sure long JSON arrays
        aren't silently truncated AND to leave room for the gpt-5.x
        family's reasoning tokens that count toward the same budget;
      - robust JSON extraction via `_extract_response_json` — handles the
        silent failure mode where the response text is in
        ``resp.output[*].content`` rather than ``output_text``;
      - structured per-attempt logging so the user can see exactly which
        chunk / attempt failed and why.
    """
    _log = log or print
    input_content = []

    if ref_b64:
        input_content.append({
            "type": "input_text",
            "text": "The first image is a REFERENCE EXAMPLE of a MARKED checkbox. "
                    "Any square containing visible crossing diagonal lines is ALWAYS classified as MARKED."
        })
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{ref_b64}",
            "detail": "high",
        })
        input_content.append({
            "type": "input_text",
            "text": "The following images are sequential pages from a contract. "
                    "Use the checkbox reference example above to correctly classify checkboxes."
        })
    else:
        input_content.append({
            "type": "input_text",
            "text": "The following images are sequential pages from a contract."
        })

    for page in page_chunk:
        image_b64 = image_to_base64(page["image"])
        input_content.append({
            "type": "input_image",
            "image_url": f"data:image/png;base64,{image_b64}",
            "detail": "high",     # critical for checkbox + small-text legibility
        })

    max_attempts = 3
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.responses.create(
                model=EXTRACTION_MODEL,
                temperature=0,
                max_output_tokens=32768,
                input=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user",   "content": input_content}
                ]
            )

            parsed = _extract_response_json(response, chunk_index, _log)
            if parsed is not None:
                return parsed
            # Detailed failure context was already logged inside the helper.
            last_err = "parse_failed"
        except Exception as e:
            _log(f"  ⚠ chunk {chunk_index} attempt {attempt}: API call failed: "
                 f"{type(e).__name__}: {str(e)[:200]}")
            last_err = f"{type(e).__name__}: {e}"

        if attempt < max_attempts:
            wait = 2 ** attempt
            _log(f"    retrying in {wait}s…")
            time.sleep(wait)

    _log(f"  ⚠ chunk {chunk_index} EXHAUSTED {max_attempts} attempts "
         f"(last error: {last_err}) — DROPPING this chunk's items.")
    return {"items": []}


def extract_items_with_dollar_values(pdf_path, ai_client, ref_b64=None, log=None):
    """Convert a PDF to images and extract all dollar-valued items.

    Per-chunk logging now records exactly how many items came back; if a
    chunk silently fails the user sees `0 items` next to that chunk number
    and a banner with the error from extract_chunk's retry loop. End of
    function logs a "PDF summary: N pages → M items across K chunks (F
    failed)" line so any silent regressions show up immediately."""
    _log = log or print
    _log(f"  Converting {os.path.basename(pdf_path)} to images @ DPI {DPI}...")
    pages = pdf_to_images(pdf_path)
    total_pages  = len(pages)
    total_chunks = math.ceil(total_pages / CHUNK_SIZE)
    _log(f"  Total pages: {total_pages} | Chunks: {total_chunks}")

    all_items = []
    failed_chunks = 0
    for chunk_index in range(total_chunks):
        start      = chunk_index * CHUNK_SIZE
        page_chunk = pages[start:start + CHUNK_SIZE]
        _log(f"    chunk {chunk_index + 1}/{total_chunks} (pages "
             f"{start + 1}-{start + len(page_chunk)})…")
        try:
            parsed = extract_chunk(ai_client, page_chunk, chunk_index + 1,
                                   ref_b64=ref_b64, log=_log)
        except Exception as e:
            _log(f"    ⚠ chunk {chunk_index + 1} threw uncaught exception: "
                 f"{type(e).__name__}: {str(e)[:200]}")
            failed_chunks += 1
            continue
        chunk_items = parsed.get("items", []) or []
        if not chunk_items:
            _log(f"      chunk {chunk_index + 1}: 0 items "
                 f"(likely a silent failure — see warnings above)")
            failed_chunks += 1
        else:
            _log(f"      chunk {chunk_index + 1}: {len(chunk_items)} items")
        for item in chunk_items:
            all_items.append({
                "Item":            item.get("item"),
                "Price":           item.get("price"),
                "Quantity":        item.get("quantity"),
                "Frequency":       item.get("frequency"),
                "Page":            item.get("page"),
                "Checkbox_Checked": item.get("checkbox_checked"),
                "Explanation":     item.get("explanation"),
                "Section_Header":  item.get("section_header"),
            })
    _log(f"  ── PDF summary: {total_pages} pages → {len(all_items)} items "
         f"across {total_chunks} chunks ({failed_chunks} failed/empty)")
    return pd.DataFrame(all_items)


def _filter_pdfs(folder_path: Path, log, min_year: int = None) -> list:
    """Apply the notebook's PDF triage (year filter, suffix dedup,
    large-file-per-date dedup). Returns [(filename, date_obj), ...] sorted
    descending by date.

    Now logs EVERY PDF in the folder + the reason for each drop, so
    investigation of "why is contract X missing from extraction?" is a
    matter of reading the run log, not bisecting code.

    `min_year` controls the year filter — anything older is dropped as
    legacy. Defaults to MIN_YEAR_CUTOFF (env-configurable, default 2022)."""
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF

    if not folder_path.exists():
        log(f"  ⚠ Folder does not exist: {folder_path}")
        return []

    def _year_ok(f):
        return extract_date_from_filename(f, min_year=min_year) is not None

    def _extract_date_str(f):
        m = re.search(r'\d{1,2}-\d{1,2}-\d{4}', f)
        return m.group(0) if m else f

    def _page_count(f):
        try:
            doc = fitz.open(str(folder_path / f))
            n   = len(doc)
            doc.close()
            return n
        except Exception as e:
            log(f"  ⚠ Could not open {f} for page count: {e} — treating as 0 pages")
            return 0

    def _is_suffix_duplicate(f, all_files_set):
        stem = f[:-4]
        m    = re.search(r'^(.+)_([^_]+)$', stem)
        if not m:
            return False
        return (m.group(1) + '.pdf') in all_files_set

    log(f"  Scanning {folder_path}")
    all_pdfs_in_folder = sorted(
        [f for f in os.listdir(folder_path) if f.lower().endswith(".pdf")]
    )
    log(f"  Found {len(all_pdfs_in_folder)} PDF(s) total in folder.")

    # Step 1: year filter
    all_pdfs_raw, year_dropped = [], []
    for f in all_pdfs_in_folder:
        (all_pdfs_raw if _year_ok(f) else year_dropped).append(f)
    if year_dropped:
        log(f"  Year filter dropped {len(year_dropped)} pre-2022 PDF(s):")
        for f in year_dropped:
            log(f"    - {f}")
    if not all_pdfs_raw:
        log("  No 2022+ PDFs found.")
        return []

    # Step 2: suffix dedup
    all_files_set  = set(all_pdfs_raw)
    suffix_dropped = [f for f in all_pdfs_raw if _is_suffix_duplicate(f, all_files_set)]
    if suffix_dropped:
        log(f"  Suffix duplicates dropped: {len(suffix_dropped)}")
        for f in suffix_dropped:
            log(f"    - {f}")
    all_pdfs = [f for f in all_pdfs_raw if f not in suffix_dropped]

    # Step 3: small / large split
    small_files, large_files = [], []
    for f in all_pdfs:
        size = os.path.getsize(str(folder_path / f))
        (large_files if size > 1 * 1024 * 1024 else small_files).append(f)
    log(f"  {len(small_files)} small file(s), {len(large_files)} large file(s).")

    # Step 4: dedup large files per date
    date_groups: dict = {}
    for f in large_files:
        date_groups.setdefault(_extract_date_str(f), []).append(f)
    selected_large = [max(group, key=_page_count) for group in date_groups.values()]
    large_dropped = [f for f in large_files if f not in selected_large]
    if large_dropped:
        log(f"  Large-file dedup dropped {len(large_dropped)} (same-date, "
            f"fewer pages):")
        for f in large_dropped:
            log(f"    - {f}")

    # Step 5: combine + sort by date desc
    filtered_files = sorted(small_files + selected_large)
    pairs = [(f, extract_date_from_filename(f, min_year=min_year))
             for f in filtered_files]
    pairs.sort(key=lambda x: x[1], reverse=True)
    log(f"  → {len(pairs)} PDF(s) will be processed:")
    for f, d in pairs:
        log(f"    • {f}  ({d.strftime('%Y-%m-%d')})")
    return pairs


def export_to_excel(df, output_path, log=None):
    """Save the extraction DataFrame to an Excel file. Notebook uses
    sheet_name='Dollar_Items' which Phase 2 reads."""
    _log = log or print
    if df.empty:
        _log("  No dollar values found — skipping export.")
        return
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Dollar_Items")
    _log(f"  Extraction saved: {output_path}")


# ── Empty-output writers + sidecar metadata ──────────────────────────────────
# Reason: the chatbot's Load button gates each agent on `is_processed`. If
# extraction returns "no_pdfs" (e.g. a client has only pre-2022 contracts)
# without writing anything, `is_processed` stays False forever and every
# Load click re-runs extraction → re-fails. By writing an EMPTY Excel + a
# sidecar JSON describing the run, we make the agent self-documenting:
#
#     extraction_output.xlsx       (0 rows, real header)
#     extraction_meta.json         {status, min_year, ran_at, …}
#
# Downstream consumers (chatbot UI + the matching agent) read the meta to
# show the user WHY the file is empty and how to fix it (change min_year,
# add newer PDFs, etc.).

# Columns to write into an empty extraction_output.xlsx so the matcher can
# still open it without column-missing errors.
_EXTRACTION_COLUMNS = [
    "Item", "Price", "Quantity", "Frequency", "Page",
    "Checkbox_Checked", "Explanation", "Section_Header",
    "Source Contract", "Date",
]
# Columns for an empty material_match_output.xlsx so downstream code doesn't
# trip on missing Material Code / Matched Description / etc.
_MATCHING_COLUMNS = [
    "Item", "Price", "Cleaned Price", "Quantity", "Frequency", "Page",
    "Checkbox_Checked", "Explanation", "Section_Header",
    "Source Contract", "Date",
    "Matched Description", "Confidence Percentage", "Material Code",
    "Match Confidence",
]


def _extraction_meta_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "extraction_meta.json"


def _matching_meta_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "material_match_meta.json"


def read_extraction_meta(client_name: str) -> dict:
    """Read the sidecar describing the most recent Fee Description run.
    Returns ``{}`` if the file isn't present or unreadable (e.g. a legacy
    output from before this feature)."""
    p = _extraction_meta_path(client_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_matching_meta(client_name: str) -> dict:
    """Read the sidecar describing the most recent Material Matching run."""
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


def _write_empty_extraction(client_name: str, status: str, min_year: int,
                            folder: Path, log) -> None:
    """Write an empty extraction_output.xlsx + a meta sidecar so the agent
    is treated as 'ran' (is_processed → True) and the UI can explain why
    there are 0 items."""
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
    _write_meta(_extraction_meta_path(client_name), {
        "status":    status,                          # "no_pdfs" | "no_items"
        "min_year":  int(min_year),
        "folder":    str(folder),
        "ran_at":    datetime.now().isoformat(timespec="seconds"),
        "rows":      0,
        "note": (
            f"Fee Description ran but produced 0 items. Reason: {status}. "
            f"Year cutoff was {min_year} — contracts dated before {min_year} "
            "were skipped. To include older contracts, change the year "
            "filter in the chatbot UI (above the Agent Outputs section) "
            "and re-run the agent."
        ),
    })
    log(f"  Wrote empty {out_path.name} + sidecar meta ({status}, "
        f"min_year={min_year})")


def _write_empty_matching(client_name: str, status: str, min_year: int,
                          log) -> None:
    """Write an empty material_match_output.xlsx + meta sidecar so the
    agent is treated as 'ran' even when there were no items to match."""
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
        "status":   status,                            # "no_extraction_items"
        "min_year": int(min_year),
        "ran_at":   datetime.now().isoformat(timespec="seconds"),
        "rows":     0,
        "note": (
            "Material Code Matching ran but had nothing to match because "
            "Fee Description produced 0 items. This usually means all "
            f"contracts for this client are dated before {min_year} and "
            "were skipped. Change the year filter in the chatbot UI and "
            "re-run Fee Description (then re-run Material Matching)."
        ),
    })
    log(f"  Wrote empty {out_path.name} + sidecar meta ({status}, "
        f"min_year={min_year})")


# ═══════════════════════════════════════════════════════════════════
# PHASE 2 — MATCHING  (verbatim from the colleague's notebook)
# ═══════════════════════════════════════════════════════════════════

MATCHING_SYSTEM_PROMPT = (
    "You are an expert semantic matching engine.\n"
    "Match each Item to the closest description from the provided dictionary.\n"
    "Rules:\n"
    "- Match on MEANING only. Choose the most specific match.\n"
    "- Only return descriptions VERBATIM from the dictionary.\n"
    "- Monthly fees and one-time/setup fees are NOT interchangeable.\n"
    "- Product specificity beats generic category matches.\n"
    "- VB = Virtual Branch, VBN = Virtual Branch Next (treat as identical).\n"
    "- Return the item EXACTLY as given, character-for-character.\n"
    "- Return STRICT JSON: {\"matches\":[{\"item\":\"...\",\"matched_description\":\"...\",\"confidence_percentage\":95}]}\n"
    "- No markdown."
)


def match_batch(ai_client, items_batch, dict_chunk, log=None):
    """Send a batch of items + dictionary chunk to the matching model. Verbatim."""
    _log = log or print
    prompt = (
        "Items (copy exactly):\n" + json.dumps(items_batch, indent=2) +
        "\n\nDictionary:\n" + json.dumps(dict_chunk, indent=2) +
        "\n\nFor each item pick the BEST matching description. Return JSON only."
    )
    try:
        response = call_api_with_retry(lambda: ai_client.responses.create(
            model=MATCHING_MODEL, temperature=0,
            input=[
                {"role": "system", "content": MATCHING_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ]
        ))
        raw = (response.output_text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"```$", "", raw).strip()
        return json.loads(raw).get("matches", []) or []
    except Exception as e:
        _log(f"  WARNING: match_batch failed: {e}")
        return []


# Dictionary loader — the original notebook hard-codes `sheet_name="Final"`;
# the chatbot's Portico dictionary uses "Combined Dictionary" so we honor
# both via config.DICTIONARY_SHEET_CANDIDATES.
def _load_dictionary(dictionary_path: Path, log) -> Optional[pd.DataFrame]:
    try:
        sheets = pd.ExcelFile(str(dictionary_path)).sheet_names
    except Exception as e:
        log(f"  ⚠ Could not open dictionary {dictionary_path.name}: {e}")
        return None
    for sheet in DICTIONARY_SHEET_CANDIDATES:
        if sheet in sheets:
            cand = pd.read_excel(str(dictionary_path), sheet_name=sheet)
            if "Description" in cand.columns and "Material Code" in cand.columns:
                log(f"  Using sheet '{sheet}' ({len(cand)} rows)")
                return cand
    for sheet in sheets:
        cand = pd.read_excel(str(dictionary_path), sheet_name=sheet)
        if "Description" in cand.columns and "Material Code" in cand.columns:
            log(f"  Using auto-detected sheet '{sheet}' ({len(cand)} rows)")
            return cand
    log(f"  ⚠ No sheet in {dictionary_path.name} has both 'Description' and 'Material Code' columns")
    return None


# Module-level sentence-transformer cache so successive run_matching() calls
# don't re-load the 420 MB model. The notebook loads it once at the start of
# Phase 2 and re-uses across clients; we do the same here.
_ST_MODEL: Optional[SentenceTransformer] = None


def _get_st_model(log):
    global _ST_MODEL
    if _ST_MODEL is None:
        log("  Loading sentence transformer model 'all-mpnet-base-v2'...")
        _ST_MODEL = SentenceTransformer("all-mpnet-base-v2")
        log("  Model loaded.")
    return _ST_MODEL


def process_client(input_file: Path, dictionary_df: pd.DataFrame,
                   descriptions: list, lookup_dict: dict,
                   output_file: Path, ai_client, st_model, log) -> int:
    """Run the full matching pipeline for one client and save matched Excel.
    Verbatim from the notebook, with these adaptations:
      - input/output paths are passed in (chatbot's per-client folder layout)
      - the OpenAI client + sentence-transformer model are passed in (chatbot
        constructs them once via fiserv_client / our cache)
    """
    log(f"  Loading: {os.path.basename(str(input_file))}")
    # Honor the notebook's sheet name; fall back to default if not present.
    try:
        items_df = pd.read_excel(str(input_file), sheet_name="Dollar_Items")
    except Exception:
        items_df = pd.read_excel(str(input_file))

    # Filter: keep rows where checkbox is null or checked, and price is non-zero
    if "Checkbox_Checked" in items_df.columns:
        checkbox_mask = items_df["Checkbox_Checked"].isna() | (items_df["Checkbox_Checked"] == 1)
    else:
        checkbox_mask = pd.Series(True, index=items_df.index)
    nonzero_mask  = ~items_df["Price"].apply(is_zero_price)
    filtered_df   = items_df[checkbox_mask & nonzero_mask]
    all_items     = filtered_df["Item"].dropna().astype(str).unique().tolist()
    log(f"  Total rows: {len(items_df)} | To match: {len(filtered_df)} | Unique items: {len(all_items)}")

    best_matches: dict = {}
    confidence_scores: dict = {}
    items = list(all_items)

    # ── Stage 1: Fuzzy / semantic matching ───────────────────────
    items_for_ai: list = []
    if items:
        _LEGAL_NOISE = re.compile(
            r'\b(pursuant to|as defined in|hereinafter|section|schedule|exhibit'
            r'|agreement|contract|dated|effective|the foregoing|referred to as'
            r'|including but not limited to|subject to|in accordance with)\b',
            flags=re.IGNORECASE
        )
        def clean(text):
            return re.sub(r'\s+', ' ', _LEGAL_NOISE.sub(' ', str(text or ''))).strip()

        clean_items = [clean(s) for s in items]
        clean_desc  = [clean(s) for s in descriptions]

        log(f"  Stage 1 (fuzzy): scoring {len(items)} unique items × {len(descriptions)} dictionary entries…")
        emb_items = st_model.encode(clean_items, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
        emb_desc  = st_model.encode(clean_desc,  batch_size=64, show_progress_bar=False, normalize_embeddings=True)
        sem_sim   = emb_items @ emb_desc.T

        tfidf = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4), min_df=1, sublinear_tf=True)
        tfidf.fit(clean_items + clean_desc)
        lex_sim  = (normalize(tfidf.transform(clean_items)) @ normalize(tfidf.transform(clean_desc)).T).toarray()
        combined = SEMANTIC_WEIGHT * sem_sim + LEXICAL_WEIGHT * lex_sim

        _MONTHLY = re.compile(r'monthly', re.IGNORECASE)
        _ONETIME = re.compile(r'(one.?time|setup|set.?up|implementation)', re.IGNORECASE)
        def fee_conflict(a, b):
            return (bool(_MONTHLY.search(a)) and bool(_ONETIME.search(b))) or \
                   (bool(_ONETIME.search(a)) and bool(_MONTHLY.search(b)))

        for i, item in enumerate(items):
            best_j = int(np.argmax(combined[i]))
            score  = float(combined[i, best_j])
            if score >= FUZZY_AUTO_ACCEPT_THRESHOLD and not fee_conflict(item, descriptions[best_j]):
                best_matches[item]      = descriptions[best_j]
                confidence_scores[item] = round(score * 100)
            else:
                items_for_ai.append(item)
        log(f"  Fuzzy accepted: {len(best_matches)} | Sending to AI: {len(items_for_ai)}")

    # ── Stage 2: AI matching (GPT-4.1) ───────────────────────────
    if items_for_ai:
        item_batches = [items_for_ai[i:i + ITEM_BATCH_SIZE] for i in range(0, len(items_for_ai), ITEM_BATCH_SIZE)]
        dict_chunks  = [descriptions[i:i + DICT_CHUNK_SIZE] for i in range(0, len(descriptions), DICT_CHUNK_SIZE)]
        log(f"  Stage 2 (LLM): {len(item_batches)} item batch(es) × {len(dict_chunks)} dict chunk(s)  (≤ {MAX_PARALLEL_CALLS} parallel)")

        for batch in item_batches:
            log(f"    AI batch: {len(batch)} items × {len(dict_chunks)} dictionary chunks...")
            candidates: dict = {item: [] for item in batch}

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CALLS) as executor:
                futures = {executor.submit(match_batch, ai_client, batch, chunk, log): chunk for chunk in dict_chunks}
                for future in as_completed(futures):
                    for m in future.result():
                        item = m.get("item")
                        desc = m.get("matched_description")
                        if item in candidates:
                            if desc and desc not in candidates[item]:
                                candidates[item].append(desc)
                        else:
                            for orig in candidates:
                                if item and item.strip().lower() == orig.strip().lower():
                                    if desc and desc not in candidates[orig]:
                                        candidates[orig].append(desc)
                                    break

            non_empty = {k: v for k, v in candidates.items() if v}
            if not non_empty:
                log("    WARNING: No candidates found — skipping ranking.")
                continue

            # Ranking pass — pick the best candidate per item
            ranking_prompt = (
                "For each item pick the BEST semantic match from its candidates.\n"
                "Prefer specific product matches. Assign CONFIDENCE (0-100).\n"
                f"DATA:\n{json.dumps(non_empty, indent=2)}\n"
                'Return JSON: {"final_matches":[{"item":"...","matched_description":"...","confidence_percentage":95}]}'
            )
            try:
                response = call_api_with_retry(lambda: ai_client.responses.create(
                    model=MATCHING_MODEL, temperature=0,
                    input=[
                        {"role": "system", "content": MATCHING_SYSTEM_PROMPT},
                        {"role": "user",   "content": ranking_prompt}
                    ]
                ))
                raw = (response.output_text or "").strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                    raw = re.sub(r"```$", "", raw).strip()
                for r in json.loads(raw).get("final_matches", []):
                    best_matches[r["item"]]      = r["matched_description"]
                    confidence_scores[r["item"]] = r["confidence_percentage"]
            except Exception as e:
                log(f"    WARNING: Ranking failed: {e}")

    # ── Merge results and export ──────────────────────────────────
    items_df["Matched Description"]   = items_df["Item"].map(best_matches)
    items_df["Confidence Percentage"] = items_df["Item"].map(confidence_scores)

    # Keep only checked/null rows for export (drop explicitly unchecked)
    if "Checkbox_Checked" in items_df.columns:
        export_df = items_df[~items_df["Checkbox_Checked"].isin([0, 0.0, False, "False"])].copy()
        export_df["Checkbox_Checked"] = export_df["Checkbox_Checked"].replace({1: "True", 1.0: "True"})
    else:
        export_df = items_df.copy()

    # Insert cleaned price column next to raw price
    if "Price" in export_df.columns and "Cleaned Price" not in export_df.columns:
        price_idx = export_df.columns.get_loc("Price")
        export_df.insert(price_idx + 1, "Cleaned Price", export_df["Price"].apply(clean_price))

    # Map matched description to material code
    export_df["Material Code"] = (
        export_df["Matched Description"].astype(str).str.strip().str.lower().map(lookup_dict)
    )
    export_df = export_df[export_df["Material Code"].notna()].copy()

    # Backward-compat: provide both confidence representations.
    if "Confidence Percentage" in export_df.columns:
        export_df["Match Confidence"] = (
            pd.to_numeric(export_df["Confidence Percentage"], errors="coerce")
            .fillna(0) / 100.0
        )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    export_df.to_excel(str(output_file), index=False)
    log(f"  Matched output saved: {output_file}  ({len(export_df)} rows)")
    return len(export_df)


# ═══════════════════════════════════════════════════════════════════
# PUBLIC SURFACE — what the chatbot frontend calls
# ═══════════════════════════════════════════════════════════════════

def output_path(client_name: str) -> Path:
    """Phase 1 output path (Fee Description Agent button)."""
    return _OUTPUT_DIR / client_name / "extraction_output.xlsx"


def matching_output_path(client_name: str) -> Path:
    """Phase 2 output path (Material Code Matching Agent button)."""
    return _OUTPUT_DIR / client_name / "material_match_output.xlsx"


def is_processed(client_name: str) -> bool:
    """Fee Description is "done" whenever an extraction_output.xlsx exists
    for the client — regardless of row count.

    The agent now writes an EMPTY Excel (+ sidecar `extraction_meta.json`)
    when no qualifying PDFs are found (e.g. all contracts dated before the
    year cutoff) so the Load button doesn't keep re-triggering this client
    forever. The chatbot UI reads the sidecar to explain why the output is
    empty and suggest changing the year filter."""
    return output_path(client_name).exists()


def matching_is_processed(client_name: str) -> bool:
    """Material Code Matching is "done" whenever a material_match_output.xlsx
    exists for the client.

    Same rationale as `is_processed` above: when Fee Description produced 0
    items (no qualifying contracts), this agent writes an empty matching
    output + sidecar so the Load button moves on instead of looping."""
    return matching_output_path(client_name).exists()


# ── Phase 1 entry point ──────────────────────────────────────────────────────
def run(
    client_name: str,
    api_key: str = "",
    model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    contracts: Optional[list] = None,            # accepted for parity; unused
    core: str = "",
    min_year: Optional[int] = None,
    **_legacy_kwargs,
) -> dict:
    """Fee Description Agent — Phase 1. Verbatim port of the notebook's
    Phase 1 cell, driven per-client.

    `min_year` controls the date filter applied to PDFs in the input
    folder. Defaults to MIN_YEAR_CUTOFF (env-configurable, default 2022).
    The chatbot UI passes the session-level value here so the user can
    change it from the year selector above the Agent Outputs panel."""
    global _LOG_HOOK
    log = progress_callback or (lambda m: None)
    _LOG_HOOK = log
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    try:
        api_key = api_key or EXTRACTION_API_KEY
        global EXTRACTION_MODEL
        if model:
            EXTRACTION_MODEL = model

        log(f"━━━ Fee Description {_VERSION} ━━━")
        log(f"Extraction model: {EXTRACTION_MODEL}")
        log(f"Year filter: contracts dated >= {min_year} are processed; "
            f"older are skipped as legacy.")

        folder = client_input_dir(client_name, core)
        pairs = _filter_pdfs(folder, log, min_year=min_year)
        if not pairs:
            log(f"  No qualifying PDFs found at {folder} "
                f"(year cutoff >= {min_year}).")
            # Write an empty Excel + sidecar meta so `is_processed` returns
            # True and the Load button doesn't keep re-trying this client.
            _write_empty_extraction(client_name, "no_pdfs", min_year,
                                    folder, log)
            return {"status": "no_pdfs", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        ai_client = make_client(api_key)
        ref_b64 = _load_checkbox_reference_b64(log)

        frames: list = []
        for fname, date_obj in pairs:
            log(f"\n  Extracting: {fname}")
            try:
                df = extract_items_with_dollar_values(
                    str(folder / fname), ai_client, ref_b64=ref_b64, log=log)
            except Exception as e:
                log(f"  ⚠ Failed on {fname}: {e} — skipping")
                continue
            if df.empty:
                log("  No items extracted.")
                continue
            df["Source Contract"] = fname
            df["Date"] = date_obj.strftime("%Y-%m-%d")
            frames.append(df)

        if not frames:
            log("  No items extracted from any qualifying PDF.")
            _write_empty_extraction(client_name, "no_items", min_year,
                                    folder, log)
            return {"status": "no_items", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        combined = pd.concat(frames, ignore_index=True)
        out_path = output_path(client_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        export_to_excel(combined, str(out_path), log=log)
        # Record a "complete" meta sidecar so the year-filter context is
        # visible alongside the data.
        _write_meta(_extraction_meta_path(client_name), {
            "status":   "complete",
            "min_year": int(min_year),
            "folder":   str(folder),
            "ran_at":   datetime.now().isoformat(timespec="seconds"),
            "rows":     int(len(combined)),
        })
        log(f"\nWrote: {out_path.name} ({len(combined)} items)")
        return {
            "status": "complete",
            "client": client_name,
            "rows":   int(len(combined)),
            "output": str(out_path),
            "min_year": int(min_year),
        }
    finally:
        _LOG_HOOK = None


# ── Phase 2 entry point ──────────────────────────────────────────────────────
def run_matching(
    client_name: str,
    api_key: str = "",
    matching_model: str = "",
    dictionary_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    contracts: Optional[list] = None,
    core: str = "",
    min_year: Optional[int] = None,
    **_legacy_kwargs,
) -> dict:
    """Material Code Matching Agent — Phase 2. Verbatim port of the
    notebook's Phase 2 cell, driven per-client. Uses the cached sentence-
    transformer model so successive calls don't re-load the 420 MB weights.

    `min_year` is forwarded into the sidecar metadata so the UI knows
    which year cutoff was in effect; it doesn't change the matching logic
    itself (the year filter applies only at PDF-discovery time in
    Phase 1)."""
    global _LOG_HOOK
    log = progress_callback or (lambda m: None)
    _LOG_HOOK = log
    if min_year is None:
        min_year = MIN_YEAR_CUTOFF
    try:
        api_key = api_key or EXTRACTION_API_KEY
        global MATCHING_MODEL
        if matching_model:
            MATCHING_MODEL = matching_model

        log(f"━━━ Material Match {_VERSION} ━━━")
        log(f"Matching model: {MATCHING_MODEL}")

        ext_path = output_path(client_name)
        if not ext_path.exists():
            log(f"  ⚠ No extraction_output.xlsx for {client_name!r} — "
                f"run the Fee Description Agent first.")
            return {"status": "no_extraction", "client": client_name}

        # If Fee Description ran but produced 0 items (e.g. all PDFs were
        # pre-cutoff or extraction itself returned no items), there's
        # nothing to match. Write an empty matching output + sidecar so
        # this agent is treated as "ran" by the Load button and doesn't
        # keep re-triggering on every click.
        try:
            try:
                _peek = pd.read_excel(str(ext_path), sheet_name="Dollar_Items")
            except Exception:
                _peek = pd.read_excel(str(ext_path))
        except Exception:
            _peek = pd.DataFrame()
        if _peek.empty:
            log(f"  Extraction output is empty (0 items) for "
                f"{client_name!r} — nothing to match. Writing empty "
                f"matching output so the agent is marked complete.")
            _write_empty_matching(client_name, "no_extraction_items",
                                  min_year, log)
            return {"status": "no_items", "client": client_name,
                    "rows": 0, "min_year": int(min_year)}

        if dictionary_path is None and core:
            dictionary_path = default_dictionary_for(core) or None
        if not dictionary_path or not Path(dictionary_path).exists():
            log(f"  ⚠ No dictionary set for {client_name!r} — cannot run matching.")
            return {"status": "no_dictionary", "client": client_name}

        log(f"  Loading dictionary: {Path(dictionary_path).name}")
        dictionary_df = _load_dictionary(Path(dictionary_path), log)
        if dictionary_df is None:
            return {"status": "bad_dictionary", "client": client_name}

        descriptions = dictionary_df["Description"].dropna().astype(str).tolist()
        lookup_dict = {
            str(d).strip().lower(): code
            for d, code in zip(dictionary_df["Description"], dictionary_df["Material Code"])
            if pd.notna(d)
        }
        log(f"  Dictionary: {len(descriptions)} description(s), "
            f"{dictionary_df['Material Code'].dropna().nunique()} unique code(s)")

        ai_client = make_client(api_key)
        st_model  = _get_st_model(log)

        out_path = matching_output_path(client_name)
        n = process_client(
            input_file=ext_path,
            dictionary_df=dictionary_df,
            descriptions=descriptions,
            lookup_dict=lookup_dict,
            output_file=out_path,
            ai_client=ai_client,
            st_model=st_model,
            log=log,
        )
        # Sidecar so the UI can show the cutoff year alongside the matched
        # output (consistent with the Fee Description sidecar).
        _write_meta(_matching_meta_path(client_name), {
            "status":   "complete",
            "min_year": int(min_year),
            "ran_at":   datetime.now().isoformat(timespec="seconds"),
            "rows":     int(n),
        })
        return {
            "status": "complete",
            "client": client_name,
            "rows":   int(n),
            "output": str(out_path),
            "min_year": int(min_year),
        }
    finally:
        _LOG_HOOK = None
