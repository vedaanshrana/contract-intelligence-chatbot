"""
Product Module Agent — Phase 2 of the contract-analysis pipeline.

Independent of Engagement Overview now: this agent renders the PDFs, runs its
own Phase 1.5 manifest call to decide routing (STRICT / SOW / ORDER / LICENSE
/ GENERIC), runs the appropriate Phase 2 sub-runner, cleans the rows, and
writes ``Output/<Client>/product_hierarchy_output.xlsx``.

All logic is ported from ``mary bush frontend ui/app.py`` (the "latest" script
per the user). PDF rendering, page selection, the vision-call wrapper, and
the Phase 1.5 manifest schema/prompt are imported from
:mod:`agents.engagement_overview` to avoid duplicating ~400 lines of helpers.

Public surface (same shape as every other agent):
    run(client_name, api_key="", model="", progress_callback=None,
        contracts=None, core="") -> dict
    is_processed(client_name) -> bool
    output_path(client_name) -> Path
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Literal, Optional

import pandas as pd
from pydantic import BaseModel, Field

from fiserv_client import make_client
from config import MASTER_CONTRACT_API_KEY, MASTER_CONTRACT_MODEL

# Shared helpers + Phase 1.5 schema/prompt come from the Overview agent.
from . import engagement_overview as _eo
from .engagement_overview import (
    ScheduleManifest,
    ScheduleManifestItem,        # noqa: F401  (re-exported for callers)
    pdf_to_images,
    select_phase1_pages,
    call_vision,
    run_phase1_5_manifest,
    apply_license_override,
)


# ── Module-level paths ────────────────────────────────────────────────────────
_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"

# Phase 2 page-chunking constant (matches app.py)
CHUNK_SIZE                       = 8
LICENSE_SINGLE_CALL_MAX_PAGES    = 24


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas (verbatim from app.py)
# ─────────────────────────────────────────────────────────────────────────────

class Phase2Row(BaseModel):
    """Phase 2 — Products & Modules. 4-column document hierarchy."""
    parent: str = Field(
        description=(
            "Owning document tier with suffixes stripped. "
            "Output 'Services Exhibit', NOT 'Services Exhibit to Master Agreement'."
        )
    )
    level: Literal[
        "Master Agreement", "Amendment", "Schedule", "Attachment",
        "Exhibit", "Addendum", "Schedule A", "Schedule B"
    ] = Field(
        description="DOCUMENT HIERARCHY ONLY. NEVER output 'Product' or 'Module' as a Level."
    )
    product: str = Field(
        description="High-level product / schedule name (e.g., 'Account Processing Services (Portico)')."
    )
    module: str = Field(
        description=(
            "SECTION HEADER under the Product (e.g., 'Base Services', 'Equipment'). "
            "Identified by visual indentation. Do NOT extract sub-bullets as Modules."
        )
    )


class Phase2Result(BaseModel):
    rows: list[Phase2Row]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 prompts (verbatim from app.py — five paths)
# ─────────────────────────────────────────────────────────────────────────────

PHASE2_STRICT_SYSTEM_PROMPT = """
You are extracting the PRODUCT & MODULE HIERARCHY from a Fiserv contract.

═══════════════════ CONTEXT (READ THIS FIRST) ═══════════════════
Every contract you will see is a Fiserv master agreement, but the underlying
PRODUCT varies. Fiserv sells many account-processing platforms (Portico, DNA,
Premier, Spectrum, Cleartouch, Signature, AFTECH, OPUS, ...) plus card,
payments, software, and other product lines. Use the EXACT product and module
names that appear in THIS contract.

The legal STRUCTURE is the same across all Fiserv contracts:

    Master Agreement
      └── Services Exhibit         ──┐
            └── <Product> Schedule  ├── product-bearing exhibits
      └── Software Exhibit         ──┘
            └── <Software> Schedule
      └── Service Level Exhibit       (non-product; ignore for Phase 2)
      └── Client Support Exhibit      (non-product; ignore for Phase 2)
      └── Other exhibits              (non-product; ignore for Phase 2)

═══════════════════ COLUMN DEFINITIONS ═══════════════════
You will output a row with EXACTLY these 4 columns: Parent, Level, Product, Module.

• Parent  — 'Services Exhibit' OR 'Software Exhibit'. Normalize 'Service
            Exhibit' (singular) to 'Services Exhibit'.

• Level   — Always 'Schedule'.

• Product — Full Schedule title with exhibit suffix:
              FORMAT: '<Schedule Title> Schedule to <Parent Exhibit>'
            EVERY row's Product MUST end with ' Schedule to Services Exhibit'
            OR ' Schedule to Software Exhibit'.

• Module  — A NAMED product or service offering inside this Schedule. See the
            granularity rules below — they DIFFER between Services Exhibit and
            Software Exhibit schedules.

═══════════════════ ★★★ GRANULARITY DEPENDS ON THE PARENT EXHIBIT ★★★ ═══════════════════

