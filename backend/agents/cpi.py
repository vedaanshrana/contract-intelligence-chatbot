"""
CPI agent adapter — two stages.

STAGE 1  (extraction, ported from find_CPI_and_extract_info.py / CPI.ipynb):
    Scans the client's contract PDFs, finds exact "CPI" mentions (with an
    "increased annually effective" / "Annual Adjustment" fallback), asks an LLM
    to pull the fee-increase effective date and minimum increase per snippet,
    and writes  Output/<ClientName>/<ClientName> CPI_matches.xlsx.

STAGE 2  (formatting, original CPI Final Output.ipynb logic):
    Reshapes that matches file into the standard CPI database output:
    Output/<ClientName>/cpi_output.xlsx

run_full() chains both stages.  Stage 1 is skipped if a matches file already
exists; Stage 2 is always (re)run from the matches file.

OCR (pytesseract + Tesseract binary) and rapidfuzz are OPTIONAL.  Text-based
PDFs are handled without them; image-only PDFs are skipped if OCR is absent.
"""

import io
import json
import os
import re
import time
import unicodedata
from calendar import month_name
from pathlib import Path
from typing import Callable, Optional

import fitz                       # PyMuPDF
import pandas as pd
from fiserv_client import make_client
from PIL import Image

from config import CPI_API_KEY, CPI_MODEL

_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"

_MONTHS = [m for m in month_name if m]   # ['January', 'February', ...]

# ── Optional dependencies ───────────────────────────────────────────────────
try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

try:
    import pytesseract
    _TESS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_TESS_PATH):
        pytesseract.pytesseract.tesseract_cmd = _TESS_PATH
    # If the binary isn't at the default path we still trust PATH; pytesseract
    # raises at call time if it's genuinely missing (handled per-call).
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False


# ════════════════════════════════════════════════════════════════════════════
# STAGE 1 — CPI EXTRACTION (ported from the notebook)
# ════════════════════════════════════════════════════════════════════════════

_SEARCH_TERM          = "CPI"
_CONTEXT_WINDOW_WORDS = 40
_OPENAI_MAX_TOKENS    = 800

# Per-run text caches (avoid re-OCR of the same file)
_OCR_TEXT_CACHE: dict  = {}
_TEXT_PAGE_CACHE: dict = {}

_SYSTEM_PROMPT = "You are a contract-reading assistant. Extract structured info from the provided snippets."

_USER_PROMPT_SNIPPET_ANALYSIS = """
You are given a list of snippets from a contract where the term "CPI" appears. For each snippet, provide a JSON object with fields:
  - "snippet": the snippet text (return a snippet that makes logical sense, you may trim but keep the relevant CPI sentence(s) and any fees or services related information that may be mentioned). Include information about conditions where the annual increase does not apply, if present.
  - "cpi_effective_date": the CPI effective date mentioned in this snippet if present (return in the form you see it, e.g., "January 1, 2020", "1/1/2020", "01_01_2020" etc.). If none, return empty string "".
  - "minimum_fee_increase": the minimum fee increase percentage or expression mentioned in this snippet if present (e.g., "2%", "at least 1.5%", "no less than 2 percent"). If none, return empty string "".

Return a JSON array of objects, e.g.:
[
  {"snippet":"...","cpi_effective_date":"...","minimum_fee_increase":"..."},
  ...
]

Here are the snippets (numbered). Provide the JSON array ONLY.
<<snippets_block>>
""".strip()

_DATE_RE     = re.compile(r'\b(\d{1,2}[_/]\d{1,2}[_/]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},\s*\d{4})\b')
_PERCENT_RE  = re.compile(r'(\d+(?:\.\d+)?\s*(?:%|percent|percentage|bps|basis points))', re.IGNORECASE)
_PERCENT_RE2 = re.compile(r'(at least|no less than|not less than|minimum of)?\s*([0-9]+(?:\.[0-9]+)?)\s*(%|percent|percentage)', re.IGNORECASE)


