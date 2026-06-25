"""Generate `Apply Snowflake Invoice Patch.ipynb` — a notebook that deploys
ONLY the files added/changed to wire up the live SAP-invoice (Snowflake)
context. Mirrors _build_patch_nb.py's structure: one %%writefile cell per file,
plus a setup cell, a config-template cell, and a verification cell.

Files packaged:
  - snowflake_invoice.py            (NEW — connector + bridge + reconciliation)
  - snowflake_config.example.toml   (NEW — credential template; copy → .toml)
  - chat_engine.py                  (MODIFIED — invoice slot + [CONTRACT]/[INVOICE] sources)
  - chatbot.py                      (MODIFIED — keyword gating, fetch, links, viewer, diagnostic)

This patch is independent of the 7-Agent Frontend patch but assumes that one is
already applied (it overwrites the latest chatbot.py / chat_engine.py, which
include all prior changes).
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT  = ROOT / "Apply Snowflake Invoice Patch.ipynb"

FILES = [
    ("snowflake_invoice.py",
     "snowflake_invoice.py — NEW (Snowflake connector, engagement/contract "
     "bridge, material-code reconciliation, keyword gating, build_invoice_context)"),
    ("chat_engine.py",
     "chat_engine.py — MODIFIED (invoice capability + {invoice_context} slot + "
     "[CONTRACT]/[INVOICE] source tags + material-reconciliation rules)"),
    ("chatbot.py",
     "chatbot.py — MODIFIED (invoice keyword gating before chat, fetch + inject "
     "invoice context, render cited [INVOICE] links + best-effort PDF viewer, "
     "on-demand Snowflake connection diagnostic)"),
]


def _src(text: str) -> list[str]:
    lines = text.split("\n")
    return [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


_n = 0
def _next_id() -> str:
    global _n
    _n += 1
    return f"sf-patch-cell-{_n:02d}"


def md_cell(text: str) -> dict:
    return {"cell_type": "markdown", "id": _next_id(),
            "metadata": {}, "source": _src(text)}


def code_cell(text: str) -> dict:
    return {"cell_type": "code", "id": _next_id(), "execution_count": None,
            "metadata": {}, "outputs": [], "source": _src(text)}


cells: list = []

cells.append(md_cell(
    "# Apply Snowflake Invoice Patch — Contract Chatbot\n"
    "\n"
    "Adds a **live SAP invoice context** (pulled from Snowflake) to the chatbot. "
    "The chatbot consults it **only** when a question mentions invoice / billing "
    "/ SAP terms; every other question behaves exactly as before and never "
    "touches Snowflake.\n"
    "\n"
    "### What it does\n"
    "- **Keyword-gated**: invoice data is fetched only for questions containing "
    "`invoice`, `bill/billed/billing`, `SAP`, `net/tax amount`, `material code`, "
    "`GL`, `profit center`, `sales office/group`, etc.\n"
    "- **Engagement bridge**: fuzzy-matches the focused client name to the "
    "invoice table's `OTC_SIH_BILLTO_NAME` (rapidfuzz, FCU↔Federal Credit Union "
    "expansion, legal-suffix stripping) → resolves the `OTC_SIH_BILLTO` "
    "customer code(s). Only that customer's invoices are pulled.\n"
    "- **Contract bridge**: maps each invoice line's `OTC_SIL_MATERIAL` back to "
    "the specific contract the Material Code Matching agent linked it to "
    "(`Source Contract`), so answers cite the exact `[CONTRACT]`.\n"
    "- **Material-code reconciliation** (the headline use-case): compares the "
    "agent/dictionary code vs the actual invoice code and classifies each as "
    "**MATCH / MISMATCH / INVOICE-ONLY / CONTRACT-ONLY**. On a MISMATCH it "
    "surfaces BOTH codes with their sources and lets the user decide; on an "
    "INVOICE-ONLY miss it uses the invoice's code tagged `[INVOICE]`.\n"
    "- **Sources**: contract facts tagged `[CONTRACT] <file> [p.N]`; invoice "
    "facts tagged `[INVOICE] <doc #> — <url>`. The UI renders clickable invoice "
    "links and a best-effort 'open in viewer' button (fetches the invoice URL "
    "and shows it in `streamlit-pdf-viewer`; falls back to the link when the "
    "URL needs VDI auth/network).\n"
    "\n"
    "### How to use\n"
    "1. Copy this `.ipynb` into the **root** of the Contract Chatbot project on "
    "the VDI (same folder as `chatbot.py`).\n"
    "2. **Run All.** Each cell writes one file via `%%writefile`.\n"
    "3. Run `pip install snowflake-connector-python` (and `tomli` if your Python "
    "is < 3.11) — see the setup cell.\n"
    "4. Copy `snowflake_config.example.toml` → **`snowflake_config.toml`** and "
    "paste your real credential (the `eyJ…` PAT goes in `password`). This file "
    "is git-ignored and is NOT shipped in the notebook.\n"
    "5. Restart Streamlit. Open the **🧾 SAP invoice (Snowflake) connection** "
    "expander under the contract viewer and click **Test connection** to "
    "confirm.\n"
    "\n"
    "Nothing else in the app changes; `config.py`, `context_builder.py`, and the "
    "agents are untouched by this patch."
))

cells.append(md_cell(
    "### Dependencies\n"
    "Run this once on the VDI (the connector is an OPTIONAL dependency — until "
    "it's installed the chatbot still runs and simply reports invoice data as "
    "unavailable)."
))
cells.append(code_cell(
    "import sys, subprocess\n"
    "# snowflake-connector-python is required for live invoice lookups.\n"
    "# tomli is only needed on Python < 3.11 (3.11+ has tomllib built in).\n"
    "pkgs = ['snowflake-connector-python']\n"
    "if sys.version_info < (3, 11):\n"
    "    pkgs.append('tomli')\n"
    "print('Installing:', pkgs)\n"
    "subprocess.run([sys.executable, '-m', 'pip', 'install', *pkgs], check=False)\n"
    "print('Done. (rapidfuzz / httpx / streamlit-pdf-viewer are already project deps.)')"
))

for rel, label in FILES:
    src_path = ROOT / rel
    if not src_path.exists():
        raise SystemExit(f"missing source file: {src_path}")
    content = src_path.read_text(encoding="utf-8")
    cells.append(md_cell(f"### {label}"))
    cells.append(code_cell(f"%%writefile {rel}\n{content}"))

# Ship the config TEMPLATE (not the real secret-bearing file).
cells.append(md_cell(
    "### snowflake_config.example.toml — credential TEMPLATE\n"
    "Writes the template. **You then copy it to `snowflake_config.toml` and "
    "paste your real credential** — that real file is git-ignored and stays on "
    "the VDI only."
))
_example = (ROOT / "snowflake_config.example.toml").read_text(encoding="utf-8")
cells.append(code_cell(f"%%writefile snowflake_config.example.toml\n{_example}"))

cells.append(md_cell(
    "### Verification — files parse, imports resolve, gating + reconciliation work."
))
cells.append(code_cell(
    "import ast, os\n"
    "FILES = ['snowflake_invoice.py', 'chat_engine.py', 'chatbot.py',\n"
    "         'snowflake_config.example.toml']\n"
    "for f in FILES:\n"
    "    if f.endswith('.py'):\n"
    "        ast.parse(open(f, encoding='utf-8').read())\n"
    "    print(f'{os.path.getsize(f):>9,} bytes  OK  {f}')\n"
    "print()\n"
    "import snowflake_invoice as sf, chat_engine\n"
    "# Gating\n"
    "assert sf.is_invoice_query('what did we bill this client?')\n"
    "assert sf.is_invoice_query('show the SAP material code')\n"
    "assert not sf.is_invoice_query('who signed the master agreement?')\n"
    "# Name bridge\n"
    "assert sf._normalize_name('Blue Cross Texas FCU') == \\\n"
    "       sf._normalize_name('BLUE CROSS TEXAS FEDERAL CREDIT UNION')\n"
    "# Reconciliation (synthetic)\n"
    "import pandas as pd\n"
    "amap = {'rows':[{'code':'M1','desc':'ATM','source_contract':'A.pdf','item':'ATM','price':'1'}],\n"
    "        'by_code':{'M1':1}, 'by_norm_desc':{sf._normalize_name('ATM'):'M1'}}\n"
    "inv = pd.DataFrame([{c:'' for c in sf.INVOICE_COLUMNS}])\n"
    "inv.loc[0,'OTC_SIL_MATERIAL']='M9'; inv.loc[0,'OTC_SIL_MATERIAL_TEXT']='New SKU'\n"
    "inv.loc[0,'OTC_SIH_INVOICE_DOCUMENT']='INV-9'\n"
    "rec = sf.reconcile_materials(amap, inv)\n"
    "assert len(rec['invoice_only'])==1 and len(rec['contract_only'])==1, rec\n"
    "# Connector presence + graceful status (won't connect here; just no-crash)\n"
    "print('snowflake connector installed:', sf.snowflake_available())\n"
    "st = sf.connection_status()\n"
    "print('connection_status keys:', sorted(st.keys()))\n"
    "# chat_engine wiring\n"
    "eng = chat_engine.ChatEngine.__new__(chat_engine.ChatEngine); eng.model='x'\n"
    "sp = chat_engine.ChatEngine.build_system_prompt(eng, client_name='T',\n"
    "        kb_context='', hierarchy_context='', extraction_context='',\n"
    "        cpi_context='', clauses_context='', invoice_context='=== SAP INVOICE DATA ===')\n"
    "assert 'SAP INVOICE DATA' in sp and '[INVOICE]' in sp\n"
    "print('\\nAll Snowflake-patch checks passed.')"
))

cells.append(md_cell(
    "### Done\n"
    "Restart Streamlit: `streamlit run chatbot.py`.\n"
    "\n"
    "**Reminders**\n"
    "- Create `snowflake_config.toml` from the template and paste your real "
    "credential. The account is a **PrivateLink** endpoint, so invoice lookups "
    "only work from **inside the Fiserv VDI network**.\n"
    "- The credential in the screenshot (`eyJ…`) is a JWT — most likely a "
    "**Programmatic Access Token**. Keep it in `password`. If it's an OAuth "
    "token instead, move it to `token` and set `authenticator = \"oauth\"`.\n"
    "- Tune `SNOWFLAKE_BILLTO_THRESHOLD` (default 86) if the client→bill-to "
    "fuzzy match is too strict/loose, and `SNOWFLAKE_MAX_INVOICE_LINES` "
    "(default 400) to widen/narrow how many lines are pulled per engagement.\n"
    "- Invoice answers depend on the **Material Code Matching agent** having run "
    "for the client (that's how the contract bridge + reconciliation get the "
    "agent-side codes). Without it, you still get raw invoice lines, just no "
    "MATCH/MISMATCH reconciliation."
))

notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "file_extension": ".py",
                          "mimetype": "text/x-python", "pygments_lexer": "ipython3"},
    },
    "nbformat": 4, "nbformat_minor": 5,
}

OUT.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"Wrote {OUT.name}  ({OUT.stat().st_size:,} bytes, {len(cells)} cells)")