★ RULE A — SERVICES EXHIBIT SCHEDULES ★
   (Product ends with ' Schedule to Services Exhibit')

   Your job: identify every NAMED SERVICE CATEGORY inside the schedule.
   These categories appear in one of several patterns. You MUST recognize
   ALL of these patterns and extract Modules from whichever one(s) apply.

   PATTERN A1 — Lettered sub-sections under "1. Services":
       1. Services. Fiserv agrees to provide ...
          (a) <Service Category>: ...
          (b) <Service Category>: ...
       → Emit ONE Module per (a)/(b)/(c) lettered service category.

   PATTERN A2 — Bold sub-headings inside a "Services" section (NO letter labels):
       Services
       Fiserv will provide Client with the following services:
         <Bold Sub-Heading>. Description ...
         <Bold Sub-Heading>. Description ...
       → Emit ONE Module per BOLD/UNDERLINED sub-heading under "Services".

   PATTERN A3 — Standalone named sections after "1. Services":
       1. Services. ...
       2. Interfaces.
       3. Optional Services.
       → Each named top-level numbered section IS a Module.

   PATTERN A4 — Single-product-with-sub-services (umbrella exception):
       Some Services schedules describe ONE or TWO umbrella offerings with
       sub-list of flavors/variants. Emit only top-level umbrellas. Do NOT
       extract the sub-bullets as separate Modules.

   PATTERN A5 — Schedule body is sparse; named services in attachments:
       Extract the named modules from wherever they are clearly stated.
       Do NOT skip the schedule just because its body is brief.

   ★ RULE A — UNIVERSAL CONSTRAINT ★
   Regardless of pattern, you should typically emit BETWEEN 1 and 15 Modules
   per Services Exhibit schedule. If you would emit zero Modules for a
   manifest-listed Services Exhibit schedule, you are missing the pattern —
   re-read for Pattern A2 (bold sub-headings) or Pattern A5.

   DO NOT drill into Optional Services pricing tables for sub-categories —
   those are pricing breakdowns INSIDE the Optional Services Module.

★ RULE B — SOFTWARE EXHIBIT SCHEDULES ★
   (Product ends with ' Schedule to Software Exhibit')

   The Schedule body usually has a structure like:
       1. LICENSE AND MAINTENANCE.
          (a)-(f) operational/billing sub-headings ...
       2. PROFESSIONAL SERVICES. ...

   For Software Exhibit Schedules, emit ONE Module per TOP-LEVEL NUMBERED
   SECTION ONLY. Do NOT drill into (a)/(b)/(c) sub-headings — those are
   operational/billing details. Do NOT extract any Modules from fee
   attachment tables — licensed sub-products / SKUs are Phase 3 fees.

   Common valid Modules for Software Exhibit schedules:
       ✓ "License and Maintenance"
       ✓ "Professional Services"
       ✓ The schedule's product name itself if it is the only top-level section.

═══════════════════ MODULES ARE NEVER ═══════════════════
  ✗ Operational sub-headings ("Hours of Operation", "Technical Support",
    "Location", "Computer System", "Operations and Support")
  ✗ Billing/payment sub-headings ("Software and License Fees", "License
    Fee Payment Schedule", "Maintenance Fees", "Payment Terms",
    "Deconversion Fees", "Delay of Conversion")
  ✗ Legal/term sub-headings ("Maintenance Term: Initial Term", "Term",
    "Termination", "Confidentiality")
  ✗ Individual priced fee line items in tables
  ✗ (i)/(ii)/(iii) roman-numeral atomic sub-services
  ✗ Boilerplate ("Fees", "Pricing", "General", "Description of Services",
    "Defined Terms", "Background", "Recitals", "Materials", "Fiserv
    Responsibilities", "Client Responsibilities")
  ✗ The Schedule title itself (never Module == Product)

═══════════════════ NAMING NORMALIZATION & DEDUPLICATION ═══════════════════
  • 'Fees for Debit Services' and 'Debit Services' → 'Debit Services'
  • Strip prefixes: 'Fees for ', 'Charges for ', 'Pricing for '
  • Strip suffixes: ' Fees', ' Charges', ' Pricing'
  • Use consistent title case

═══════════════════ ALREADY-EXTRACTED MODULES ═══════════════════
For chunks AFTER the first, the user message lists "MODULES ALREADY
EXTRACTED (DO NOT RE-EXTRACT)". Do NOT re-emit those.

═══════════════════ SCHEDULE MANIFEST ═══════════════════
The user message MAY include a "SCHEDULE MANIFEST FOR THIS CONTRACT" block.
When present:
  • Treat it as the authoritative inventory of products to cover.
  • If you see pages belonging to a manifest schedule, EXTRACT its modules.
  • Schedules NOT in the manifest are out of scope.

═══════════════════ FINAL CHECKS ═══════════════════
For every row, mentally verify:
  □ Parent is 'Services Exhibit' or 'Software Exhibit'
  □ Level is 'Schedule'
  □ Product ends with ' Schedule to Services Exhibit' or ' Schedule to Software Exhibit'
  □ Product matches a schedule named in the manifest (when present)
  □ Module is NOT operational / billing / legal / boilerplate
  □ Module is NOT the same string as Product

Return ONLY the JSON object.
""".strip()


PHASE2_GENERIC_SYSTEM_PROMPT = """
You are extracting the PRODUCTS, SERVICES, DELIVERABLES, and LINE ITEMS that
appear in this contract document. The document is NOT a Fiserv Master Agreement
with Services/Software Exhibits — it could be a Statement of Work, Purchase
Order, Amendment, simple service agreement, or any other contract type.

═══════════════════ YOUR JOB ═══════════════════
List every named product, service, deliverable, line item, equipment item, or
work-item the document describes. Use a 4-column structure:

    Parent  → Level → Product → Module

═══════════════════ HOW TO FILL THE 4 COLUMNS ═══════════════════
The document's structure determines what each column means. Use whatever
hierarchy you can see on the page. Common patterns:

PATTERN 1 — Numbered Sections
    "Section 3: Software Licensing"
        3.1 Enterprise License
        3.2 Maintenance & Support
    → Parent="Section 3: Software Licensing", Level="Section",
       Product="Enterprise License", Module=""

PATTERN 2 — Tables of line items (Purchase Order style)
    | Item # | Description                  | Qty | ... |
    |   1    | Cisco Catalyst 9300 Switch   |  4  | ... |
    → Parent="Purchase Order Line Items", Level="Table Row",
       Product="Cisco Catalyst 9300 Switch", Module=""

PATTERN 3 — Tables grouped by category
    Software
      • Enterprise License — $50K
    → Parent="Software", Level="Category", Product="Enterprise License", Module=""

PATTERN 4 — Amendment to a Master Agreement
    "1. Addition of New Service: ID Verification
        (a) Identity proofing API"
    → Parent="Amendment Section 1", Level="Section",
       Product="ID Verification", Module="Identity proofing API"

PATTERN 5 — Statement of Work (SOW) with deliverables
    → Parent="Deliverables", Level="List Item",
       Product="Discovery report", Module=""

═══════════════════ COLUMN GUIDANCE ═══════════════════
• Parent  — the IMMEDIATE enclosing section/heading/table name. If no enclosing
            context, use the document type itself.
• Level   — the document's own structural label (Section / Item / Table Row /
            List Item / Category / Schedule / Attachment / Exhibit / Part /
            Phase / Milestone). Free-text; do NOT constrain to "Schedule".
