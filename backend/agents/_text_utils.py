"""
Shared text-extraction helpers used by CPI and the clause extractors.

These functions are intentionally generic — no contract-domain logic lives
here.  Per-clause specifics (search keywords, prompt, output schema) belong
in clause_extractor.py and the individual agent configs.

Optional dependencies (rapidfuzz, pytesseract) are soft-imported; everything
degrades gracefully when they're missing.
"""

import io
import os
import re
import unicodedata
from pathlib import Path
from typing import Callable, Optional

import fitz                       # PyMuPDF
from PIL import Image

# ── Optional dependencies ────────────────────────────────────────────────────
try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

try:
    import pytesseract
    _TESS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_TESS_PATH):
        pytesseract.pytesseract.tesseract_cmd = _TESS_PATH
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


# ── Normalization ────────────────────────────────────────────────────────────

# Match the zero-width / non-breaking / soft-hyphen characters PyMuPDF leaves
# in extracted text.  Using explicit unicode escapes avoids invisible-char bugs.
_INVISIBLE_RE = re.compile(r'[​-‏﻿\xa0\xad]')


def normalize_for_match(s: str) -> str:
    """Lowercase + strip all non-alphanumerics + drop invisible chars.
    Used to compare PDF tokens against search keywords robustly."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _INVISIBLE_RE.sub("", s)
    return re.sub(r'[^A-Za-z0-9]', '', s).lower().strip()


def normalize_snippet_text(s: str) -> str:
    """Collapse whitespace + lowercase (used for fuzzy snippet dedup)."""
    return re.sub(r'\s+', ' ', (s or "").strip()).lower()


def similarity(a: str, b: str) -> float:
    """0-100 token-overlap similarity.  Uses rapidfuzz when available."""
    if HAS_RAPIDFUZZ:
        return fuzz.token_set_ratio(a, b)
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


# ── PDF text helpers ─────────────────────────────────────────────────────────

def tokenize_page_words(page) -> list:
    """Return a list of {text, norm} dicts, sorted top-to-bottom, left-to-right."""
    words = page.get_text("words")
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (round(w[1], 2), round(w[0], 2)))
    return [{"text": w[4], "norm": normalize_for_match(w[4])} for w in words_sorted]


def find_exact_term_locations(term_norm: str, page_tokens: list) -> list:
    """Indices of tokens whose normalized form equals `term_norm`."""
    return [i for i, tok in enumerate(page_tokens) if tok["norm"] == term_norm]


def find_phrase_locations(phrase_text: str, page_tokens: list) -> list:
    """Indices where a multi-word normalized phrase starts in page_tokens.
    e.g. find_phrase_locations('annual adjustment', tokens) -> [12, 87]
    Each word in the phrase is matched after normalization (whitespace/punct stripped)."""
    parts = [normalize_for_match(p) for p in phrase_text.split() if p.strip()]
    if not parts:
        return []
    out = []
    tlen = len(parts)
    for i in range(len(page_tokens) - tlen + 1):
        if all(page_tokens[i + j]["norm"] == parts[j] for j in range(tlen)):
            out.append(i)
    return out


def make_snippet(page_tokens: list, match_idx: int, window: int = 40) -> str:
    """Join `window` words on each side of `match_idx` back into a string."""
    start = max(0, match_idx - window)
    end   = min(len(page_tokens) - 1, match_idx + window)
    return " ".join(page_tokens[i]["text"] for i in range(start, end + 1))


# ── OCR ──────────────────────────────────────────────────────────────────────

def ocr_pdf_to_text_pages(pdf_path: Path, dpi: int = 300) -> list:
    """OCR every page of an image-based PDF.  Returns [] if OCR is unavailable."""
    if not HAS_OCR:
        return []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            pages.append(pytesseract.image_to_string(img))
        except Exception:
            pages.append("")
    doc.close()
    return pages


def read_pdf_pages(pdf_path: Path, ocr_when_empty: bool = True) -> tuple:
    """Open a PDF and return (page_text_list, ocr_used: bool).
    Falls back to OCR per-page only when embedded text is empty."""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return [], False

    text_pages = []
    has_embedded_text = False
    for page in doc:
        t = page.get_text("text")
        text_pages.append(t)
        if t.strip():
            has_embedded_text = True

    if has_embedded_text or not ocr_when_empty or not HAS_OCR:
        doc.close()
        return text_pages, False

    # Whole PDF is image-only → OCR all pages.
    doc.close()
    ocr_pages = ocr_pdf_to_text_pages(pdf_path)
    return ocr_pages, True


# ── Snippet deduplication ────────────────────────────────────────────────────

def dedupe_similar_snippets(snippets: list, threshold: int = 70) -> list:
    """Merge near-duplicate snippets (e.g. same clause repeated across pages).
    Keeps the longest representative within each similarity cluster."""
    if not snippets:
        return []
    kept: list = []
    for s in snippets:
        norm_s = normalize_snippet_text(s)
        if not norm_s:
            continue
        found = False
        for idx, (ks, kn) in enumerate(kept):
            if similarity(kn, norm_s) >= threshold:
                if len(s) > len(ks):
                    kept[idx] = (s, norm_s)
                found = True
                break
        if not found:
            kept.append((s, norm_s))
    return [t[0] for t in kept]


def unique_preserve_order(items: list) -> list:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
