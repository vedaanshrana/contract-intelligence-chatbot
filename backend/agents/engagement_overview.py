"""
Engagement Overview Agent — Phase 1 of the contract-analysis pipeline.

Replaces the previous Master Contract agent. The logic here is ported from the
standalone Clarity Contract Extractor (mary bush frontend ui/app.py, the
"latest" script per the user) but adapted to:

  • Use fiserv_client.make_client(...) so token usage is metered automatically.
  • Read MASTER_CONTRACT_API_KEY / MASTER_CONTRACT_MODEL from config.py.
  • Write one Excel row per contract (Output/<Client>/engagement_overview_output.xlsx)
    instead of a per-contract Key/Value sheet (the chatbot's downstream
    context_builder iterates rows).

What this agent does (Phase 1 only — Phase 2 lives in product_module.py now):
  1. Phase 1     — scope, parties, signatories, DocuSign id, effective date,
                   contract summary.
  2. Phase 1.5   — schedule manifest + document_type classification (MA /
                   Amendment / SOW / Order / LicenseAgreement / Other).
                   Used here only to populate the "Document Type" column and
                   to know whether to fire the SOW supplement.
  3. Phase 1 SOW supplement (conditional)
                  — 7 extra fields when document_type == "SOW".

Product Module Agent (agents/product_module.py) imports the helpers below
(pdf_to_images, select_phase1_pages, call_vision, …) so we don't duplicate
~400 lines of utility code. Keeping these helpers public on this module is
intentional.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Callable, Literal, Optional

import fitz                                        # PyMuPDF
import pandas as pd
from PIL import Image
from pydantic import BaseModel, Field

from fiserv_client import make_client
from config import MASTER_CONTRACT_API_KEY, MASTER_CONTRACT_MODEL

fitz.TOOLS.mupdf_display_errors(False)


# ── Module-level paths ────────────────────────────────────────────────────────
_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"


# ── Render / chunking constants (match app.py) ────────────────────────────────
DPI               = 300
PHASE1_N_FRONT    = 4
PHASE1_N_BACK     = 4
API_TIMEOUT       = 180
MAX_RETRIES       = 5


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas (verbatim from app.py)
# ─────────────────────────────────────────────────────────────────────────────

class Phase1Scope(BaseModel):
    """Phase 1 — Scope. One record per contract."""
    type_of_agreement: str = Field(
        description=(
            "Agreement type: 'Master Agreement', 'Amendment Agreement', 'Statement of Work', etc. "
            "If Amendment, ALSO mention the Master Agreement being amended, "
            "e.g., 'Amendment Agreement to Master Agreement dated 2018-01-15'."
        )
    )
    client_name:              str = Field(description="Full legal name of the client / financial institution.")
    client_address:           str = Field(description="Full mailing address of the client. Empty if not found.")
    service_provider_name:    str = Field(description="Full legal name of the service provider (e.g., Fiserv).")
    service_provider_address: str = Field(description="Full mailing address of the provider. Empty if not found.")

    client_signatory_name:    str = Field(description="Printed name of the client signatory.")
    client_signatory_title:   str = Field(description="Title of the client signatory (e.g., 'CEO').")
    client_signatory_date:    str = Field(description="Date the client signed (as printed). Empty if not found.")

    provider_signatory_name:  str = Field(description="Printed name of the provider signatory.")
    provider_signatory_title: str = Field(description="Title of the provider signatory.")
    provider_signatory_date:  str = Field(description="Date the provider signed (as printed). Empty if not found.")

    docusign_envelope_id: str = Field(
        description=(
            "DocuSign Envelope ID. Look in the TOP-LEFT or TOP-RIGHT of each page "
            "for text like 'DocuSign Envelope ID: ABC1234-...'. The ID is a UUID-"
            "like alphanumeric string (8-4-4-4-12 hex pattern). "
            "Return the bare ID string only (no 'DocuSign Envelope ID:' prefix). "
            "If the contract was NOT signed via DocuSign or no such marker is "
            "visible on any page, return the literal string 'NA'."
        )
    )
    effective_date: str = Field(
        description=(
            "Contract effective date. If not explicit, INFER from 'effective as of' clauses "
            "or the latest signature date. Empty only if truly indeterminable."
        )
    )
    contract_summary: str = Field(
        description=(
            "ONE paragraph (4-6 sentences) summarising the contract — what it is, "
            "what services it covers, term & renewal posture, and notable special terms. "
            "If amendment: mention what it changes. Keep it plain English, single paragraph."
        )
    )


class ScheduleManifestItem(BaseModel):
    """One row in the Phase 1.5 schedule manifest."""
    parent: Literal["Services Exhibit", "Software Exhibit"] = Field(
        description="The governing Exhibit (only these two are tracked)."
    )
    schedule_title: str = Field(
        description=(
            "Exact title of the Schedule as it appears in the contract, e.g., "
            "'Account Processing Services (Portico)', 'Wisdom Software'. "
            "Title only — do NOT append ' Schedule to Services/Software Exhibit'."
        )
    )


class ScheduleManifest(BaseModel):
    """Phase 1.5 — document type + product-Schedule inventory."""
    document_type: Literal[
        "MasterAgreement", "Amendment", "SOW", "Order",
        "LicenseAgreement", "Other",
    ] = Field(
        description=(
            "Overall document type. Drives downstream Phase 2 routing.\n"
            "  • 'MasterAgreement' — full master with Services/Software Exhibits\n"
            "  • 'Amendment'       — amends an existing master agreement\n"
            "  • 'SOW'             — Statement of Work for a specific project\n"
            "  • 'Order'           — Subsequent Order / Purchase Order / hardware order\n"
            "  • 'LicenseAgreement'— Software License Agreement with checkbox-style "
            "module lists (or 'License and Service Agreement' / 'Software License "
            "Agreement' / 'Master License Agreement' / 'License Agreement' in title)\n"
            "  • 'Other'           — last resort"
        )
    )
    schedules: list[ScheduleManifestItem]


class Phase1SowSupplement(BaseModel):
    """7 extra fields populated only when document_type == 'SOW'."""
    client_email: str = Field(
        description="Client's contact email address. 'NA' if absent."
    )
    service_provider_email: str = Field(
        description="Service provider's contact email address. 'NA' if absent."
    )
    client_number: str = Field(
        description="Client's account / customer ID. 'NA' if absent."
    )
    project_reference_number: str = Field(
        description="Project reference number / project ID / SOW number. 'NA' if absent."
    )
    project_name: str = Field(
        description="Official name of the project as titled in the SOW. 'NA' if absent."
    )
    sow_creator: str = Field(
        description="Name of the SOW author. 'NA' if absent."
    )
    sow_expiration: str = Field(
        description="Expiration / valid-through date or phrase. 'NA' if absent."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompts (verbatim from app.py)
# ─────────────────────────────────────────────────────────────────────────────

PHASE1_SYSTEM_PROMPT = """
You are a forensic contract analyst. You are looking at BOTH the FRONT MATTER
and the SIGNATURE PAGE of a contract. The images include the first few pages
(scope, recitals, parties) and the LAST few pages (signature block).