def _normalize_for_match(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub('[\u200b-\u200f\ufeff\xa0\xad]', '', s)
    return re.sub(r'[^A-Za-z0-9]', '', s).lower().strip()


def _similarity(a: str, b: str) -> float:
    if _HAS_RAPIDFUZZ:
        return fuzz.token_set_ratio(a, b)
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def _ocr_pdf_to_text_pages(pdf_path: Path) -> list:
    """OCR every page of an image-based PDF. Returns [] if OCR is unavailable."""
    if not _HAS_OCR:
        return []
    text_pages = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            text_pages.append(pytesseract.image_to_string(img))
        except Exception:
            text_pages.append("")
    doc.close()
    return text_pages


def _tokenize_page_words(page) -> list:
    words = page.get_text("words")
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (round(w[1], 2), round(w[0], 2)))
    return [{"text": w[4], "norm": _normalize_for_match(w[4])} for w in words_sorted]


def _find_exact_term_locations(term_norm: str, page_tokens: list) -> list:
    return [i for i, tok in enumerate(page_tokens) if tok["norm"] == term_norm]


def _make_snippet(page_tokens: list, match_idx: int, window: int = _CONTEXT_WINDOW_WORDS) -> str:
    start = max(0, match_idx - window)
    end   = min(len(page_tokens) - 1, match_idx + window)
    return " ".join(page_tokens[i]["text"] for i in range(start, end + 1))


def _unique_preserve_order(items: list) -> list:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _normalize_snippet_text(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or "").strip()).lower()


def _dedupe_similar_snippets(snippets: list, threshold: int = 70) -> list:
    if not snippets:
        return []
    kept: list = []
    for s in snippets:
        norm_s = _normalize_snippet_text(s)
        if not norm_s:
            continue
        found = False
        for idx, (ks, kn) in enumerate(kept):
            if _similarity(kn, norm_s) >= threshold:
                if len(s) > len(ks):
                    kept[idx] = (s, norm_s)
                found = True
                break
        if not found:
            kept.append((s, norm_s))
    return [t[0] for t in kept]


def _extract_annual_adjustment_snippets(pdf_path: Path) -> list:
    """Snippets from 'Annual Adjustment' to the first '.' after whichever is greater/lesser."""
    snippets: list = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return snippets

    text_pages = []
    for page in doc:
        txt = page.get_text("text")
        if txt and txt.strip():
            text_pages.append(txt)
        elif _HAS_OCR:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                text_pages.append(pytesseract.image_to_string(img))
            except Exception:
                text_pages.append("")
        else:
            text_pages.append("")
    doc.close()

    for page_index, page_text in enumerate(text_pages):
        lower     = page_text.lower()
        start_idx = lower.find("annual adjustment")
        if start_idx == -1:
            continue
        region      = lower[start_idx:]
        greater_idx = region.find("whichever is greater")
        lesser_idx  = region.find("whichever is lesser")
        if greater_idx != -1 and lesser_idx != -1:
            phrase_idx = min(greater_idx, lesser_idx)
        elif greater_idx != -1:
            phrase_idx = greater_idx
        elif lesser_idx != -1:
            phrase_idx = lesser_idx
        else:
            continue
        phrase_abs = start_idx + phrase_idx
        dot_idx    = lower.find('.', phrase_abs)
        if dot_idx == -1:
            dot_idx = len(page_text)
        snippet = page_text[start_idx:dot_idx + 1].strip()
        snippets.append(f"(File: {pdf_path.name} - Page {page_index + 1}) {snippet}")
    return snippets


def _extract_fallback_increased_annually(pdf_path: Path) -> list:
    """Fallback when 'CPI' not found: look for 'increased annually effective'."""
    target_norm = _normalize_for_match("increased annually effective")
    snippets: list = []
    name = pdf_path.name

    if name in _OCR_TEXT_CACHE:
        pages_text = _OCR_TEXT_CACHE[name]
    elif name in _TEXT_PAGE_CACHE:
        pages_text = _TEXT_PAGE_CACHE[name]
    else:
        try:
            doc = fitz.open(str(pdf_path))
        except Exception:
            return snippets
        pages_text, has_text = [], False
        for page in doc:
            t = page.get_text("text")
            pages_text.append(t)
            if t.strip():
                has_text = True
        doc.close()
        if has_text:
            _TEXT_PAGE_CACHE[name] = pages_text
        else:
            pages_text = _ocr_pdf_to_text_pages(pdf_path)
            _OCR_TEXT_CACHE[name] = pages_text

    for page_index, text in enumerate(pages_text, start=1):
        if not isinstance(text, str):
            continue
        if target_norm in _normalize_for_match(text):
            snippets.append(f"(File: {name} - Page {page_index}) {text.strip()}")
    return snippets


