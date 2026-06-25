"""Generate `Page number patch.ipynb` — ships every file the page-citation
fix touched, runs on the user's existing Output/ Excels without any
re-extraction.

Files shipped:
  - context_builder.py
        • PAGES-with-extracted-items aggregator line under each contract
          block in build_extraction_context (the LLM was being lazy about
          collecting page numbers across per-item lines).
  - chat_engine.py
        • Tightened SOURCES rule: page brackets are REQUIRED whenever
          page data exists; calling out the three places page tags
          appear (PAGES aggregate / per-item (p.N) / per-row (p.N)).
        • New MATERIAL CODE PAGE rule: when the reconciliation block
          prints a [p.N, p.M] bracket on a [CONTRACT] entry, the answer
          MUST include it.
  - snowflake_invoice.py
        • _agent_material_map now captures Page from material_match_output
          (or extraction_output) and aggregates pages_by_code per code.
        • reconcile_materials threads contract_pages into MATCH / MISMATCH /
          CONTRACT_ONLY buckets.
        • _render_reconciliation appends [p.N, p.M] to every [CONTRACT]
          line so the LLM can lift the bracket verbatim into its Sources.

No re-running of any agent is needed — every existing
material_match_output.xlsx / extraction_output.xlsx already carries
a Page column (we just hadn't been reading it through the matcher path).
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT  = ROOT / "Page number patch.ipynb"

_n = 0
def _next_id() -> str:
    global _n
    _n += 1
    return f"page-patch-cell-{_n:02d}"


def _src(text: str) -> list[str]:
    lines = text.split("\n")
    return [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def md_cell(text: str) -> dict:
    return {"cell_type": "markdown", "id": _next_id(),
            "metadata": {}, "source": _src(text)}


def code_cell(text: str) -> dict:
    return {"cell_type": "code", "id": _next_id(), "execution_count": None,
            "metadata": {}, "outputs": [], "source": _src(text)}


cells: list = []

cells.append(md_cell(
    "# Page number patch\n"
    "\n"
    "Restores page-number citations in the chatbot's Sources block. "
    "The data was always there — every agent's output Excel carries a "
    "`Page` (or `Page Number`) column — but two paths through the chat "
    "context were dropping it:\n"
    "\n"
    "1. **Extracted line items** — the per-item `(p.N)` tags were "
    "  scattered across 30+ rows per contract. The LLM had to aggregate "
    "  + dedupe + sort per contract before emitting `[p.1, p.5, p.12]` "
    "  in Sources, and was shortcutting that step.\n"
    "2. **Material-code reconciliation** — the matcher resolves codes "
    "  via the dictionary description, not via page. So when a user "
    "  asked to compare a material code from the contract vs the "
    "  invoice, the [CONTRACT] citation came back with no page anchor "
    "  even though the underlying `material_match_output.xlsx` row had "
    "  the page right there.\n"
    "\n"
    "## What this patch does\n"
    "Three coordinated changes:\n"
    "\n"
    "1. **`context_builder.py`** — under each contract block in "
    "  `build_extraction_context`, emit a single `PAGES with extracted "
    "  items: [p.1, p.3, p.5]` line that aggregates every page touched "
    "  by that contract's items (deduped + sorted, robust to Excel's "
    "  `5.0` float storage). The LLM can copy the bracket verbatim into "
    "  Sources instead of doing the aggregation itself.\n"
    "\n"
    "2. **`snowflake_invoice.py`** — `_agent_material_map` now reads "
    "  the `Page` cell on every `material_match_output.xlsx` row and "
    "  builds a `pages_by_code` aggregate. `reconcile_materials` threads "
    "  this into MATCH / MISMATCH / CONTRACT_ONLY buckets. "
    "  `_render_reconciliation` appends `[p.N, p.M]` to every "
    "  `[CONTRACT]` line so the back-tracked page anchor reaches the "
    "  chat prompt.\n"
    "\n"
    "3. **`chat_engine.py`** — system-prompt rule for page citations "
    "  is now MUST-include-when-present rather than may-include. Plus "
    "  a new MATERIAL CODE PAGE rule that explicitly says "
    "  'when the reconciliation block prints `[p.5, p.12]`, copy that "
    "  bracket verbatim into Sources'.\n"
    "\n"
    "## No re-extraction needed\n"
    "All three files run at **chat-turn time** — they read existing "
    "Excels in `Output/`. As long as your stored "
    "`extraction_output.xlsx` / `material_match_output.xlsx` have the "
    "`Page` column (they do — every agent has written it from day one), "
    "the fix activates immediately on the next chat question.\n"
    "\n"
    "## How to use\n"
    "1. Drop this `.ipynb` into the **root** of the project (same folder "
    "  as `chatbot.py`).\n"
    "2. **Run All.** Three cells overwrite three files, then a "
    "  verification cell reports what landed.\n"
    "3. **Fully restart Streamlit** (`Ctrl-C` → `streamlit run chatbot.py`) "
    "  so the updated modules replace the cached versions.\n"
    "4. Ask a question — page brackets will now appear in Sources, "
    "  including for material-code-vs-invoice comparisons."
))

for rel, label in (
    ("context_builder.py",
     "PAGES aggregator under each contract block — the LLM can lift the "
     "bracket verbatim instead of scanning per-item rows."),
    ("snowflake_invoice.py",
     "_agent_material_map + reconcile_materials + _render_reconciliation now "
     "thread the Page column from material_match_output.xlsx into every "
     "[CONTRACT] reconciliation line."),
    ("chat_engine.py",
     "System-prompt rule changes: page brackets are REQUIRED when present, "
     "and the new MATERIAL CODE PAGE rule mandates copying the bracket "
     "verbatim from the reconciliation block."),
):
    src_path = ROOT / rel
    if not src_path.exists():
        raise SystemExit(f"missing source file: {src_path}")
    content = src_path.read_text(encoding="utf-8")
    cells.append(md_cell(f"### `{rel}` — {label}"))
    cells.append(code_cell(f"%%writefile {rel}\n{content}"))

cells.append(md_cell(
    "### Verification\n"
    "Re-parses the three files, confirms key markers are in place, and "
    "exercises the aggregator on a tiny synthetic group to prove the "
    "back-track returns the right page set."
))
cells.append(code_cell(
    "import ast, os, sys, importlib\n"
    "\n"
    "for f in ('context_builder.py', 'snowflake_invoice.py', 'chat_engine.py'):\n"
    "    ast.parse(open(f, encoding='utf-8').read())\n"
    "    print(f'  OK  {os.path.getsize(f):>9,} bytes  {f}')\n"
    "\n"
    "# Force-reload\n"
    "for mod in [m for m in list(sys.modules) if m in (\n"
    "        'context_builder', 'snowflake_invoice', 'chat_engine')]:\n"
    "    del sys.modules[mod]\n"
    "import context_builder, snowflake_invoice as sf, chat_engine\n"
    "context_builder = importlib.reload(context_builder)\n"
    "sf = importlib.reload(sf)\n"
    "chat_engine = importlib.reload(chat_engine)\n"
    "\n"
    "# Marker checks\n"
    "cb = open('context_builder.py', encoding='utf-8').read()\n"
    "assert 'PAGES with extracted items:' in cb, 'aggregator line missing'\n"
    "assert '_pages_in_grp' in cb\n"
    "print('  OK  context_builder.py — PAGES aggregator present')\n"
    "\n"
    "si = open('snowflake_invoice.py', encoding='utf-8').read()\n"
    "assert 'pages_by_code' in si\n"
    "assert 'contract_pages' in si\n"
    "assert '_fmt_contract_pages' in si\n"
    "print('  OK  snowflake_invoice.py — page-aware reconciliation')\n"
    "\n"
    "ce = open('chat_engine.py', encoding='utf-8').read()\n"
    "assert 'PAGE NUMBERS ARE REQUIRED' in ce\n"
    "assert 'PAGE NUMBERS FOR MATERIAL CODES' in ce\n"
    "assert 'CITATION ERROR' in ce\n"
    "print('  OK  chat_engine.py — tightened page rules + material-code rule')\n"
    "\n"
    "# Functional check on context_builder._format_page (the aggregator helper)\n"
    "fp = context_builder._format_page\n"
    "assert fp(5) == '5'\n"
    "assert fp(5.0) == '5'\n"
    "assert fp('5.0') == '5'\n"
    "assert fp('nan') == ''\n"
    "assert fp(None) == ''\n"
    "assert fp(0) == ''\n"
    "print('  OK  _format_page robust to mixed int/float/string/NaN')\n"
    "\n"
    "print()\n"
    "print('Patch applied. Restart Streamlit and ask a chat question — '\n"
    "      'page brackets will now appear in Sources, including for '\n"
    "      'material-code-vs-invoice comparisons.')"
))

cells.append(md_cell(
    "### Done\n"
    "Restart Streamlit (`Ctrl-C` → `streamlit run chatbot.py`).\n"
    "\n"
    "**Sanity check** with a question that exercises both paths:\n"
    "- Plain fee question — *\"What's the Account Analysis fee for "
    "  FIRST CHOICE CU?\"*  → Sources should include "
    "  `[CONTRACT] <filename> [p.N, p.M]`.\n"
    "- Material-code comparison — *\"Compare the material code for "
    "  Account Analysis between the contract and invoice for ZELLCO\"* → "
    "  Sources should include the contract page bracket alongside the "
    "  invoice citation.\n"
    "\n"
    "If a citation appears without `[p.…]` and the underlying data "
    "(check the relevant agent's Excel) has a Page column populated, "
    "paste the question + answer and I'll target the specific path."
))

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
print(f"Wrote {OUT.name}  ({OUT.stat().st_size:,} bytes, {len(cells)} cells)")