EXTRACT into the strict schema provided:

1. type_of_agreement — pick the precise label. If AMENDMENT, name the Master
   Agreement being amended.

2. client_name / client_address      — financial institution receiving services.
3. service_provider_name / address   — the provider (often Fiserv).

4. SIGNATORIES (six fields — three per side):
     • client_signatory_name / title / date
     • provider_signatory_name / title / date
   The signature block is on the LAST few images provided. Parse it carefully:
     • Names appear under 'By:' or 'Name:' lines
     • Titles on 'Title:' lines
     • Dates on 'Date:' lines
   Empty string only if genuinely absent.

5. docusign_envelope_id — Look in the TOP-LEFT or TOP-RIGHT margin of EVERY
   page for text reading "DocuSign Envelope ID: <ID>". The ID is typically a
   UUID-like string in 8-4-4-4-12 hex format. Return the BARE ID only (no
   "DocuSign Envelope ID:" prefix). If absent on every page → "NA".

6. effective_date — look for 'effective as of', 'Agreement Date', or 'effective
   from'. If not explicit, INFER from the later of the two signature dates.
   If genuinely not stated and cannot be inferred, output "NA".

7. contract_summary
   ONE paragraph (4-6 sentences) summarising the contract, services, term,
   and renewal posture. Plain English, single paragraph — no bullet list.