def _local_extract_from_snippets(snippets: list) -> list:
    out = []
    for s in snippets:
        dm = _DATE_RE.search(s)
        pm = _PERCENT_RE.search(s) or _PERCENT_RE2.search(s)
        out.append({
            "snippet": s.strip(),
            "cpi_effective_date": dm.group(0) if dm else "",
            "minimum_fee_increase": pm.group(0).strip() if pm else "",
        })
    return out


def _call_llm_analyze_snippets(client, snippets: list, model: str,
                               retries: int = 2, wait_secs: int = 2) -> list:
    if not snippets:
        return []
    block = "\n\n".join(f"{i+1}. {s}" for i, s in enumerate(snippets))
    user_prompt = _USER_PROMPT_SNIPPET_ANALYSIS.replace("<<snippets_block>>", block)

    def _coerce(parsed) -> list:
        out = []
        for obj in parsed:
            if isinstance(obj, dict):
                out.append({
                    "snippet": obj.get("snippet", "").strip(),
                    "cpi_effective_date": obj.get("cpi_effective_date", "").strip(),
                    "minimum_fee_increase": obj.get("minimum_fee_increase", "").strip(),
                })
        return out

    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=_OPENAI_MAX_TOKENS,
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return _coerce(parsed)
            except Exception:
                m = re.search(r'\[.*\]', text, flags=re.DOTALL)
                if m:
                    try:
                        return _coerce(json.loads(m.group(0)))
                    except Exception:
                        pass
            return _local_extract_from_snippets(snippets)
        except Exception:
            if attempt < retries:
                time.sleep(wait_secs * (attempt + 1))
            else:
                return _local_extract_from_snippets(snippets)
    return []


def _parse_contract_effective_date_from_filename(name: str) -> str:
    m = re.search(r'(\d{1,2}[_-]\d{1,2}[_-]\d{4})', name)
    return m.group(1) if m else ""


def _detect_contract_type_from_filename(name: str) -> str:
    low = name.lower()
    if "master agreement" in low or "master" in low:
        return "Master Agreement"
    if "amendment" in low or "amend" in low:
        return "Amendment"
    return ""


