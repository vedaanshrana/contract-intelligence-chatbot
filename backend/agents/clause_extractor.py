"""
Generic clause extractor.

Each new clause-type agent (term/renewal, termination, SLA, volume tiers) is
just a ClauseExtractor configured with:
  • a name (drives output filename)
  • search keywords / phrases to locate relevant snippets in the PDFs
  • a system + user prompt for the LLM
  • a list of fields to extract  (mapped to output Excel columns)

The framework handles PDF iteration (with OCR fallback for scanned files),
snippet collection + dedup, LLM calls with retries, JSON parsing, and writing
the per-client xlsx.

Output filename:  Output/<ClientName>/<name>_output.xlsx
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import fitz                       # PyMuPDF
import pandas as pd
from fiserv_client import make_client

from . import _text_utils as tu


_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"


# ── Filename helpers (shared across extractors) ──────────────────────────────

def _parse_effective_date(name: str) -> str:
    m = re.search(r'(\d{1,2}[_-]\d{1,2}[_-]\d{4})', name)
    return m.group(1) if m else ""


def _detect_contract_type(name: str) -> str:
    low = name.lower()
    if "master agreement" in low or "master" in low: return "Master Agreement"
    if "amendment" in low or "amend" in low:         return "Amendment"
    if "services"  in low:                           return "Services"
    return ""


def _pdf_filter(name: str) -> bool:
    """Default: only scan master/amendment/services contracts."""
    low = name.lower()
    return "master" in low or "amendment" in low or "services" in low


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class ClauseConfig:
    """Per-clause configuration passed to ClauseExtractor."""

    # Identity
    name:        str                            # e.g. "term_renewal"
    display:     str                            # e.g. "Term & Renewal"

    # PDF scanning
    search_terms:   list = field(default_factory=list)  # single-word triggers (normalized)
    search_phrases: list = field(default_factory=list)  # multi-word triggers
    context_window: int  = 60                           # words on each side of the hit

    # LLM
    api_key:        str = ""
    model:          str = "gpt-4o-mini"
    system_prompt:  str = ""
    user_prompt:    str = ""        # MUST contain "<<snippets_block>>"
    max_tokens:     int = 1200

    # Output schema
    # field_mapping maps {json_field_name -> Excel column name}.
    # The order of the dict controls the order of columns in the output xlsx.
    field_mapping:  dict = field(default_factory=dict)


# ── Core extractor ───────────────────────────────────────────────────────────

class ClauseExtractor:
    """Generic 'find snippets → LLM → write Excel' pipeline for any clause type."""

    def __init__(self, cfg: ClauseConfig):
        if "<<snippets_block>>" not in cfg.user_prompt:
            raise ValueError("user_prompt must contain '<<snippets_block>>' placeholder")
        self.cfg = cfg

    # ── output paths ─────────────────────────────────────────────────────────
    def output_path(self, client_name: str) -> Path:
        return _OUTPUT_DIR / client_name / f"{self.cfg.name}_output.xlsx"

    def is_processed(self, client_name: str) -> bool:
        p = self.output_path(client_name)
        return p.exists() and p.stat().st_size > 1_000

    # ── per-PDF snippet collection ───────────────────────────────────────────
    def _snippets_for_pdf(self, pdf_path: Path, log: Callable[[str], None]) -> tuple:
        """Return (snippet_strings, first_page_with_hit, ocr_used)."""
        snippets: list = []
        first_hit_page: Optional[int] = None
        ocr_used = False

        text_pages, ocr_used = tu.read_pdf_pages(pdf_path)
        if not text_pages:
            log(f"  ⚠ {pdf_path.name}: no text and OCR unavailable — skipped")
            return [], None, False

        # Normalized single-token triggers
        term_norms = [tu.normalize_for_match(t) for t in self.cfg.search_terms if t.strip()]

        try:
            doc = fitz.open(str(pdf_path))
        except Exception as e:
            log(f"  ⚠ Could not open {pdf_path.name}: {e}")
            return [], None, ocr_used

        try:
            for page_no in range(len(doc)):
                # Token-level tokens — from PDF text if available, else from OCR text.
                if ocr_used and page_no < len(text_pages):
                    raw = text_pages[page_no] or ""
                    page_tokens = [{"text": w, "norm": tu.normalize_for_match(w)} for w in raw.split()]
                else:
                    page_tokens = tu.tokenize_page_words(doc[page_no])

                # Single-token hits
                hits = set()
                for tn in term_norms:
                    for idx in tu.find_exact_term_locations(tn, page_tokens):
                        hits.add(idx)
                # Phrase hits
                for phrase in self.cfg.search_phrases:
                    for idx in tu.find_phrase_locations(phrase, page_tokens):
                        hits.add(idx)

                if hits and first_hit_page is None:
                    first_hit_page = page_no + 1

                for idx in sorted(hits):
                    snip = tu.make_snippet(page_tokens, idx, window=self.cfg.context_window)
                    snippets.append(f"(File: {pdf_path.name} - Page {page_no + 1}) {snip}")
        finally:
            doc.close()

        return snippets, first_hit_page, ocr_used

    # ── LLM call ─────────────────────────────────────────────────────────────
    def _call_llm(self, client, snippets: list, log: Callable[[str], None],
                  retries: int = 2, wait_secs: int = 2) -> list:
        if not snippets:
            return []
        block       = "\n\n".join(f"{i+1}. {s}" for i, s in enumerate(snippets))
        user_prompt = self.cfg.user_prompt.replace("<<snippets_block>>", block)

        for attempt in range(retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=self.cfg.model,
                    messages=[
                        {"role": "system", "content": self.cfg.system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_tokens=self.cfg.max_tokens,
                    temperature=0.0,
                )
                text = resp.choices[0].message.content.strip()
                # Strip markdown code fences if present
                text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
                text = re.sub(r"```$", "", text).strip()
                try:
                    parsed = json.loads(text)
                except Exception:
                    m = re.search(r'\[.*\]|\{.*\}', text, flags=re.DOTALL)
                    parsed = json.loads(m.group(0)) if m else None
                if parsed is None:
                    return []
                # Allow either a single object or a list; normalize to list.
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception as e:
                if attempt < retries:
                    time.sleep(wait_secs * (attempt + 1))
                else:
                    log(f"  ⚠ LLM call failed after retries: {e}")
                    return []
        return []

    # ── main entry point ─────────────────────────────────────────────────────
    def extract(
        self,
        client_name: str,
        api_key: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
        contracts: Optional[list] = None,
        core: str = "",
    ) -> dict:
        log    = progress_callback or (lambda msg: None)
        folder = (_INPUT_DIR / core / client_name) if core else (_INPUT_DIR / client_name)
        if not folder.exists():
            return {"status": "no_folder", "client": client_name}

        client = make_client(api_key or self.cfg.api_key)

        pdfs = sorted(set(list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))),
                      key=lambda p: p.name)
        pdfs = [p for p in pdfs if _pdf_filter(p.name)]
        # Scope-agent filter (if provided)
        if contracts is not None:
            wanted = {str(c) for c in contracts}
            pdfs   = [p for p in pdfs if p.name in wanted]

        log(f"Scanning {len(pdfs)} PDFs for {self.cfg.display}…")

        rows: list = []
        for p in pdfs:
            snippets, first_page, ocr_used = self._snippets_for_pdf(p, log)
            snippets = tu.dedupe_similar_snippets(snippets, threshold=70)

            row: dict = {
                "Filename":                p.name,
                "Contract Type":           _detect_contract_type(p.name),
                "Contract Effective Date": _parse_effective_date(p.name),
                "OCR":                     "Yes" if ocr_used else "No",
                "Page Number":             first_page if first_page else "",
            }
            # Initialize all clause-specific columns to "" so the xlsx is rectangular.
            for col in self.cfg.field_mapping.values():
                row[col] = ""
            row["Source Snippet"] = ""

            if not snippets:
                rows.append(row)
                continue

            log(f"  Analyzing {p.name} ({len(snippets)} snippet(s))…")
            results = self._call_llm(client, snippets, log)

            # The LLM may return multiple objects per contract — merge them.
            for jfield, xcol in self.cfg.field_mapping.items():
                vals = []
                for obj in results:
                    if isinstance(obj, dict):
                        v = obj.get(jfield)
                        if v not in (None, "", "null"):
                            vals.append(str(v).strip())
                row[xcol] = " | ".join(tu.unique_preserve_order(vals))

            # Source snippet — prefer LLM's chosen snippet if returned, else join raw.
            llm_snips = [str(o.get("snippet") or o.get("source_snippet") or "").strip()
                         for o in results if isinstance(o, dict)]
            llm_snips = [s for s in llm_snips if s]
            row["Source Snippet"] = " ||| ".join(tu.unique_preserve_order(llm_snips)) \
                                    if llm_snips else " ||| ".join(snippets[:3])

            rows.append(row)

        # Always write the file, even when empty, so downstream code can rely on it.
        cols = ["Filename", "Contract Type", "Contract Effective Date"] \
               + list(self.cfg.field_mapping.values()) \
               + ["Source Snippet", "OCR", "Page Number"]
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

        out_path = self.output_path(client_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(str(out_path), index=False)
        log(f"Wrote: {out_path.name} ({len(df)} rows)")

        return {
            "status": "complete",
            "client": client_name,
            "rows":   len(df),
            "output": str(out_path),
        }