Use ONLY information visible in the images. Return ONLY the JSON object.
""".strip()


PHASE1_5_MANIFEST_PROMPT = """
You are looking at the FRONT MATTER of a contract.

Your tasks:
  (A) Classify the document_type.
  (B) Build the SCHEDULE MANIFEST — every product Schedule appearing under the
      Services Exhibit and Software Exhibit (only relevant when
      document_type == "MasterAgreement"; output empty list otherwise).

═══ TASK A: document_type ═══
Pick the value that best describes this document:
  • "MasterAgreement" — full Fiserv master with Services / Software Exhibits
    containing multiple product Schedules.
  • "Amendment" — modifies an existing master agreement.
  • "SOW" — Statement of Work for ONE specific project. Look for "Statement
    of Work" or "SOW" in the title.
  • "Order" — Subsequent Order / Purchase Order / similar short procurement
    document, usually <20 pages with a central table of items.
  • "LicenseAgreement" — software License Agreement (or License Exhibit). PICK
    THIS aggressively when ANY of these signals appear, even if you can't see
    the checkbox content directly:
        1) TITLE PATTERN — front matter contains "License and Service
           Agreement", "Software License Agreement", "Master License
           Agreement", or bare "License Agreement" (not "Master Services").
        2) EXHIBIT NAMING — exhibits labeled "Exhibit 1a / 1b / 1c / 2a / …".
        3) CHECKBOX HALLMARK — section titled "Modules & Total License Fee"
           or intro text "boxes marked below with an 'X' indicate the
           Software licensed by the [Client]".
        4) PLATFORM-CENTRIC STRUCTURE — licenses ONE software platform plus
           surrounding services (Professional Services, Basic Maintenance,
           etc.).
    Seeing just the title page that says "License and Service Agreement" or
    "Software License Agreement" is enough.
  • "Other" — last resort.

═══ TASK B: schedule manifest ═══
Only populate `schedules` when document_type == "MasterAgreement". For
SOW / Order / Amendment / LicenseAgreement / Other → empty list.

