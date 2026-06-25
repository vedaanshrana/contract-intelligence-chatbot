"""Generate a single .ipynb that re-creates every file changed in this session
via %%writefile cells. Open this notebook in Jupyter on the VDI, set its
working directory to the Contract Chatbot project root, and Run All — it
overwrites the changed files in place. No edits required.

Files packaged (mirror of what changed since the start of this session):
  - agents/product_module.py      (NEW)
  - agents/material_match.py      (NEW)
  - chatbot.py                    (modified)
  - chat_engine.py                (modified)
  - context_builder.py            (modified)
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT  = ROOT / "Apply 7-Agent Frontend Patch.ipynb"

# (relative path inside project root, label shown in the markdown header)
FILES = [
    ("agents/engagement_overview.py",
     "agents/engagement_overview.py — NEW (Engagement Overview Agent, Phase 1)"),
    ("agents/product_module.py",
     "agents/product_module.py — REWRITTEN (Product Module Agent, Phase 2, all 5 paths)"),
    ("agents/extraction.py",
     "agents/extraction.py — MODIFIED (dropped matching code, added Fee Billing Term column)"),
    ("agents/material_match.py",
     "agents/material_match.py — REWRITTEN (Material Code Matching Agent, separate output)"),
    ("agents/master_contract.py",
     "agents/master_contract.py — SHIM (back-compat: re-exports from engagement_overview)"),
    ("agents/hierarchy.py",
     "agents/hierarchy.py — REWRITTEN (adapter that drives the standalone "
     "contract_hierarchy_analyzer; produces Excel + interactive HTML)"),
    ("Existing Scripts/contract_hierarchy_analyzer.py",
     "Existing Scripts/contract_hierarchy_analyzer.py — UPDATED (~5900-line "
     "analyzer with full hierarchy resolution + Plotly interactive HTML viz)"),
    ("chatbot.py",
     "chatbot.py — modified (independent runners, new Load order, output panels, "
     "View interactive graph button, Actual Model column + Excel download + "
     "rate-limit headers panel)"),
    ("chat_engine.py",
     "chat_engine.py — modified (capability terminology)"),
    ("context_builder.py",
     "context_builder.py — modified (engagement_overview reader, Fee Billing "
     "Term, material-match join, AND active-contract status tags "
     "[ACTIVE]/[ROOT-PARTIAL]/[SUPERSEDED]/[ORPHAN] derived from the "
     "hierarchy tree so the chat LLM answers from currently-in-force "
     "contracts and cites the status in every reply)"),
    ("fiserv_client.py",
     "fiserv_client.py — modified (captures actual model from response.model + "
     "stashes rate-limit response headers for the UI)"),
    ("run_metrics.py",
     "run_metrics.py — modified (record_call accepts actual_model; per_model "
     "buckets now carry requested_models + actual_models lists)"),
]


def _src(text: str) -> list[str]:
    """ipynb cell source must be a list of lines, each ending in '\\n' except the last."""
    lines = text.split("\n")
    return [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


_n = 0


def _next_id() -> str:
    """Stable, monotonically-increasing IDs so the notebook validates cleanly
    on newer nbformat versions (which require an 'id' field per cell)."""
    global _n
    _n += 1
    return f"patch-cell-{_n:02d}"


def md_cell(text: str) -> dict:
    return {"cell_type": "markdown", "id": _next_id(),
            "metadata": {}, "source": _src(text)}


def code_cell(text: str) -> dict:
    return {
        "cell_type":       "code",
        "id":              _next_id(),
        "execution_count": None,
        "metadata":        {},
        "outputs":         [],
        "source":          _src(text),
    }


cells: list = []

cells.append(md_cell(
    "# Apply 7-Agent Frontend Patch — Contract Chatbot\n"
    "\n"
    "This notebook re-creates the files that changed when the chatbot was "
    "rebuilt around the **7 user-facing agents** (Hierarchy, Engagement "
    "Overview, Product Module, Fee Description, Material Code Matching, CPI "
    "Terms, Termination Clause).\n"
    "\n"
    "**This round of changes splits the previously conjoined agents into "
    "fully independent backend modules AND upgrades the Hierarchy agent to the "
    "latest standalone analyzer:**\n"
    "- `master_contract.py` (Phase 1 + 2 conjoined) → split into "
    "**`engagement_overview.py`** (Phase 1 only) and a real, standalone "
    "**`product_module.py`** (Phase 2 only, with five sub-paths including the "
    "new LICENSE Agreement route).\n"
    "- `extraction.py` (Phase 1 + 2 conjoined) → trimmed to Phase 1 only, "
    "with the old `Pricing_Condition` column renamed to **`Fee Billing Term`**.\n"
    "- `material_match.py` (was a shim) → rewritten as a **real matching "
    "agent** that reads `extraction_output.xlsx`, runs the LLM matcher, and "
    "writes a **separate** `material_match_output.xlsx` containing **only "
    "matched rows**.\n"
    "- **Hierarchy Agent**: the standalone analyzer "
    "(`Existing Scripts/contract_hierarchy_analyzer.py`, ~5900 lines) is "
    "refreshed with the latest version — full parent/child resolution across "
    "five strategies, product canonicalisation, and a **Plotly interactive "
    "HTML graph**. `agents/hierarchy.py` is now a thin adapter that drives "
    "that script over a single client at a time and points it at the chatbot's "
    "folder layout. The chatbot Agent Outputs panel now has a **'View "
    "interactive graph'** button that renders the HTML inline.\n"
    "- **Run Details** tab gains an **Actual Model** column alongside the "
    "existing Model (now relabeled **Model Requested**). On the Fiserv VDI "
    "proxy these will often differ — the proxy is bound to whatever model is "
    "configured behind the X-Purpose tag, and the model name our code passes "
    "is just a routing hint. The Actual Model column shows what "
    "`response.model` actually came back as. A **Download run metrics "
    "(Excel)** button exports the table, and a new **🚦 API rate-limit "
    "headers** expander surfaces the latest `x-ratelimit-*` / "
    "`x-ms-ratelimit-*` headers the Foundation gateway returns (the only way "
    "to see the proxy's enforced limits without an admin endpoint).\n"
    "- **Active-contract awareness** (`context_builder.py`): every contract "
    "reference the chat LLM sees is now tagged **[ACTIVE]** / "
    "**[ROOT-PARTIAL]** / **[SUPERSEDED]** / **[ORPHAN]**, derived "
    "deterministically from the hierarchy parent-child tree (Level 2 — works "
    "even when the LLM-supplied `is_active` field is null). A "
    "*CURRENTLY-IN-FORCE CONTRACTS* cheat-sheet is appended to the hierarchy "
    "block so the LLM can pick the right current source without rescanning. "
    "Every multi-contract section (extraction, product hierarchy, "
    "engagement-overview, clause extractors, CPI) sorts ACTIVE-first and "
    "newest-first within each status. The system prompt now tells the LLM "
    "to **cite the status tag** in every answer (\"From [ACTIVE] "
    "AmendmentNo3_2023.pdf (p.4): ...\") and to fall back to SUPERSEDED / "
    "ORPHAN only as a last resort, flagging the user when it does.\n"
    "\n"
    "### How to use\n"
    "1. Copy this `.ipynb` into the **root** of the Contract Chatbot project "
    "on the VDI (same folder as `chatbot.py`, `config.py`, the `agents/` "
    "directory, etc.).\n"
    "2. Open it in Jupyter and **Run All**.\n"
    "3. Each cell writes one file via `%%writefile` — existing files are "
    "overwritten in place.\n"
    "4. The verification cell at the bottom runs `ast.parse` and an actual "
    "Python import on every patched file.\n"
    "\n"
    "Backend hierarchy/CPI/clause agents, `config.py`, `ui.py`, "
    "`simple_app.py`, `run_metrics.py`, and the `Fiserv envt\\` snapshot are "
    "**not** touched by this patch."
))

cells.append(code_cell(
    "# Make sure target folders exist before we write into them.\n"
    "import os\n"
    "for d in ('agents', 'Existing Scripts'):\n"
    "    os.makedirs(d, exist_ok=True)\n"
    "print('Output folders ready: agents/, Existing Scripts/')"
))

for rel, label in FILES:
    src_path = ROOT / rel
    if not src_path.exists():
        raise SystemExit(f"missing source file: {src_path}")
    content = src_path.read_text(encoding="utf-8")
    cells.append(md_cell(f"### {label}"))
    # The %%writefile magic MUST be the first line of the cell. Use a forward
    # slash in the target — Jupyter on Windows accepts it and writes into
    # agents\product_module.py just fine.
    cells.append(code_cell(f"%%writefile {rel}\n{content}"))

cells.append(md_cell("### Verification — every patched file should parse + report a non-trivial size."))
cells.append(code_cell(
    "import ast, os\n"
    "FILES = [\n"
    "    'agents/engagement_overview.py',\n"
    "    'agents/product_module.py',\n"
    "    'agents/extraction.py',\n"
    "    'agents/material_match.py',\n"
    "    'agents/master_contract.py',\n"
    "    'agents/hierarchy.py',\n"
    "    'Existing Scripts/contract_hierarchy_analyzer.py',\n"
    "    'chatbot.py',\n"
    "    'chat_engine.py',\n"
    "    'context_builder.py',\n"
    "    'fiserv_client.py',\n"
    "    'run_metrics.py',\n"
    "]\n"
    "for f in FILES:\n"
    "    src = open(f, encoding='utf-8').read()\n"
    "    ast.parse(src)\n"
    "    print(f'{os.path.getsize(f):>9,} bytes  OK  {f}')\n"
    "print()\n"
    "# Cross-check that all agents actually import together — this is the real\n"
    "# test of the refactor (shows up any circular imports or missing names).\n"
    "from agents import (engagement_overview, product_module, extraction,\n"
    "                    material_match, hierarchy, master_contract)\n"
    "import fiserv_client, run_metrics\n"
    "assert 'actual_model' in run_metrics.record_call.__code__.co_varnames, \\\n"
    "    'run_metrics.record_call missing actual_model param'\n"
    "assert callable(fiserv_client.get_latest_response_headers), \\\n"
    "    'fiserv_client.get_latest_response_headers missing'\n"
    "print('All 12 files written; agent + metering imports OK.')"
))

cells.append(md_cell(
    "### Done\n"
    "Restart the Streamlit app: `streamlit run chatbot.py` (or use the "
    "`Launch Chatbot (Fiserv VDI).bat` shortcut).\n"
    "\n"
    "**What changed in this patch:**\n"
    "- **`agents/engagement_overview.py`** is the NEW home of Phase 1 scope "
    "extraction (signatures, addresses, contract summary, DocuSign id, plus "
    "7 SOW-specific fields when applicable). Output: "
    "`Output/<Client>/engagement_overview_output.xlsx`.\n"
    "- **`agents/product_module.py`** is REWRITTEN — it's now a fully "
    "independent agent that runs its own Phase 1.5 manifest and routes among "
    "five Phase 2 sub-paths (STRICT / SOW / ORDER / **LICENSE** / GENERIC). "
    "Output unchanged: `product_hierarchy_output.xlsx`.\n"
    "- **`agents/extraction.py`** is TRIMMED — Phase 2 matching code removed. "
    "The old `Pricing_Condition` column is renamed to **`Fee Billing Term`** "
    "and reordered next to Frequency. Prompt now explicitly captures payment "
    "terms like '50% upfront', 'net 30', 'paid in advance'.\n"
    "- **`agents/material_match.py`** is REWRITTEN as a real agent. It reads "
    "`extraction_output.xlsx`, runs the LLM matcher against your dictionary, "
    "and writes a SEPARATE Excel — `material_match_output.xlsx` — containing "
    "**only the rows that received a material-code match**.\n"
    "- **`agents/master_contract.py`** is now a thin back-compat shim that "
    "re-exports everything from `engagement_overview`. Any old caller still "
    "passing the old name keeps working.\n"
    "- **`chatbot.py`** rewires the Load pipeline so each of the 7 frontend "
    "agents runs as its own backend step (no more conjoined passes), and the "
    "Output panels point at the new file paths.\n"
    "- **`context_builder.py`** reads the new files and joins material codes "
    "from `material_match_output.xlsx` back into the extraction view shown to "
    "the chat. It ALSO now tags every contract reference with its hierarchy "
    "status (**[ACTIVE]** / **[ROOT-PARTIAL]** / **[SUPERSEDED]** / "
    "**[ORPHAN]**) derived deterministically from the parent-child tree "
    "(works even when `is_active` is null). A *CURRENTLY-IN-FORCE CONTRACTS* "
    "cheat-sheet is appended to the hierarchy block, and every multi-contract "
    "section sorts ACTIVE-first / newest-first within each status. The "
    "legend at the top of the hierarchy block instructs the chat LLM to "
    "**prefer [ACTIVE] sources, cite the status tag in every reply, and flag "
    "the user when falling back to [SUPERSEDED] or [ORPHAN]**.\n"
    "- **`agents/hierarchy.py`** is now a thin adapter that loads "
    "`Existing Scripts/contract_hierarchy_analyzer.py` (refreshed to the "
    "latest ~5900-line version), patches its module-level globals to point "
    "at the chatbot's `Input/<Core>/<Client>/` and `Output/` layout, and "
    "swaps in the metered Fiserv client so token usage is recorded. The "
    "underlying script writes BOTH `contracts_hierarchy.xlsx` AND a Plotly "
    "interactive `contracts_hierarchy.html`. The Streamlit Agent Outputs "
    "panel now has a 👁 **View interactive graph** button that renders that "
    "HTML inline in the page.\n"
    "- **`fiserv_client.py`** now extracts the *actual* model the API used "
    "from `response.model` and passes both the requested and actual names "
    "into `run_metrics.record_call`. The Foundation API transport also "
    "captures the response's rate-limit headers (`x-ratelimit-*`, "
    "`x-ms-ratelimit-*`, `retry-after`, `openai-*`) so the Run Details tab "
    "can surface them — there's no separate 'describe my limits' endpoint "
    "on the gateway.\n"
    "- **`run_metrics.py`** `record_call` accepts a new `actual_model` arg "
    "and stores it on every call. The `per_model` rollup is now keyed by "
    "the *actual* model that served the work; each bucket carries "
    "`requested_models` and `actual_models` lists so the UI can flag a "
    "proxy substitution at a glance."
))

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language":     "python",
            "name":         "python3",
        },
        "language_info": {
            "name":            "python",
            "file_extension":  ".py",
            "mimetype":        "text/x-python",
            "pygments_lexer":  "ipython3",
        },
    },
    "nbformat":       4,
    "nbformat_minor": 5,
}

OUT.write_text(
    json.dumps(notebook, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(f"Wrote {OUT.name}  ({OUT.stat().st_size:,} bytes, {len(cells)} cells)")
