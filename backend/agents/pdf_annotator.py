"""
Unified PDF highlighter — replaces and generalises the two highlighter scripts
from Implementation Fee.ipynb.

Given a contract PDF and a list of `Highlight` specs (each with a string to match,
a color, and a tag), this walks every page, groups words into lines, and adds a
highlight annotation on each line that contains the target string.

Designed to be cheap and side-effect-free: no LLM calls, just text matching and
PDF annotation via PyMuPDF.  Safe to run on every contract during Load.

The chatbot uses this to produce a per-contract annotated PDF that lives at:
    Output/<ClientName>/highlighted/<contract_stem>.highlighted.pdf

Each agent contributes its own highlights:
  • Extraction → every extracted Item (line-level fees)
  • CPI        → the CPI / "Annual Adjustment" / "increased annually" snippets
  • Clause extractors → the Source Snippet for term/renewal/termination/SLA/volume

Colors (RGB 0-1) — chosen to be distinct under typical PDF rendering:
  Fee         : light yellow      (1.00, 0.95, 0.55)
  CPI         : light green       (0.70, 0.95, 0.70)
  Term        : light blue        (0.68, 0.85, 0.95)
  Termination : light red/pink    (0.98, 0.75, 0.78)
  SLA         : light purple      (0.85, 0.75, 0.95)
  Volume tiers: light orange      (1.00, 0.82, 0.55)
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz


# ── Colors (RGB 0-1) ─────────────────────────────────────────────────────────
COLORS = {
    "fee":         (1.00, 0.95, 0.55),
    "cpi":         (0.70, 0.95, 0.70),
    "term":        (0.68, 0.85, 0.95),
    "termination": (0.98, 0.75, 0.78),
    "sla":         (0.85, 0.75, 0.95),
    "volume":      (1.00, 0.82, 0.55),
}

# Light annotation settings
_Y_TOL   = 3.0
_PADDING = 1.5
_MIN_MATCH_CHARS = 4   # ignore very short matches that would highlight everything


@dataclass
class Highlight:
    text:  str             # raw text we want to find (case-insensitive substring)
    color: tuple           # (r, g, b), values in 0-1
    tag:   str = ""        # label shown in the annotation's title bar


# ── Text utilities (lightweight; not shared with _text_utils.py because the
#    matching mode here is "any substring on the line", not normalized tokens) ─

def _normalize(s: str) -> str:
    if not isinstance(s, str): return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r'\s+', ' ', s).strip()


def _group_words_into_lines(word_list, y_tol: float = _Y_TOL) -> list:
    words = [{"x0": w[0], "y0": w[1], "x1": w[2], "y1": w[3], "text": _normalize(w[4])}
             for w in word_list]
    words.sort(key=lambda w: (round(w["y0"], 2), w["x0"]))
    lines: list = []
    for w in words:
        if not lines:
            lines.append([w]); continue
        last = lines[-1]
        ys   = sorted(x["y0"] for x in last)
        med  = ys[len(ys) // 2]
        if abs(w["y0"] - med) <= y_tol:
            last.append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda x: x["x0"])
    return lines


def _bbox_for_line(words_in_line, padding: float = _PADDING):
    x0 = min(w["x0"] for w in words_in_line)
    y0 = min(w["y0"] for w in words_in_line)
    x1 = max(w["x1"] for w in words_in_line)
    y1 = max(w["y1"] for w in words_in_line)
    return fitz.Rect(x0 - padding, y0 - padding, x1 + padding, y1 + padding)


def _build_target_strings(highlights: list) -> list:
    """Prepare normalized lower-case substrings (filtered for length)."""
    out = []
    for h in highlights:
        norm = re.sub(r'\s+', ' ', (h.text or "").strip()).lower()
        if len(norm) >= _MIN_MATCH_CHARS:
            out.append((norm, h.color, h.tag))
    return out


def annotate(
    pdf_path: Path,
    highlights: list,
    out_path: Path,
) -> dict:
    """
    Highlight `highlights` in `pdf_path` and save annotated PDF to `out_path`.
    Returns {status, n_highlights, n_lines_checked}.
    """
    pdf_path = Path(pdf_path)
    out_path = Path(out_path)
    if not pdf_path.exists():
        return {"status": "no_input", "input": str(pdf_path)}

    targets = _build_target_strings(highlights)
    if not targets:
        # Nothing to highlight — copy the original through so the viewer link still works.
        try:
            doc = fitz.open(str(pdf_path))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            doc.save(str(out_path), garbage=4, deflate=True)
            doc.close()
            return {"status": "complete", "n_highlights": 0, "output": str(out_path)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        return {"status": "error", "error": str(e)}

    n_hits   = 0
    n_lines  = 0
    for pno in range(len(doc)):
        page = doc[pno]
        words = page.get_text("words")
        if not words:
            continue
        for ln_words in _group_words_into_lines(words):
            n_lines += 1
            line_norm = " ".join(w["text"] for w in ln_words).lower()
            if not line_norm.strip():
                continue
            for target_norm, color, tag in targets:
                if target_norm in line_norm:
                    try:
                        rect = _bbox_for_line(ln_words)
                        pr   = page.rect
                        rect = fitz.Rect(max(rect.x0, pr.x0), max(rect.y0, pr.y0),
                                         min(rect.x1, pr.x1), min(rect.y1, pr.y1))
                        if rect.is_empty: continue
                        annot = page.add_highlight_annot(rect)
                        annot.set_colors({"stroke": color})
                        if tag:
                            annot.set_info(title=tag)
                        annot.update()
                        n_hits += 1
                    except Exception:
                        pass
                    break   # one highlight per line is enough

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.save(str(out_path), garbage=4, deflate=True)
    except Exception:
        # Fall back to a temp file if direct save fails.
        tmp = out_path.with_name(out_path.stem + "_tmp.pdf")
        doc.save(str(tmp), garbage=4, deflate=True)
        tmp.replace(out_path)
    finally:
        doc.close()

    return {
        "status": "complete",
        "n_highlights": n_hits,
        "n_lines_checked": n_lines,
        "output": str(out_path),
    }


# ── High-level driver: read all agent outputs and build per-contract highlights ─

def highlights_for_client(client_name: str,
                          output_dir: Path,
                          input_dir:  Path) -> dict:
    """
    Walk every PDF in Input/<Client>/ and produce an annotated copy in
    Output/<Client>/highlighted/.  Highlights come from whichever agent outputs
    are present for that client (extraction, CPI matches, four clause extractors).

    Returns {client, pdfs_annotated, total_highlights, errors}.
    """
    import pandas as pd

    client_in   = input_dir  / client_name
    client_out  = output_dir / client_name
    highlight_dir = client_out / "highlighted"
    highlight_dir.mkdir(parents=True, exist_ok=True)

    if not client_in.exists():
        return {"status": "no_folder", "client": client_name}

    # ── Build a {contract_filename → [Highlight, ...]} map from agent outputs ──
    per_contract: dict = {}

    def _push(filename: str, h: Highlight):
        if not filename or not isinstance(filename, str): return
        # Strip any folder prefix the agents may have added
        key = Path(filename).name
        per_contract.setdefault(key, []).append(h)

    # Extraction → highlight each Item (yellow)
    extr = client_out / "extraction_output.xlsx"
    if extr.exists():
        try:
            df = pd.read_excel(str(extr))
            for _, row in df.iterrows():
                fname = str(row.get("Source Contract", "") or "")
                item  = str(row.get("Item", "") or "").strip()
                if fname and item and len(item) >= _MIN_MATCH_CHARS:
                    _push(fname, Highlight(item[:120], COLORS["fee"], "Extracted fee"))
        except Exception:
            pass

    # CPI matches → highlight the snippets / Annual Adjustment / "increased annually" (green)
    cpi_m = client_out / f"{client_name} CPI_matches.xlsx"
    if cpi_m.exists():
        try:
            df = pd.read_excel(str(cpi_m))
            for _, row in df.iterrows():
                fname = str(row.get("Filename", "") or "")
                # Filename in the matches file may be stem-only — re-attach .pdf
                if fname and not fname.lower().endswith((".pdf", ".PDF")):
                    fname = fname + ".pdf"
                snip  = str(row.get("CPI Snippets (LLM)", "") or "").strip()
                # Snippets are joined by " ||| " — split and use chunks.
                for part in snip.split("|||"):
                    p = part.strip()
                    if len(p) >= _MIN_MATCH_CHARS:
                        # Use the first sentence as a stable substring target
                        first_sent = re.split(r'[.!?]\s', p, maxsplit=1)[0]
                        _push(fname, Highlight(first_sent[:120], COLORS["cpi"], "CPI"))
        except Exception:
            pass

    # Clause extractors → highlight the Source Snippet (color by clause)
    _CLAUSE_FILES = [
        ("term_renewal_output.xlsx", "term",        "Term/Renewal"),
        ("termination_output.xlsx",  "termination", "Termination"),
        ("sla_output.xlsx",          "sla",         "SLA"),
        ("volume_tiers_output.xlsx", "volume",      "Volume tier"),
    ]
    for fname_xlsx, color_key, tag in _CLAUSE_FILES:
        p = client_out / fname_xlsx
        if not p.exists(): continue
        try:
            df = pd.read_excel(str(p))
            for _, row in df.iterrows():
                ct_fname = str(row.get("Filename", "") or "")
                snip     = str(row.get("Source Snippet", "") or "").strip()
                if not ct_fname or not snip: continue
                for part in snip.split("|||"):
                    p_ = part.strip()
                    if len(p_) >= _MIN_MATCH_CHARS:
                        first_sent = re.split(r'[.!?]\s', p_, maxsplit=1)[0]
                        _push(ct_fname, Highlight(first_sent[:120], COLORS[color_key], tag))
        except Exception:
            pass

    # ── Annotate each PDF (every PDF gets a copy, even if no highlights) ──
    total_hits = 0
    errors     = []
    annotated  = 0
    pdfs = sorted(set(list(client_in.glob("*.pdf")) + list(client_in.glob("*.PDF"))),
                  key=lambda x: x.name)
    for pdf in pdfs:
        hl   = per_contract.get(pdf.name, [])
        outp = highlight_dir / f"{pdf.stem}.highlighted.pdf"
        result = annotate(pdf, hl, outp)
        if result.get("status") == "complete":
            annotated  += 1
            total_hits += result.get("n_highlights", 0)
        else:
            errors.append({"pdf": pdf.name, "error": result.get("error", result.get("status"))})

    return {
        "status":           "complete",
        "client":           client_name,
        "pdfs_annotated":   annotated,
        "total_highlights": total_hits,
        "errors":           errors,
        "highlight_dir":    str(highlight_dir),
    }


def is_processed(client_name: str, output_dir: Path) -> bool:
    """Treat a client as 'annotated' if a highlighted/ folder exists with at least one PDF."""
    d = output_dir / client_name / "highlighted"
    if not d.exists(): return False
    return any(d.glob("*.pdf"))