For each schedule:
  • parent: "Services Exhibit" or "Software Exhibit"
  • schedule_title: bare title as printed (e.g., "Account Processing Services
                    (Portico)", "Wisdom Software")

DO NOT include the Master Agreement itself, Amendments, Service Level Exhibit,
Client Support Exhibit, Equipment Terms, or Professional Services Terms.

Return ONLY the JSON object.
""".strip()


PHASE1_SOW_SUPPLEMENT_PROMPT = """
You are looking at the FRONT MATTER of a Statement of Work (SOW). Extract
these 7 SOW-specific fields. If a field is genuinely absent, return the
literal string "NA" — do NOT leave it blank.

  • client_email             — client's contact email
  • service_provider_email   — Fiserv project manager's email
  • client_number            — client account / customer ID
  • project_reference_number — project ID / SOW number
  • project_name             — the SOW's project name
  • sow_creator              — name of the SOW author
  • sow_expiration           — SOW expiration date or validity window

Return ONLY the JSON object defined by the schema.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# PDF rendering helpers (shared with product_module.py via import)
# ─────────────────────────────────────────────────────────────────────────────

def pdf_to_images(pdf_path: str | Path, dpi: int = DPI) -> list[dict]:
    """Render every PDF page to a PIL Image AND extract its text layer.
    Returns: list of dicts with keys page_number / image / text."""
    doc = fitz.open(str(pdf_path))
    out: list[dict] = []
    for i in range(len(doc)):
        page = doc.load_page(i)
        mat  = fitz.Matrix(dpi / 72, dpi / 72)
        pix  = page.get_pixmap(matrix=mat)
        img  = Image.open(BytesIO(pix.tobytes("png")))
        try:
            text = page.get_text("text") or ""
        except Exception:
            text = ""
        out.append({"page_number": i + 1, "image": img, "text": text})
    doc.close()
    return out


def image_to_b64(img) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Signature-page detection (used to pick Phase 1 image set)
# ─────────────────────────────────────────────────────────────────────────────

SIGNATURE_KEYWORDS = [
    "in witness whereof",
    "intending to be legally bound",
    "the parties have executed",
    "signed by:",
    "authorized signatory",
    "authorised signatory",
]
SIGNATURE_WEAK_PATTERNS = [
    r"^\s*by\s*:",
    r"^\s*name\s*:",
    r"^\s*title\s*:",
    r"^\s*signature\s*:",
    r"^\s*date\s*:",
]


def score_signature_page(text: str) -> int:
    if not text:
        return 0
    score = 0
    low = text.lower()
    for kw in SIGNATURE_KEYWORDS:
        if kw in low:
            score += 5
    weak_hits = 0
    for pat in SIGNATURE_WEAK_PATTERNS:
        if re.search(pat, low, re.MULTILINE | re.IGNORECASE):
            weak_hits += 1
    if weak_hits >= 2:
        score += 3 * weak_hits
    return score


def find_signature_pages(pages: list[dict], min_score: int = 5,
                         max_pages: int = 4) -> list[dict]:
    scored = [(score_signature_page(p.get("text", "")), p) for p in pages]
    candidates = [(s, p) for (s, p) in scored if s >= min_score]
    if not candidates:
        return []
    candidates.sort(key=lambda sp: (-sp[0], sp[1]["page_number"]))
    return [p for (_, p) in candidates[:max_pages]]


def select_phase1_pages(pages: list[dict],
                        n_front: int = PHASE1_N_FRONT,
                        n_back:  int = PHASE1_N_BACK) -> list[dict]:
    """Front n_front + detected signature pages (fallback: last n_back)."""
    if len(pages) <= n_front + n_back:
        return pages
    front     = pages[:n_front]
    sig_pages = find_signature_pages(pages, min_score=5, max_pages=n_back)
    sources   = front + (sig_pages if sig_pages else pages[-n_back:])
    seen: set[int] = set()
    out: list[dict] = []
    for p in sources:
        if p["page_number"] not in seen:
            seen.add(p["page_number"])
            out.append(p)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LLM call helpers (shared with product_module.py)
# ─────────────────────────────────────────────────────────────────────────────

def is_gpt5_family(model_name: str) -> bool:
    """gpt-5 family rejects temperature != 1 → must omit the parameter."""
    m = (model_name or "").lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3")


def pydantic_to_strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic → OpenAI strict json_schema (all properties required,
    additionalProperties=False everywhere)."""
    schema = model.model_json_schema()

    def _harden(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            for v in node.values():
                _harden(v)
        elif isinstance(node, list):
            for v in node:
                _harden(v)

    _harden(schema)
    return schema


def _backend_is_fiserv() -> bool:
    return (os.environ.get("OPENAI_BACKEND") or "").lower() == "fiserv"


def _build_response_format(schema: dict, schema_name: str) -> dict:
    """OpenAI supports json_schema strict; the Fiserv Foundation gateway
    doesn't reliably enforce strict schemas, so we downgrade to json_object
    on that backend. Pydantic validation still catches schema drift."""
    if _backend_is_fiserv():
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema, "strict": True},
    }


def call_with_retry(fn, max_retries: int = MAX_RETRIES, label: str = ""):
    """Exponential backoff for 429 + transient network errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            is_rate = "429" in str(e) or "rate" in msg or "too many requests" in msg
            is_net  = any(k in msg for k in (
                "connection", "timeout", "timed out", "remotedisconnected",
                "apiconnection", "read timed out", "ssl", "broken pipe",
                "reset by peer",
            ))
            if attempt == max_retries - 1:
                raise
            if is_rate:
                time.sleep(20 * (2 ** attempt))
            elif is_net:
                time.sleep(5 * (2 ** attempt))
            else:
                raise
    raise RuntimeError(f"call_with_retry exhausted ({label})")


def call_vision(client, *, model: str, system_prompt: str,
                pages: list[dict], response_model: type[BaseModel],
                schema_name: str, label: str = "",
                extra_user_instruction: str = "") -> BaseModel:
    """Send pages to the vision model with strict-JSON enforcement, parse
    via Pydantic. Works on OpenAI and Fiserv FoundationClient via the
    chat.completions.create surface."""
    user_text = (
        f"The following {len(pages)} image(s) are sequential pages from a "
        f"contract. Page numbers (1-indexed within the document): "
        f"{[p['page_number'] for p in pages]}."
    )
    if extra_user_instruction:
        user_text += "\n\n" + extra_user_instruction

    content: list = [{"type": "text", "text": user_text}]
    for p in pages:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{image_to_b64(p['image'])}"
            },
        })

    schema = pydantic_to_strict_schema(response_model)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": content},
    ]

    kwargs = {
        "model":            model,
        "messages":         messages,
        "response_format":  _build_response_format(schema, schema_name),
        "timeout":          API_TIMEOUT,
    }
    if not is_gpt5_family(model):
        kwargs["temperature"] = 0

    resp = call_with_retry(
        lambda: client.chat.completions.create(**kwargs),
        label=label,
    )
    raw  = resp.choices[0].message.content
    data = json.loads(raw)
    return response_model.model_validate(data)


