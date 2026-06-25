"""Shared configuration for the Contract Intelligence Chatbot."""
import os
from pathlib import Path

# Load .env if present (silently no-op if python-dotenv isn't installed).
# Values already in the environment win, so a real `export FOO=...` overrides
# whatever is in .env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

# ── Folder layout ────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "Input"            # Input/<Core>/<Client>/*.pdf (new) or Input/<Client>/*.pdf (legacy)
OUTPUT_DIR  = BASE_DIR / "Output"           # Output/<Client>/  — flat, keyed by client name
SCRIPTS_DIR = BASE_DIR / "Existing Scripts" # original scripts – never modified
KB_PATH     = BASE_DIR / "FD306_Full_Context_KnowledgeBase.md"


def client_input_dir(client_name: str, core: str = "") -> Path:
    """Resolve the actual folder of contract PDFs for a client.

    New layout:  Input/<Core>/<Client>/
    Legacy:      Input/<Client>/   (when no Core is provided)
    """
    if core:
        return INPUT_DIR / core / client_name
    return INPUT_DIR / client_name

# ── OpenAI model names ────────────────────────────────────────────────────────
# These mirror what the existing scripts use; override in the UI if needed.
HIERARCHY_MODEL  = "gpt-5.2"
EXTRACTION_MODEL = "gpt-5.2-2025-12-11"
MATCHING_MODEL   = "gpt-4.1-2025-04-14"
CHAT_MODEL       = "gpt-4.1-2025-04-14"

# ── API keys ──────────────────────────────────────────────────────────────────
# Each key prefers its environment variable (set in .env or shell); the
# hardcoded fallback below keeps the laptop setup working without env vars.
# When OPENAI_BACKEND=fiserv these api_key values are unused — the Foundation
# API authenticates via VDI network + X-Email-Id header instead.

# Use `or` (not get's default arg) so an EMPTY string in .env still falls
# through to the hardcoded fallback.  Otherwise `CHAT_API_KEY=` in .env
# silently overrides the fallback with "" and the agent 401s.
CHAT_API_KEY = (
    os.environ.get("CHAT_API_KEY")
)

# Extraction agent — used for line-item extraction + matching.
# The Hierarchy agent uses its own key embedded in contract_hierarchy_analyzer.py.
EXTRACTION_API_KEY = (
    os.environ.get("EXTRACTION_API_KEY")
)

# CPI extractor — the notebook's embedded key was revoked, reuse extraction key.
CPI_API_KEY = os.environ.get("CPI_API_KEY") or EXTRACTION_API_KEY
CPI_MODEL   = os.environ.get("CPI_MODEL", "gpt-4o-mini")

# Master Contract (vision: signatures + addresses + summary)
MASTER_CONTRACT_API_KEY = os.environ.get("MASTER_CONTRACT_API_KEY") or EXTRACTION_API_KEY
MASTER_CONTRACT_MODEL   = os.environ.get("MASTER_CONTRACT_MODEL", "gpt-5.2-2025-12-11")

# ── MNR Template Agent (forensic extraction → matching → SAP-ready MNR Excel) ─
# Mirrors MNR_Setup_vRG_KeplerCannon_Apr2026.py. Caches are namespaced per
# client under Output/<Client>/.mnr_cache/ so no manual deletion is needed
# between clients (the script's cache files lived next to the script and had
# to be wiped by hand each run).
MNR_API_KEY = os.environ.get("MNR_API_KEY") or EXTRACTION_API_KEY
MNR_MODEL   = os.environ.get("MNR_MODEL", "gpt-5.2-2025-12-11")

# Frequently Used Material Codes catalog — the PRIMARY dictionary the matcher
# prefers over the full Portico master. Resolution order:
#   1. Input/<Core>/<filename>     (drop a per-Core copy alongside contracts)
#   2. BASE_DIR / <filename>       (project-level fallback)
# The default filename matches the script's hard-coded path.
MNR_FREQ_CATALOG_NAME = os.environ.get(
    "MNR_FREQ_CATALOG_NAME", "Frequently Used Material Codes.xlsx"
)

# Reference image of a marked checkbox — already in the project root.
MNR_CHECKBOX_REF = BASE_DIR / "marked_checkbox_example.png"

# Optional empty MNR template (column headers Janneth's biller workflow expects).
# If missing, the agent synthesizes a default header from build_mnr_rows().
MNR_TEMPLATE_NAME = os.environ.get("MNR_TEMPLATE_NAME", "MNR_template.xlsx")