def _scan_pdfs_in_folder(folder: Path, client, model: str,
                         log: Callable[[str], None],
                         contracts: Optional[list] = None) -> list:
    rows: list = []
    term_norm = _normalize_for_match(_SEARCH_TERM)

    pdfs = list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))
    pdfs = sorted(set(pdfs), key=lambda p: p.name)

    wanted = {str(c) for c in contracts} if contracts is not None else None

    for p in pdfs:
        # Scope-agent filter (if provided)
        if wanted is not None and p.name not in wanted:
            continue
        low = p.name.lower()
        if not ("master" in low or "amendment" in low or "services" in low):
            continue

        try:
            doc = fitz.open(str(p))
        except Exception as e:
            log(f"  ⚠ Could not open {p.name}: {e}")
            continue

        # Build text pages (OCR only if no embedded text)
        has_text   = False
        text_pages = []
        for page in doc:
            t = page.get_text("text")
            text_pages.append(t)
            if t.strip():
                has_text = True

        ocr_text_pages = None
        ocr_used = False
        if has_text:
            _TEXT_PAGE_CACHE[p.name] = text_pages
        else:
            if _HAS_OCR:
                log(f"  OCR triggered for {p.name}")
                ocr_text_pages = _ocr_pdf_to_text_pages(p)
                _OCR_TEXT_CACHE[p.name] = ocr_text_pages
                ocr_used = True
            else:
                log(f"  ⚠ {p.name} is image-only and OCR is unavailable — skipping text scan")

        # ── Main search: CPI ──
        found_any        = False
        first_match_page = None
        snippets_for_llm = []
        for page_no in range(len(doc)):
            page = doc[page_no]
            if ocr_text_pages is not None:
                raw = ocr_text_pages[page_no] if page_no < len(ocr_text_pages) else ""
                page_tokens = [{"text": w, "norm": _normalize_for_match(w)} for w in raw.split()]
            else:
                page_tokens = _tokenize_page_words(page)

            matches = _find_exact_term_locations(term_norm, page_tokens)
            if matches and first_match_page is None:
                first_match_page = page_no + 1
            for mi in matches:
                found_any = True
                snippets_for_llm.append(
                    f"(File: {p.name} - Page {page_no+1}) {_make_snippet(page_tokens, mi)}"
                )
        doc.close()

        # CASE 1 — CPI found
        if found_any:
            log(f"  Analyzing {p.name} ({len(snippets_for_llm)} CPI snippet(s))…")
            snippets_for_llm = _dedupe_similar_snippets(snippets_for_llm, threshold=70)
            l_out = _call_llm_analyze_snippets(client, snippets_for_llm, model)

            combined = " ||| ".join(i.get("snippet", "") for i in l_out) if l_out \
                       else " ||| ".join(snippets_for_llm)

            aa = _extract_annual_adjustment_snippets(p)
            if aa:
                aa = _dedupe_similar_snippets(aa, threshold=70)
                combined = " ||| ".join(aa)

            cpi_dates = _unique_preserve_order(
                [i.get("cpi_effective_date", "").strip() for i in l_out if i.get("cpi_effective_date", "").strip()])
            min_incs = _unique_preserve_order(
                [i.get("minimum_fee_increase", "").strip() for i in l_out if i.get("minimum_fee_increase", "").strip()])

            rows.append({
                "Filename": p.name,
                "Contract Type": _detect_contract_type_from_filename(p.name),
                "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
                "CPI Snippets (LLM)": combined,
                "Fee Increase Effective Date(s)": ", ".join(cpi_dates),
                "Minimum Fee Increase(s)": ", ".join(min_incs),
                "OCR": "Yes" if ocr_used else "No",
                "Page Number": first_match_page,
            })
            continue

        # CASE 2 — fallback
        fb = _extract_fallback_increased_annually(p)
        if fb:
            fb_page = None
            m = re.search(r'Page\s+(\d+)', fb[0])
            if m:
                fb_page = int(m.group(1))
            fb = _dedupe_similar_snippets(fb, threshold=70)
            log(f"  Analyzing {p.name} (fallback snippet(s))…")
            l_out = _call_llm_analyze_snippets(client, fb, model)
            combined  = " ||| ".join(i.get("snippet", "") for i in l_out)
            cpi_dates = _unique_preserve_order(
                [i.get("cpi_effective_date", "").strip() for i in l_out if i.get("cpi_effective_date", "").strip()])
            min_incs = _unique_preserve_order(
                [i.get("minimum_fee_increase", "").strip() for i in l_out if i.get("minimum_fee_increase", "").strip()])
            rows.append({
                "Filename": p.name,
                "Contract Type": _detect_contract_type_from_filename(p.name),
                "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
                "CPI Snippets (LLM)": combined,
                "Fee Increase Effective Date(s)": ", ".join(cpi_dates),
                "Minimum Fee Increase(s)": ", ".join(min_incs),
                "OCR": "Yes" if ocr_used else "No",
                "Page Number": fb_page,
            })
        else:
            rows.append({
                "Filename": p.name,
                "Contract Type": _detect_contract_type_from_filename(p.name),
                "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
                "CPI Snippets (LLM)": "",
                "Fee Increase Effective Date(s)": "",
                "Minimum Fee Increase(s)": "",
                "OCR": "Yes" if ocr_used else "No",
                "Page Number": "",
            })
    return rows