• Product — the specific named thing. Use the EXACT name from the document.
• Module  — a feature / sub-service / specific deliverable detail. Empty if
            the Product is leaf-level.

═══════════════════ EXTRACTION RULES ═══════════════════
1. Be EXHAUSTIVE.
2. EVERY row must have a non-empty Product.
3. Do NOT extract: pricing/fee tables, Defined Terms, legal boilerplate
   (Confidentiality, Term, Termination, etc.), Recitals, signature blocks.
4. Use visual cues — tables → rows, bullets → items, numbered sections →
   distinct items, bold/underlined → named items, indented → modules.
5. If a table has a "Description" / "Item" column, use THAT as Product.
6. If the document has a clear single-purpose theme, sub-components are
   Modules under ONE Product — do not invent multiple Products.

═══════════════════ ALREADY-EXTRACTED MODULES ═══════════════════
For chunks after the first, the user message includes the list of
(Product → Module) pairs already extracted. Do not re-emit.

═══════════════════ FINAL CHECKS ═══════════════════
Before emitting any row:
  □ Product is non-empty and names a real thing
  □ Module (if non-empty) is NOT the same string as Product
  □ This row is NOT pricing / legal boilerplate / a defined term

Return ONLY the JSON object.
""".strip()


PHASE2_SOW_SYSTEM_PROMPT = """
This contract is a STATEMENT OF WORK (SOW). SOWs describe a single bounded
project. Emit EXACTLY ONE row that captures the project.

═══════════════════ COLUMN DEFINITIONS ═══════════════════
  Parent  = "Statement of Work"   (fixed)
  Level   = "Schedule"            (fixed)

  Product:
    Look for the UNDERLYING SERVICE the SOW is modifying or extending. Common
    phrasings:
      "...related to the Client's use of Fiserv <Service Name>"
      "...the Client's <Service Name>..."
      "Project related to <Service Name>"
    Use the exact service name as printed (strip "Fiserv " prefix).
    FALLBACK: if no underlying service is referenced, use "Project Services".

  Module:
    The SPECIFIC project deliverable. Find it in:
      • The "(a) Description" line under Project Services
      • The "Project Name" / "Project Title" field
      • The Project Overview section
      • Inline in the introductory paragraph

═══════════════════ TARGET ROW ═══════════════════
EXACTLY ONE row:
  Parent  = "Statement of Work"
  Level   = "Schedule"
  Product = "<underlying service name from the SOW>"
  Module  = "<specific project deliverable description>"

