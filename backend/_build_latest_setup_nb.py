"""Generate `LATEST/Apply Full Project Setup.ipynb` — a single notebook that
recreates the entire Contract Chatbot project from a clean folder.

Designed for the VDI rollout where the colleague needs to drop everything
into a fresh directory and run a notebook that lays the project down for
them. Replaces the per-feature patch notebooks for this one purpose: this
is the "fresh-install" route, the patches are the "drop-in fix" route.

The output notebook layout:
  1. Markdown intro + usage instructions
  2. Directory setup cell (creates agents/, Input/, Output/, Existing Scripts/)
  3. Dependency install cell (pip install -r requirements.txt — commented
     out by default so the user can choose; gets uncommented and run if
     they're on a fresh machine)
  4. For every source file: a %%writefile cell with the file's verbatim
     contents (one cell per file, prefixed by a markdown header explaining
     what the file does)
  5. For binary / .ipynb files: a base64 + Path.write_bytes() cell
  6. Final verification cell — ast.parse every .py, confirm marked_checkbox
     image is in place, list all output files

The notebook is self-contained — no patches need to be applied afterwards.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

ROOT    = Path(__file__).resolve().parent
OUT_DIR = ROOT / "LATEST"
OUT_DIR.mkdir(exist_ok=True)
OUT     = OUT_DIR / "Apply Full Project Setup.ipynb"


# ─────────────────────────────────────────────────────────────────────────────
# FILE INVENTORY — what to ship, grouped for readable notebook sections.
# Files are written verbatim from disk (whatever's there now is what ends
# up in the notebook).
# ─────────────────────────────────────────────────────────────────────────────

# Root-level source files (text). Tuple: (relative path, one-line description).
ROOT_TEXT_FILES = [
    ("requirements.txt",
     "Pinned Python dependencies — install with `pip install -r requirements.txt`."),
    ("config.py",
     "Central config: model names, API keys, folder layout, per-Core defaults."),
    ("fiserv_client.py",
     "OpenAI-shaped wrapper that routes calls through OpenAI directly OR the "
     "Fiserv Foundation API proxy when OPENAI_BACKEND=fiserv."),
    ("run_metrics.py",
     "Lightweight per-call token + runtime metrics store used by the sidebar."),
    ("chat_engine.py",
     "ChatEngine — builds the system prompt and routes chat turns to the LLM."),
    ("context_builder.py",
     "Per-client context assembly for the chat tab — pulls every agent's "
     "output into one prompt with hierarchy-status tags."),
    ("chatbot.py",
     "Main Streamlit app. The single entrypoint: `streamlit run chatbot.py`."),
    ("ui.py",
     "Small UI helpers used by the main app."),
    ("simple_app.py",
     "Minimal CLI / batch alternative to the Streamlit app (rarely used)."),
    ("snowflake_invoice.py",
     "Live SAP invoice context module — SFlogic-style matching, direct "
     "invoice lookup, per-invoice + grand totals, material-code summary."),
    ("snowflake_config.example.toml",
     "Template for the Snowflake credential file (`snowflake_config.toml` is "
     "git-ignored and supplied at runtime on VDI)."),
    ("FD306_Full_Context_KnowledgeBase.md",
     "Fiserv SAP billing reference embedded into every chat prompt."),
    ("PROJECT_OVERVIEW.md",
     "High-level project orientation for new contributors."),
]

# Agents/* — one cell per agent (each is a substantial Python module).
AGENT_FILES = [
    ("agents/__init__.py",
     "Package marker (empty)."),
    ("agents/_text_utils.py",
     "Small text-normalisation helpers shared across agents."),
    ("agents/hierarchy.py",
     "Hierarchy Agent — wraps Existing Scripts/contract_hierarchy_analyzer.py."),
    ("agents/engagement_overview.py",
     "Engagement Overview Agent (Phase 1 — addresses, signatures, summary)."),
    ("agents/product_module.py",
     "Product Module Agent (Phase 2 — products and modules per contract)."),
    ("agents/extraction.py",
     "PORTICO Fee Description Agent — verbatim port of Harshit's notebook + "
     "chunk retry + per-chunk diagnostics + sidecar metadata."),
    ("agents/material_match.py",
     "Material Code Matching Agent — thin re-export of extraction.run_matching."),
    ("agents/dna_extraction.py",
     "DNA Fee Description + Material Code Matching — verbatim port of the "
     "DNA notebook (chat.completions.create API, section-aware matcher, "
     "anchor-keyword index)."),
    ("agents/master_contract.py",
     "Backward-compat shim re-exporting engagement_overview."),
    ("agents/scope_agent.py",
     "Cheap per-agent contract triage so each agent runs only on relevant files."),
    ("agents/cpi.py",
     "CPI Terms Agent — annual escalation clauses (floors, caps, eligibility)."),
    ("agents/clause_extractor.py",
     "Shared clause-extraction primitives used by the four clause agents."),
    ("agents/termination.py",
     "Termination Clause Agent — for-cause + for-convenience + survival."),
    ("agents/term_renewal.py",
     "Term & Renewal Agent — initial term, renewal period, auto-renew."),
    ("agents/sla.py",
     "SLA & Service-Credit Agent — uptime, credit formulas, response time."),
    ("agents/volume_tiers.py",
     "Volume Tier Agent — minimum commitments, tier breakpoints, true-up."),
    ("agents/pdf_annotator.py",
     "(Helper — utilities for annotating PDFs; not used at runtime)."),
]

# Binary files — base64-encoded inside the notebook.
BINARY_FILES = [
    ("marked_checkbox_example.png",
     "Reference image used by extraction prompts to calibrate checkbox "
     "detection. Required at the project root for accurate extraction."),
]

# Reference notebooks (treated as binary because they're large JSON blobs;
# embedding via %%writefile would risk escaping issues).
NOTEBOOK_FILES = [
    ("Existing Scripts/Contract Extraction.ipynb",
     "PORTICO reference notebook — colleague's working extraction + matching."),
    ("Existing Scripts/DNA Latest Extraction Matching 06.03.ipynb",
     "DNA reference notebook — colleague's working extraction + matching."),
    ("Existing Scripts/CPI Final Output.ipynb",
     "CPI reference notebook — colleague's working CPI extraction."),
    ("Existing Scripts/contract_hierarchy_analyzer.py",
     "Colleague's standalone hierarchy analyzer (text but lives under "
     "Existing Scripts/)."),
]


# ─────────────────────────────────────────────────────────────────────────────
# Notebook-cell helpers
# ─────────────────────────────────────────────────────────────────────────────

_n = 0
def _next_id() -> str:
    global _n
    _n += 1
    return f"setup-cell-{_n:03d}"


def _src(text: str) -> list[str]:
    """Convert a string into Jupyter's `source` list (one line per element,
    each terminated with newline except possibly the last)."""
    lines = text.split("\n")
    return [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def md_cell(text: str) -> dict:
    return {"cell_type": "markdown", "id": _next_id(),
            "metadata": {}, "source": _src(text)}


def code_cell(text: str) -> dict:
    return {"cell_type": "code", "id": _next_id(), "execution_count": None,
            "metadata": {}, "outputs": [], "source": _src(text)}


def _write_text_cell(rel: str) -> dict:
    """%%writefile cell for a text source file."""
    p = ROOT / rel
    content = p.read_text(encoding="utf-8")
    # Ensure trailing newline so subsequent file diff cleanly.
    if not content.endswith("\n"):
        content += "\n"
    return code_cell(f"%%writefile {rel}\n{content}")


def _write_binary_cell(rel: str) -> dict:
    """base64-decode + write_bytes() cell for a binary file (PNG, .ipynb)."""
    p = ROOT / rel
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    # Wrap the base64 string at 76 chars per Python convention.
    chunks = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return code_cell(
        "import base64\n"
        "from pathlib import Path\n"
        f"_target = Path({rel!r})\n"
        "_target.parent.mkdir(parents=True, exist_ok=True)\n"
        "_b64 = (\n"
        f'"""{chunks}"""\n'
        ")\n"
        "_target.write_bytes(base64.b64decode(_b64))\n"
        f"print(f'wrote {{_target}}  '\n"
        f"      f'({{_target.stat().st_size:,}} bytes)')"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build the cell list
# ─────────────────────────────────────────────────────────────────────────────

cells: list = []

# ── Intro ────────────────────────────────────────────────────────────────────
cells.append(md_cell(
    "# Apply Full Project Setup — Contract Chatbot\n"
    "\n"
    "Drop this notebook into a **fresh, empty folder** on the target machine "
    "(VDI or laptop) and click **Run All**. Every file the chatbot needs at "
    "runtime gets written into the correct relative path. After the notebook "
    "completes you'll have a complete, working copy of the project — just "
    "fill in the runtime config (`.env`, `snowflake_config.toml`, `Input/`).\n"
    "\n"
    "### What this notebook creates\n"
    "1. **Directory structure** — `agents/`, `Input/PORTICO/`, `Input/DNA/`, "
    "  `Output/`, `Existing Scripts/`.\n"
    "2. **Top-level source files** — `chatbot.py`, `config.py`, "
    "  `chat_engine.py`, `context_builder.py`, `fiserv_client.py`, "
    "  `run_metrics.py`, `ui.py`, `simple_app.py`, `snowflake_invoice.py`, "
    "  knowledge base, requirements.\n"
    "3. **All 7 frontend agents** under `agents/` (PORTICO extraction + "
    "  matching, DNA extraction + matching, hierarchy, engagement overview, "
    "  product module, CPI, clause agents).\n"
    "4. **Reference image** `marked_checkbox_example.png` for the extraction "
    "  prompt's checkbox calibration.\n"
    "5. **Reference notebooks** under `Existing Scripts/` (the colleague's "
    "  working PORTICO + DNA + CPI scripts kept for diff reference).\n"
    "6. **Templates** — `snowflake_config.example.toml` (the real "
    "  `snowflake_config.toml` is supplied per-machine on VDI).\n"
    "\n"
    "### After Run All\n"
    "1. (optional) Install deps:  `pip install -r requirements.txt`\n"
    "2. Copy your `.env` (the colleague's secrets file) into the project "
    "  root, or create one from scratch — see `config.py` for the keys it "
    "  reads.\n"
    "3. Drop client PDFs into `Input/<CORE>/<CLIENT NAME>/`.\n"
    "4. Drop the per-core dictionaries:\n"
    "   - `Input/PORTICO/Portico Dictionary 05-26.xlsx`\n"
    "   - `Input/DNA/DNA Dictionary US 03-26.xlsx`\n"
    "   - `Input/DNA/DNA Section Headers Normalization.xlsx`\n"
    "5. On VDI: create `snowflake_config.toml` from the example template "
    "  and paste your real PAT.\n"
    "6. Launch:  `streamlit run chatbot.py`\n"
    "\n"
    "### Backend selection (important on VDI)\n"
    "Set in `.env` or shell:\n"
    "```\n"
    "OPENAI_BACKEND=fiserv          # in VDI — routes via the Foundation proxy\n"
    "FISERV_EMAIL=you@fiserv.com    # your @fiserv.com address\n"
    "```\n"
    "On a laptop with OpenAI access leave `OPENAI_BACKEND=openai` (default).\n"
    "\n"
    "---"
))

# ── Directory setup ──────────────────────────────────────────────────────────
cells.append(md_cell(
    "### Step 1 — Create directory structure\n"
    "All other cells assume these folders exist."
))
cells.append(code_cell(
    "from pathlib import Path\n"
    "\n"
    "for d in (\n"
    "    'agents',\n"
    "    'Existing Scripts',\n"
    "    'Input', 'Input/PORTICO', 'Input/DNA',\n"
    "    'Output',\n"
    "):\n"
    "    Path(d).mkdir(parents=True, exist_ok=True)\n"
    "    print(f'  ✓ {d}/')\n"
    "print()\n"
    "print('Directory structure ready.')"
))

cells.append(md_cell(
    "### Step 2 — (Optional) Install Python dependencies\n"
    "Skip if your environment already has them. Uncomment and run otherwise. "
    "On Fiserv VDI ensure pip is configured for the internal proxy first."
))
cells.append(code_cell(
    "# import sys, subprocess\n"
    "# subprocess.run([sys.executable, '-m', 'pip', 'install',\n"
    "#                 '-r', 'requirements.txt'], check=True)\n"
    "# print('Dependencies installed.')\n"
    "print('(skipping pip install — uncomment the lines above if needed)')"
))

# ── Top-level source files ──────────────────────────────────────────────────
cells.append(md_cell(
    "## Step 3 — Top-level source files\n"
    "Each cell below writes one file. The cells are grouped by category for "
    "readability but they can be run in any order — they don't depend on "
    "each other."
))
for rel, label in ROOT_TEXT_FILES:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"missing source file: {p}")
    cells.append(md_cell(f"### `{rel}` — {label}"))
    cells.append(_write_text_cell(rel))

# ── Agents ──────────────────────────────────────────────────────────────────
cells.append(md_cell(
    "## Step 4 — Agents (`agents/`)\n"
    "All 17 modules under `agents/`. The unusually large ones are:\n"
    "- `extraction.py` — PORTICO Fee Description + Material Matching\n"
    "- `dna_extraction.py` — DNA Fee Description + Material Matching\n"
    "- `product_module.py` — Phase 2 Product Module Agent (5 paths)\n"
    "- `cpi.py` — CPI Terms Agent"
))
for rel, label in AGENT_FILES:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"missing agent file: {p}")
    cells.append(md_cell(f"### `{rel}` — {label}"))
    cells.append(_write_text_cell(rel))

# ── Binary assets ───────────────────────────────────────────────────────────
cells.append(md_cell(
    "## Step 5 — Binary assets\n"
    "Files that can't go through %%writefile (images, .ipynb JSON blobs). "
    "Each is base64-encoded inline; the cell decodes and writes to disk."
))
for rel, label in BINARY_FILES:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"missing binary file: {p}")
    cells.append(md_cell(f"### `{rel}` — {label}"))
    cells.append(_write_binary_cell(rel))

# ── Reference notebooks ─────────────────────────────────────────────────────
cells.append(md_cell(
    "## Step 6 — Reference scripts under `Existing Scripts/`\n"
    "These are the colleague's working notebooks (and the hierarchy "
    "analyzer .py). Kept here for diff reference — the chatbot's agents "
    "are ports of these notebooks. NOT required at runtime, but useful "
    "when investigating extraction / matching drift."
))
for rel, label in NOTEBOOK_FILES:
    p = ROOT / rel
    if not p.exists():
        print(f"⚠ skipping missing reference file: {p}")
        continue
    cells.append(md_cell(f"### `{rel}` — {label}"))
    cells.append(_write_binary_cell(rel))

# ── Verification ────────────────────────────────────────────────────────────
cells.append(md_cell(
    "## Step 7 — Verification\n"
    "Parses every Python file, confirms the package structure, lists the "
    "checkbox reference image, and prints a quick summary so you know "
    "everything landed correctly."
))
cells.append(code_cell(
    "import ast, os, sys\n"
    "from pathlib import Path\n"
    "\n"
    "errors = []\n"
    "py_count = 0\n"
    "total_size = 0\n"
    "\n"
    "for p in sorted(Path('.').rglob('*.py')):\n"
    "    if any(part.startswith('.') or part == '__pycache__'\n"
    "           for part in p.parts):\n"
    "        continue\n"
    "    py_count += 1\n"
    "    total_size += p.stat().st_size\n"
    "    try:\n"
    "        ast.parse(p.read_text(encoding='utf-8'))\n"
    "    except SyntaxError as e:\n"
    "        errors.append(f'  ✗ {p}: {e}')\n"
    "\n"
    "print(f'Parsed {py_count} Python file(s), total {total_size:,} bytes.')\n"
    "if errors:\n"
    "    print()\n"
    "    print('Syntax errors:')\n"
    "    for e in errors:\n"
    "        print(e)\n"
    "    raise SystemExit('Fix the syntax errors above before continuing.')\n"
    "\n"
    "print()\n"
    "print('Critical assets:')\n"
    "for f in (\n"
    "    'chatbot.py', 'config.py', 'fiserv_client.py', 'context_builder.py',\n"
    "    'snowflake_invoice.py', 'requirements.txt',\n"
    "    'marked_checkbox_example.png', 'snowflake_config.example.toml',\n"
    "    'agents/extraction.py', 'agents/material_match.py',\n"
    "    'agents/dna_extraction.py',\n"
    "):\n"
    "    p = Path(f)\n"
    "    print(f'  {\"OK\" if p.exists() else \"MISSING\":>7}  '\n"
    "          f'{p.stat().st_size:>9,} bytes  {f}'\n"
    "          if p.exists() else f'  MISSING  {f}')\n"
    "\n"
    "print()\n"
    "print('Directory tree:')\n"
    "for d in sorted(Path('.').rglob('*')):\n"
    "    if d.is_dir() and not any(p.startswith('.') or p == '__pycache__'\n"
    "                              for p in d.parts):\n"
    "        depth = len(d.parts)\n"
    "        if depth <= 2:\n"
    "            print(f'  {\"  \" * (depth - 1)}{d.name}/')\n"
    "\n"
    "print()\n"
    "print('Setup complete. Next:')\n"
    "print('  1. (optional) pip install -r requirements.txt')\n"
    "print('  2. Create .env  (see config.py for the keys it reads)')\n"
    "print('  3. Drop client PDFs into Input/<CORE>/<CLIENT>/')\n"
    "print('  4. Drop core dictionaries into Input/<CORE>/')\n"
    "print('  5. streamlit run chatbot.py')"
))


# ─────────────────────────────────────────────────────────────────────────────
# Write the notebook
# ─────────────────────────────────────────────────────────────────────────────

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python",
                       "name": "python3"},
        "language_info": {"name": "python", "file_extension": ".py",
                          "mimetype": "text/x-python",
                          "pygments_lexer": "ipython3"},
    },
    "nbformat": 4, "nbformat_minor": 5,
}

OUT.write_text(json.dumps(notebook, ensure_ascii=False, indent=1),
               encoding="utf-8")
print(f"Wrote {OUT}")
print(f"  {OUT.stat().st_size:,} bytes, {len(cells)} cells, "
      f"{len(ROOT_TEXT_FILES)} root files, {len(AGENT_FILES)} agents, "
      f"{len(BINARY_FILES)} binary, {len(NOTEBOOK_FILES)} notebooks/scripts")