# Stage-1 PDF render settings — verbatim from the script.
MNR_DPI         = int(os.environ.get("MNR_DPI", "600"))
MNR_CHUNK_SIZE  = int(os.environ.get("MNR_CHUNK_SIZE", "12"))
MNR_MATCH_MIN_CONF = float(os.environ.get("MNR_MATCH_MIN_CONF", "0.70"))

# Hierarchy agent — per-contract metadata extraction (contract_type, dates,
# parties, parent_references, section_structure). The agent uses the
# Responses API via the metered make_client(), so token counts are recorded.
HIERARCHY_API_KEY = os.environ.get("HIERARCHY_API_KEY") or EXTRACTION_API_KEY

# Scope Agent — cheap text-only decisions on which contracts each agent processes
SCOPE_AGENT_API_KEY = os.environ.get("SCOPE_AGENT_API_KEY") or EXTRACTION_API_KEY
SCOPE_AGENT_MODEL   = os.environ.get("SCOPE_AGENT_MODEL", "gpt-4o-mini")

# Extraction model can also be overridden (defaults match the original notebook)
EXTRACTION_MODEL_OVERRIDE = os.environ.get("EXTRACTION_MODEL", "")
MATCHING_MODEL_OVERRIDE   = os.environ.get("MATCHING_MODEL", "")

# Material Validation agent — re-scores matched material codes against live
# Snowflake invoice history. Text-only reranker, so a gpt-4.x model fits
# (mirrors MATCHING_MODEL). Key falls back to the extraction key like the others.
VALIDATION_API_KEY = os.environ.get("VALIDATION_API_KEY") or EXTRACTION_API_KEY
VALIDATION_MODEL   = os.environ.get("VALIDATION_MODEL", "gpt-4.1-2025-04-14")

# Which LLM backend to use — "openai" (default) or "fiserv" (VDI Foundation API)
OPENAI_BACKEND = (os.environ.get("OPENAI_BACKEND") or "openai").lower()

# ── Extraction settings ───────────────────────────────────────────────────────
DPI         = 600   # PDF render resolution (mirrors existing scripts)
CHUNK_SIZE  = 8    # PDF pages per API call

# ── Per-Core default material-code dictionaries ──────────────────────────────
# Picked up automatically by the Extraction agent when the user hasn't supplied
# a Dictionary .xlsx in Advanced settings.  Paths are absolute (resolved
# relative to BASE_DIR.parent to escape the Contract Chatbot folder).
CORE_DEFAULT_DICTIONARIES: dict = {
    "PORTICO": BASE_DIR.parent / "Material Code Setup"
                                / "Portico Material Code Cleanup vRG"
                                / "Portico_Consolidated_Material_Dictionary vRG.xlsx",
    # DNA clients can be added here once their canonical dict is chosen.
}

# Sheet names the Extraction agent should try (in order) when reading any
# dictionary file.  The original notebook hardcoded "Final", but the
# Portico dictionary uses "Combined Dictionary".
DICTIONARY_SHEET_CANDIDATES: tuple = (
    "Combined Dictionary",
    "Final",
    "Dictionary",
    "Sheet1",
)


def default_dictionary_for(core: str) -> Path:
    """Return the material-code dictionary for a Core.

    Resolution order (the Input/<Core>/ folder wins so users can swap
    dictionaries by simply dropping a new .xlsx alongside the contracts):
      1. Any .xlsx inside  Input/<Core>/  (prefer filenames containing
         'dict', otherwise the largest .xlsx).
      2. Explicit mapping in CORE_DEFAULT_DICTIONARIES.
      3. Path() if nothing is found.
    """
    # 1) Anything dropped into Input/<Core>/
    if core:
        core_dir = INPUT_DIR / core
        if core_dir.exists():
            xlsx = [p for p in core_dir.glob("*.xlsx") if p.is_file()]
            if xlsx:
                named = [p for p in xlsx if "dict" in p.name.lower()]
                if named:
                    return max(named, key=lambda p: p.stat().st_size)
                return max(xlsx, key=lambda p: p.stat().st_size)

    # 2) Explicit fallback mapping
    p = CORE_DEFAULT_DICTIONARIES.get(core)
    if p and Path(p).exists():
        return Path(p)

    return Path()

# ── Knowledge-base sections to embed in chat context ─────────────────────────
# These section numbers correspond to the FD306 knowledge base structure.
KB_KEY_SECTION_NUMS = {"8", "9", "11", "14", "15", "17"}
KB_MAX_SECTION_CHARS = 2500   # cap per section to control prompt length