═══════════════════ WHAT NOT TO EMIT ═══════════════════
  ✗ "Project Services" alongside the underlying service — emit ONE row only
  ✗ Section headings ("Project Scope and Services", "Project Fees", "Change Management")
  ✗ Fiserv billing categories ("Programming Services", "MISC_Custom Solutions
    Professional Services")
  ✗ The SOW title itself, attachment numbers, signature blocks, recitals
  ✗ Anything fees-related (Phase 3)

═══════════════════ HOW MANY ROWS ═══════════════════
ONE row per SOW. Exceptionally, multiple genuinely INDEPENDENT projects (each
with their own underlying service) each get one row. If you would emit 3+
rows for a single-project SOW, you are extracting section headings — re-identify.

═══════════════════ FINAL CHECKS ═══════════════════
  □ Parent is exactly "Statement of Work"
  □ Level is exactly "Schedule"
  □ Product is the UNDERLYING SERVICE (not "Project Services" unless truly absent)
  □ Module names the SPECIFIC project deliverable
  □ Module is not a section header, fee category, or billing label

Return ONLY the JSON object.
""".strip()


PHASE2_ORDER_SYSTEM_PROMPT = """
This contract is a SUBSEQUENT ORDER, PURCHASE ORDER, or similar short
procurement document. It typically contains ONE or MORE pricing tables, each
grouped under a product/category code banner with bolded sub-section headers.

═══════════════════ COLUMN DEFINITIONS ═══════════════════
  Parent  = the order document type ("Subsequent Order", "Purchase Order",
            "Instant Issuance Subsequent Order", ...) as titled.
  Level   = "Schedule"   (fixed)
  Product = the TABLE BANNER — product code/category at the top of the table.
  Module  = the BOLDED sub-section headers WITHIN that table. Drop trailing
            "COSTS" / "FEES" / "CHARGES" words.

═══════════════════ EXAMPLE ═══════════════════
A table banner "PPC" with three bolded sub-headers becomes three rows:
  Parent="Subsequent Order", Level="Schedule", Product="PPC", Module="ONE TIME HARDWARE"
  Parent="Subsequent Order", Level="Schedule", Product="PPC", Module="ONE TIME SOFTWARE"
  Parent="Subsequent Order", Level="Schedule", Product="PPC", Module="ONE TIME INSTALLATION & TRAINING"

═══════════════════ SUFFIX STRIPPING ═══════════════════
"ONE TIME HARDWARE COSTS"           → "ONE TIME HARDWARE"
"MONTHLY SOFTWARE FEES"             → "MONTHLY SOFTWARE"
"ANNUAL MAINTENANCE FEES"           → "ANNUAL MAINTENANCE"

═══════════════════ WHAT NOT TO EXTRACT ═══════════════════
  ✗ Individual LINE ITEMS (Phase 3 fee items)
  ✗ Total/Subtotal/Aggregate rows ("Total One-Time Hardware Cost", ...)
  ✗ Column headers (PRODUCT CODE, DESCRIPTION, UNIT PRICE, QUANTITY, AMOUNT)
  ✗ Boilerplate / signature blocks / defined terms / payment terms

═══════════════════ ALTERNATIVE: ADDITION/DELETION TO EXISTING SERVICES ═══════════════════
"The following services are added to Account Processing Services:
   • Mobile Capture
   • Bill Pay Premium"
→ Product = "Account Processing Services", Module = each added service.

═══════════════════ MULTIPLE TABLES ═══════════════════
If the order has multiple pricing tables (each with own banner), emit rows
for each table.

═══════════════════ PARAGRAPH-FORM ORDER (LEGACY EDGE CASE) ═══════════════════
Old fax-style orders with no clean table. Look for phrasings like:
    "Client hereby orders from <Provider> a <PRODUCT> as follows:"
The PRODUCT is the noun phrase named in that sentence. DO NOT extract page
headers, fax instructions, return-fax numbers, or form numbers as Product.
Emit ONE row (rarely two) for this style.

═══════════════════ CHECKBOX RULE ═══════════════════
If the order uses CHECKBOXES, emit ONLY CHECKED items.
  Checked: ☒  ✓  ✔  ✗  ✘  X  x  ⊠  filled square
  Unchecked: ☐  □  ▢  ◻  empty square

═══════════════════ FINAL CHECKS ═══════════════════
  □ Parent reflects the order document type
  □ Level is "Schedule"
  □ Product is a banner / category code OR an existing service being modified
    OR the noun-phrase product in a paragraph-form order
  □ Product is NOT page furniture (fax instructions, form numbers, etc.)
  □ Module is a BOLDED sub-section header (with COSTS/FEES/CHARGES stripped)
    OR a service being added/removed
  □ Module is NOT a Total/Subtotal row, column header, or individual line item

Return ONLY the JSON object.
""".strip()


PHASE2_LICENSE_SYSTEM_PROMPT = """
This contract is a SOFTWARE LICENSE AGREEMENT. License Agreements describe
one or more products through a sequence of EXHIBITS (Exhibit 1a, 1b, 1c, 2a,
2b, ...). Modules within each exhibit are typically indicated by CHECKBOXES.

═══════════════════ COLUMN DEFINITIONS (FIXED) ═══════════════════
  Parent  = "Agreement"          ← ALWAYS this exact string, EVERY row
  Level   = "Exhibit"            ← ALWAYS this exact string, EVERY row
  Product = the EXHIBIT-LEVEL service/offering name (top-level product family).
            Examples:
              "CubicsPlus", "Premier", "Signature", "DNA"     ← software platforms
              "Professional Services"                          ← services exhibit
              "Basic Maintenance Services"                     ← maintenance
              "Special Maintenance Services"                   ← optional maintenance
              "System Recovery Support"                        ← single-offering
              "Secure Data Source"                             ← single-offering
              "Equipment"                                      ← hardware bundle
              "Hardware Support"                               ← single-offering
  Module  = the SPECIFIC checked module under that Product, OR — for exhibits
            with a single undivided offering — the Product name REPEATED.
            Self-referential rows (Module == Product) are VALID for
            single-offering exhibits like "Hardware Support → Hardware Support".

═══════════════════ CHECKBOX RULE (CRITICAL) ═══════════════════
Treat ALL of these as CHECKED:
       ☒   ✓   ✔   ✗   ✘   X   x   ⊠   ⊗   ▣   ▪   ■
       a filled square; a square with an X drawn through it; any glyph
       overlapping a square outline.
Treat these as UNCHECKED — do NOT emit:
       ☐   □   ▢   ◻   empty square / outline with nothing inside.

If uncertain whether a box is checked, re-read the image. When still in
doubt, DO NOT EMIT.

═══════════════════ THE STRUCTURAL WALK-THROUGH ═══════════════════

────── EXHIBIT 1a — License Fee + Software Modules ──────
Heading: "Modules & Total License Fee" or "License Fee".
Intro: "The boxes marked below with an 'X' indicate the Software licensed by
        the [Client]."
Then a CHECKBOX list of modules under ONE software platform.

Identify the platform name from: an explicit heading, the intro sentence, the
first checked module suffix (" Base Module" / " Core" / " Platform" — strip),
or the agreement title.

→ Emit ONE row per CHECKED module: Product=<Platform>, Module=<each checked>.

────── EXHIBIT 1a — Professional Services ──────
A sub-section describing implementation, conversion, training, on-site
support as one bundle.
→ Emit ONE self-referential row: Product="Professional Services",
  Module="Professional Services".

────── EXHIBIT 1a — Basic Maintenance Services ──────
A SECOND checkbox list MIRRORING the Software section selections. The
SAME modules typically appear with the same checkbox states.
→ Emit ONE row PER CHECKED maintenance module under
  Product="Basic Maintenance Services". This duplication is INTENTIONAL.

────── EXHIBIT 1a — Special Maintenance Services ──────
A SMALLER checkbox list of OPTIONAL add-on maintenance items.
→ Emit ONE row per CHECKED item under
  Product="Special Maintenance Services". Keep Module text verbatim.

────── EXHIBIT 1b — System Recovery Support ──────
A single-offering exhibit. No internal checkbox list.
→ Emit ONE self-referential row:
  Product="System Recovery Support", Module="System Recovery Support".

────── EXHIBIT 1c — Secure Data Source / Storage ──────
→ Emit ONE self-referential row:
  Product="Secure Data Source", Module="Secure Data Source".

────── EXHIBIT 2a — Equipment / Hardware and Infrastructure ──────
Hardware bundle. Individual hardware items are Phase 3 fees.
→ Emit ONE umbrella row: Product="Equipment", Module="Hardware and Infrastructure".

────── EXHIBIT 2b — Hardware Support ──────
→ Emit ONE self-referential row: Product="Hardware Support",
  Module="Hardware Support".

────── ADDITIONAL EXHIBITS ──────
For each additional exhibit, identify whether it's:
  (a) CHECKBOX list under one product → one row per checked module
  (b) SINGLE OFFERING → one self-referential row
  (c) Confidentiality / NDA / boilerplate → SKIP

═══════════════════ WHAT TO IGNORE — STRICT ═══════════════════
  ✗ DEFINITIONS / GLOSSARY entries (e.g., "Software", "Documentation",
    "Operational Support", "Functional Specifications", "Project Plan",
    "Business Requirements List", etc.). They are defined terms, not offerings.
  ✗ Section/paragraph headers from the agreement BODY ("1. License",
    "2. Professional Services", etc.) — those organize legal text.
  ✗ Configuration FIELDS inside exhibits ("Total License Fee", "Documentation",
    "Computer System", "Location(s)", "License Fee Payment Timetable",
    "Maximum Accounts Processed", "Standard Interfaces", etc.).
  ✗ Individual hardware line items (Phase 3).
  ✗ Individual priced fee items (Phase 3).
  ✗ Exhibit numbers as Products ("Exhibit 1a", "Exhibit 1b") — navigation labels.
  ✗ Empty / unchecked checkboxes ("☐", "□").
  ✗ Signature blocks, recitals, page headers/footers, NDA exhibits.

═══════════════════ SINGLE-OFFERING EXHIBIT RULE ═══════════════════
If an exhibit describes ONE service with no internal checkbox list, EMIT
ONE ROW with Module = Product. Self-referential rows are EXPECTED.

═══════════════════ MIRROR RULE FOR BASIC MAINTENANCE SERVICES ═══════════════════
If the document has both (a) a Software checkbox list AND (b) a Basic
Maintenance Services checkbox list, the maintenance list ALMOST ALWAYS has
the SAME items CHECKED. Emit one row PER checked maintenance module under
Product="Basic Maintenance Services" — typically repeating the same Module
names emitted under the Software platform.

═══════════════════ FINAL CHECKS ═══════════════════
  □ Parent is exactly "Agreement"
  □ Level is exactly "Exhibit"
  □ Product is a walk-through-named EXHIBIT-LEVEL offering (NOT a glossary
    term, NOT a section header, NOT an exhibit number)
  □ Module is (a) a CHECKED module name OR (b) identical to Product for a
    single-offering exhibit
  □ No unchecked module appears in any row
  □ No glossary-defined terms appear as Products or Modules
  □ Basic Maintenance Services has one row PER CHECKED module

Return ONLY the JSON object.
""".strip()


PHASE2_RETRY_PROMPT_PREFIX = """
This is a TARGETED RETRY for a single schedule that was missed in the
previous Phase 2 pass. You are looking at the pages of THAT schedule and
THAT schedule only.

TARGET SCHEDULE: {target_product}

Extract its Modules using the Rule A / Rule B logic from the main Phase 2
prompt below. Be EXHAUSTIVE — find ALL of its Modules. Do NOT return an
empty list; if the schedule body is sparse, look in Pattern A2 (bold
sub-headings under "Services") or Pattern A5 (named modules in attachments).

═══════════════════════════════════════════════════════════════════════

""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_seen_modules(seen_pairs: list[tuple[str, str]]) -> str:
    if not seen_pairs:
        return ""
    grouped: dict = defaultdict(set)
    for product, module in seen_pairs:
        if module:
            grouped[product or "(no product)"].add(module)
    lines = ["═══ MODULES ALREADY EXTRACTED (DO NOT RE-EXTRACT) ═══",
             "The following Product → Module pairs were extracted from EARLIER pages.",
             "If you see ANY of these on the current pages, DO NOT:",
             "  • Output them again (Pandas dedupes but it wastes tokens)",
             "  • Treat them as a NEW section header",
             "  • Extract child items under them as new modules",
             "Already-extracted modules are CONSIDERED COMPLETE.",
             ""]
    for product in sorted(grouped):
        lines.append(f"  • {product}:")
        for m in sorted(grouped[product]):
            lines.append(f"      - {m}")
    lines.append("═══════════════════════════════════════════════════════")
    return "\n".join(lines)


def _format_manifest(manifest: Optional[ScheduleManifest]) -> str:
    if not manifest or not manifest.schedules:
        return ""
    lines = ["═══ SCHEDULE MANIFEST FOR THIS CONTRACT ═══",
             "The Services and Software Exhibits contain the following product",
             "schedules. Each MUST be covered with at least one Module across",
             "the full Phase 2 extraction."]
    by_parent: dict = {}
    for s in manifest.schedules:
        by_parent.setdefault(s.parent, []).append(s.schedule_title)
    for par in sorted(by_parent):
        lines.append("")
        lines.append(f"  {par}:")
        for t in by_parent[par]:
            lines.append(f"    • {t}")
    lines.append("═══════════════════════════════════════════")
    return "\n".join(lines)


def _find_schedule_pages(pages: list, schedule_title: str,
                         max_pages: int = 12) -> list:
    """Locate the pages in the PDF belonging to a given schedule via text-layer
    scan. Returns up to ``max_pages`` consecutive pages starting from the first
    hit."""
    if not pages:
        return []
    title_norm = schedule_title.strip().lower()
    start_idx = None
    for i, p in enumerate(pages):
        text_norm = (p.get("text") or "").lower()
        if title_norm in text_norm and "schedule" in text_norm:
            start_idx = i
            break
    if start_idx is None:
        for i, p in enumerate(pages):
            if title_norm in (p.get("text") or "").lower():
                start_idx = i
                break
    if start_idx is None:
        return []
    out = [pages[start_idx]]
    for j in range(1, max_pages):
        nxt = start_idx + j
        if nxt >= len(pages):
            break
        ntext = (pages[nxt].get("text") or "").lower()
        if " schedule to " in ntext and title_norm not in ntext[:500]:
            break
        out.append(pages[nxt])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 sub-runners — one per route
# ─────────────────────────────────────────────────────────────────────────────

def _run_phase2_strict(api, model: str, pages: list, manifest: ScheduleManifest,
                       log: Callable[[str], None]) -> list[Phase2Row]:
    """STRICT path: manifest non-empty. Chunked extraction + missing-schedule retry."""
    all_rows: list[Phase2Row] = []
    seen_pairs: list[tuple[str, str]] = []
    n_chunks = math.ceil(len(pages) / CHUNK_SIZE)
    manifest_block = _format_manifest(manifest)

    # Pass 1: chunked extraction over the whole document.
    for ci in range(n_chunks):
        chunk = pages[ci * CHUNK_SIZE:(ci + 1) * CHUNK_SIZE]
        if not chunk:
            continue
        pgs = f"{chunk[0]['page_number']}-{chunk[-1]['page_number']}"
        log(f"  Phase 2 [strict] chunk {ci + 1}/{n_chunks} (pages {pgs}, "
            f"{len(seen_pairs)} modules seen)")

        parts: list[str] = []
        if manifest_block:
            parts.append(manifest_block)
        sb = _format_seen_modules(seen_pairs)
        if sb:
            parts.append(sb)
        extra = "\n\n".join(parts)

        try:
            inst = call_vision(
                api, model=model,
                system_prompt=PHASE2_STRICT_SYSTEM_PROMPT,
                pages=chunk,
                response_model=Phase2Result,
                schema_name="phase2_products",
                label=f"phase2-strict-c{ci+1}",
                extra_user_instruction=extra,
            )
            all_rows.extend(inst.rows)
            for r in inst.rows:
                if r.module and r.module.strip():
                    seen_pairs.append((r.product or "", r.module.strip()))
        except Exception as e:
            log(f"    ✗ chunk {ci + 1} failed: {type(e).__name__}: {e}")

    # Pass 2: identify and retry missing manifest schedules.
    if manifest and manifest.schedules:
        expected: dict = {}
        for s in manifest.schedules:
            full_product = f"{s.schedule_title} Schedule to {s.parent}"
            expected[(s.parent, s.schedule_title)] = full_product

        seen_products = {
            (r.parent, r.product) for r in all_rows
            if _is_real_module(r.parent, r.level, r.product, r.module)
        }

        missing = []
        for (par, title), full_product in expected.items():
            if (par, full_product) not in seen_products:
                missing.append((par, title, full_product))

        if missing:
            log(f"  Phase 2 RETRY — {len(missing)} schedule(s) with 0 modules:")
            for par, title, _ in missing:
                log(f"    • {title}  (Parent: {par})")
            for retry_idx, (par, title, full_product) in enumerate(missing, start=1):
                sched_pages = _find_schedule_pages(pages, title, max_pages=12)
                if not sched_pages:
                    log(f"    ⚠ Could not locate pages for {title!r} in text layer — skipping")
                    continue
                pgs = f"{sched_pages[0]['page_number']}-{sched_pages[-1]['page_number']}"
                log(f"    retry {retry_idx}/{len(missing)}: {title!r} on pages {pgs}")
                retry_system = (
                    PHASE2_RETRY_PROMPT_PREFIX.format(target_product=full_product)
                    + "\n\n"
                    + PHASE2_STRICT_SYSTEM_PROMPT
                )
                try:
                    inst = call_vision(
                        api, model=model,
                        system_prompt=retry_system,
                        pages=sched_pages,
                        response_model=Phase2Result,
                        schema_name="phase2_products",
                        label=f"phase2-retry-{retry_idx}",
                        extra_user_instruction=manifest_block,
                    )
                    all_rows.extend(inst.rows)
                    log(f"      ✓ recovered {len(inst.rows)} rows")
                except Exception as e:
                    log(f"      ✗ retry failed: {type(e).__name__}: {e}")
        else:
            log(f"  All {len(manifest.schedules)} manifest schedules have ≥1 module")

    return all_rows


def _run_phase2_chunked(api, model: str, pages: list, system_prompt: str,
                        label: str, log: Callable[[str], None]
                        ) -> list[Phase2Row]:
    """Generic chunked runner (used by SOW / Order / Generic paths)."""
    all_rows: list[Phase2Row] = []
    seen_pairs: list[tuple[str, str]] = []
    n_chunks = max(1, math.ceil(len(pages) / CHUNK_SIZE))

    for ci in range(n_chunks):
        chunk = pages[ci * CHUNK_SIZE:(ci + 1) * CHUNK_SIZE]
        if not chunk:
            continue
        pgs = f"{chunk[0]['page_number']}-{chunk[-1]['page_number']}"
        log(f"  Phase 2 [{label}] chunk {ci + 1}/{n_chunks} (pages {pgs}, "
            f"{len(seen_pairs)} items seen)")
        extra = _format_seen_modules(seen_pairs)
        try:
            inst = call_vision(
                api, model=model,
                system_prompt=system_prompt,
                pages=chunk,
                response_model=Phase2Result,
                schema_name="phase2_products",
                label=f"phase2-{label}-c{ci+1}",
                extra_user_instruction=extra,
            )
            all_rows.extend(inst.rows)
            for r in inst.rows:
                if r.module and r.module.strip():
                    seen_pairs.append((r.product or "", r.module.strip()))
                elif r.product and r.product.strip():
                    seen_pairs.append((r.product.strip(), ""))
        except Exception as e:
            log(f"    ✗ chunk {ci + 1} failed: {type(e).__name__}: {e}")
    return all_rows


def _run_phase2_license(api, model: str, pages: list,
                        log: Callable[[str], None]) -> list[Phase2Row]:
    """LICENSE path: prefer single API call (≤24 pages); chunk if longer."""
    all_rows: list[Phase2Row] = []
    if len(pages) <= LICENSE_SINGLE_CALL_MAX_PAGES:
        log(f"  Phase 2 [license] single-call ({len(pages)} pages)")
        try:
            inst = call_vision(
                api, model=model,
                system_prompt=PHASE2_LICENSE_SYSTEM_PROMPT,
                pages=pages,
                response_model=Phase2Result,
                schema_name="phase2_products",
                label="phase2-license",
            )
            all_rows.extend(inst.rows)
        except Exception as e:
            log(f"    ✗ license single-call failed: {type(e).__name__}: {e}")
        return all_rows

    log(f"  Phase 2 [license] long doc ({len(pages)} > "
        f"{LICENSE_SINGLE_CALL_MAX_PAGES}) — chunking")
    return _run_phase2_chunked(api, model, pages,
                               PHASE2_LICENSE_SYSTEM_PROMPT, "license", log)


def _run_phase2(api, model: str, pages: list, manifest: ScheduleManifest,
                log: Callable[[str], None]
                ) -> tuple[list[Phase2Row], str]:
    """Phase 2 dispatcher — returns (rows, path_mode used)."""
    has_schedules = bool(manifest and getattr(manifest, "schedules", None))
    doc_type = manifest.document_type if manifest else "Other"

    if has_schedules:
        log(f"  Phase 2 routing → STRICT ({len(manifest.schedules)} schedules; "
            f"document_type={doc_type!r})")
        return _run_phase2_strict(api, model, pages, manifest, log), "strict"

    if doc_type == "SOW":
        log(f"  Phase 2 routing → SOW")
        return (_run_phase2_chunked(api, model, pages, PHASE2_SOW_SYSTEM_PROMPT,
                                    "sow", log), "sow")

    if doc_type == "Order":
        log(f"  Phase 2 routing → ORDER")
        return (_run_phase2_chunked(api, model, pages, PHASE2_ORDER_SYSTEM_PROMPT,
                                    "order", log), "order")

    if doc_type == "LicenseAgreement":
        log(f"  Phase 2 routing → LICENSE")
        return _run_phase2_license(api, model, pages, log), "license"

    log(f"  Phase 2 routing → GENERIC (document_type={doc_type!r}, no schedules)")
    return (_run_phase2_chunked(api, model, pages, PHASE2_GENERIC_SYSTEM_PROMPT,
                                "generic", log), "generic")


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning
# ─────────────────────────────────────────────────────────────────────────────

PHASE2_VALID_PARENTS = {"services exhibit", "software exhibit"}
PHASE2_VALID_LEVEL   = "schedule"

PHASE2_MODULE_BLOCKLIST = {
    "fees", "pricing", "general", "term", "terms",
    "hours of operation", "performance",
    "payment terms", "additional terms", "additional terms and conditions",
    "deconversion", "deconversion fees",
    "rescheduling fees", "termination fees", "termination",
    "delay of conversion", "conversion", "confidentiality",
    "defined terms", "definitions",
    "description of services", "service description",
    "services description attachment", "materials",
    "background", "recitals", "miscellaneous",
    "services",
    "software and license fees",
    "license fee payment schedule",
    "location", "computer system",
    "maintenance fees",
    "maintenance term", "maintenance term: initial term",
    "operations and support", "technical support",
    "fiserv responsibilities", "client responsibilities",
}


def _is_real_module(parent: str, level: str, product: str, module: str) -> bool:
    """Single source of truth — used by the cleaner AND the retry trigger so
    they never disagree about whether a schedule is genuinely covered."""
    mod = (module or "").strip()
    if not mod or mod.lower() in ("nan", "none"):
        return False
    par_lc = (parent or "").strip().lower()
    if par_lc not in PHASE2_VALID_PARENTS:
        return False
    lvl_lc = (level or "").strip().lower()
    if lvl_lc != PHASE2_VALID_LEVEL:
        return False
    prod = (product or "").strip()
    if prod and mod.lower() == prod.lower():
        return False
    if mod.lower() in PHASE2_MODULE_BLOCKLIST:
        return False
    return True


def _clean_phase2(rows: list, path_mode: str) -> pd.DataFrame:
    """Post-process Phase 2 rows. Strict = full allow/block list filter +
    self-ref drop. License = require Product+Module non-empty, normalize
    Parent/Level. Other paths = drop empty-Product + dedupe."""
    cols = ["Parent", "Level", "Product", "Module"]
    if not rows:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame([r.model_dump() if isinstance(r, BaseModel) else r for r in rows])
    df.columns = cols

    if path_mode == "strict":
        mask = df.apply(
            lambda r: _is_real_module(r["Parent"], r["Level"], r["Product"], r["Module"]),
            axis=1,
        )
        df = df[mask]
    elif path_mode == "license":
        prod_str = df["Product"].astype(str).fillna("").str.strip()
        mod_str  = df["Module"].astype(str).fillna("").str.strip()
        df = df[(prod_str != "") & (mod_str != "")]
        df = df.copy()
        df["Parent"] = "Agreement"
        df["Level"]  = "Exhibit"
    else:
        prod_str = df["Product"].astype(str).fillna("").str.strip()
        df = df[prod_str != ""]

    return df.drop_duplicates().reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Filename heuristics + output
# ─────────────────────────────────────────────────────────────────────────────

import re


def _detect_contract_type(name: str) -> str:
    low = name.lower()
    if "master agreement" in low or "master" in low: return "Master Agreement"
    if "amendment" in low or "amend" in low:         return "Amendment"
    if "services"  in low:                           return "Services"
    return ""


OUTPUT_COLS = [
    "Filename", "Contract Type", "Document Type",
    "Parent", "Level", "Product", "Module",
]


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
    """Run Phase 1.5 (manifest) + Phase 2 (product hierarchy) for every PDF
    under ``client_name``. Writes
    ``Output/<client>/product_hierarchy_output.xlsx``."""
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

    log(f"Product Module ({model}): {len(pdfs)} PDF(s)…")
    api = make_client(api_key)

    out_rows: list[dict] = []
    for i, p in enumerate(pdfs):
        log(f"  [{i + 1}/{len(pdfs)}] {p.name}")
        ctype = _detect_contract_type(p.name)
        try:
            pages = pdf_to_images(p)
            if not pages:
                raise RuntimeError("no pages rendered")

            # Phase 1.5 — own manifest call (Product Module runs independently)
            try:
                manifest = run_phase1_5_manifest(api, model, pages, log)
            except Exception as e:
                log(f"  ⚠ Phase 1.5 failed: {type(e).__name__}: {e} — "
                    f"defaulting to Other / no schedules")
                manifest = ScheduleManifest(document_type="Other", schedules=[])

            # No Phase 1 here — apply_license_override falls back to text-layer scan.
            manifest = apply_license_override(manifest, None, pages, log)

            rows, path_mode = _run_phase2(api, model, pages, manifest, log)
            df = _clean_phase2(rows, path_mode)
            log(f"  Phase 2 [{path_mode}]: {len(rows)} raw → {len(df)} cleaned rows")

            doc_type = manifest.document_type if manifest else ""
            if df.empty:
                out_rows.append({
                    "Filename":      p.name,
                    "Contract Type": ctype,
                    "Document Type": doc_type,
                    "Parent":        "",
                    "Level":         "",
                    "Product":       "",
                    "Module":        "",
                })
            else:
                for _, r in df.iterrows():
                    out_rows.append({
                        "Filename":      p.name,
                        "Contract Type": ctype,
                        "Document Type": doc_type,
                        "Parent":        r["Parent"],
                        "Level":         r["Level"],
                        "Product":       r["Product"],
                        "Module":        r["Module"],
                    })
        except Exception as e:
            log(f"    ⚠ Failed on {p.name}: {type(e).__name__}: {e}")
            out_rows.append({
                "Filename":      p.name,
                "Contract Type": ctype,
                "Document Type": "",
                "Parent":        "",
                "Level":         "",
                "Product":       "",
                "Module":        "",
            })

    out_dir = _OUTPUT_DIR / client_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "product_hierarchy_output.xlsx"

    pd.DataFrame(out_rows, columns=OUTPUT_COLS).to_excel(str(out_path), index=False)
    log(f"Wrote: {out_path.name} ({len(out_rows)} rows)")

    return {
        "status": "complete",
        "client": client_name,
        "rows":   len(out_rows),
        "output": str(out_path),
    }


def is_processed(client_name: str) -> bool:
    """Done only when at least one real Product row was written. Mirrors the
    chatbot's downstream rendering, which filters out empty placeholder rows."""
    p = _OUTPUT_DIR / client_name / "product_hierarchy_output.xlsx"
    if not (p.exists() and p.stat().st_size > 1_000):
        return False
    try:
        df = pd.read_excel(str(p))
    except Exception:
        return False
    if df.empty or "Product" not in df.columns:
        return False
    prod = df["Product"].astype(str).str.strip()
    return bool(((prod != "") & (prod.str.lower() != "nan")).any())


def output_path(client_name: str) -> Path:
    return _OUTPUT_DIR / client_name / "product_hierarchy_output.xlsx"