# ─────────────────────────────────────────────────────────────────────────────
# Phase runners
# ─────────────────────────────────────────────────────────────────────────────

def run_phase1(client, model: str, pages: list[dict], log: Callable[[str], None]
               ) -> Phase1Scope:
    subset = select_phase1_pages(pages)
    log(f"  Phase 1: sending {len(subset)} pages "
        f"{[p['page_number'] for p in subset]}")
    return call_vision(
        client, model=model,
        system_prompt=PHASE1_SYSTEM_PROMPT,
        pages=subset,
        response_model=Phase1Scope,
        schema_name="phase1_scope",
        label="phase1",
    )


def run_phase1_5_manifest(client, model: str, pages: list[dict],
                          log: Callable[[str], None]) -> ScheduleManifest:
    subset = select_phase1_pages(pages)
    log(f"  Phase 1.5: classifying document_type + schedule manifest "
        f"({len(subset)} front-matter pages)")
    return call_vision(
        client, model=model,
        system_prompt=PHASE1_5_MANIFEST_PROMPT,
        pages=subset,
        response_model=ScheduleManifest,
        schema_name="phase1_5_manifest",
        label="phase1.5",
    )


def run_phase1_sow_supplement(client, model: str, pages: list[dict],
                              log: Callable[[str], None]) -> Phase1SowSupplement:
    subset = select_phase1_pages(pages)
    log(f"  Phase 1 (SOW supplement): extracting 7 SOW-specific fields")
    return call_vision(
        client, model=model,
        system_prompt=PHASE1_SOW_SUPPLEMENT_PROMPT,
        pages=subset,
        response_model=Phase1SowSupplement,
        schema_name="phase1_sow_supplement",
        label="phase1.sow",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Document-type overrides (License Agreement detection from title)
# ─────────────────────────────────────────────────────────────────────────────

_LICENSE_PHRASES = (
    "license and service agreement",
    "software license agreement",
    "master license agreement",
)


def apply_license_override(manifest: ScheduleManifest,
                           phase1: Optional[Phase1Scope],
                           pages: list[dict],
                           log: Callable[[str], None]) -> ScheduleManifest:
    """If Phase 1 or the page-1 text-layer says 'License Agreement', force the
    manifest's document_type to 'LicenseAgreement' (and clear schedules so
    downstream Phase 2 takes the LICENSE path). Ported from app.py."""
    hint = ""
    if phase1 is not None and getattr(phase1, "type_of_agreement", ""):
        hint = phase1.type_of_agreement or ""
    elif pages and pages[0].get("text"):
        hint = pages[0]["text"][:2000] or ""

    low = hint.lower()
    bare_license = (
        "license agreement" in low
        and "master services" not in low
        and "account processing" not in low
    )
    if any(p in low for p in _LICENSE_PHRASES) or bare_license:
        if manifest.document_type != "LicenseAgreement":
            log(f"  ⚠ Manifest said {manifest.document_type!r}, but title "
                f"({hint[:80]!r}) indicates a License Agreement — overriding.")
        manifest.document_type = "LicenseAgreement"
        manifest.schedules     = []
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# Filename heuristics (kept for backwards-compat columns)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_contract_type(name: str) -> str:
    low = name.lower()
    if "master agreement" in low or "master" in low: return "Master Agreement"
    if "amendment" in low or "amend" in low:         return "Amendment"
    if "services"  in low:                           return "Services"
    return ""


def _parse_effective_date_from_filename(name: str) -> str:
    m = re.search(r'(\d{1,2}[_-]\d{1,2}[_-]\d{4})', name)
    return m.group(1) if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# Output schema (one row per contract — chatbot/context_builder reads rows)
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_COLS = [
    # Backwards-compat columns the chatbot context_builder reads
    "Filename", "Contract Type", "Contract Effective Date", "Document Type",
    # Richer scope fields from Phase 1
    "Type of Agreement", "Effective Date", "DocuSign Envelope ID",
    "Client Name", "Client Address", "Provider Name", "Provider Address",
    "Client Signatory Name",   "Client Signatory Title",   "Client Signatory Date",
    "Provider Signatory Name", "Provider Signatory Title", "Provider Signatory Date",
    "Contract Summary",
    # SOW-only — empty for non-SOWs
    "Project Name", "Project Reference Number", "SOW Creator", "SOW Expiration",
    "Client Email", "Provider Email", "Client Number",
    # Bookkeeping
    "Pages Sent",
]


def _blank_row(filename: str) -> dict:
    """Empty placeholder row used when a contract fails."""
    row = {c: "" for c in OUTPUT_COLS}
    row["Filename"]                 = filename
    row["Contract Type"]            = _detect_contract_type(filename)
    row["Contract Effective Date"]  = _parse_effective_date_from_filename(filename)
    return row


def _row_from_phase1(filename: str,
                     phase1: Phase1Scope,
                     manifest: Optional[ScheduleManifest],
                     sow: Optional[Phase1SowSupplement],
                     pages_sent: list[int]) -> dict:
    row = _blank_row(filename)
    row["Document Type"]              = manifest.document_type if manifest else ""
    row["Type of Agreement"]          = phase1.type_of_agreement
    row["Effective Date"]             = phase1.effective_date
    row["DocuSign Envelope ID"]       = phase1.docusign_envelope_id
    row["Client Name"]                = phase1.client_name
    row["Client Address"]             = phase1.client_address
    row["Provider Name"]              = phase1.service_provider_name
    row["Provider Address"]           = phase1.service_provider_address
    row["Client Signatory Name"]      = phase1.client_signatory_name
    row["Client Signatory Title"]     = phase1.client_signatory_title
    row["Client Signatory Date"]      = phase1.client_signatory_date
    row["Provider Signatory Name"]    = phase1.provider_signatory_name
    row["Provider Signatory Title"]   = phase1.provider_signatory_title
    row["Provider Signatory Date"]    = phase1.provider_signatory_date
    row["Contract Summary"]           = phase1.contract_summary
    if sow is not None:
        row["Project Name"]              = sow.project_name
        row["Project Reference Number"]  = sow.project_reference_number
        row["SOW Creator"]               = sow.sow_creator
        row["SOW Expiration"]            = sow.sow_expiration
        row["Client Email"]              = sow.client_email
        row["Provider Email"]            = sow.service_provider_email
        row["Client Number"]             = sow.client_number
    row["Pages Sent"]                 = ", ".join(str(n) for n in pages_sent)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(
    client_name: str,
    api_key: str = "",
    model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    contracts: Optional[list] = None,
    core: str = "",
) -> dict:
    """Run Phase 1 + 1.5 (+ SOW supplement when applicable) for every PDF
    under ``client_name``.

    Output: ``Output/<client>/engagement_overview_output.xlsx`` — one row per
    contract, columns = ``OUTPUT_COLS``."""
    folder = (_INPUT_DIR / core / client_name) if core else (_INPUT_DIR / client_name)
    if not folder.exists():
        return {"status": "no_folder", "client": client_name}

    log = progress_callback or (lambda m: None)
    api_key = api_key or MASTER_CONTRACT_API_KEY
    model   = model   or MASTER_CONTRACT_MODEL

    pdfs = sorted(
        set(list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))),
        key=lambda p: p.name,
    )
    if contracts is not None:
        wanted = {str(c) for c in contracts}
        pdfs   = [p for p in pdfs if p.name in wanted]
    if not pdfs:
        return {"status": "no_pdfs", "client": client_name}

    log(f"Engagement Overview ({model}): {len(pdfs)} PDF(s)…")
    api = make_client(api_key)

    rows: list[dict] = []
    for i, p in enumerate(pdfs):
        log(f"  [{i + 1}/{len(pdfs)}] {p.name}")
        try:
            pages = pdf_to_images(p)
            if not pages:
                raise RuntimeError("no pages rendered")

            phase1 = run_phase1(api, model, pages, log)

            try:
                manifest = run_phase1_5_manifest(api, model, pages, log)
            except Exception as e:
                log(f"  ⚠ Phase 1.5 failed: {type(e).__name__}: {e} — "
                    f"defaulting to Other / no schedules")
                manifest = ScheduleManifest(document_type="Other", schedules=[])

            manifest = apply_license_override(manifest, phase1, pages, log)

            sow_supp: Optional[Phase1SowSupplement] = None
            if manifest.document_type == "SOW":
                try:
                    sow_supp = run_phase1_sow_supplement(api, model, pages, log)
                except Exception as e:
                    log(f"  ⚠ SOW supplement failed: {type(e).__name__}: {e}")
                    sow_supp = None

            page_nums = [pg["page_number"] for pg in select_phase1_pages(pages)]
            rows.append(_row_from_phase1(p.name, phase1, manifest, sow_supp,
                                         page_nums))

        except Exception as e:
            log(f"    ⚠ Failed on {p.name}: {type(e).__name__}: {e}")
            rows.append(_blank_row(p.name))

    out_dir = _OUTPUT_DIR / client_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "engagement_overview_output.xlsx"

    pd.DataFrame(rows, columns=OUTPUT_COLS).to_excel(str(out_path), index=False)
    log(f"Wrote: {out_path.name} ({len(rows)} contract rows)")

    return {
        "status":  "complete",
        "client":  client_name,
        "rows":    len(rows),
        "output":  str(out_path),
    }


def is_processed(client_name: str) -> bool:
    p = _OUTPUT_DIR / client_name / "engagement_overview_output.xlsx"
    return p.exists() and p.stat().st_size > 1_000


def output_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "engagement_overview_output.xlsx"