def extract(
    client_name: str,
    api_key: str = "",
    model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    contracts: Optional[list] = None,
    core: str = "",
) -> dict:
    """
    Stage 1 — scan the client's PDFs and write <ClientName> CPI_matches.xlsx.

    contracts: optional allowlist of PDF filenames (from the scope agent).
               When None, every Master/Amendment/Services PDF is scanned.

    Returns {status, client, rows, output}.
    """
    _OCR_TEXT_CACHE.clear()
    _TEXT_PAGE_CACHE.clear()

    folder = (_INPUT_DIR / core / client_name) if core else (_INPUT_DIR / client_name)
    if not folder.exists():
        return {"status": "no_folder", "client": client_name}

    log    = progress_callback or (lambda msg: None)
    client = make_client(api_key or CPI_API_KEY)

    log(f"Scanning PDFs for CPI language in {client_name}…")
    rows = _scan_pdfs_in_folder(folder, client, model or CPI_MODEL, log, contracts=contracts)

    cols = ["Filename", "Contract Type", "Contract Effective Date", "CPI Snippets (LLM)",
            "Fee Increase Effective Date(s)", "Minimum Fee Increase(s)", "OCR", "Page Number"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

    out_path = _OUTPUT_DIR / client_name / f"{client_name} CPI_matches.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(str(out_path), index=False)
    log(f"Wrote CPI matches: {out_path.name} ({len(df)} rows)")

    return {"status": "complete", "client": client_name, "rows": len(df), "output": str(out_path)}


# ════════════════════════════════════════════════════════════════════════════
# STAGE 2 — CPI FORMATTING (original CPI Final Output.ipynb logic)
# ════════════════════════════════════════════════════════════════════════════

def _trim_leading_paren(text):
    if pd.isna(text): return text
    s = str(text).lstrip()
    if s.startswith("("):
        idx = s.find(")")
        if idx != -1: return s[idx + 1:].strip()
    return s


def _extract_client_name(filename):
    if pd.isna(filename): return ""
    parts = []
    for chunk in str(filename).split():
        parts.extend(chunk.split("-"))
    return " ".join(
        p for p in parts
        if any(c.isalpha() for c in p) and p == p.upper() and p.upper() != "PDF"
    )


def _extract_year(text):
    if pd.isna(text): return ""
    s = str(text)
    m = re.search(r"\b(20\d{2})\b", s)
    if m: return m.group(1)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = m.group(3)
        return str(2000 + int(y)) if len(y) == 2 else y
    return ""


def _extract_month(text):
    if pd.isna(text): return ""
    s = str(text)
    for mo in _MONTHS:
        if mo.lower() in s.lower(): return mo
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 12: return month_name[n]
    return ""


def _contract_year_plus_one(text):
    if pd.isna(text): return ""
    s     = str(text).strip()
    last4 = s[-4:] if len(s) >= 4 else ""
    return str(int(last4) + 1) if last4.isdigit() else ""


def _compute_cpi_terms(row):
    snippet = row.get("CPI Snippets (LLM)")
    if pd.isna(snippet) or str(snippet).strip() == "":
        return "CPI Language not Found"
    s   = str(snippet)
    pct = re.findall(r"(\d+(?:\.\d+)?)\s*(%|percent)", s, re.I)
    min_fee = ", ".join(f"{m[0]}%" for m in pct) if pct else "NA"
    if "whichever is greater" in s.lower():
        return f"> CPI-U or {min_fee}"
    if "cpi" not in s.lower() and "consumer price index" not in s.lower():
        return f"Max {min_fee}"
    return "Limited to CPI-U" if min_fee == "NA" else f"< CPI-U or {min_fee}"


def _compute_elig_year(row):
    if pd.isna(row.get("CPI Snippets (LLM)")) or str(row.get("CPI Snippets (LLM)")).strip() == "":
        return ""
    y = _extract_year(row.get("Fee Increase Effective Date(s)"))
    return y if y else _contract_year_plus_one(row.get("Contract Effective Date"))


def _split_lang(snippet):
    if pd.isna(snippet) or str(snippet).strip() == "": return "", ""
    s = str(snippet)
    return (s, "") if "30" in s else ("", s)


def _specific_lang(snippet):
    if pd.isna(snippet) or str(snippet).strip() == "": return ""
    s   = str(snippet)
    idx = s.lower().rfind("limited")
    if idx != -1: return s[idx:].strip()
    return s.strip() if "30" in s else ""


def run(
    client_name: str,
    cpi_matches_path: Path,
    core_name: str = "",
) -> dict:
    """
    Stage 2 — format a CPI matches Excel file into cpi_output.xlsx.
    """
    if not Path(cpi_matches_path).exists():
        return {"status": "no_input", "client": client_name}

    df = pd.read_excel(str(cpi_matches_path))
    if df.empty:
        # No PDFs produced any rows — still emit an empty formatted file.
        out_path = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_excel(str(out_path), index=False)
        return {"status": "complete", "client": client_name, "rows": 0, "output": str(out_path)}

    df["Filename"]            = df["Filename"].astype(str).str[:-4]
    df["CPI Snippets (LLM)"]  = df["CPI Snippets (LLM)"].apply(_trim_leading_paren)

    out = pd.DataFrame()
    out["Client Name"]           = df["Filename"].apply(_extract_client_name)
    out["Core"]                  = core_name or ""
    out["Contract Type"]         = df["Contract Type"]
    out["Contract Effective Date"] = df["Contract Effective Date"].astype(str).str.replace("_", "-", regex=False)
    out["CPI Terms (per Contract)"] = df.apply(_compute_cpi_terms, axis=1)
    out["CPI Eligibility Year"]  = df.apply(_compute_elig_year, axis=1)
    out["CPI Eligibility Month"] = df["Fee Increase Effective Date(s)"].apply(_extract_month)
    out["Notice Requirement"]    = df["CPI Snippets (LLM)"].apply(
        lambda s: "30 Days" if not pd.isna(s) and str(s).strip() else ""
    )
    out["Specific Contract Language/Information"] = df["CPI Snippets (LLM)"].apply(_specific_lang)

    normal_col, review_col = [], []
    for s in df["CPI Snippets (LLM)"]:
        n, r = _split_lang(s)
        normal_col.append(n); review_col.append(r)
    out["Contract Language/Information"]             = normal_col
    out["Contract Language/Information (For Review)"] = review_col
    out["Item Type"] = "Item"
    out["Path"]      = "sites/fss/CPI/Lists/CU CPI Database"

    for col in ("OCR", "Page Number"):
        if col in df.columns: out[col] = df[col]

    output_path = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_excel(str(output_path), index=False)

    return {
        "status": "complete",
        "client": client_name,
        "rows":   len(out),
        "output": str(output_path),
    }


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def run_full(
    client_name: str,
    api_key: str = "",
    model: str = "",
    core_name: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    force_extract: bool = False,
    contracts: Optional[list] = None,
    core: str = "",
) -> dict:
    """
    Run both stages: extract CPI matches from PDFs (Stage 1) then format (Stage 2).
    If a matches file already exists and force_extract is False, Stage 1 is skipped.

    contracts: optional allowlist of PDF filenames (from the scope agent).
    """
    log = progress_callback or (lambda msg: None)

    matches = find_cpi_input(client_name)
    if matches is None or force_extract:
        ext = extract(client_name, api_key=api_key, model=model,
                      progress_callback=progress_callback, contracts=contracts, core=core)
        if ext.get("status") != "complete":
            return ext
        matches = Path(ext["output"])
    else:
        log(f"Using existing CPI matches file: {matches.name}")

    log("Formatting CPI output…")
    return run(client_name, matches, core_name=core_name)


def is_processed(client_name: str) -> bool:
    p = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
    return p.exists() and p.stat().st_size > 1_000


def find_cpi_input(client_name: str) -> Optional[Path]:
    """
    Search for a CPI matches file for this client in Output/ and Input/ dirs.
    Returns the first match or None.
    """
    search_dirs = [
        _OUTPUT_DIR / client_name,
        _INPUT_DIR  / client_name,
    ]
    patterns = [
        "*CPI*matches*.xlsx",
        "*cpi*matches*.xlsx",
        "*CPI*.xlsx",
        "*cpi*.xlsx",
    ]
    for d in search_dirs:
        if not d.exists(): continue
        for pat in patterns:
            hits = list(d.glob(pat))
            if hits: return hits[0]
    return None
